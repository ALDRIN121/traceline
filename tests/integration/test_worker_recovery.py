"""Real PostgreSQL recovery proof for the durable worker service.

Requires an explicit disposable ``TEST_DATABASE_URL``; the test creates its
own workspace-scoped rows and never touches a shared database.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.jobs import JobQueue
from llm_agent_eval.storage import Storage
from llm_agent_eval.worker_service import WorkerService


PG_URL = os.environ.get("TEST_DATABASE_URL")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 17, tzinfo=timezone.utc)

    def __call__(self):
        return self.value.isoformat()

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.mark.integration
def test_worker_recovery_after_reclaim_publishes_once_and_fences_late_worker():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL worker recovery integration")
    store = Storage(PG_URL)
    store.create_schema()
    workspace_id = f"worker_{uuid.uuid4().hex}"
    actor = Actor("worker-owner", workspace_id, "owner")
    clock = Clock()
    try:
        before_restart = JobQueue(store, actor, fingerprint_key=b"t" * 32, clock=clock, lease_seconds=2)
        job = before_restart.enqueue({"kind": "test"}, "recovery")
        leased = before_restart.claim("worker-before-restart")
        assert leased is not None

        # The process dies without completing; its lease expires.
        clock.advance(3)

        after_restart = JobQueue(store, actor, fingerprint_key=b"t" * 32, clock=clock, lease_seconds=2)
        published = []

        def handler(act, command, ctx):
            published.append("ran")
            return {"recovered": True}

        result = WorkerService(after_restart, handler, worker_id="worker-after-restart").run_once()
        assert result.job_id == job.job_id
        assert result.status == "completed"
        assert result.result == {"recovered": True}
        assert published == ["ran"]

        # The dead worker's late publication is fenced by the reclaimed fence token.
        from llm_agent_eval.jobs import StaleLease
        with pytest.raises(StaleLease):
            after_restart.complete(job.job_id, leased.fence, {"stale": True})

        # Publication is not double-applied: a second service run claims nothing.
        assert WorkerService(after_restart, handler, worker_id="worker-second-pass").run_once() is None
    finally:
        store.close()


@pytest.mark.integration
def test_worker_shutdown_on_postgres_abandons_lease_for_reclaim():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL worker recovery integration")
    store = Storage(PG_URL)
    store.create_schema()
    workspace_id = f"worker_{uuid.uuid4().hex}"
    actor = Actor("worker-owner", workspace_id, "owner")
    try:
        import threading

        queue = JobQueue(store, actor, fingerprint_key=b"t" * 32, lease_seconds=30)
        job = queue.enqueue({"kind": "test"}, "shutdown")
        stop = threading.Event()

        def handler(act, command, ctx):
            stop.set()
            ctx.checkpoint()
            return {}

        result = WorkerService(queue, handler, worker_id="worker-shutdown").run_once(stop_event=stop)
        assert result.status == "leased"
        reclaimed = queue.claim("worker-later")
        assert reclaimed.job_id == job.job_id
    finally:
        store.close()
