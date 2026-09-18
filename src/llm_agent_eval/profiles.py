"""Immutable administrator-managed model profiles and independent role selections."""
from __future__ import annotations

from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .auth import Actor
from .contracts import WorkflowError
from .versions import VersionStore

SUPPORTED_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "deepseek", "azure", "ollama", "openai_compatible"})
PARAMETER_ALLOWLIST = frozenset({"max_tokens", "max_completion_tokens", "reasoning_effort"})


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    provider: Literal["openai", "anthropic", "gemini", "deepseek", "azure", "ollama", "openai_compatible"]
    model: str = Field(min_length=1, max_length=255)
    secret_ref: str = Field(default="", max_length=255)
    base_url: str | None = None
    api_version: str | None = None
    structured_output: Literal["none", "json_mode", "json_schema"] = "json_schema"
    supported_parameters: list[str] = Field(default_factory=list)
    parameters: dict[str, int | str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=60, gt=0, le=600, allow_inf_nan=False)
    keyless: bool = False

    @model_validator(mode="after")
    def _validate_binding(self):
        if not self.model.strip() or not self.name.strip():
            raise ValueError("name and model are required")
        if set(self.supported_parameters) - PARAMETER_ALLOWLIST or set(self.parameters) - set(self.supported_parameters):
            raise ValueError("parameters must be explicitly supported; sampling parameters are forbidden")
        for key, value in self.parameters.items():
            if key in {"max_tokens", "max_completion_tokens"} and (type(value) is not int or not 1 <= value <= 1_000_000):
                raise ValueError("token limits must be bounded positive integers")
            if key == "reasoning_effort" and value not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                raise ValueError("reasoning_effort is not supported")
        if self.keyless:
            if self.provider not in {"ollama", "openai_compatible"} or self.secret_ref:
                raise ValueError("only explicit local profiles may be keyless")
        elif not self.secret_ref:
            raise ValueError("a secret reference is required")
        if self.provider in {"ollama", "openai_compatible", "azure"} and not self.base_url:
            raise ValueError("this provider requires base_url")
        if self.provider == "azure" and not self.api_version:
            raise ValueError("Azure requires an explicit API version")
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base_url must be an absolute endpoint without credentials, query or fragment")
            try:
                _ = parsed.port
                local = ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = parsed.hostname == "localhost"
            if parsed.scheme != "https" and not (self.keyless and local):
                raise ValueError("authenticated endpoints require HTTPS; cleartext is keyless loopback only")
        return self

    @property
    def litellm_model(self) -> str:
        prefix = {"openai_compatible": "openai", "ollama": "ollama_chat"}.get(self.provider, self.provider)
        return self.model if self.model.startswith(prefix + "/") else f"{prefix}/{self.model}"


def require_admin(actor: Actor) -> None:
    actor.require(write=True)
    if actor.role != "owner":
        raise WorkflowError("Administrator permission required", code="forbidden", status=403)


class ProfileStore:
    def __init__(self, storage):
        self.storage = storage
        self.versions = VersionStore(storage)

    def create(self, project_id: str, profile: ModelProfile, expected_revision: int, actor: Actor):
        require_admin(actor)
        profile = ModelProfile.model_validate(profile.model_dump())
        if profile.secret_ref:
            with self.storage.workspace_transaction(actor.workspace_id) as conn:
                if conn.execute("SELECT secret_id FROM secret_refs WHERE workspace_id=? AND secret_id=? AND state='active'", (actor.workspace_id, profile.secret_ref)).fetchone() is None:
                    raise WorkflowError("Profile secret reference is unavailable", code="secret_unavailable")
        return self.versions.create("model_profile", project_id, {"record_type": "model_profile", "profile": profile.model_dump()}, expected_revision, actor)

    def get(self, version_id: str, actor: Actor) -> ModelProfile:
        record = self.versions.get(version_id, actor)
        if record.kind != "model_profile" or record.content.get("record_type") != "model_profile":
            raise WorkflowError("A model profile version is required")
        return ModelProfile.model_validate(record.content["profile"])

    def select(self, project_id: str, *, harness_profile_id: str, judge_profile_id: str, expected_revision: int, actor: Actor):
        require_admin(actor)
        for ref in (harness_profile_id, judge_profile_id):
            record = self.versions.get(ref, actor)
            if record.parent_id != project_id:
                raise WorkflowError("Profile belongs to another project")
            self.get(ref, actor)
        return self.versions.create("model_selection", project_id, {"record_type": "model_selection", "harness_profile_id": harness_profile_id, "judge_profile_id": judge_profile_id}, expected_revision, actor)

    def selected(self, selection_id: str, role: Literal["harness", "judge"], actor: Actor) -> ModelProfile:
        if role not in {"harness", "judge"}:
            raise WorkflowError("Unknown model role")
        record = self.versions.get(selection_id, actor)
        if record.kind != "model_selection" or record.content.get("record_type") != "model_selection":
            raise WorkflowError("A model selection version is required")
        return self.get(record.content[f"{role}_profile_id"], actor)
