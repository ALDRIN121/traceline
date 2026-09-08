"""Durable queue and secret-reference contracts."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.jobs import JobQueue, StaleLease
from llm_agent_eval.secrets import SecretStore
from llm_agent_eval.storage import Storage


SENTINEL = "sk-test-do-not-persist-0001"
FINGERPRINT_KEY = b"t" * 32


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value.isoformat()

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def durable_store(tmp_path):
    store = Storage(tmp_path / "jobs.db")
    store.create_schema()
    yield store
    store.close()


@pytest.fixture
def owner():
    return Actor("owner", "workspace_a", "owner")


def test_enqueue_is_idempotent_but_rejects_key_reuse_for_changed_command(durable_store, owner):
    queue = JobQueue(durable_store, owner, fingerprint_key=FINGERPRINT_KEY)
    first = queue.enqueue({"kind": "artifact", "value": "stable"}, "one-key")
    duplicate = queue.enqueue({"value": "stable", "kind": "artifact"}, "one-key")

    assert duplicate.job_id == first.job_id
    assert len(queue.pending_outbox()) == 1
    with pytest.raises(WorkflowError) as conflict:
        queue.enqueue({"kind": "artifact", "value": "changed"}, "one-key")
    assert conflict.value.code == "idempotency_conflict"


def test_expired_lease_is_reclaimed_and_late_worker_cannot_complete(durable_store, owner):
    clock = Clock()
    queue = JobQueue(durable_store, owner, fingerprint_key=FINGERPRINT_KEY, clock=clock, lease_seconds=5)
    job = queue.enqueue({"kind": "artifact", "value": "stable"}, "reclaim")
    first = queue.claim("worker-a")
    assert first and first.job_id == job.job_id

    clock.advance(6)
    second = queue.claim("worker-b")
    assert second and second.fence > first.fence
    with pytest.raises(StaleLease):
        queue.complete(job.job_id, first.fence, {"sha256": "old"})

    completed = queue.complete(job.job_id, second.fence, {"sha256": "new"})
    assert completed.status == "completed"
    assert queue.claim("worker-c") is None
    with pytest.raises(StaleLease):
        queue.complete(job.job_id, second.fence, {"sha256": "again"})


def test_cancellation_intent_survives_worker_restart_and_prevents_completion(durable_store, owner):
    clock = Clock()
    queue = JobQueue(durable_store, owner, fingerprint_key=FINGERPRINT_KEY, clock=clock, lease_seconds=5)
    job = queue.enqueue({"kind": "artifact"}, "cancel")
    lease = queue.claim("worker-a")
    assert lease is not None
    requested = queue.request_cancel(job.job_id)
    assert requested.cancellation_requested is True

    clock.advance(6)
    restarted = JobQueue(durable_store, owner, fingerprint_key=FINGERPRINT_KEY, clock=clock, lease_seconds=5)
    assert restarted.claim("worker-b") is None
    assert restarted.get(job.job_id).status == "cancelled"
    with pytest.raises(StaleLease):
        restarted.complete(job.job_id, lease.fence, {"late": True})


def test_secret_reference_uses_process_owned_service_identity(durable_store, owner, tmp_path, monkeypatch):
    key_path = tmp_path.parent / "install-owned-material" / "fernet.key"
    monkeypatch.setenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", "proxy")
    secrets = SecretStore(durable_store, owner, key_path)
    ref = secrets.put("provider_api_key", SENTINEL, allowed_services={"proxy"})

    assert SENTINEL not in str(secrets.get_ciphertext(ref.secret_id))
    assert secrets.resolve(ref.secret_id) == SENTINEL
    monkeypatch.setenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", "runner")
    untrusted_process = SecretStore(durable_store, owner, key_path)
    with pytest.raises(WorkflowError) as denied:
        untrusted_process.resolve(ref.secret_id)
    assert denied.value.code == "forbidden"

    rotated = secrets.rotate(ref.secret_id, "sk-rotated", allowed_services={"proxy", "importer"})
    assert rotated.secret_id == ref.secret_id
    monkeypatch.setenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", "importer")
    importer = SecretStore(durable_store, owner, key_path)
    assert importer.resolve(ref.secret_id) == "sk-rotated"
    assert [event["action"] for event in secrets.audit(ref.secret_id)] == ["created", "resolved", "rotated", "resolved"]
    assert Path(key_path).is_file()
