"""Install-owned encrypted secret references with explicit service authorization.

Ciphertext is a versioned envelope — ``v1.<key_id>.<base64url(nonce|body|tag)>``
— encrypted with AES-256-GCM from the standard ``cryptography`` library. The
additional authenticated data binds every ciphertext to its workspace and
secret ID, so a ciphertext copied to a sibling row fails integrity
verification instead of silently decrypting.

Legacy prototype ciphertext (HMAC-keystream XOR) is never decrypted on the
normal path and never written again. ``migrate_legacy_secrets`` performs the
explicit, transactional migration: each row is re-encrypted into the v1
envelope or the whole batch rolls back with the failing secret's ID.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
from pathlib import Path
import secrets
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .auth import Actor
from .contracts import NotFound, WorkflowError
from .install_state import load_install_key
from .storage import Storage, _now

#: The persisted legacy install key remains the v1 encryption key. Credential
#: rotation rewrites a secret value; replacing the root key is not automatic.
KEY_ID = "install-local"

_ENVELOPE_VERSION = "v1"
_NONCE_BYTES = 12
_AEAD = AESGCM

# The retired prototype envelope: HMAC-SHA-256 keystream XOR with a raw HMAC
# tag, base64(nonce16 | tag32 | body). Decode-only, for explicit migration.
_LEGACY_NONCE_BYTES = 16
_LEGACY_TAG_BYTES = 32


def _legacy_xor(data: bytes, stream: bytes) -> bytes:
    return bytes(left ^ right for left, right in zip(data, stream))


def _legacy_keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    return b"".join(
        hmac.digest(key, nonce + index.to_bytes(4, "big"), "sha256")
        for index in range((length + 31) // 32)
    )


class SecretStore:
    def __init__(self, storage: Storage, actor: Actor, key_path: Path):
        self.storage, self.actor, self.key_path = storage, actor, Path(key_path)
        self.key = load_install_key(self.key_path)
        self.service_identity = os.environ.get("LLM_AGENT_EVAL_SERVICE_IDENTITY", "")

    def _aad(self, secret_id: str) -> bytes:
        """Envelope AAD: binds ciphertext to its workspace and secret ID."""
        return json.dumps([_ENVELOPE_VERSION, KEY_ID, self.actor.workspace_id, secret_id],
                          separators=(",", ":")).encode("utf-8")

    def _encrypt(self, value: str, secret_id: str) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = _AEAD(self.key).encrypt(nonce, value.encode("utf-8"), self._aad(secret_id))
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{_ENVELOPE_VERSION}.{KEY_ID}.{encoded}"

    def _decrypt(self, encoded: str, secret_id: str) -> str:
        head = encoded.split(".", 2)
        if len(head) == 3 and head[0] == _ENVELOPE_VERSION:
            if head[1] != KEY_ID:
                # A recognized version with an unrecognized key id is a
                # foreign envelope, never a shape-detection fallback.
                raise WorkflowError(
                    "Secret ciphertext names an unknown install key",
                    code="secret_unsupported_version",
                    status=409,
                )
            return self._decrypt_v1(head[2], secret_id)
        if self._is_legacy(encoded):
            raise WorkflowError(
                "Legacy secret ciphertext requires explicit migration",
                code="secret_legacy_ciphertext",
                status=409,
            )
        raise WorkflowError(
            "Secret ciphertext envelope is not supported",
            code="secret_unsupported_version",
            status=409,
        )

    def _is_legacy(self, encoded: str) -> bool:
        """Shape detection for the retired prototype envelope."""
        try:
            data = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError, UnicodeEncodeError):
            return False
        return len(data) > _LEGACY_NONCE_BYTES + _LEGACY_TAG_BYTES

    def _decrypt_legacy(self, encoded: str, secret_id: str) -> str:
        if not self._is_legacy(encoded):
            raise WorkflowError(
                "Legacy secret ciphertext is not decodable",
                code="secret_corrupt",
                status=409,
            )
        data = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        nonce, tag, body = data[:_LEGACY_NONCE_BYTES], data[_LEGACY_NONCE_BYTES:_LEGACY_NONCE_BYTES + _LEGACY_TAG_BYTES], data[_LEGACY_NONCE_BYTES + _LEGACY_TAG_BYTES:]
        # The legacy tag authenticates the stored ciphertext (nonce + body),
        # not the plaintext. Verify before decrypting (authenticate-then-decrypt).
        expected = hmac.digest(self.key, nonce + body, "sha256")
        if not hmac.compare_digest(tag, expected):
            raise WorkflowError(
                "Legacy secret ciphertext failed integrity verification",
                code="secret_corrupt",
                status=409,
            )
        keystream = _legacy_keystream(self.key, nonce, len(body))
        return _legacy_xor(body, keystream).decode("utf-8")

    def _decrypt_v1(self, encoded: str, secret_id: str) -> str:
        try:
            data = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise WorkflowError("Secret ciphertext is not decodable", code="secret_corrupt", status=409) from exc
        if len(data) <= _NONCE_BYTES:
            raise WorkflowError("Secret ciphertext is truncated", code="secret_corrupt", status=409)
        nonce, body = data[:_NONCE_BYTES], data[_NONCE_BYTES:]
        try:
            plain = _AEAD(self.key).decrypt(nonce, body, self._aad(secret_id))
        except InvalidTag as exc:
            raise WorkflowError(
                "Secret ciphertext failed integrity verification", code="secret_corrupt", status=409
            ) from exc
        return plain.decode("utf-8")

    def _audit(self, conn, secret_id: str, action: str, service: str | None = None) -> None:
        conn.execute("INSERT INTO secret_audit (workspace_id,audit_id,secret_id,action,service,actor_id,created_at) VALUES (?,?,?,?,?,?,?)",
                     (self.actor.workspace_id, uuid.uuid4().hex, secret_id, action, service, self.actor.actor_id, _now()))

    def put(self, secret_type: str, value: str, *, allowed_services: set[str]):
        self.actor.require(write=True)
        if not isinstance(value, str) or not value or not allowed_services:
            raise WorkflowError("Secret value and allowed services are required")
        secret_id, now = uuid.uuid4().hex, _now()
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            conn.execute("INSERT INTO secret_refs (workspace_id,secret_id,secret_type,ciphertext,allowed_services_json,key_id,state,created_at) VALUES (?,?,?,?,?,?, 'active',?)",
                         (self.actor.workspace_id, secret_id, secret_type, self._encrypt(value, secret_id), json.dumps(sorted(allowed_services)), KEY_ID, now))
            self._audit(conn, secret_id, "created")
        return type("SecretRef", (), {"secret_id": secret_id, "secret_type": secret_type, "allowed_services": frozenset(allowed_services)})()

    def _row(self, secret_id: str):
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            row = conn.execute("SELECT * FROM secret_refs WHERE workspace_id=? AND secret_id=?", (self.actor.workspace_id, secret_id)).fetchone()
            if row is None:
                raise NotFound()
            return dict(row)

    def get_ciphertext(self, secret_id: str) -> str:
        return self._row(secret_id)["ciphertext"]

    def resolve(self, secret_id: str) -> str:
        self.actor.require(write=True)
        if not self.service_identity:
            raise WorkflowError("Trusted service identity is required", code="forbidden", status=403)
        row = self._row(secret_id)
        if self.service_identity not in json.loads(row["allowed_services_json"]):
            raise WorkflowError("Secret access denied", code="forbidden", status=403)
        value = self._decrypt(row["ciphertext"], secret_id)
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            self._audit(conn, secret_id, "resolved", self.service_identity)
        return value

    def migrate_legacy_secrets(self) -> dict[str, int]:
        """Explicitly re-encrypt legacy XOR ciphertext into the v1 envelope.

        Atomic per workspace: every legacy row is decoded and fully verified
        before any row is rewritten; a corrupt row aborts the whole batch,
        rolls back, and names itself in the error. Returns a small report.
        """
        self.actor.require(write=True)
        if not self.service_identity:
            raise WorkflowError("Trusted service identity is required", code="forbidden", status=403)
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            rows = [dict(row) for row in conn.execute(
                "SELECT secret_id,ciphertext,allowed_services_json FROM secret_refs "
                "WHERE workspace_id=? AND ciphertext NOT LIKE ?",
                (self.actor.workspace_id, "v1.%"),
            ).fetchall()]
            decoded = []
            for row in rows:
                secret_id = row["secret_id"]
                if self.service_identity not in json.loads(row["allowed_services_json"]):
                    raise WorkflowError("Secret access denied", code="forbidden", status=403)
                try:
                    decoded.append((secret_id, self._decrypt_legacy(row["ciphertext"], secret_id)))
                except WorkflowError as exc:
                    raise WorkflowError(
                        f"Legacy secret {secret_id} could not be migrated",
                        code=exc.code,
                        status=exc.status,
                    ) from exc
            for secret_id, value in decoded:
                conn.execute(
                    "UPDATE secret_refs SET ciphertext=?,key_id=? WHERE workspace_id=? AND secret_id=?",
                    (self._encrypt(value, secret_id), KEY_ID, self.actor.workspace_id, secret_id),
                )
                self._audit(conn, secret_id, "rotated", self.service_identity)
        return {"migrated": len(decoded)}

    def rotate(self, secret_id: str, value: str, *, allowed_services: set[str]):
        self.actor.require(write=True)
        if not value or not allowed_services:
            raise WorkflowError("Secret value and allowed services are required")
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            changed = conn.execute("UPDATE secret_refs SET ciphertext=?,allowed_services_json=?,rotated_at=? WHERE workspace_id=? AND secret_id=?",
                                   (self._encrypt(value, secret_id), json.dumps(sorted(allowed_services)), _now(), self.actor.workspace_id, secret_id))
            if not changed.rowcount:
                raise NotFound()
            self._audit(conn, secret_id, "rotated")
        return type("SecretRef", (), {"secret_id": secret_id, "allowed_services": frozenset(allowed_services)})()

    def audit(self, secret_id: str) -> list[dict]:
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM secret_audit WHERE workspace_id=? AND secret_id=? ORDER BY created_at,audit_id", (self.actor.workspace_id, secret_id)).fetchall()]
