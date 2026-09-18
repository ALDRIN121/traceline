from __future__ import annotations

from pathlib import Path

from llm_agent_eval.contracts import InvocationResult, VerificationRecord
from llm_agent_eval.targets.local import LocalTargetAdapter
from llm_agent_eval.runtime.sandbox import SandboxResult


class FakeSandbox:
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.manifests = []
        self.inputs = []

    def run(self, request):
        self.requests.append(request)
        self.manifests.append((request.input_dir / "manifest.json").read_text())
        self.inputs.append((request.input_dir / "case.json").read_text())
        return self.result


def test_local_adapter_normalizes_result_and_hides_expected_values(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    sandbox = FakeSandbox(SandboxResult(
        state="completed", exit_code=0,
        output_files={"result.json": b'{"status":"completed","answer":"ok"}'},
        cleanup="complete", container_name="case-1",
    ))
    adapter = LocalTargetAdapter(sandbox=sandbox)

    result = adapter.invoke({
        "image": "sha256:" + "a" * 64,
        "source_dir": str(source),
        "entrypoint": ["/usr/bin/python", "/source/agent.py"],
        "timeout_seconds": 30,
    }, {"question": "hi", "expected": "must not be copied"}, {"workspace_id": "ws"})

    assert isinstance(result, InvocationResult)
    assert result.outcome == "ok"
    assert result.output == {"status": "completed", "answer": "ok"}
    request = sandbox.requests[0]
    assert request.input_dir != source
    manifest = sandbox.manifests[0]
    assert "expected" not in manifest  # the protocol manifest never carries labels
    assert '"must not be copied"' in sandbox.inputs[0]  # caller owns the input payload


def test_local_adapter_maps_sandbox_failure_to_typed_outcome(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    sandbox = FakeSandbox(SandboxResult(
        state="blocked_setup", code="rootless_runtime_required", cleanup="complete",
    ))
    result = LocalTargetAdapter(sandbox=sandbox).invoke({
        "image": "sha256:" + "a" * 64,
        "source_dir": str(source),
        "entrypoint": ["/bin/sh"],
    }, {"q": "hi"}, {"workspace_id": "ws"})

    assert result.outcome == "runtime_unavailable"
    assert result.remote_uncertainty == "blocked"


def test_local_adapter_verify_requires_observed_final_output(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    sandbox = FakeSandbox(SandboxResult(
        state="completed", exit_code=0, output_files={}, cleanup="complete",
    ))
    result = LocalTargetAdapter(sandbox=sandbox).verify({
        "image": "sha256:" + "a" * 64,
        "source_dir": str(source),
        "entrypoint": ["/bin/sh"],
    }, {"q": "ping"}, {"workspace_id": "ws"})

    assert isinstance(result, VerificationRecord)
    assert result.state == "no_output"
    assert result.capabilities["final_output"] == "unavailable"


def test_local_adapter_preserves_timeout_as_a_typed_outcome(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "agent.py").write_text("print('ok')")
    sandbox = FakeSandbox(SandboxResult(
        state="timed_out", code="timed_out", cleanup="complete",
    ))
    result = LocalTargetAdapter(sandbox=sandbox).invoke({
        "image": "sha256:" + "a" * 64,
        "source_dir": str(source),
        "entrypoint": ["/bin/sh"],
        "timeout_seconds": 3,
    }, {"q": "hi"}, {"workspace_id": "ws"})

    assert result.outcome == "timeout"
