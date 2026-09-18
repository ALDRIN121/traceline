from llm_agent_eval.cli import build_parser, main


def test_worker_command_runs_one_empty_claim(tmp_path):
    args = build_parser().parse_args(["worker", "--once", "--artifact-root", str(tmp_path / "state")])
    assert args.once
    assert main(["worker", "--once", "--db", str(tmp_path / "db.sqlite"),
                 "--artifact-root", str(tmp_path / "state")]) == 0
    key = tmp_path / "state" / "install-fingerprint.key"
    assert len(key.read_bytes()) == 32
