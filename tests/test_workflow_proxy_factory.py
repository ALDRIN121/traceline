from __future__ import annotations

import json
from types import SimpleNamespace

from llm_agent_eval.api import create_app
from llm_agent_eval.auth import Actor
from llm_agent_eval.secrets import SecretStore
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.storage import Storage
from llm_agent_eval.workflow_api import default_proxy_session_factory


def test_app_wires_a_default_trusted_proxy_session_factory(tmp_path):
    storage = Storage(tmp_path / "proxy-factory.db")
    storage.create_schema()
    try:
        app = create_app(
            storage=storage,
            artifact_root=tmp_path / "artifacts",
            gateway=MockGateway({}),
        )
        assert callable(app.state.workflow_worker.proxy_session_factory)
    finally:
        storage.close()


def test_default_factory_loads_install_routes_and_resolves_proxy_secret(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "proxy-factory.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    monkeypatch.setenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", "proxy")
    try:
        secret = SecretStore(storage, actor, tmp_path / "artifacts" / "install-secret.key").put(
            "provider", "real-provider-key", allowed_services={"proxy"},
        )
        root = tmp_path / "artifacts"
        root.mkdir(exist_ok=True)
        (root / "proxy-routes.json").write_text(json.dumps({"routes": [{
            "host": "api.example.test",
            "origin": "https://api.example.test",
            "path": "/v1/chat/completions",
            "provider": "openai",
            "model": "fixture-model",
            "secret_ref": secret.secret_id,
            "dummy_key": "dummy-case-key",
            "max_input_tokens": 100,
            "max_output_tokens": 20,
            "input_micros_per_token": 2,
            "output_micros_per_token": 3,
            "price_version": "fixture-v1",
            "input_token_counter": "trusted_chat_messages",
        }]}), encoding="utf-8")
        (root / "proxy-routes.json").chmod(0o600)

        session = default_proxy_session_factory(storage, root)(
            actor, "run-1", SimpleNamespace(content={"limits": {"budget_usd_micros": 100}}), None,
        )
        assert session.routes[0].secret_ref == secret.secret_id
        assert session.secret_resolver(secret.secret_id) == "real-provider-key"
    finally:
        storage.close()
