from __future__ import annotations

from fastapi.testclient import TestClient

from llm_agent_eval.api import create_app
from llm_agent_eval.auth import Actor
from llm_agent_eval.secrets import SecretStore
from llm_agent_eval.storage import Storage


def test_owner_can_create_and_rotate_secret_reference_without_echoing_value(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "secret-api.db")
    storage.create_schema()
    app = create_app(
        storage=storage,
        workspace_id="ws",
        artifact_root=tmp_path / "artifacts",
        auth_resolver=lambda _request: Actor("owner", "ws", "owner"),
    )
    client = TestClient(app)
    try:
        created = client.post(
            "/api/secrets",
            json={
                "secret_type": "provider_api_key",
                "value": "sk-live-provider",
                "allowed_services": ["proxy"],
            },
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["state"] == "active"
        assert payload["secret"]["allowed_services"] == ["proxy"]
        assert "value" not in payload and "sk-live-provider" not in created.text

        secret_id = payload["secret"]["secret_id"]
        rotated = client.post(
            f"/api/secrets/{secret_id}/rotate",
            json={"value": "sk-rotated-provider", "allowed_services": ["proxy"]},
        )
        assert rotated.status_code == 200
        assert "sk-rotated-provider" not in rotated.text

        monkeypatch.setenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", "proxy")
        resolved = SecretStore(
            storage, Actor("owner", "ws", "owner"), tmp_path / "artifacts" / "install-secret.key",
        ).resolve(secret_id)
        assert resolved == "sk-rotated-provider"
    finally:
        client.close()
        storage.close()


def test_secret_mutations_require_owner_and_reject_unknown_services(tmp_path):
    storage = Storage(tmp_path / "secret-api-auth.db")
    storage.create_schema()
    actor = Actor("editor", "ws", "editor")
    app = create_app(
        storage=storage,
        workspace_id="ws",
        artifact_root=tmp_path / "artifacts",
        auth_resolver=lambda _request: actor,
    )
    client = TestClient(app)
    try:
        forbidden = client.post(
            "/api/secrets",
            json={"secret_type": "provider_api_key", "value": "x", "allowed_services": ["proxy"]},
        )
        assert forbidden.status_code == 403
    finally:
        client.close()
        storage.close()

    storage = Storage(tmp_path / "secret-api-services.db")
    storage.create_schema()
    app = create_app(
        storage=storage,
        workspace_id="ws",
        artifact_root=tmp_path / "services-artifacts",
        auth_resolver=lambda _request: Actor("owner", "ws", "owner"),
    )
    client = TestClient(app)
    try:
        invalid = client.post(
            "/api/secrets",
            json={"secret_type": "provider_api_key", "value": "x", "allowed_services": ["unknown"]},
        )
        assert invalid.status_code == 422
    finally:
        client.close()
        storage.close()
