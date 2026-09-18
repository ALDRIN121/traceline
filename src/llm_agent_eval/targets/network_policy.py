"""DNS/IP/TLS/redirect/origin policy for hosted targets."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse
import socket

from ..contracts import WorkflowError


class PolicyDenied(WorkflowError):
    def __init__(self, code: str, message: str):
        super().__init__(message, code=code, status=422)


class EndpointPolicy:
    def __init__(self, allow_exact: set[str] | None = None, resolver=None):
        self.allow_exact = {item.lower() for item in (allow_exact or set())}
        self.resolver = resolver

    def authorize(self, url: str) -> dict:
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        except (ValueError, TypeError) as exc:
            raise PolicyDenied("disallowed_destination", "Target URL is invalid") from exc
        if parsed.scheme not in {"http", "https"}:
            raise PolicyDenied("disallowed_destination", "Only http and https targets are supported")
        if parsed.username is not None or parsed.password is not None:
            raise PolicyDenied("disallowed_destination", "Target URL must not contain credentials")
        if not host or not 1 <= port <= 65535 or any(c in url for c in "\\\r\n\t") or "%" in host:
            raise PolicyDenied("disallowed_destination", "Target host or port is invalid")
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise PolicyDenied("disallowed_destination", "Target hostname is invalid") from exc
        exact = f"{host}:{port}"
        allowed = exact in self.allow_exact
        resolver = self.resolver or socket.getaddrinfo
        try:
            records = resolver(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise PolicyDenied("dns_failure", "Target hostname could not be resolved") from exc
        addresses = []
        for item in records:
            address = item[4][0]
            try:
                if "%" in address:
                    raise ValueError("scoped address")
                parsed_ip = ip_address(address)
            except ValueError as exc:
                raise PolicyDenied("disallowed_destination", "Target resolved to an invalid address") from exc
            # Mapped/scoped addresses are ambiguous across socket families;
            # multicast is_global may be true, but it is never a HTTP target.
            if parsed_ip.is_multicast or getattr(parsed_ip, "ipv4_mapped", None) is not None or (
                not allowed and not parsed_ip.is_global
            ):
                raise PolicyDenied("dns_rebinding" if host != address else "disallowed_destination",
                                   "Target resolves to a protected destination")
            normalized = str(parsed_ip)
            if normalized not in addresses:
                addresses.append(normalized)
        if not addresses:
            raise PolicyDenied("dns_failure", "Target hostname could not be resolved")
        return {"pinned_host": host, "pinned_port": port, "scheme": parsed.scheme, "addresses": addresses}
