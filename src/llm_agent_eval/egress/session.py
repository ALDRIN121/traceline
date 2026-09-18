"""Per-run proxy, CA, and internal-network session lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
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
        except Exception:
            if self.lease is not None:
                self.network.remove(self.lease)
                self.lease = None
            raise
        return ProxySessionInfo(
            network_name=self.lease.name,
            network_run_id=self.run_id,
            proxy_endpoint=f"http://host.containers.internal:{endpoint.port}",
            ca_cert=self.ca.directory / "interception-ca.pem",
        )

    def stop(self) -> dict[str, Any]:
        proxy_state = self.proxy.stop() if self.proxy is not None else {"state": "already_stopped"}
        network_state = self.network.remove(self.lease) if self.lease is not None else {"state": "already_removed"}
        self.proxy = None
        self.lease = None
        return {"proxy": proxy_state, "network": network_state}

    def __enter__(self) -> ProxySessionInfo:
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
