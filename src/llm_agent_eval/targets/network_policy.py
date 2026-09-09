"""DNS/IP/TLS/redirect/origin policy for hosted targets."""

from __future__ import annotations

from ipaddress import ip_address, ip_network
from urllib.parse import urlparse
import socket

from ..contracts import WorkflowError

PROTECTED = (
    ip_network("127.0.0.0/8"),
    ip_network("10.0.0.0/8"),
    ip_network("172.16.0.0/12"),
    ip_network("192.168.0.0/16"),
    ip_network("169.254.0.0/16"),
    ip_network("::1/128"),
    ip_network("fc00::/7"),
)


class PolicyDenied(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(message, code=code, status=422)


class EndpointPolicy:
    def __init__(self, allow_exact: set[str] | None = None, resolver=None):
        self.allow_exact = {item.lower() for item in (allow_exact or set())}
        self.resolver = resolver

    def authorize(self, url: str) -> dict:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise PolicyDenied("disallowed_destination", "Only http and https targets are supported")
        if not parsed.hostname:
            raise PolicyDenied("disallowed_destination", "Target URL is missing a host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        exact = f"{parsed.hostname}:{port}".lower()
        if exact in self.allow_exact:
            return {"pinned_host": parsed.hostname, "pinned_port": port, "scheme": parsed.scheme}
        resolver = self.resolver or socket.getaddrinfo
        try:
            records = resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise PolicyDenied("dns_failure", "Target hostname could not be resolved") from exc
        addresses = []
        for item in records:
            sockaddr = item[4]
            addresses.append(sockaddr[0])
        if not addresses:
            raise PolicyDenied("dns_failure", "Target hostname could not be resolved")
        for address in addresses:
            parsed_ip = ip_address(address.split("%", 1)[0])
            if any(parsed_ip in network for network in PROTECTED):
                raise PolicyDenied("dns_rebinding" if parsed.hostname != address else "disallowed_destination",
                                   "Target resolves to a protected destination")
        return {"pinned_host": parsed.hostname, "pinned_port": port, "scheme": parsed.scheme, "addresses": addresses}
