from __future__ import annotations

import io
import zipfile

import pytest

from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.evaluator_registry import CustomEvaluatorRegistry
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore


def _archive() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("evaluate.py", "# reviewed evaluator\n")
    return output.getvalue()


def _registry(tmp_path):
    storage = Storage(tmp_path / "registry.db")
    storage.create_schema()
    project = storage.create_project(workspace_id="ws-a", name="agent")
    actor = Actor("owner-a", "ws-a", "owner")
    artifact = ArtifactStore(storage, tmp_path / "artifacts", actor).put(
        "ws-a", _archive(), "application/zip"
    )
    return storage, actor, project, artifact


def _content(artifact_id: str, state: str = "draft"):
    return {
        "name": "refund-policy",
        "artifact_ids": [artifact_id],
        "image_digest": "sha256:" + "a" * 64,
        "entrypoint": ["/usr/bin/python", "/source/evaluate.py"],
        "score_range": [0, 1],
        "pass_threshold": 0.5,
        "review": {"state": state},
    }


def test_custom_evaluator_is_versioned_then_explicitly_approved(tmp_path):
    storage, actor, project, artifact = _registry(tmp_path)
    try:
        registry = CustomEvaluatorRegistry(storage, tmp_path / "artifacts", actor)
        draft = registry.create(project.project_id, _content(artifact.artifact_id), 0)
        assert draft.kind == "evaluator"
        assert draft.content["review"]["state"] == "draft"
        with pytest.raises(WorkflowError, match="approved"):
            registry.resolve(draft.version_id)

        approved = registry.approve(draft.version_id)
        assert approved.revision == 2
        assert approved.content["review"]["state"] == "approved"
        resolved = registry.resolve(approved.version_id)
        assert resolved.version_id == approved.version_id
        assert resolved.entrypoint == ("/usr/bin/python", "/source/evaluate.py")
        assert resolved.source_dir.joinpath("evaluate.py").read_text() == "# reviewed evaluator\n"
    finally:
        storage.close()


def test_custom_evaluator_definition_fails_closed_for_mutable_runtime_inputs(tmp_path):
    storage, actor, project, artifact = _registry(tmp_path)
    try:
        registry = CustomEvaluatorRegistry(storage, tmp_path / "artifacts", actor)
        bad = _content(artifact.artifact_id)
        bad["image_digest"] = "latest"
        with pytest.raises(WorkflowError, match="image_digest"):
            registry.create(project.project_id, bad, 0)
    finally:
        storage.close()

