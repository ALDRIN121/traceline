"""End-to-end integration tests: the sample agent through the whole stack.

Proves the engine's smoke gate and run pipeline against a real, well-formed
agent fixture (``fixtures/sample-agent/``) talking to a mock OpenAI-compatible
LLM server (``tests/fixtures/mock_llm_server.py``), and that the engine
correctly FAILS the deliberately-broken reference agent instead of fabricating
success.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import llm_agent_eval.engine as engine_module
from llm_agent_eval.dashboard import DEFAULT_DEFINITION, resolve_definition
from llm_agent_eval.engine import Engine, ProjectNotEvaluableError
from llm_agent_eval.evaluators import CaseStatus
from llm_agent_eval.lifecycle import RunCaseStatus, SmokeState
from llm_agent_eval.storage import Storage

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_AGENT = ROOT / "fixtures" / "sample-agent"
REFERENCE_AGENT = ROOT / "fixtures" / "reference-agent" / "multi_agent_system.py"
WORKSPACE = "ws-e2e"

sys.path.insert(0, str(FIXTURES))
import mock_llm_server  # noqa: E402  (tests/fixtures has no __init__.py)
sys.path.remove(str(FIXTURES))

AGENT_ENTRYPOINT = [sys.executable, str(SAMPLE_AGENT)]


def agent_llm_env(server: mock_llm_server.MockLLMServer) -> dict[str, str]:
    """Point the agent's LLM client at the offline mock (a dummy key — the
    sandbox holds a dummy key by design, §11A)."""
    return {
        "LLM_AGENT_EVAL_LLM_BASE_URL": server.base_url,
        "LLM_AGENT_EVAL_LLM_MODEL": server.model,
        "LLM_AGENT_EVAL_LLM_KEY": "dummy-key-not-a-secret",
    }


@contextmanager
def agent_env(env: dict[str, str]):
    """Merge extra env into the engine's ``invoke_agent`` calls. The runner
    is the only channel to the agent subprocess (the engine calls
    ``invoke_agent`` directly, so we patch it on the engine module)."""
    original = engine_module.invoke_agent

    def invoke_with_env(**kwargs):
        merged = dict(env)
        merged.update(kwargs.get("env_extra") or {})
        return original(**kwargs, env_extra=merged)

    engine_module.invoke_agent = invoke_with_env
    try:
        yield
    finally:
        engine_module.invoke_agent = original


def make_spec(n_cases: int = 3) -> dict:
    """The support-triage suite: three refund cases; three metrics — the
    canonical §9A.5 trace rule, a scalar over the eligibility tool's output,
    and a cost-bound count guard."""
    cases = [
        {
            "case_id": f"refund-{i}",
            "name": f"refund request {i}",
            "input": {
                "order_id": f"ORD-{1000 + i}",
                "customer_message": f"Please refund my order ORD-{1000 + i}",
            },
        }
        for i in range(n_cases)
    ]
    metrics = [
        {
            # §9A.5 canonical shape: every search_articles call follows an
            # LLM response (the agent decided, then searched, then composed).
            "metric_id": "search_follows_llm_response",
            "name": "calls search_articles after an LLM response (tool use "
                    "follows an LLM decision)",
            "type": "trace_rule",
            "target": {"type": "trace", "on_missing": "fail"},
            "evaluator": {
                "type": "trace_rule",
                "rule": {
                    "op": "for_all",
                    "match": {"event": "tool_call", "tool": "search_articles"},
                    "assert": {"op": "exists_before", "match": {"event": "llm_response"}},
                },
            },
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 1.0},
            "provisional": False,
        },
        {
            # A scalar over the refund-eligibility tool's output.
            "metric_id": "refund_eligibility_checked",
            "name": "refund eligibility tool reports eligible",
            "type": "scalar",
            "target": {
                "type": "tool_output",
                "tool": "check_refund_eligibility",
                "selector": "$.eligible",
                "occurrence": "last",
                "on_missing": "fail",
            },
            "evaluator": {"type": "exact_match", "expected": True},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "gate": {"min": 1.0},
            "provisional": False,
        },
        {
            # A cost-bound guard: the agent must not loop on the model (each
            # case calls the LLM exactly twice: triage + compose).
            "metric_id": "llm_call_count_bounded",
            "name": "LLM responses bounded (cost guard)",
            "type": "trace_rule",
            "target": {"type": "trace", "on_missing": "fail"},
            "evaluator": {
                "type": "trace_rule",
                "rule": {"op": "count", "match": {"event": "llm_response"}},
            },
            "scoring": {
                # Exactly two LLM responses per case (triage + compose):
                # a broken matcher (count 0) must FAIL, not pass vacuously.
                "type": "binary", "range": [0, 1], "condition": "raw_value == 2",
            },
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
            "provisional": False,
        },
    ]
    return {
        "spec_version": "1",
        "name": "support-triage-suite",
        "dataset_version": "v1",
        "cases": cases,
        "metrics": metrics,
    }


def make_evaluable_project(engine: Engine, server: mock_llm_server.MockLLMServer):
    """create_project -> prepare_project -> smoke (against the mock) ->
    advance to EVALUABLE, the §32A happy path."""
    project = engine.create_project(WORKSPACE, "sample-agent")
    project = engine.prepare_project(
        project.project_id, WORKSPACE, entrypoint=AGENT_ENTRYPOINT
    )
    assert project.smoke_state == SmokeState.RUNTIME_PREPARED.value
    with agent_env(agent_llm_env(server)):
        attempt = engine.smoke(project.project_id, WORKSPACE, timeout_seconds=60.0)
    assert attempt.state == SmokeState.SMOKE_PASSED.value
    assert attempt.trace_path is not None
    assert Path(attempt.trace_path).exists(), "smoke evidence trace missing"
    return engine.advance_project(project.project_id, WORKSPACE, SmokeState.EVALUABLE)


@pytest.fixture()
def store(tmp_path: Path):
    db = Storage(tmp_path / "eval.db")
    db.create_schema()
    yield db
    db.close()


@pytest.fixture()
def engine(store: Storage, tmp_path: Path):
    return Engine(store, work_root=tmp_path / "work")


@pytest.fixture()
def llm_server():
    server = mock_llm_server.MockLLMServer().start()
    yield server
    server.stop()


def test_smoke_reference_agent_fails(engine: Engine) -> None:
    """The deliberately-broken reference agent must fail the smoke gate with
    entrypoint_missing — it declares no entrypoint (§32A.5 expects it here) —
    and the engine must never fabricate success for it."""
    project = engine.create_project(WORKSPACE, "reference-agent")
    project = engine.prepare_project(project.project_id, WORKSPACE, entrypoint=())
    assert project.smoke_state == SmokeState.ENTRYPOINT_MISSING.value
    assert project.smoke_failure_detail == "no entrypoint declared"

    # The gate: runs against a non-EVALUABLE project are refused.
    with pytest.raises(ProjectNotEvaluableError):
        engine.create_run(
            WORKSPACE, make_spec(), project_id=project.project_id, tier="quick"
        )

    # Re-preparing with the broken file as the entrypoint: the smoke
    # invocation runs the file — it does not parse (truncated mid-method) —
    # so the attempt is invocation_failed, never a fabricated pass.
    project = engine.prepare_project(
        project.project_id, WORKSPACE,
        entrypoint=[sys.executable, str(REFERENCE_AGENT)],
    )
    assert project.smoke_state == SmokeState.RUNTIME_PREPARED.value
    attempt = engine.smoke(project.project_id, WORKSPACE, timeout_seconds=30.0)
    assert attempt.state == SmokeState.INVOCATION_FAILED.value


def test_smoke_sample_agent_passes(engine: Engine, llm_server) -> None:
    """The well-formed sample agent passes the smoke gate — a real probe
    invocation whose trace is persisted as evidence — and lands EVALUABLE."""
    project = make_evaluable_project(engine, llm_server)
    assert project.smoke_state == SmokeState.EVALUABLE.value
    assert llm_server.requests, "the agent never reached the mock LLM"
    # Every request carried a usage block the agent turned into cost events.
    for request in llm_server.requests:
        assert isinstance(request["model"], str) and request["messages"]


def test_end_to_end_run(engine: Engine, store: Storage, llm_server) -> None:
    """A full run: 3 refund cases, 3 metrics (trace rule, scalar, cost guard),
    per-case scores with evidence links, run-level aggregation, gates, cost
    summaries stamped with the engine's price version, and a resolvable
    dashboard."""
    project = make_evaluable_project(engine, llm_server)
    run = engine.create_run(
        WORKSPACE, make_spec(n_cases=3), project_id=project.project_id,
        tier="quick", repeats=1, retry_max=0, entrypoint=AGENT_ENTRYPOINT,
        timeout_seconds=60.0,
    )
    with agent_env(agent_llm_env(llm_server)):
        result = engine.run(run.run_id, WORKSPACE)

    assert result.status == "complete"
    assert len(result.case_results) == 3
    for case in result.case_results:
        assert case.status == RunCaseStatus.COMPLETED.value
        assert case.classification == "PASSING"
        (outcome,) = case.repeat_outcomes
        assert outcome.status == "completed"
        assert outcome.event_count > 0
        assert len(outcome.scores) == 3
        assert all(s.status is CaseStatus.PASS for s in outcome.scores)

    # Per-case scores in storage, with evidence links (§12B.7).
    scores = store.get_case_scores(run_id=run.run_id, workspace_id=WORKSPACE)
    assert len(scores) == 9  # 3 cases x 3 metrics
    assert all(s.status == "PASS" for s in scores)
    rule_scores = [s for s in scores if s.metric_id == "search_follows_llm_response"]
    assert all(s.evidence_event_ids for s in rule_scores), \
        "trace-rule scores must carry evidence event ids"
    count_scores = [s for s in scores if s.metric_id == "llm_call_count_bounded"]
    assert all(s.evidence_event_ids for s in count_scores), \
        "count-rule scores must carry evidence event ids (non-vacuous count)"

    # Run-level aggregation: COMPLETE; gates evaluated on the gated metrics.
    metric_rows = store.get_run_metric_results(run.run_id, WORKSPACE)
    assert len(metric_rows) == 3
    assert all(r.aggregation_state == "COMPLETE" for r in metric_rows)
    gated = {m.metric_id: m for m in result.metric_results}
    assert gated["search_follows_llm_response"].gate_status == "PASS"
    assert gated["refund_eligibility_checked"].gate_status == "PASS"
    # The ungated metric reports but never gates: gate_status PASS with the
    # "no gate declared" marker (active=False, §7A.2).
    gate_results = {g.metric_id: g for g in result.gate_results}
    assert gate_results["search_follows_llm_response"].passed is True
    assert gate_results["search_follows_llm_response"].active is True
    assert gate_results["llm_call_count_bounded"].active is False
    assert gate_results["llm_call_count_bounded"].passed is True
    assert "no gate" in gate_results["llm_call_count_bounded"].message

    # Cost: the mock's usage flowed through capture into cost summaries, keyed
    # by the ENGINE's price version (the agent-side version is stamped over,
    # §12A.5) — never by the agent's deliberately-wrong one.
    summaries = store.get_cost_summaries(run.run_id, WORKSPACE)
    assert summaries, "no cost summaries — the mock usage never reached the engine"
    assert [s["price_version"] for s in summaries] == [engine.price_version]
    assert engine.price_version != "1999-12"  # the agent's fake version
    assert all(s["input_tokens"] > 0 and s["output_tokens"] > 0 for s in summaries)

    # Dashboard resolution against the completed run: default definition is
    # final, and a metric_summary block resolves the cost stat.
    resolved = resolve_definition(DEFAULT_DEFINITION, {}, {}, store, workspace_id=WORKSPACE)
    assert resolved.metrics_complete is True
    assert resolved.render_final is True

    cost_dashboard = {
        "version": 3,
        "name": "e2e cost dashboard",
        "layout": {"type": "grid", "columns": 12},
        "filters": [{"id": "run", "type": "run_selector", "default": "latest"}],
        "blocks": [
            {
                "component": "metric_summary",
                "span": 4,
                "bind": {
                    "metric": "llm_call_count_bounded",
                    "run": "$filters.run",
                    "stat": "cost",
                },
            }
        ],
    }
    resolved = resolve_definition(cost_dashboard, {}, {}, store, workspace_id=WORKSPACE)
    assert resolved.metrics_complete is True
    assert resolved.render_final is True
    block = resolved.blocks[0].payload
    assert block["available"] is True
    assert block["value"] is not None and block["value"] > 0
    assert block["cost_breakdown"], "cost stat must carry the per-version breakdown"

    # The agent really hit the mock: 3 cases x 2 LLM calls (+ the smoke probe).
    assert len(llm_server.requests) >= 7


def test_fresh_process_per_case(engine: Engine, store: Storage, llm_server) -> None:
    """Each case runs in a fresh process (§11A invariant): case A's mutation
    of a module-global state file must be invisible to case B."""
    project = make_evaluable_project(engine, llm_server)
    run = engine.create_run(
        WORKSPACE, make_spec(n_cases=2), project_id=project.project_id,
        tier="quick", repeats=1, retry_max=0, entrypoint=AGENT_ENTRYPOINT,
        timeout_seconds=60.0,
    )
    with agent_env({**agent_llm_env(llm_server), "LLM_AGENT_EVAL_MUTATE": "1"}):
        result = engine.run(run.run_id, WORKSPACE)

    assert result.status == "complete"
    assert len(result.case_results) == 2
    # Two attempt rows: one fresh process per case.
    attempts = store.list_attempts(run.run_id, WORKSPACE)
    assert len(attempts) == 2
    assert {a.case_id for a in attempts} == {"refund-0", "refund-1"}
    for case in result.case_results:
        (outcome,) = case.repeat_outcomes
        assert outcome.result_payload is not None
        assert outcome.result_payload["state_len"] == 1, \
            "module-global agent state leaked between cases — the container was reused"
        assert outcome.result_payload["case_id"] == case.case_id
