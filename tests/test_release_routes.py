"""Release mode never registers prototype source execution routes."""

from llm_agent_eval.api import create_app
from llm_agent_eval.auth import Actor
from llm_agent_eval.storage import Storage


def test_release_does_not_register_local_path_analysis(tmp_path):
    store = Storage(tmp_path / "test.db")
    store.create_schema()
    try:
        app = create_app(storage=store, artifact_root=tmp_path / "artifacts",
                         release_mode=True,
                         auth_resolver=lambda request: Actor("owner", "default", "owner"))
        paths = app.openapi()["paths"]
        for path in (
            "/api/projects/analyze", "/api/projects/upload", "/api/harness/chat",
            "/api/harness/author", "/api/runs/sample", "/api/evals/{eval_id}/run",
            "/runs", "/runs/{run_id}/start", "/runs/{run_id}/resume",
            "/api/import-jobs/{job_id}/drain",
        ):
            assert "post" not in paths.get(path, {}), path
        assert "post" in paths["/api/uploads"]
        assert "get" in paths["/runs"]
    finally:
        store.close()
