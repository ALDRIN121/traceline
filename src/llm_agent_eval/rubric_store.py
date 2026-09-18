"""Immutable judge rubric versions (blocker 7).

A rubric version is the immutable content the judge scores against. Rubric
bodies are stored as ``judge_rubric`` object versions scoped to the project;
a judge metric references ``rubric_version_id`` — the version id of this
store. Judging resolves the rubric through the immutable version id, never
"latest", so a stored spec keeps judging against the rubric it was authored
with.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .auth import Actor
from .contracts import WorkflowError
from .versions import VersionStore

__all__ = ["RubricContent", "RubricStore"]


class RubricContent(BaseModel):
    """The immutable rubric body plus the availability map the judge may cite."""

    model_config = ConfigDict(extra="forbid")

    rubric_id: str = Field(min_length=1, max_length=255)
    version_label: str = Field(default="1", max_length=64)
    instructions: str = Field(min_length=1, max_length=20_000)
    #: Bounded candidate output offered to the judge, keyed by logical field.
    candidate_fields: list[str] = Field(min_length=1)
    #: Evidence ids the rubric declares available for citation.
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("rubric_id", "instructions")
    @classmethod
    def _bounded(cls, value: str) -> str:
        return value.strip()

    @field_validator("candidate_fields", "evidence_ids")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("entries must be unique")
        if any(not item.strip() or len(item) > 255 for item in value):
            raise ValueError("entries must be bounded non-empty strings")
        return value


class RubricStore:
    """Create/read immutable rubric versions through the VersionStore."""

    def __init__(self, storage):
        self.versions = VersionStore(storage)

    def create(self, project_id: str, content: RubricContent, expected_revision: int, actor: Actor):
        actor.require(write=True)
        content = RubricContent.model_validate(content.model_dump())
        return self.versions.create(
            "judge_rubric", project_id,
            {"record_type": "judge_rubric", "rubric": content.model_dump()},
            expected_revision, actor,
        )

    def get(self, version_id: str, actor: Actor) -> RubricContent:
        record = self.versions.get(version_id, actor)
        if record.kind != "judge_rubric" or record.content.get("record_type") != "judge_rubric":
            raise WorkflowError("A judge rubric version is required", code="rubric_version_invalid")
        return RubricContent.model_validate(record.content["rubric"])
