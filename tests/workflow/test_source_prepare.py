from __future__ import annotations

import io
import zipfile

from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.runtime.build import BuildJob
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


class FakeBuilder:
    def prepare(self, source_dir, profile):
        return BuildJob("b", "prepared", "snapshot", "sha256:" + "a" * 64,
                        profile.entrypoint, {"build_policy_version": profile.policy_version})


def test_source_prepare_worker_publishes_executable_version(tmp_path):
    storage = Storage(tmp_path / "source.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="agent")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("agent.py", "print('ok')")
    artifact = ArtifactStore(storage, tmp_path / "artifacts", actor).put(
        "ws", archive.getvalue(), "application/zip"
    )
    source = VersionStore(storage).create(
        "source", project.project_id,
        {"artifact_ids": [artifact.artifact_id], "readiness": "blocked", "manifest": {}}, 0, actor,
    )
    worker = WorkflowWorker(storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32)
    # The public worker needs a builder seam for this offline test.
    from llm_agent_eval.runtime import source as source_module
    original = source_module.BuildService
    source_module.BuildService = lambda: FakeBuilder()
    try:
        job = worker.queue(actor).enqueue({
            "kind": "source_prepare", "source_version_id": source.version_id,
            "runtime_profile": {"base_image": "sha256:" + "b" * 64, "entrypoint": ["/bin/sh"]},
        }, "prepare-once")
        terminal = worker.service(actor).run_once(job_id=job.job_id)
        assert terminal.status == "completed"
        assert terminal.result["state"] == "executable"
    finally:
        source_module.BuildService = original
        storage.close()
