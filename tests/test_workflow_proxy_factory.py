from __future__ import annotations

from llm_agent_eval.api import create_app
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.storage import Storage


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
