"""Packaging smoke tests for the distributable evaluation engine."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
import venv
from pathlib import Path

from packaging.requirements import Requirement


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _venv_executable(venv_dir: Path, name: str) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / f"{name}.exe"
    return venv_dir / "bin" / name


def test_package_declares_multipart_for_the_zip_upload_route():
    """Removing python-multipart makes FastAPI ZIP route registration fail."""
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as manifest:
        dependencies = tomllib.load(manifest)["project"]["dependencies"]

    assert any(Requirement(dependency).name == "python-multipart" for dependency in dependencies)


def test_clean_install_cli_and_app_factory_smoke(tmp_path):
    """A lock-synchronized isolated install exposes the CLI and API factory."""
    venv_dir = tmp_path / "install"
    venv.EnvBuilder(with_pip=False, system_site_packages=False).create(venv_dir)
    python = _venv_python(venv_dir)
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    uv = shutil.which("uv")
    assert uv is not None, "the checked-in lockfiles require uv for this installation smoke test"

    synced = subprocess.run(
        [uv, "pip", "sync", "--python", str(python), str(REPOSITORY_ROOT / "requirements-dev.lock")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert synced.returncode == 0, synced.stderr

    installed = subprocess.run(
        [uv, "pip", "install", "--python", str(python), "--no-deps", str(REPOSITORY_ROOT)],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr

    cli_help = subprocess.run(
        [str(_venv_executable(venv_dir, "eval-engine")), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cli_help.returncode == 0, cli_help.stderr
    assert "serve" in cli_help.stdout

    app_smoke = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from pathlib import Path; "
                "from llm_agent_eval.api import create_app; "
                "from llm_agent_eval.engine import Engine; "
                "from llm_agent_eval.storage import Storage; "
                "db = Storage(Path('temporary.db')); db.create_schema(); "
                "app = create_app(storage=db, engine=Engine(db, work_root=Path('work'))); "
                    "assert any(getattr(route, 'path', None) == '/api/projects/upload' for route in app.routes); "
                "db.close()"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert app_smoke.returncode == 0, app_smoke.stderr
