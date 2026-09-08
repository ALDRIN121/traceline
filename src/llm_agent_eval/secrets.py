"""Install-owned encrypted secret references with explicit service authorization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import uuid

from .auth import Actor
from .contracts import NotFound, WorkflowError
from .storage import Storage, _now


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(left ^ right for left, right in zip(data, stream))


class SecretStore:
    def __init__(self, storage: Storage, actor: Actor, key_path: Path):
        self.storage, self.actor, self.key_path = storage, actor, Path(key_path)
        self.key = self._load_or_create_key()
        self.service_identity = os.environ.get("LLM_AGENT_EVAL_SERVICE_IDENTITY", "")

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise RuntimeError("install secret material is invalid")
            return key
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        return key

    def _encrypt(self, value: str) -> str:
        nonce = secrets.token_bytes(16)
        raw = value.encode("utf-8")
        stream = b"".join(hmac.digest(self.key, nonce + index.to_bytes(4, "big"), "sha256") for index in range((len(raw) + 31) // 32))
        ciphertext = _xor(raw, stream)
        tag = hmac.digest(self.key, nonce + ciphertext, "sha256")
        return base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")

    def _decrypt(self, encoded: str) -> str:
        data = base64.urlsafe_b64decode(encoded.encode("ascii"))
        nonce, tag, ciphertext = data[:16], data[16:48], data[48:]
        if not hmac.compare_digest(tag, hmac.digest(self.key, nonce + ciphertext, "sha256")):
            raise WorkflowError("Secret ciphertext failed integrity verification", code="secret_corrupt", status=409)
        stream = b"".join(hmac.digest(self.key, nonce + index.to_bytes(4, "big"), "sha256") for index in range((len(ciphertext) + 31) // 32))
        return _xor(ciphertext, stream).decode("utf-8")

    def _audit(self, conn, secret_id: str, action: str, service: str | None = None) -> None:
        conn.execute("INSERT INTO secret_audit (workspace_id,audit_id,secret_id,action,service,actor_id,created_at) VALUES (?,?,?,?,?,?,?)",
                     (self.actor.workspace_id, uuid.uuid4().hex, secret_id, action, service, self.actor.actor_id, _now()))

    def put(self, secret_type: str, value: str, *, allowed_services: set[str]):
        self.actor.require(write=True)
        if not isinstance(value, str) or not value or not allowed_services:
            raise WorkflowError("Secret value and allowed services are required")
        secret_id, now = uuid.uuid4().hex, _now()
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            conn.execute("INSERT INTO secret_refs (workspace_id,secret_id,secret_type,ciphertext,allowed_services_json,key_id,state,created_at) VALUES (?,?,?,?,?,'install-local','active',?)",
                         (self.actor.workspace_id, secret_id, secret_type, self._encrypt(value), json.dumps(sorted(allowed_services)), now))
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
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            self._audit(conn, secret_id, "resolved", self.service_identity)
        return self._decrypt(row["ciphertext"])

    def rotate(self, secret_id: str, value: str, *, allowed_services: set[str]):
        self.actor.require(write=True)
        if not value or not allowed_services:
            raise WorkflowError("Secret value and allowed services are required")
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            changed = conn.execute("UPDATE secret_refs SET ciphertext=?,allowed_services_json=?,rotated_at=? WHERE workspace_id=? AND secret_id=?",
                                   (self._encrypt(value), json.dumps(sorted(allowed_services)), _now(), self.actor.workspace_id, secret_id))
            if not changed.rowcount:
                raise NotFound()
            self._audit(conn, secret_id, "rotated")
        return type("SecretRef", (), {"secret_id": secret_id, "allowed_services": frozenset(allowed_services)})()

    def audit(self, secret_id: str) -> list[dict]:
        with self.storage.workspace_transaction(self.actor.workspace_id) as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM secret_audit WHERE workspace_id=? AND secret_id=? ORDER BY created_at,audit_id", (self.actor.workspace_id, secret_id)).fetchall()]
