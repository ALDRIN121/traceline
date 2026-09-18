"""A single-origin transport that dials an approved IP, not a hostname."""

from __future__ import annotations

from ipaddress import ip_address

import httpx


class PinnedTransport(httpx.HTTPTransport):
    """Keep HTTP Host and TLS identity while removing a second DNS lookup.

    Each invocation owns its transport/pool; a connection is never shared with
    another target or a later DNS authorization. Retries and proxies are off.
    """

    def __init__(self, pin: dict):
        addresses = pin.get("addresses") or []
        if not addresses:
            raise ValueError("A resolved destination is required")
        self.address = str(ip_address(addresses[0]))
        self.host = httpx.URL(f"{pin['scheme']}://{_authority(pin['pinned_host'])}").raw_host
        self.port = pin["pinned_port"]
        self.scheme = pin["scheme"]
        super().__init__(verify=True, trust_env=False, retries=0)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (
            request.url.raw_host != self.host
            or (request.url.port or (443 if request.url.scheme == "https" else 80)) != self.port
            or request.url.scheme != self.scheme
        ):
            raise httpx.TransportError("Request origin does not match approved destination")
        # The numeric URL controls the socket destination. httpcore's SNI
        # extension controls both TLS SNI and certificate hostname verification.
        pinned = httpx.Request(
            request.method,
            request.url.copy_with(host=self.address),
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": self.host.decode("ascii")},
        )
        return super().handle_request(pinned)


def _authority(host: str) -> str:
    return f"[{host}]" if ":" in host else host
