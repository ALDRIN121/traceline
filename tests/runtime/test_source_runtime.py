from __future__ import annotations

import io
import zipfile

import pytest

from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.runtime.build import BuildJob
from llm_agent_eval.runtime.source import SourceRuntimeService
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore


ACTOR = Actor("owner", "ws", "owner")
IMAGE = "sha256:" + "b" * 64


def _archive(*files: tuple[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in files:
            archive.writestr(name, body)
    return output.getvalue()


class FakeBuilder:
    def prepare(self, source_dir, profile):
        return BuildJob(
            job_id="build-1", state="prepared", source_digest="source-digest",
            image_digest=IMAGE, entrypoint=profile.entrypoint,
            provenance={"build_policy_version": profile.policy_version},
        )


def _source(tmp_path):
    storage = Storage(tmp_path / "runtime.db")
    storage.create_schema()
    project = storage.create_project(workspace_id="ws", name="agent")
    artifact = ArtifactStore(storage, tmp_path / "artifacts", ACTOR).put(
        "ws", _archive(("agent.py", b"print('ok')\n")), "application/zip"
    )
    source = VersionStore(storage).create(
        "source", project.project_id,
        {"artifact_ids": [artifact.artifact_id], "readiness": "blocked", "manifest": {}},
        0, ACTOR,
    )
    return storage, project, source


def test_source_runtime_materializes_only_sanitized_files_and_records_runtime(tmp_path):
    storage, project, source = _source(tmp_path)
    try:
        service = SourceRuntimeService(
            storage, tmp_path / "artifacts", ACTOR, build_service=FakeBuilder()
        )
        prepared = service.prepare(
            source.version_id,
            {
                "base_image": IMAGE, "entrypoint": ["/usr/bin/python", "/source/agent.py"],
                "egress": "proxy",
            },
        )

        assert prepared.content["readiness"] == "executable"
        assert prepared.content["runtime"]["image_digest"] == IMAGE
        assert prepared.content["runtime"]["egress"] == "proxy"
        snapshot = service.materialize(prepared.version_id)
        assert (snapshot.source_dir / "agent.py").read_text() == "print('ok')\n"
        assert snapshot.source_dir.is_dir()
    finally:
        storage.close()


def test_source_runtime_rejects_missing_runtime_profile(tmp_path):
    storage, _project, source = _source(tmp_path)
    try:
        with pytest.raises(WorkflowError) as error:
            SourceRuntimeService(storage, tmp_path / "artifacts", ACTOR, build_service=FakeBuilder()).prepare(
                source.version_id, {}
            )
        assert error.value.code == "runtime_profile_required"
    finally:
        storage.close()
