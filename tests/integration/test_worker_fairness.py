"""Opt-in multi-process fairness rehearsal for the PostgreSQL worker pool."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path
import queue
import uuid

import pytest

from llm_agent_eval.auth import Actor
from llm_agent_eval.jobs import JobQueue
from llm_agent_eval.storage import Storage


def _run_one_fair_dispatch(
    database_url: str, artifact_root: str, workspaces: tuple[str, str],
    ready, start, results,
):
    from llm_agent_eval.gateway import MockGateway
    from llm_agent_eval.worker import WorkflowWorker

    storage = Storage(database_url)
    try:
        worker = WorkflowWorker(
            storage, Path(artifact_root), MockGateway({}), fingerprint_key=b"f" * 32,
            handlers={
                "fair": lambda actor, _command, _context: {
                    "workspace_id": actor.workspace_id,
                },
            },
        )
        actors = tuple(Actor("workflow-service", workspace, "owner") for workspace in workspaces)
        ready.set()
        start.wait(10)
        result, _cursor = worker.run_fair_once(actors, worker_id="shared-fair-pool")
        results.put(result.result["workspace_id"] if result is not None else None)
    finally:
        storage.close()


@pytest.mark.integration
def test_postgres_fair_dispatch_coordinates_multiple_processes(tmp_path: Path):
    database_url = os.environ.get("TEST_DATABASE_WORKER_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_WORKER_URL is required")

    setup = Storage(database_url)
    workspace_a = f"fair_a_{uuid.uuid4().hex}"
    workspace_b = f"fair_b_{uuid.uuid4().hex}"
    try:
        setup.create_schema()
        for index in range(4):
            JobQueue(
                setup, Actor("setup", workspace_a, "owner"), fingerprint_key=b"f" * 32,
            ).enqueue({"kind": "fair"}, f"a-{index}")
        JobQueue(
            setup, Actor("setup", workspace_b, "owner"), fingerprint_key=b"f" * 32,
        ).enqueue({"kind": "fair"}, "b-0")
    finally:
        setup.close()

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    ready_a = context.Event()
    ready_b = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_run_one_fair_dispatch,
            args=(database_url, str(tmp_path / "artifacts"), (workspace_a, workspace_b), ready_a, start, results),
        ),
        context.Process(
            target=_run_one_fair_dispatch,
            args=(database_url, str(tmp_path / "artifacts"), (workspace_a, workspace_b), ready_b, start, results),
        ),
    ]
    for process in processes:
        process.start()
    try:
        assert ready_a.wait(10) and ready_b.wait(10)
        start.set()
        values = [results.get(timeout=20), results.get(timeout=20)]
        assert set(values) == {workspace_a, workspace_b}
    finally:
        for process in processes:
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        with pytest.raises(queue.Empty):
            results.get_nowait()
