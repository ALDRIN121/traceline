from __future__ import annotations

from types import SimpleNamespace

from llm_agent_eval.auth import Actor
from llm_agent_eval.contracts import InvocationResult
from llm_agent_eval.events import RedactionState, make_event
from llm_agent_eval.gateway import MockGateway
from llm_agent_eval.run_plans import RunPlanService
from llm_agent_eval.storage import Storage
from llm_agent_eval.versions import VersionStore
from llm_agent_eval.worker import WorkflowWorker


class FakeTarget:
    def invoke(self, manifest, case_input, context):
        event = make_event(
            event_type="tool_result", run_id=context["run_id"],
            workspace_id=context["workspace_id"], case_id=context["case_id"],
            attempt_id=context["attempt_id"], tool="answer",
            payload={"output": {"value": "ok"}},
            redaction_state=RedactionState(status="clean"),
        )
        return InvocationResult(
            outcome="ok", output={"status": "completed"},
            capabilities={"final_output": "observed"}, trace_events=(event,),
        )


def _setup(tmp_path):
    storage = Storage(tmp_path / "worker.db")
    storage.create_schema()
    actor = Actor("owner", "ws", "owner")
    project = storage.create_project(workspace_id="ws", name="agent")
    versions = VersionStore(storage)
    evaluation = versions.create("evaluation", project.project_id, {"spec": {
        "spec_version": "1", "name": "eval", "dataset_version": "v1",
        "metrics": [{"metric_id": "answer", "name": "answer", "type": "scalar",
          "target": {"type": "tool_output", "tool": "answer", "selector": "$.value",
                      "occurrence": "last", "on_missing": "fail"},
          "evaluator": {"type": "exact_match", "expected": "ok"},
          "scoring": {"type": "binary", "range": [0, 1]},
          "aggregation": {"method": "pass_rate", "on_error": "fail"},
          "gate": {"min": 1.0}, "provisional": False}],
    }}, 0, actor)
    dataset = versions.create("dataset", project.project_id, {"cases": [
        {"case_id": "c1", "name": "one", "input": {"q": "hi"}},
    ]}, 0, actor)
    worker = WorkflowWorker(
        storage, tmp_path / "artifacts", MockGateway({}), fingerprint_key=b"f" * 32,
        execution_target_factory=lambda actor, refs, context: (FakeTarget(), {}, ("/source/agent",)),
    )
    plan = RunPlanService(storage).plan_run(actor, {
        "project_id": project.project_id, "evaluation_version_id": evaluation.version_id,
        "dataset_version_id": dataset.version_id,
    }, {"tier": "quick"})
    return storage, actor, worker, plan


def test_evaluation_run_worker_executes_and_is_idempotent(tmp_path):
    storage, actor, worker, plan = _setup(tmp_path)
    try:
        # Replace the deliberately unused target reference with a real target version
        # only through the public version store, preserving the frozen plan contract.
        # The target factory is selected before the version is otherwise interpreted.
        auth = RunPlanService(storage).authorize(actor, plan.plan_id, plan.content_digest)
        job = RunPlanService(storage).enqueue_run(
            worker, actor, plan.plan_id, auth.authorization_id, plan.content_digest, "once"
        )
        terminal = worker.service(actor).run_once(job_id=job.job_id)
        assert terminal.status == "completed"
        assert terminal.result["state"] == "complete"
        replay = worker.queue(actor).get(job.job_id)
        assert replay.status == "completed"
    finally:
        storage.close()


def test_scheduled_slots_create_distinct_measured_runs(tmp_path):
    storage, actor, worker, plan = _setup(tmp_path)
    try:
        auth = RunPlanService(storage).authorize(actor, plan.plan_id, plan.content_digest)
        command = {
            "kind": "evaluation_run", "plan_id": plan.plan_id,
            "plan_hash": plan.content_digest, "authorization_id": auth.authorization_id,
            "schedule_id": "schedule-1",
        }
        first = worker.queue(actor).enqueue({**command, "schedule_slot": "slot-1"}, "schedule:slot-1")
        second = worker.queue(actor).enqueue({**command, "schedule_slot": "slot-2"}, "schedule:slot-2")
        first_result = worker.service(actor).run_once(job_id=first.job_id)
        second_result = worker.service(actor).run_once(job_id=second.job_id)

        assert first_result.result["run_id"] != second_result.result["run_id"]
        assert len(storage.list_runs("ws")) == 2
    finally:
        storage.close()


def test_worker_binds_proxy_session_records_to_persisted_trace(tmp_path):
    storage, actor, _worker, plan = _setup(tmp_path)
    sessions = []

    class ProxyTarget(FakeTarget):
        def invoke(self, manifest, case_input, context):
            context["proxy_records"].append({
                "source": "proxy", "type": "llm_response", "host": "provider.test",
                "path": "/v1/chat/completions", "model": "fixture-model", "status": 200,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "cost_usd_micros": 2, "price_version": "fixture-v1",
            })
            return super().invoke(manifest, case_input, context)

    class ProxySession:
        def __init__(self):
            self.records = []
            self.started = False

        def start(self):
            self.started = True
            return SimpleNamespace(
                proxy_endpoint="http://proxy.internal:3128",
                network_name="llm-agent-eval-run-managed",
                network_run_id="run",
                ca_cert=tmp_path / "ca.pem",
            )

        def stop(self):
            self.started = False
            return {"state": "stopped"}

    worker = WorkflowWorker(
        storage, tmp_path / "proxy-artifacts", MockGateway({}), fingerprint_key=b"f" * 32,
        execution_target_factory=lambda actor, refs, context: (
            ProxyTarget(), {"egress": "proxy"}, ("/source/agent",)
        ),
        proxy_session_factory=lambda actor, run_id, plan, context: (
            sessions.append(ProxySession()) or sessions[-1]
        ),
    )
    try:
        auth = RunPlanService(storage).authorize(actor, plan.plan_id, plan.content_digest)
        job = RunPlanService(storage).enqueue_run(
            worker, actor, plan.plan_id, auth.authorization_id, plan.content_digest, "proxy-once"
        )
        terminal = worker.service(actor).run_once(job_id=job.job_id)
        assert terminal.status == "completed", terminal.error
        events = storage.get_trace_events(run_id=terminal.result["run_id"], workspace_id="ws")
        assert any(event.source.value == "proxy" for event in events)
        assert sessions and sessions[0].started is False
    finally:
        storage.close()
