"""Per-run proxy, CA, and internal-network session lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Sequence

from ..runtime.network import NetworkLease, PodmanRunNetwork
from ..runtime.sandbox import PodmanSandbox
from .ca import InstallCA
from .listener import ProxyEndpoint, ProxyInstance
from .recording import Budget, ProviderRoute


@dataclass(frozen=True)
class ProxySessionInfo:
    network_name: str
    network_run_id: str
    proxy_endpoint: str
    ca_cert: Path


class _PodmanProxyRelay:
    """Trusted dual-homed TCP relay for the host-side recording proxy.

    An internal Podman network cannot reach the host gateway on all supported
    Podman backends. The relay is therefore attached to the case network and
    to a separate engine-managed egress network; the untrusted case sees only
    the relay's internal address. The recording proxy still makes every route,
    credential, TLS, and budget decision on the trusted host-side leg.
    """

    _IMAGE_REF = "docker.io/library/alpine:3.20"
    _LISTEN_PORT = 3128

    def __init__(self, sandbox: PodmanSandbox, network_name: str, run_id: str):
        self.sandbox = sandbox
        self.network_name = network_name
        self.run_id = run_id
        self.name = "llm-agent-eval-relay-" + hashlib.sha256(run_id.encode()).hexdigest()[:24]
        self.address: str | None = None
        self.started = False

    def _image_digest(self) -> str:
        result = self.sandbox._command(["image", "inspect", "--format", "{{.Id}}", self._IMAGE_REF])
        if result.returncode or result.interrupted:
            raise RuntimeError("proxy relay image is unavailable")
        image = result.stdout.decode("utf-8", "replace").strip()
        if image.startswith("sha256:"):
            digest = image
        elif re.fullmatch(r"[0-9a-f]{64}", image):
            digest = "sha256:" + image
        else:
            raise RuntimeError("proxy relay image identity is not immutable")
        return digest

    def start(self, host_port: int) -> str:
        if type(host_port) is not int or not 1 <= host_port <= 65535:
            raise RuntimeError("proxy listener port is invalid")
        image = self._image_digest()
        command = [
            "run", "--detach", "--pull=never", "--name", self.name,
            "--network", self.network_name, "--network", "podman",
            "--add-host=host.containers.internal:host-gateway",
            "--read-only", "--user=65532:65532", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=16",
            "--memory=64m", "--log-driver=none", "--tmpfs", "/tmp:rw,size=4m",
            "--entrypoint=/bin/sh", image, "-c",
            f"exec nc -lk -p {self._LISTEN_PORT} -e nc host.containers.internal {host_port}",
        ]
        result = self.sandbox._command(command)
        # A timed-out detach can still leave a container behind. Mark the
        # exact engine-owned name as started before inspecting it so failure
        # cleanup always attempts to remove that container.
        self.started = True
        if result.returncode or result.interrupted:
            self.stop()
            raise RuntimeError("proxy relay could not start")
        inspected = self.sandbox._command(["inspect", "--format", "json", self.name])
        if inspected.returncode or inspected.interrupted:
            self.stop()
            raise RuntimeError("proxy relay identity could not be inspected")
        try:
            payload = json.loads(inspected.stdout)
            record = payload[0] if isinstance(payload, list) else payload
            networks = record["NetworkSettings"]["Networks"]
            address = networks[self.network_name]["IPAddress"]
            if not isinstance(address, str) or not re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", address):
                raise ValueError
        except (KeyError, TypeError, ValueError, IndexError, json.JSONDecodeError) as exc:
            self.stop()
            raise RuntimeError("proxy relay has no internal network address") from exc
        self.address = address
        return address

    def stop(self) -> dict[str, str]:
        if not self.started and self.address is None:
            return {"state": "already_stopped"}
        result = self.sandbox._command(["rm", "--force", "--time", "0", self.name])
        self.address = None
        self.started = False
        return {"state": "stopped" if result.returncode == 0 and not result.interrupted else "failed"}


class ProxyRunSession:
    """Own every trusted resource required by one local evaluation run."""

    def __init__(
        self,
        *,
        run_id: str,
        install_root: Path,
        routes: Sequence[ProviderRoute],
        budget: Budget,
        secret_resolver: Callable[[str], str],
        record: Callable[[dict[str, Any]], None],
        sandbox: PodmanSandbox | None = None,
        network: PodmanRunNetwork | None = None,
    ):
        self.run_id = run_id
        self.install_root = Path(install_root)
        self.routes = list(routes)
        self.budget = budget
        self.secret_resolver = secret_resolver
        self.record = record
        self.sandbox = sandbox or PodmanSandbox()
        self.network = network or PodmanRunNetwork(self.sandbox)
        self.ca: InstallCA | None = None
        self.proxy: ProxyInstance | None = None
        self.relay: _PodmanProxyRelay | None = None
        self.lease: NetworkLease | None = None

    def start(self) -> ProxySessionInfo:
        if self.proxy is not None:
            raise RuntimeError("proxy run session is already started")
        self.ca = InstallCA.load_or_create(self.install_root)
        self.lease = self.network.create(self.run_id)
        try:
            self.proxy = ProxyInstance(
                self.routes,
                self.budget,
                self.secret_resolver,
                self.record,
                ca=self.ca,
                host="0.0.0.0",
            )
            endpoint = self.proxy.start()
            if isinstance(self.network, PodmanRunNetwork):
                self.relay = _PodmanProxyRelay(self.sandbox, self.lease.name, self.run_id)
                relay_address = self.relay.start(endpoint.port)
                proxy_endpoint = f"http://{relay_address}:{_PodmanProxyRelay._LISTEN_PORT}"
            else:
                # Synthetic network seams preserve the historical host-gateway
                # contract used by unit tests; real Podman sessions use the
                # internal relay address above.
                proxy_endpoint = f"http://host.containers.internal:{endpoint.port}"
        except Exception:
            if self.relay is not None:
                self.relay.stop()
                self.relay = None
            if self.proxy is not None:
                self.proxy.stop()
                self.proxy = None
            if self.lease is not None:
                self.network.remove(self.lease)
                self.lease = None
            raise
        return ProxySessionInfo(
            network_name=self.lease.name,
            network_run_id=self.run_id,
            proxy_endpoint=proxy_endpoint,
            ca_cert=self.ca.directory / "interception-ca.pem",
        )

    def stop(self) -> dict[str, Any]:
        relay_state = self.relay.stop() if self.relay is not None else {"state": "already_stopped"}
        proxy_state = self.proxy.stop() if self.proxy is not None else {"state": "already_stopped"}
        network_state = self.network.remove(self.lease) if self.lease is not None else {"state": "already_removed"}
        self.relay = None
        self.proxy = None
        self.lease = None
        return {"relay": relay_state, "proxy": proxy_state, "network": network_state}

    def __enter__(self) -> ProxySessionInfo:
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
