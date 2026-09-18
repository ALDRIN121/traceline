"""Versioned AES-GCM secret envelope, migration, and install-key contracts."""

import base64
from concurrent.futures import ThreadPoolExecutor
import hmac as hmac_mod
from pathlib import Path

import pytest

from llm_agent_eval import install_state
from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.install_state import load_fingerprint_key, load_install_key
from llm_agent_eval.secrets import KEY_ID, SecretStore
from llm_agent_eval.storage import Storage

SENTINEL = "sk-test-do-not-persist-0001"


@pytest.fixture
def durable_store(tmp_path):
    store = Storage(tmp_path / "secrets.db")
    store.create_schema()
    yield store
    store.close()


@pytest.fixture
def owner():
    return Actor("owner", "workspace_a", "owner")


@pytest.fixture
def proxy_identity(monkeypatch):
    monkeypatch.setenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", "proxy")


def _legacy_encrypt(key: bytes, value: str) -> str:
    """The retired prototype format: HMAC keystream XOR + raw HMAC tag."""
    nonce = bytes(16)
    raw = value.encode("utf-8")
    stream = b"".join(
        hmac_mod.digest(key, nonce + index.to_bytes(4, "big"), "sha256")
        for index in range((len(raw) + 31) // 32)
    )
    ciphertext = bytes(left ^ right for left, right in zip(raw, stream))
    tag = hmac_mod.digest(key, nonce + ciphertext, "sha256")
    return base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")


def _insert_raw_secret(store, workspace_id, secret_id, ciphertext, key_id=KEY_ID):
    """Insert a row as the retired prototype wrote it: key_id 'install-local'."""
    with store.workspace_transaction(workspace_id) as conn:
        conn.execute(
            "INSERT INTO secret_refs (workspace_id,secret_id,secret_type,ciphertext,"
            "allowed_services_json,key_id,state,created_at) VALUES (?,?,?,?,?,?,'active','2026-01-01T00:00:00+00:00')",
            (workspace_id, secret_id, "provider_api_key", ciphertext, '["proxy"]', key_id),
        )


def test_put_and_resolve_roundtrip_writes_versioned_envelope_only(durable_store, owner, proxy_identity, tmp_path):
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    ref = secrets.put("provider_api_key", SENTINEL, allowed_services={"proxy"})
    rotated = secrets.rotate(ref.secret_id, "sk-rotated-0002", allowed_services={"proxy"})

    for recorded in (secrets.get_ciphertext(ref.secret_id),):
        version, key_id, _ = recorded.split(".", 2)
        assert (version, key_id) == ("v1", KEY_ID)
        assert SENTINEL not in recorded and "sk-rotated-0002" not in recorded
    assert secrets.resolve(ref.secret_id) == "sk-rotated-0002"
    assert rotated.secret_id == ref.secret_id


def test_tampered_or_wrongly_bound_ciphertext_fails_closed(durable_store, owner, proxy_identity, tmp_path):
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    ref = secrets.put("provider_api_key", SENTINEL, allowed_services={"proxy"})
    raw = secrets.get_ciphertext(ref.secret_id)
    head, key_id, payload = raw.split(".", 2)
    flipped = "A" if payload[0] != "A" else "B"
    tampered = f"{head}.{key_id}.{flipped}{payload[1:]}"
    with store_transaction(durable_store) as conn:
        conn.execute(
            "UPDATE secret_refs SET ciphertext=? WHERE workspace_id=? AND secret_id=?",
            (tampered, owner.workspace_id, ref.secret_id),
        )
    with pytest.raises(WorkflowError) as tamper:
        secrets.resolve(ref.secret_id)
    assert tamper.value.code == "secret_corrupt"


def store_transaction(store, workspace_id="workspace_a"):
    return store.workspace_transaction(workspace_id)


def test_ciphertext_is_bound_to_workspace_and_secret_id(durable_store, owner, proxy_identity, tmp_path):
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    first = secrets.put("provider_api_key", SENTINEL, allowed_services={"proxy"})
    second = secrets.put("provider_api_key", "sk-other-0003", allowed_services={"proxy"})
    ciphertext_a = secrets.get_ciphertext(first.secret_id)
    with store_transaction(durable_store) as conn:
        conn.execute(
            "UPDATE secret_refs SET ciphertext=? WHERE workspace_id=? AND secret_id=?",
            (ciphertext_a, owner.workspace_id, second.secret_id),
        )
    with pytest.raises(WorkflowError) as rebound:
        secrets.resolve(second.secret_id)
    assert rebound.value.code == "secret_corrupt"


def test_unknown_envelope_version_or_key_fails_closed(durable_store, owner, proxy_identity, tmp_path):
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    ref = secrets.put("provider_api_key", SENTINEL, allowed_services={"proxy"})
    raw = secrets.get_ciphertext(ref.secret_id)
    _, _, payload = raw.split(".", 2)
    for forged in (f"v9.{KEY_ID}.{payload}", f"v1.unknown-key.{payload}"):
        with store_transaction(durable_store) as conn:
            conn.execute(
                "UPDATE secret_refs SET ciphertext=? WHERE workspace_id=? AND secret_id=?",
                (forged, owner.workspace_id, ref.secret_id),
            )
        with pytest.raises(WorkflowError) as unknown:
            secrets.resolve(ref.secret_id)
        assert unknown.value.code == "secret_unsupported_version"


def test_envelope_rejects_ignored_characters_and_does_not_audit_resolution(durable_store, owner, proxy_identity, tmp_path):
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    ref = secrets.put("provider_api_key", SENTINEL, allowed_services={"proxy"})
    raw = secrets.get_ciphertext(ref.secret_id)
    with store_transaction(durable_store) as conn:
        conn.execute("UPDATE secret_refs SET ciphertext=? WHERE workspace_id=? AND secret_id=?",
                     (raw + "!", owner.workspace_id, ref.secret_id))
    with pytest.raises(WorkflowError) as malformed:
        secrets.resolve(ref.secret_id)
    assert malformed.value.code == "secret_corrupt"
    assert [event["action"] for event in secrets.audit(ref.secret_id)] == ["created"]


def test_missing_service_identity_blocks_legacy_migration(durable_store, owner, tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", raising=False)
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    _insert_raw_secret(durable_store, owner.workspace_id, "legacy", _legacy_encrypt(secrets.key, SENTINEL))
    with pytest.raises(WorkflowError) as denied:
        secrets.migrate_legacy_secrets()
    assert denied.value.code == "forbidden"


def test_legacy_ciphertext_requires_explicit_migration(durable_store, owner, proxy_identity, tmp_path):
    key_path = tmp_path / "install.key"
    legacy_store = SecretStore(durable_store, owner, key_path)
    _insert_legacy = _legacy_encrypt(legacy_store.key, SENTINEL)
    _insert_raw_secret(durable_store, owner.workspace_id, "legacy-one", _insert_legacy)

    with pytest.raises(WorkflowError) as blocked:
        legacy_store.resolve("legacy-one")
    assert blocked.value.code == "secret_legacy_ciphertext"

    report = legacy_store.migrate_legacy_secrets()
    assert report["migrated"] == 1
    migrated = legacy_store.get_ciphertext("legacy-one")
    assert migrated.startswith(f"v1.{KEY_ID}.")
    assert legacy_store.resolve("legacy-one") == SENTINEL


def test_migration_is_transactional_and_fails_closed_on_corrupt_legacy(durable_store, owner, proxy_identity, tmp_path):
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    good_legacy = _legacy_encrypt(secrets.key, SENTINEL)
    _insert_raw_secret(durable_store, owner.workspace_id, "legacy-good", good_legacy)
    _insert_raw_secret(durable_store, owner.workspace_id, "legacy-corrupt", good_legacy[:-4] + "AAAA")

    with pytest.raises(WorkflowError) as corrupt:
        secrets.migrate_legacy_secrets()
    assert corrupt.value.code == "secret_corrupt"

    with store_transaction(durable_store) as conn:
        rows = {row["secret_id"]: row["ciphertext"] for row in conn.execute(
            "SELECT secret_id,ciphertext FROM secret_refs").fetchall()}
    assert rows["legacy-good"] == good_legacy  # rollback left both rows untouched


def test_install_key_survives_restart_and_a_foreign_key_cannot_decrypt(durable_store, owner, proxy_identity, tmp_path):
    key_path = tmp_path / "install.key"
    first = SecretStore(durable_store, owner, key_path)
    ref = first.put("provider_api_key", SENTINEL, allowed_services={"proxy"})

    restarted = SecretStore(durable_store, owner, key_path)
    assert restarted.resolve(ref.secret_id) == SENTINEL
    assert Path(key_path).stat().st_mode & 0o777 == 0o600

    stranger = SecretStore(durable_store, owner, tmp_path / "other-install.key")
    with pytest.raises(WorkflowError) as foreign:
        stranger.resolve(ref.secret_id)
    assert foreign.value.code == "secret_corrupt"


def test_install_key_rejects_invalid_material_and_races_safely(tmp_path, monkeypatch):
    bad_path = tmp_path / "short.key"
    bad_path.write_bytes(b"too-short")
    with pytest.raises(RuntimeError):
        load_install_key(bad_path)

    race_path = tmp_path / "raced" / "install.key"
    real_link = install_state.os.link

    def racing_link(source, destination):
        race_path.write_bytes(b"z" * 32)  # a concurrent installer publishes first
        race_path.chmod(0o600)
        return real_link(source, destination)

    monkeypatch.setattr(install_state.os, "link", racing_link)
    assert load_install_key(race_path) == b"z" * 32
    assert load_install_key(race_path) == b"z" * 32


def test_install_key_is_not_published_until_complete(tmp_path, monkeypatch):
    path = tmp_path / "install.key"
    real_fsync = install_state.os.fsync

    def fsync_before_publish(fd):
        assert not path.exists()
        return real_fsync(fd)

    monkeypatch.setattr(install_state.os, "fsync", fsync_before_publish)
    assert len(load_install_key(path)) == 32


def test_fingerprint_key_is_persistent_random_material(tmp_path):
    path = tmp_path / "state" / "fingerprint.key"
    first = load_fingerprint_key(path)
    assert len(first) == 32 and first != b"\x00" * 32
    assert load_fingerprint_key(path) == first
    assert path.stat().st_mode & 0o777 == 0o600

    short = tmp_path / "short-fp.key"
    short.write_bytes(b"n" * 16)
    with pytest.raises(RuntimeError):
        load_fingerprint_key(short)


def test_install_key_rejects_symlinks_and_publicly_readable_files(tmp_path):
    target = tmp_path / "target.key"
    target.write_bytes(b"k" * 32)
    target.chmod(0o600)
    linked = tmp_path / "linked.key"
    linked.symlink_to(target)
    with pytest.raises(RuntimeError):
        load_install_key(linked)
    target.chmod(0o644)
    with pytest.raises(RuntimeError):
        load_install_key(target)


def test_migration_denies_unapproved_service(durable_store, owner, tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", "runner")
    secrets = SecretStore(durable_store, owner, tmp_path / "install.key")
    raw = _legacy_encrypt(secrets.key, SENTINEL)
    _insert_raw_secret(durable_store, owner.workspace_id, "legacy", raw)
    with pytest.raises(WorkflowError) as denied:
        secrets.migrate_legacy_secrets()
    assert denied.value.code == "forbidden"
    assert secrets.get_ciphertext("legacy") == raw


def test_install_key_creation_is_race_safe_across_threads(tmp_path):
    """Two concurrent installers on a fresh directory resolve to one key."""
    path = tmp_path / "raced-dir" / "install.key"

    def installer():
        return load_install_key(path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        keys = [future.result() for future in [pool.submit(installer), pool.submit(installer)]]

    assert keys[0] == keys[1]
    assert len(keys[0]) == 32
    assert path.stat().st_mode & 0o777 == 0o600
