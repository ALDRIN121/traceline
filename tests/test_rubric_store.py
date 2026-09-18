"""Offline contracts for the immutable rubric store (blocker 7)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import WorkflowError
from llm_agent_eval.rubric_store import RubricContent, RubricStore
from llm_agent_eval.storage import Storage

OWNER = "owner"


def make_workspace(tmp_path):
    storage = Storage(tmp_path / "eval.db")
    storage.create_schema()
    storage.create_project(workspace_id="default", name="p")
    return storage, Actor(OWNER, "default", OWNER)


def rubric(**overrides):
    fields = {
        "rubric_id": "safety",
        "instructions": "Score refusal discipline from 0 to 1.",
        "candidate_fields": ["final_output"],
        "evidence_ids": ["evt_1", "evt_2"],
    }
    fields.update(overrides)
    return RubricContent(**fields)


def make_store(tmp_path):
    storage, actor = make_workspace(tmp_path)
    return RubricStore(storage), actor, storage


def test_rubric_create_and_read_roundtrip(tmp_path):
    store, actor, storage = make_store(tmp_path)
    project_id = storage.create_project(workspace_id="default", name="rubrics").project_id
    record = store.create(project_id, rubric(), 0, actor)
    assert store.get(record.version_id, actor) == rubric()


def test_rubric_content_is_bounded(tmp_path):
    with pytest.raises(ValidationError):
        rubric(instructions="")
    with pytest.raises(ValidationError):
        rubric(evidence_ids=["evt_1", "evt_1"])
    with pytest.raises(ValidationError):
        rubric(candidate_fields=[])


def test_rubric_requires_write(tmp_path):
    store, _, storage = make_store(tmp_path)
    viewer = Actor("v", "default", "viewer")
    with pytest.raises(WorkflowError):
        store.create("project-1", rubric(), 0, viewer)


def test_missing_rubric_version_fails(tmp_path):
    store, actor, _ = make_store(tmp_path)
    with pytest.raises(WorkflowError):
        store.get("missing", actor)
