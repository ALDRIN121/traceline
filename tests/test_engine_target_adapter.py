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


def test_engine_ingests_worker_proxy_records_as_authoritative_trace(tmp_path):
    storage = Storage(tmp_path / "proxy-record.db")
    storage.create_schema()
    proxy_records = []

    class ProxyRecordingAdapter(FakeAdapter):
        def invoke(self, manifest, case_input, context):
            context["proxy_records"].append({
                "source": "proxy",
                "type": "llm_response",
                "host": "provider.test",
                "path": "/v1/chat/completions",
                "model": "fixture-model",
                "status": 200,
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                "cost_usd_micros": 5,
                "price_version": "fixture-v1",
            })
            return super().invoke(manifest, case_input, context)

    try:
        engine = Engine(
            storage, target_adapter=ProxyRecordingAdapter(),
            target_execution_context={"proxy_records": proxy_records},
            work_root=tmp_path / "work",
        )
        run = engine.create_run(
            "ws", _spec(), tier="quick", entrypoint=("/source/agent",),
            invocation_manifest={"image": "sha256:" + "a" * 64},
        )
        result = engine.run(run.run_id, "ws")
        assert result.status == RunStatus.COMPLETE.value
        events = storage.get_trace_events(run_id=run.run_id, workspace_id="ws")
        assert [event.type.value for event in events] == ["tool_result", "llm_response"]
        provider_event = events[-1]
        assert provider_event.source.value == "proxy"
        assert provider_event.payload == {
            "host": "provider.test", "path": "/v1/chat/completions",
            "model": "fixture-model", "status": 200,
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
        assert provider_event.cost is not None
        assert provider_event.cost.cost_usd == 0.000005
        assert provider_event.cost.tokens.input == 2
        assert provider_event.cost.tokens.output == 3
    finally:
        storage.close()


def test_engine_lands_adapter_cancellation_as_cancelled_run(tmp_path):
    class CancelAdapter:
        def invoke(self, manifest, case_input, context):
            return InvocationResult(
                outcome="cancelled", output=None,
                capabilities={"final_output": "unavailable"},
                remote_uncertainty="cancelled",
            )

    storage = Storage(tmp_path / "cancelled.db")
    storage.create_schema()
    try:
        engine = Engine(storage, target_adapter=CancelAdapter(), work_root=tmp_path / "work")
        run = engine.create_run(
            "ws", _spec(), tier="quick", entrypoint=("/source/agent",),
            invocation_manifest={"image": "sha256:" + "a" * 64},
        )
        result = engine.run(run.run_id, "ws")
        assert result.status == RunStatus.CANCELLED.value
        assert storage.get_case(run.run_id, "c1", "ws").status == "cancelled"
        assert storage.list_attempts(run.run_id, "ws")[0].status == "cancelled"
    finally:
        storage.close()


def test_engine_resumes_orphaned_remote_job_on_new_attempt(tmp_path):
    observed = []

    class ResumeAdapter(FakeAdapter):
        def invoke(self, manifest, case_input, context):
            observed.append(context.get("remote_job_id"))
            result = super().invoke(manifest, case_input, context)
            return InvocationResult(
                outcome=result.outcome, output=result.output,
                capabilities=result.capabilities,
                connector_observations={"remote_job_id": context["remote_job_id"]},
                trace_events=result.trace_events,
            )

    storage = Storage(tmp_path / "resume-remote-job.db")
    storage.create_schema()
    try:
        engine = Engine(storage, target_adapter=ResumeAdapter(), work_root=tmp_path / "work")
        run = engine.create_run(
            "ws", _spec(), tier="quick", entrypoint=("/source/agent",),
            invocation_manifest={"image": "sha256:" + "a" * 64},
        )
        old = storage.create_attempt(
            run_id=run.run_id, case_id="c1", workspace_id="ws", repeat_index=0, attempt=0,
        )
        storage.set_attempt_status(old.attempt_id, "ws", "running")
        storage.save_remote_job(
            attempt_id=old.attempt_id, run_id=run.run_id, case_id="c1",
            workspace_id="ws", remote_job_id="remote-existing", state="submitted",
        )
        engine._reconcile_stale(run.run_id, "ws")

        current = storage.create_attempt(
            run_id=run.run_id, case_id="c1", workspace_id="ws", repeat_index=1, attempt=0,
        )
        result = engine._invoke_target(
            run, run.spec.cases[0], "ws", current.attempt_id, repeat_index=1, attempt=0,
        )
        assert result.status == "completed"
        assert observed == ["remote-existing"]
        assert storage.get_remote_job(current.attempt_id, "ws")["state"] == "completed"
    finally:
        storage.close()


def test_engine_persists_proxy_evidence_when_adapter_cancels(tmp_path):
    class ProxyCancelAdapter:
        def invoke(self, manifest, case_input, context):
            context["proxy_records"].append({
                "source": "proxy", "type": "budget_exceeded",
                "host": "provider.test", "path": "/v1/chat/completions",
                "model": "fixture-model", "status": 429,
            })
            return InvocationResult(
                outcome="cancelled", output=None,
                capabilities={"final_output": "unavailable"},
                remote_uncertainty="cancelled",
            )

    storage = Storage(tmp_path / "cancelled-proxy.db")
    storage.create_schema()
    try:
        engine = Engine(
            storage, target_adapter=ProxyCancelAdapter(),
            target_execution_context={"proxy_records": []}, work_root=tmp_path / "work",
        )
        run = engine.create_run(
            "ws", _spec(), tier="quick", entrypoint=("/source/agent",),
            invocation_manifest={"image": "sha256:" + "a" * 64},
        )
        result = engine.run(run.run_id, "ws")
        assert result.status == RunStatus.CANCELLED.value
        events = storage.get_trace_events(run_id=run.run_id, workspace_id="ws")
        assert len(events) == 1
        assert events[0].source.value == "proxy"
        assert events[0].type.value == "budget_exceeded"
    finally:
        storage.close()
