"""Lease-safe worker publication and continuously claimed work."""

from datetime import datetime, timedelta, timezone
import threading

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.jobs import JobQueue, StaleLease
from llm_agent_eval.storage import Storage
from llm_agent_eval.worker import WorkflowWorker
from llm_agent_eval.worker_service import WorkerService, PreparedResult


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 17, tzinfo=timezone.utc)

    def __call__(self):
        return self.value.isoformat()

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def queue(tmp_path):
    storage = Storage(tmp_path / "worker.db")
    storage.create_schema()
    actor = Actor("worker", "workspace", "owner")
    queue = JobQueue(storage, actor, fingerprint_key=b"t" * 32, clock=Clock(), lease_seconds=2)
    yield queue
    storage.close()


def projects(queue):
    with queue.storage.workspace_transaction("workspace") as conn:
        return conn.execute("SELECT * FROM projects WHERE workspace_id=?", ("workspace",)).fetchall()


def test_expired_worker_cannot_publish_side_effects(queue):
    queue.enqueue({"kind": "test"}, "one")
    lease = queue.claim("old")
    queue.clock.advance(3)
    replacement = queue.claim("new")
    assert replacement.fence > lease.fence
    with pytest.raises(StaleLease):
        with queue.publication(lease):
            queue.storage.create_project(workspace_id="workspace", name="stale publication")
    assert projects(queue) == []


def test_expiry_during_publication_rolls_back_side_effect(queue):
    job = queue.enqueue({"kind": "test"}, "one")
    lease = queue.claim("worker")
    with pytest.raises(StaleLease):
        with queue.publication(lease):
            queue.storage.create_project(workspace_id="workspace", name="not committed")
            queue.clock.advance(3)
    assert projects(queue) == []
    assert queue.get(job.job_id).status == "leased"


def test_cancelled_job_cannot_be_recorded_as_failure(queue):
    job = queue.enqueue({"kind": "test"}, "one")
    lease = queue.claim("worker")
    queue.request_cancel(job.job_id)
    with pytest.raises(StaleLease):
        queue.fail(job.job_id, lease.fence, {"code": "handler_failed"})
    assert queue.acknowledge_cancel(job.job_id, lease.fence).status == "cancelled"


def test_service_runs_handler_and_persists_guarded_result(queue):
    job = queue.enqueue({"kind": "test"}, "one")

    def handler(actor, command, context):
        context.checkpoint()
        context.storage.create_project(workspace_id=actor.workspace_id, name="published")
        return {"ok": True}

    result = WorkerService(queue, handler).run_once()
    assert result.job_id == job.job_id
    assert result.status == "completed"
    assert result.result == {"ok": True}
    assert len(projects(queue)) == 1


def test_guarded_storage_rejects_late_write(queue):
    job = queue.enqueue({"kind": "test"}, "one")

    def handler(actor, command, context):
        queue.clock.advance(3)
        queue.claim("replacement")
        context.storage.create_project(workspace_id=actor.workspace_id, name="late")
        return {}

    result = WorkerService(queue, handler).run_once()
    assert result.status == "leased" and result.fence == 2
    assert projects(queue) == []


def test_prepared_side_effect_and_job_completion_rollback_together(queue, monkeypatch):
    job = queue.enqueue({"kind": "test"}, "one")

    def handler(actor, command, context):
        def publish():
            context.storage.create_project(workspace_id=actor.workspace_id, name="rolled back")
            return {"ok": True}
        return PreparedResult(publish)

    def fail_completion(*args):
        raise RuntimeError("completion unavailable")

    monkeypatch.setattr(queue, "complete", fail_completion)
    result = WorkerService(queue, handler).run_once()
    assert result.status == "failed"
    assert projects(queue) == []


def test_shutdown_during_handler_abandons_lease_for_reclaim(queue):
    job = queue.enqueue({"kind": "test"}, "one")
    stop = threading.Event()

    def handler(actor, command, context):
        stop.set()
        context.checkpoint()

    result = WorkerService(queue, handler).run_once(stop_event=stop)
    assert result.status == "leased"
    reclaimed = queue.claim("after-shutdown")
    assert reclaimed.job_id == job.job_id and reclaimed.fence == 2


def test_cancellation_during_handler_is_acknowledged(queue):
    job = queue.enqueue({"kind": "test"}, "one")

    def handler(actor, command, context):
        queue.request_cancel(job.job_id)
        context.checkpoint()
        raise AssertionError("cancelled handler proceeded")

    result = WorkerService(queue, handler).run_once()
    assert result.status == "cancelled"


def test_continuous_service_claims_two_jobs_without_drain(queue):
    for key in ("one", "two"):
        queue.enqueue({"kind": "test"}, key)
    stop = threading.Event()
    calls = []

    def handler(actor, command, context):
        calls.append(context.lease.job_id)
        return {}

    service = WorkerService(queue, handler)
    original = service.run_once

    def run_once(**kwargs):
        result = original(**kwargs)
        if len(calls) == 2:
            stop.set()
        return result

    service.run_once = run_once
    service.run_forever(stop_event=stop, poll_seconds=0.01)
    assert len(calls) == 2
    assert all(queue.get(job).status == "completed" for job in calls)


def test_worker_identity_changes_on_process_service_restart(queue):
    first = WorkerService(queue, lambda *args: {})
    second = WorkerService(queue, lambda *args: {})
    assert first.worker_id != second.worker_id


def test_heartbeat_extends_lease_while_handler_is_busy(queue, monkeypatch):
    queue.enqueue({"kind": "test"}, "one")
    beat = threading.Event()
    original = queue.heartbeat

    def heartbeat(*args):
        result = original(*args)
        beat.set()
        return result

    monkeypatch.setattr(queue, "heartbeat", heartbeat)

    def handler(actor, command, context):
        queue.clock.advance(1)
        assert beat.wait(3), "heartbeat thread never renewed the lease"
        queue.clock.advance(1)
        context.checkpoint()
        return {}

    result = WorkerService(queue, handler).run_once()
    assert result.status == "completed"


def test_multi_workspace_dispatcher_round_robins_queued_work(tmp_path):
    storage = Storage(tmp_path / "fair-worker.db")
    storage.create_schema()
    try:
        first = Actor("worker", "workspace-a", "owner")
        second = Actor("worker", "workspace-b", "owner")
        JobQueue(storage, first, fingerprint_key=b"t" * 32).enqueue({"kind": "fair"}, "a-1")
        JobQueue(storage, second, fingerprint_key=b"t" * 32).enqueue({"kind": "fair"}, "b-1")
        JobQueue(storage, first, fingerprint_key=b"t" * 32).enqueue({"kind": "fair"}, "a-2")

        worker = WorkflowWorker(
            storage,
            tmp_path / "artifacts",
            gateway=None,
            fingerprint_key=b"t" * 32,
            handlers={
                "fair": lambda actor, _command, _context: {
                    "workspace_id": actor.workspace_id,
                },
            },
        )

        actors = [first, second]
        first_result, cursor = worker.run_fair_once(actors, cursor=0, worker_id="fair-worker")
        second_result, _cursor = worker.run_fair_once(actors, cursor=cursor, worker_id="fair-worker")

        assert first_result.status == second_result.status == "completed"
        assert first_result.result["workspace_id"] != second_result.result["workspace_id"]
    finally:
        storage.close()
