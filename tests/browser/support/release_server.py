"""Disposable release-mode FastAPI server for browser auth coverage."""

from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

import uvicorn

from llm_agent_eval.api import create_app
from llm_agent_eval.auth import create_install_auth_resolver
from llm_agent_eval.engine import Engine
from llm_agent_eval.storage import Storage


WORKSPACE = "release-browser-workspace"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args()

    token_path = Path(args.token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="traceline-release-browser-") as temporary:
        root = Path(temporary)
        storage = Storage(root / "release.db")
        storage.create_schema()
        storage.create_project(
            workspace_id=WORKSPACE,
            name="Release auth agent",
            entrypoint=("python", "agent.py"),
        )
        resolver = create_install_auth_resolver(token_path, WORKSPACE)
        app = create_app(
            storage=storage,
            engine=Engine(storage, work_root=root / "work"),
            workspace_id=WORKSPACE,
            artifact_root=root / "artifacts",
            auth_resolver=resolver,
            release_mode=True,
        )
        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            storage.close()
            token_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
