"""Per-install TLS interception CA, persisted outside the repository.

The CA private key decrypts every intercepted session for an install. It is
therefore created next to other install-owned material (artifact root), with
owner-only permissions, and never inside the repository.
"""
from __future__ import annotations

import datetime
import ipaddress
import os
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_CA_NAME = "LLM Agent Eval per-install interception CA"
_CERT = "interception-ca.pem"


class InstallCA:
    """Load or create one install-owned ECDSA P-256 interception CA."""

    def __init__(self, directory: Path, certificate_pem: bytes, private_key_pem: bytes):
        self.directory = directory
        self._certificate_pem = certificate_pem
        self._private_key_pem = private_key_pem

    @classmethod
    def load_or_create(cls, directory: Path) -> "InstallCA":
        directory = Path(directory).resolve()
        for parent in directory.parents:
            if (parent / ".git").is_dir():
                raise ValueError("install state must never live inside a repository")
        directory.mkdir(parents=True, exist_ok=True)
        cert_path, key_path = directory / _CERT, directory / "interception-ca.key"
        if cert_path.exists() or key_path.exists():
            if not (cert_path.exists() and key_path.exists()):
                raise ValueError("install CA material is incomplete; refusing to regenerate")
            try:
                cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
                cert.public_key().verify(
                    cert.signature, cert.tbs_certificate_bytes,
                    ec.ECDSA(cert.signature_hash_algorithm),
                )
            except (InvalidSignature, TypeError, ValueError) as exc:
                raise ValueError("install CA material is invalid or corrupt") from exc
            return cls(directory, cert_path.read_bytes(), key_path.read_bytes())
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _CA_NAME)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True,
                    content_commitment=False, key_encipherment=False,
                    data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        cert_bytes = cert.public_bytes(serialization.Encoding.PEM)
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        _exclusive_write(cert_path, cert_bytes, 0o600)
        _exclusive_write(key_path, key_bytes, 0o600)
        return cls(directory, cert_bytes, key_bytes)

    @property
    def certificate_pem(self) -> bytes:
        return self._certificate_pem

    def context_for(self, hostname: str) -> "object":
        """A server-side TLS context presenting a leaf for ``hostname`` signed
        by this CA. Resolved connections pass this to the agent-side listener;
        the sandbox must install this CA as the only trusted root."""
        leaf_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.datetime.now(datetime.timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
        try:
            san_name = x509.IPAddress(ipaddress.ip_address(hostname))
        except ValueError:
            san_name = x509.DNSName(hostname)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(x509.load_pem_x509_certificate(self._certificate_pem).subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(minutes=30))
            .add_extension(x509.SubjectAlternativeName([san_name]), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .sign(
                serialization.load_pem_private_key(self._private_key_pem, password=None),
                hashes.SHA256(),
            )
        )
        context = SSLContext()
        context.load_cert_chain(
            leaf.public_bytes(serialization.Encoding.PEM),
            leaf_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        return context


class SSLContext:
    """Minimal server context: TLS 1.2+, no client certificates."""

    def __init__(self):
        import ssl

        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._context.options |= ssl.OP_NO_COMPRESSION

    @property
    def minimum_version(self):
        return self._context.minimum_version

    def load_cert_chain(self, cert_pem: bytes, key_pem: bytes):
        import tempfile

        # ssl.SSLContext requires a file path; the combined PEM is written to
        # the private temp area and removed immediately after loading.
        fd, path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(cert_pem)
                handle.write(key_pem)
            self._context.load_cert_chain(path)
        finally:
            os.unlink(path)

    def wrap_socket(self, socket, **kwargs):
        return self._context.wrap_socket(socket, **kwargs)


def _exclusive_write(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(path, mode)
