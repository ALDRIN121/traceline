"""Disposable FastAPI server for browser acceptance coverage."""

from __future__ import annotations

import argparse
import io
from pathlib import Path
import tempfile
import zipfile

import uvicorn

from llm_agent_eval.api import create_app
from llm_agent_eval.artifacts import ArtifactStore
from llm_agent_eval.auth import Actor
from llm_agent_eval.engine import Engine
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.knowledge import KnowledgeStore
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore


WORKSPACE = "browser-workspace"
ACTOR = Actor("browser-owner", WORKSPACE, "owner")


def _source_archive() -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(
            "src/support/agent.py",
            "def build_tools():\n"
            "    return {\"lookup_order_status\": object()}\n",
        )
    return archive.getvalue()


def _seed(storage: Storage, artifact_root: Path) -> None:
    project = storage.create_project(
        workspace_id=WORKSPACE,
        name="Seeded support agent",
        entrypoint=("python", "agent.py"),
    )
    artifact = ArtifactStore(storage, artifact_root, ACTOR).put(
        WORKSPACE, _source_archive(), "application/zip"
    )
    VersionStore(storage).create(
        "source",
        project.project_id,
        {"artifact_ids": [artifact.artifact_id], "readiness": "blocked", "manifest": {}},
        0,
        ACTOR,
    )
    KnowledgeStore(storage, artifact_root).current(project.project_id, ACTOR)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="traceline-browser-") as temporary:
        root = Path(temporary)
        storage = Storage(root / "browser.db")
        storage.create_schema()
        artifact_root = root / "artifacts"
        _seed(storage, artifact_root)
        try:
            app = create_app(
                storage=storage,
                engine=Engine(storage, work_root=root / "work"),
                workspace_id=WORKSPACE,
                artifact_root=artifact_root,
                gateway=MockGateway({}),
            )
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            storage.close()


if __name__ == "__main__":
    main()
