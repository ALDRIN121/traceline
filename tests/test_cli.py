"""Tests for the eval-engine CLI (harness §17).

Covers init (skeleton + valid spec template), smoke (the §32A gate: pass and
failure states), run (happy path, gate failure, invalid spec, usage errors),
results, dashboards (§37B declarative resolution), and serve (health probe).
Exit codes: 0 success, 1 operational failure, 2 usage.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from llm_agent_eval.cli import main
from llm_agent_eval.spec import validate_spec

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = [sys.executable, str(FIXTURES / "fake_agent.py")]


def make_spec(n_cases=2, *, metric_overrides=None):
    cases = [
        {"case_id": f"c{i}", "name": f"case {i}", "input": {"order_id": str(i)},
         "metadata": {"tags": ["core"]}}
        for i in range(n_cases)
    ]
    metric = {
        "metric_id": "refund_amount",
        "name": "refund amount correct",
        "type": "scalar",
        "target": {
            "type": "tool_output",
            "tool": "refund_order",
            "selector": "$.amount",
            "occurrence": "last",
            "on_missing": "fail",
        },
        "evaluator": {"type": "numeric", "expected": 49.99, "tolerance": 0},
        "scoring": {"type": "binary", "range": [0, 1],
                    "condition": "raw_value == 49.99"},
        "aggregation": {"method": "pass_rate", "on_error": "fail"},
        "gate": {"min": 1.0},
        "provisional": False,
    }
    if metric_overrides:
        metric.update(metric_overrides)
    return {
        "spec_version": "1",
        "name": "refund-suite",
        "dataset_version": "v1",
        "run_tier": "quick",
        "cases": cases,
        "metrics": [metric],
    }


def run_cli(capsys, db, *argv):
    """Invoke main() capturing output; argparse errors surface as SystemExit."""
    try:
        code = main(["--db", str(db), *argv])
    except SystemExit as exc:  # argparse usage errors
        code = exc.code if isinstance(exc.code, int) else 2
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_project(dir: Path, entrypoint, cwd=None):
    dir.mkdir(parents=True, exist_ok=True)
    config = {"entrypoint": entrypoint}
    if cwd:
        config["cwd"] = cwd
    (dir / "project.json").write_text(json.dumps(config))


def write_spec(dir: Path, spec=None):
    dir.mkdir(parents=True, exist_ok=True)
    p = dir / "spec.json"
    p.write_text(json.dumps(spec or make_spec()))
    return p


class TestInit:
    def test_init_creates_skeleton(self, capsys, tmp_path):
        target = tmp_path / "agent"
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "init", str(target))
        assert code == 0
        assert "state: created" in out
        spec_path = target / "spec.json"
        project_path = target / "project.json"
        assert spec_path.is_file() and project_path.is_file()
        assert (target / "agent").is_dir()
        # The template is a valid EvaluationSpec.
        validate_spec(spec_path.read_text())
        assert json.loads(project_path.read_text())["entrypoint"] == ["python", "agent.py"]

    def test_init_refuses_existing_dir(self, capsys, tmp_path):
        target = tmp_path / "agent"
        target.mkdir()
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "init", str(target))
        assert code == 2
        assert "already exists" in err


class TestUsage:
    def test_unknown_command_is_usage_error(self, capsys, tmp_path):
        code, _, _ = run_cli(capsys, tmp_path / "eval.db", "frobnicate")
        assert code == 2

    def test_run_missing_spec_is_usage_error(self, capsys, tmp_path):
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "run", str(tmp_path / "nope.json"))
        assert code == 2
        assert "not found" in err

    def test_run_without_entrypoint_is_usage_error(self, capsys, tmp_path):
        p = write_spec(tmp_path)
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "run", str(p))
        assert code == 2
        assert "entrypoint" in err

    def test_smoke_without_project_files(self, capsys, tmp_path):
        (tmp_path / "empty").mkdir()
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "smoke", str(tmp_path / "empty"))
        assert code == 2
        assert "project.json" in err


class TestOperations:
    def test_backup_creates_an_explicit_stateful_archive(self, capsys, tmp_path):
        artifact_root = tmp_path / "artifacts"
        code, out, err = run_cli(
            capsys,
            tmp_path / "eval.db",
            "backup",
            str(tmp_path / "backup.zip"),
            "--artifact-root",
            str(artifact_root),
        )
        assert code == 0, err
        result = json.loads(out)
        assert result["state"] == "ready"
        assert result["storage"] == "sqlite"
        assert result["install_keys"] == "excluded_from_archive_restore_separately"
        assert (tmp_path / "backup.zip").is_file()

    def test_restore_requires_fresh_sqlite_destinations(self, capsys, tmp_path):
        backup = tmp_path / "backup.zip"
        code, _, err = run_cli(
            capsys, tmp_path / "eval.db", "backup", str(backup),
            "--artifact-root", str(tmp_path / "artifacts"),
        )
        assert code == 0, err
        code, out, err = run_cli(
            capsys, tmp_path / "other.db", "restore", str(backup),
            "--destination-db", str(tmp_path / "restored.db"),
            "--destination-artifact-root", str(tmp_path / "restored-artifacts"),
        )
        assert code == 0, err
        result = json.loads(out)
        assert result["state"] == "restored"
        assert result["install_keys"] == "required_separately"


class TestSmoke:
    def test_smoke_passes(self, capsys, tmp_path):
        target = tmp_path / "agent"
        run_cli(capsys, tmp_path / "eval.db", "init", str(target))
        write_project(target, AGENT)
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "smoke", str(target))
        assert code == 0, out
        assert "state: SMOKE_PASSED" in out
        assert "smoke: ok" in out

    def test_smoke_failure_state_is_operational(self, capsys, tmp_path):
        target = tmp_path / "agent"
        run_cli(capsys, tmp_path / "eval.db", "init", str(target))
        write_project(target, [sys.executable, str(tmp_path / "missing_agent.py")])
        code, out, err = run_cli(capsys, tmp_path / "eval.db", "smoke", str(target))
        assert code == 1, out
        assert "state:" in out
        assert "failed" in err


class TestRun:
    def test_run_happy_path(self, capsys, tmp_path):
        target = tmp_path / "agent"
        write_project(target, AGENT)
        spec_path = write_spec(target)
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path))
        assert code == 0, out
        assert "run: state: complete" in out
        assert "case c0: completed" in out
        assert "gate PASS" in out

    def test_run_gate_failure_is_operational(self, capsys, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_AGENT_OUTPUT_AMOUNT", "0.0")
        target = tmp_path / "agent"
        write_project(target, AGENT)
        spec_path = write_spec(target)
        code, out, err = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path))
        assert code == 1, out
        assert "run: state: complete" in out
        assert "gate failed" in err

    def test_run_invalid_spec_is_operational(self, capsys, tmp_path):
        spec = make_spec()
        del spec["metrics"][0]["target"]["on_missing"]
        spec_path = write_spec(tmp_path, spec)
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path))
        assert code == 1
        assert "spec validation failed" in err

    def test_run_prints_explicit_state_on_failure(self, capsys, tmp_path):
        spec = make_spec()
        spec["run_tier"] = "standard"  # 100-case minimum > 2 cases
        spec_path = write_spec(tmp_path, spec)
        write_project(tmp_path, AGENT)
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path))
        assert code == 1
        assert "error" in err


class TestResults:
    def test_results_after_run(self, capsys, tmp_path):
        target = tmp_path / "agent"
        write_project(target, AGENT)
        spec_path = write_spec(target)
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path), "--repeat", "3")
        assert code == 0
        run_id = out.split("run: created ", 1)[1].split(" ", 1)[0]
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "results", run_id)
        assert code == 0
        assert "status complete" in out
        assert "metric refund_amount" in out
        assert "state: ok" in out

    def test_results_missing_run(self, capsys, tmp_path):
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "results", "ghost")
        assert code == 1
        assert "not found" in err

    def test_ci_status_returns_stable_gate_exit_code(self, capsys, tmp_path):
        target = tmp_path / "agent"
        write_project(target, AGENT)
        spec_path = write_spec(target)
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path), "--repeat", "3")
        assert code == 0
        run_id = out.split("run: created ", 1)[1].split(" ", 1)[0]

        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "ci-status", run_id)

        assert code == 0
        assert json.loads(out)["exit_code"] == 0


class TestDashboards:
    def test_dashboards_default_definition(self, capsys, tmp_path):
        target = tmp_path / "agent"
        write_project(target, AGENT)
        spec_path = write_spec(target)
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path))
        run_id = out.split("run: created ", 1)[1].split(" ", 1)[0]
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "dashboards", run_id)
        assert code == 0, out
        assert "Default run overview" in out
        assert "renders as final" in out
        assert "block [0:0] run_table" in out
        assert "case c0: completed" in out
        assert "trace_evidence" in out

    def test_dashboards_invalid_definition_is_operational(self, capsys, tmp_path):
        target = tmp_path / "agent"
        write_project(target, AGENT)
        spec_path = write_spec(target)
        code, out, _ = run_cli(capsys, tmp_path / "eval.db", "run", str(spec_path))
        run_id = out.split("run: created ", 1)[1].split(" ", 1)[0]
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"version": 3, "name": "x", "layout": {"type": "grid", "columns": 12},
                                   "filters": [], "blocks": [
                                       {"component": "HTMLPreview", "span": 6, "bind": {}}]}))
        code, _, err = run_cli(
            capsys, tmp_path / "eval.db", "dashboards", run_id, "--definition", str(bad)
        )
        assert code == 1
        assert "unknown component" in err

    def test_dashboards_missing_run(self, capsys, tmp_path):
        code, _, err = run_cli(capsys, tmp_path / "eval.db", "dashboards", "ghost")
        assert code == 1
        assert "not found" in err


class TestServe:
    def test_serve_health(self, tmp_path):
        db = tmp_path / "eval.db"
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        proc = subprocess.Popen(
            [sys.executable, "-m", "llm_agent_eval.cli", "--db", str(db),
             "serve", "--host", "127.0.0.1", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 20
            ok = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                    if resp.status_code == 200 and resp.json()["status"] == "ok":
                        ok = True
                        break
                except Exception:
                    time.sleep(0.2)
            assert ok, "serve did not become healthy in time"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
