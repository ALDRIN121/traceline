import os

from llm_agent_eval.cli import build_parser, main


def test_worker_command_runs_one_empty_claim(tmp_path):
    args = build_parser().parse_args(["worker", "--once", "--artifact-root", str(tmp_path / "state")])
    assert args.once
    assert main(["worker", "--once", "--db", str(tmp_path / "db.sqlite"),
                 "--artifact-root", str(tmp_path / "state")]) == 0
    key = tmp_path / "state" / "install-fingerprint.key"
    assert len(key.read_bytes()) == 32


def test_standalone_worker_wires_the_trusted_proxy_factory(tmp_path, monkeypatch):
    import llm_agent_eval.worker as worker_module

    captured = {}

    class StubWorker:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        def run_once(self, *args, **kwargs):
            return None

    monkeypatch.setattr(worker_module, "WorkflowWorker", StubWorker)
    monkeypatch.delenv("LLM_AGENT_EVAL_SERVICE_IDENTITY", raising=False)

    assert main([
        "worker", "--once", "--db", str(tmp_path / "db.sqlite"),
        "--artifact-root", str(tmp_path / "state"),
    ]) == 0
    assert callable(captured["proxy_session_factory"])
    assert os.environ["LLM_AGENT_EVAL_SERVICE_IDENTITY"] == "proxy"
