"""Process-level worker termination and PostgreSQL lease recovery."""

from __future__ import annotations

import multiprocessing
import os
import time
import uuid

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.jobs import JobQueue
from llm_agent_eval.storage import Storage
from llm_agent_eval.worker_service import WorkerService


PG_URL = os.environ.get("TEST_DATABASE_URL")


def _claim_and_block(database_url: str, workspace_id: str, job_id: str, claimed):
    store = Storage(database_url)
    actor = Actor("worker-child", workspace_id, "owner")
    queue = JobQueue(store, actor, fingerprint_key=b"p" * 32, lease_seconds=2)

    def handler(_actor, _command, _context):
        claimed.set()
        time.sleep(30)
        return {"unexpected": True}

    try:
        WorkerService(queue, handler, worker_id="worker-process-before").run_once(job_id=job_id)
    finally:
        store.close()


@pytest.mark.integration
def test_killed_worker_process_is_reclaimed_and_fenced_on_postgres():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL process recovery")
    workspace_id = f"process_{uuid.uuid4().hex}"
    actor = Actor("worker-owner", workspace_id, "owner")
    store = Storage(PG_URL)
    store.create_schema()
    try:
        queue = JobQueue(store, actor, fingerprint_key=b"p" * 32, lease_seconds=2)
        job = queue.enqueue({"kind": "process-recovery"}, f"process-recovery:{workspace_id}")
        context = multiprocessing.get_context("spawn")
        claimed = context.Event()
        child = context.Process(
            target=_claim_and_block,
            args=(PG_URL, workspace_id, job.job_id, claimed),
        )
        child.start()
        try:
            assert claimed.wait(10), "worker process did not claim the job"
            child.terminate()
            child.join(10)
            assert not child.is_alive()
        finally:
            if child.is_alive():
                child.kill()
                child.join(10)

        time.sleep(3)

        published = []

        def handler(_actor, _command, _context):
            published.append(True)
            return {"recovered": True}

        recovered = WorkerService(
            JobQueue(store, actor, fingerprint_key=b"p" * 32, lease_seconds=2),
            handler,
            worker_id="worker-process-after",
        ).run_once()
        assert recovered.job_id == job.job_id
        assert recovered.status == "completed"
        assert recovered.result == {"recovered": True}
        assert published == [True]
    finally:
        store.close()
