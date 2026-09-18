from __future__ import annotations

from llm_agent_eval.contracts import InvocationResult
from llm_agent_eval.engine import Engine
from llm_agent_eval.events import RedactionState, make_event
from llm_agent_eval.lifecycle import RunStatus
from llm_agent_eval.storage import Storage


def _spec():
    return {
        "spec_version": "1", "name": "adapter-run", "dataset_version": "v1",
        "cases": [{"case_id": "c1", "name": "one", "input": {"q": "hi"}}],
        "metrics": [{
            "metric_id": "answer", "name": "answer", "type": "scalar",
            "target": {"type": "tool_output", "tool": "answer", "selector": "$.value",
                        "occurrence": "last", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": "ok"},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 1.0}, "provisional": False,
        }],
    }


class FakeAdapter:
    def invoke(self, manifest, case_input, context):
        event = make_event(
            event_type="tool_result", run_id=context["run_id"],
            workspace_id=context["workspace_id"], case_id=context["case_id"],
            attempt_id=context["attempt_id"], repeat_index=context["repeat_index"],
            attempt=context["attempt"], tool="answer", payload={"output": {"value": "ok"}},
            redaction_state=RedactionState(status="clean"),
        )
        return InvocationResult(
            outcome="ok", output={"status": "completed"},
            capabilities={"final_output": "observed"}, trace_events=(event,),
        )


def test_engine_scores_adapter_result_through_existing_trace_pipeline(tmp_path):
    storage = Storage(tmp_path / "engine.db")
    storage.create_schema()
    try:
        engine = Engine(storage, target_adapter=FakeAdapter(), work_root=tmp_path / "work")
        run = engine.create_run(
            "ws", _spec(), tier="quick", entrypoint=("/source/agent",),
            invocation_manifest={"image": "sha256:" + "a" * 64},
        )
        result = engine.run(run.run_id, "ws")
        assert result.status == RunStatus.COMPLETE.value
        assert result.metric_results[0].value == 1.0
        assert storage.list_attempts(run.run_id, "ws")[0].event_count == 1
    finally:
        storage.close()
