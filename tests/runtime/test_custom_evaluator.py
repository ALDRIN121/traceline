from pathlib import Path

from llm_agent_eval.runtime.custom_evaluator import CustomEvaluatorSandbox
from llm_agent_eval.runtime.sandbox import SandboxResult


class FakeSandbox:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return self.result


def test_custom_evaluator_has_bounded_typed_output_and_no_network(tmp_path):
    source = tmp_path / "evaluator"
    source.mkdir()
    (source / "eval.py").write_text("pass")
    sandbox = FakeSandbox(SandboxResult(
        state="completed", output_files={"score.json": b'{"score": 0.8, "evidence_refs": []}'},
        cleanup="complete",
    ))
    result = CustomEvaluatorSandbox(sandbox=sandbox).evaluate(
        image="sha256:" + "a" * 64, source_dir=source, entrypoint=("/bin/sh",),
        evidence={"answer": "ok"}, reference={"expected": "ok"},
    )
    assert result["score"] == 0.8
    assert sandbox.requests[0].egress == "none"
