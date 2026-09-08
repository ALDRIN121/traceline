"""Actual PostgreSQL recovery proof for the job lease/fencing boundary."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.jobs import JobQueue, StaleLease
from llm_agent_eval.storage import Storage


PG_URL = os.environ.get("TEST_DATABASE_URL")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 8, tzinfo=timezone.utc)

    def __call__(self):
        return self.value.isoformat()

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


@pytest.mark.integration
def test_postgres_restart_reclaims_expired_job_and_fences_late_worker():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL job recovery integration")
    store = Storage(PG_URL)
    store.create_schema()
    workspace_id = f"jobs_{uuid.uuid4().hex}"
    actor = Actor("owner", workspace_id, "owner")
    clock = Clock()
    try:
        before_restart = JobQueue(store, actor, fingerprint_key=b"t" * 32, clock=clock, lease_seconds=2)
        job = before_restart.enqueue({"kind": "artifact", "digest": "stable"}, "restart")
        old = before_restart.claim("worker-before-restart")
        assert old is not None

        clock.advance(3)
        after_restart = JobQueue(store, actor, fingerprint_key=b"t" * 32, clock=clock, lease_seconds=2)
        reclaimed = after_restart.claim("worker-after-restart")
        assert reclaimed is not None and reclaimed.job_id == job.job_id
        with pytest.raises(StaleLease):
            after_restart.complete(job.job_id, old.fence, {"result": "late"})
        assert after_restart.complete(job.job_id, reclaimed.fence, {"result": "accepted"}).status == "completed"
    finally:
        store.close()
