"""Tests for the declarative dashboard backend (UX §37B).

Covers the definition model and validator (§37B.2: closed component
vocabulary, closed binding grammar, type-checked inputs, grid rules, the
raw-scan rule), server-side resolution against pre-aggregated results
(§37B.3), and versioning (§37B.4). All block payloads are computed from
aggregation tables only — a resolver that scans trace_events is a test
failure by construction.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from llm_agent_eval.dashboard import (
    DEFAULT_DEFINITION,
    REGISTRY_VERSION,
    DefinitionResolutionError,
    DefinitionValidationError,
    ComponentSpec,
    InputSpec,
    get_registry_version,
    register_component,
    registry_components,
    resolve_definition,
    validate_definition,
)
from llm_agent_eval.engine import Engine
from llm_agent_eval.storage import Storage

FIXTURES = Path(__file__).parent / "fixtures"
AGENT = [sys.executable, str(FIXTURES / "fake_agent.py")]
WORKSPACE = "ws1"


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
        "cases": cases,
        "metrics": [metric],
    }


@pytest.fixture()
def store(tmp_path):
    db = Storage(tmp_path / "eval.db")
    db.create_schema()
    yield db
    db.close()


@pytest.fixture()
def engine(store, tmp_path):
    return Engine(store, work_root=tmp_path / "work")


def completed_run(engine, store, n_cases=2, **kwargs):
    spec = make_spec(n_cases=n_cases)
    run = engine.create_run(
        WORKSPACE, spec, tier="quick", repeats=1, retry_max=0,
        entrypoint=AGENT, **kwargs,
    )
    result = engine.run(run.run_id, WORKSPACE)
    assert result.status == "complete"
    return run


# The §37B.1 canonical shape: version 3, 12-column grid, filters declared,
# blocks covering every binding form ($filters, $selection, $run, literals).
CANONICAL = {
    "version": 3,
    "name": "Refund Safety",
    "layout": {"type": "grid", "columns": 12},
    "filters": [
        {"id": "run", "type": "run_selector", "default": "latest"},
        {"id": "case_tag", "type": "tag_filter", "default": None},
    ],
    "blocks": [
        {"component": "metric_summary", "span": 3,
         "bind": {"metric": "refund_amount", "run": "$filters.run", "stat": "pass_rate"}},
        {"component": "run_table", "span": 9,
         "bind": {"run": "$run.id"}},
        {"component": "case_table", "span": 12,
         "bind": {"run": "$filters.run", "tag": "$filters.case_tag", "selection": "case"}},
        {"component": "trace_evidence", "span": 12,
         "bind": {"run": "$filters.run", "case": "$selection.case"}},
    ],
}


def problems_of(defn):
    with pytest.raises(DefinitionValidationError) as exc:
        validate_definition(defn)
    return exc.value.problems


def codes_of(defn):
    return {p.code for p in problems_of(defn)}


class TestValidator:
    def test_canonical_validates(self):
        validate_definition(CANONICAL)

    def test_default_definition_validates(self):
        validate_definition(DEFAULT_DEFINITION)

    def test_definition_from_json_string(self):
        validate_definition(json.dumps(CANONICAL))

    def test_version_must_be_3(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["version"] = 2
        assert "unsupported_version" in codes_of(defn)

    def test_grid_columns_fixed_at_12(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["layout"]["columns"] = 8
        assert "grid_violation" in codes_of(defn)

    def test_span_outside_grid(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["span"] = 13
        assert "grid_violation" in codes_of(defn)
        defn["blocks"][0]["span"] = 0
        assert "grid_violation" in codes_of(defn)

    def test_unknown_component(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["component"] = "HTMLPreview"
        assert "unknown_component" in codes_of(defn)

    def test_canonical_metric_card_validates(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["component"] = "MetricCard"
        validate_definition(defn)

    def test_unknown_filter_reference(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][1]["bind"]["run"] = "$filters.nope"
        assert "unknown_filter_ref" in codes_of(defn)

    def test_unknown_selection_reference(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][3]["bind"]["case"] = "$selection.other"
        assert "unknown_selection_ref" in codes_of(defn)

    def test_unknown_run_field(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][1]["bind"]["run"] = "$run.nope"
        assert "unknown_run_field" in codes_of(defn)

    def test_invalid_binding_prefix(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["run"] = "$expr(evil)"
        assert "invalid_binding" in codes_of(defn)

    def test_missing_required_input(self):
        defn = json.loads(json.dumps(CANONICAL))
        del defn["blocks"][0]["bind"]["stat"]
        assert "missing_required_input" in codes_of(defn)

    def test_type_mismatch(self):
        # A tag (string) bound to a run_id input — rule 3.
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["run"] = "$filters.case_tag"
        assert "type_mismatch" in codes_of(defn)

    def test_enum_input_rejects_unknown_literal(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["stat"] = "latest"
        assert "type_mismatch" in codes_of(defn)

    def test_unknown_input(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["onclick"] = "alert(1)"
        assert "unknown_input" in codes_of(defn)

    def test_duplicate_filter_id(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["filters"].append({"id": "run", "type": "run_selector"})
        assert "duplicate_filter_id" in codes_of(defn)

    def test_unknown_filter_type(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["filters"][0]["type"] = "sql_injection"
        assert "unknown_filter_type" in codes_of(defn)

    def test_conflicting_selection_types(self):
        # Two components publish the same selection id with different types.
        # (A test-only component, registered and removed around the check —
        # the validator's conflicting_selection path needs two types for one
        # selection id, which the built-ins never do.)
        from llm_agent_eval.dashboard import _REGISTRY

        defn = json.loads(json.dumps(CANONICAL))
        comp = ComponentSpec(
            name="zz_case_picker",
            version=1,
            inputs=(
                InputSpec("run", "run_id", required=True),
                InputSpec("selection", "selection_source", required=False),
            ),
            selection_type="metric_id",  # conflicts with case_table's case_id
            resolve=lambda bind, ctx: {"component": "zz_case_picker"},
        )
        _REGISTRY["zz_case_picker"] = comp
        try:
            defn["blocks"].append(
                {"component": "zz_case_picker", "span": 6,
                 "bind": {"run": "$filters.run", "selection": "case"}}
            )
            assert "conflicting_selection" in codes_of(defn)
        finally:
            _REGISTRY.pop("zz_case_picker", None)

    def test_registry_mismatch_quarantines(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["registry_version"] = 99
        assert "registry_mismatch" in codes_of(defn)

    def test_raw_scan_component_refused_at_registration(self):
        """Rule 6: the registry itself refuses trace-requiring components —
        a definition can never reference one."""
        spec = ComponentSpec(
            name="raw_viewer",
            version=1,
            inputs=(),
            resolve=lambda bind, ctx: {},
            trace_required=True,
        )
        with pytest.raises(DefinitionValidationError) as exc:
            register_component(spec)
        assert exc.value.problems[0].code == "raw_scan_violation"

    def test_problems_carry_field_and_message(self):
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["component"] = "nope"
        problems = problems_of(defn)
        assert any(
            p.field == "blocks[0].component" and p.code == "unknown_component"
            for p in problems
        )

    def test_registry_exposes_components_and_version(self):
        assert get_registry_version() == REGISTRY_VERSION == 1
        names = set(registry_components())
        assert {"metric_summary", "run_table", "case_table", "trace_evidence"} <= names
        assert {"MetricCard", "RunSummary", "TestCaseTable", "TraceTimeline"} <= names


class TestResolution:
    def test_resolve_canonical_against_completed_run(self, engine, store):
        run = completed_run(engine, store)
        resolved = resolve_definition(
            CANONICAL, {"run": run.run_id}, {"case": "c0"}, store, workspace_id=WORKSPACE
        )
        assert resolved.name == "Refund Safety"
        assert resolved.registry_version == 1
        assert resolved.run["run_id"] == run.run_id
        assert resolved.metrics_complete is True
        assert resolved.render_final is True

        blocks = {b.component: b for b in resolved.blocks}
        summary = blocks["metric_summary"].payload
        assert summary["available"] is True
        assert summary["stat"] == "pass_rate"
        assert summary["value"] == 1.0
        assert summary["aggregation_state"] == "COMPLETE"
        assert summary["gate_status"] == "PASS"
        assert summary["provisional"] is False
        assert summary["render_final"] is True

        table = blocks["run_table"].payload
        assert table["cases"]["total"] == 2
        assert table["cases"]["completed"] == 2
        assert table["metrics"][0]["aggregation_state"] == "COMPLETE"

        cases = blocks["case_table"].payload["cases"]
        assert len(cases) == 2
        row = cases[0]["metrics"][0]
        assert row["status"] == "PASS"
        assert row["is_authoritative"] is True
        assert row["on_retry_override"] is False
        assert row["badges"] == ["PASS"]
        assert cases[0]["selection"] == {"source": "case", "value": cases[0]["case_id"]}

        evidence = blocks["trace_evidence"].payload
        assert evidence["placeholder"] is False
        assert evidence["trace_url"] == f"/runs/{run.run_id}/traces/c0"
        # The scalar metric's evidence is the tool_result event that produced
        # the asserted value (event ids are attempt-scoped: {attempt}-evN).
        ids = evidence["metrics"][0]["evidence_event_ids"]
        assert len(ids) == 1
        assert ids[0].endswith("-ev2")

    def test_run_filter_default_latest(self, engine, store):
        completed_run(engine, store)
        second = completed_run(engine, store)
        resolved = resolve_definition(CANONICAL, {}, {}, store, workspace_id=WORKSPACE)
        assert resolved.run["run_id"] == second.run_id

    def test_placeholder_when_no_selection(self, engine, store):
        run = completed_run(engine, store)
        resolved = resolve_definition(CANONICAL, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)
        blocks = {b.component: b for b in resolved.blocks}
        evidence = blocks["trace_evidence"].payload
        assert evidence["placeholder"] is True
        assert evidence["case_id"] is None

    def test_selection_binds_case_id(self, engine, store):
        run = completed_run(engine, store)
        resolved = resolve_definition(
            CANONICAL, {"run": run.run_id}, {"case": "c1"}, store, workspace_id=WORKSPACE
        )
        blocks = {b.component: b for b in resolved.blocks}
        evidence = blocks["trace_evidence"].payload
        assert evidence["case_id"] == "c1"
        ids = evidence["metrics"][0]["evidence_event_ids"]
        assert len(ids) == 1
        assert ids[0].endswith("-ev2")  # the tool_result event that produced the value

    def test_tag_filter_excludes_cases(self, engine, store):
        run = completed_run(engine, store)
        resolved = resolve_definition(
            CANONICAL, {"run": run.run_id, "case_tag": "core"}, {}, store,
            workspace_id=WORKSPACE,
        )
        blocks = {b.component: b for b in resolved.blocks}
        assert len(blocks["case_table"].payload["cases"]) == 2
        resolved = resolve_definition(
            CANONICAL, {"run": run.run_id, "case_tag": "other"}, {}, store,
            workspace_id=WORKSPACE,
        )
        blocks = {b.component: b for b in resolved.blocks}
        assert blocks["case_table"].payload["cases"] == []

    def test_provisional_metric_badges(self, engine, store):
        spec = make_spec(metric_overrides={"provisional": True})
        run = engine.create_run(
            WORKSPACE, spec, tier="quick", repeats=1, retry_max=0, entrypoint=AGENT
        )
        engine.run(run.run_id, WORKSPACE)
        defn = json.loads(json.dumps(CANONICAL))
        resolved = resolve_definition(defn, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)
        blocks = {b.component: b for b in resolved.blocks}
        row = blocks["case_table"].payload["cases"][0]["metrics"][0]
        assert row["provisional"] is True
        assert "provisional" in row["badges"]
        assert blocks["metric_summary"].payload["provisional"] is True

    def test_p95_stat_resolution(self, engine, store):
        spec = make_spec(metric_overrides={
            "scoring": {"type": "binary", "range": [0, 1], "condition": "raw_value == 49.99"},
            "aggregation": {"method": "p95", "on_error": "fail"},
        })
        run = engine.create_run(
            WORKSPACE, spec, tier="quick", repeats=1, retry_max=0, entrypoint=AGENT
        )
        engine.run(run.run_id, WORKSPACE)
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["stat"] = "p95"
        resolved = resolve_definition(defn, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)
        blocks = {b.component: b for b in resolved.blocks}
        summary = blocks["metric_summary"].payload
        assert summary["available"] is True
        assert summary["method"] == "p95"
        assert summary["value"] is not None

    def test_cost_stat_available_from_summaries(self, engine, store):
        """Cost is an aggregates-only surface (§37B.2 rule 6): the fake agent
        emits 3 cost-bearing events per attempt (cost_usd 0.0012 each), the
        engine writes cost_summaries at finalize, and the resolver reads
        exactly those — never trace_events. 2 cases x 1 repeat = 6 events."""
        run = completed_run(engine, store)
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["stat"] = "cost"
        resolved = resolve_definition(defn, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)
        blocks = {b.component: b for b in resolved.blocks}
        summary = blocks["metric_summary"].payload
        assert summary["available"] is True
        assert summary["value"] == round(6 * 0.0012, 6)  # 0.0072
        breakdown = summary["cost_breakdown"]
        assert len(breakdown) == 1  # one price_version after ingestion stamping
        assert breakdown[0]["price_version"] == "2026-08"  # stamped, not "1999-01"
        assert breakdown[0]["input_tokens"] == 60   # 6 events x 10
        assert breakdown[0]["output_tokens"] == 120  # 6 events x 20
        assert breakdown[0]["usd_micros"] == 7200

    def test_cost_stat_unavailable_until_finalize(self, engine, store):
        """A cancelled run never reaches finalize, so no summaries exist and
        the stat stays unavailable — the resolver still never scans raw
        traces for it."""
        run = engine.create_run(
            WORKSPACE, make_spec(), tier="quick", repeats=1, retry_max=0, entrypoint=AGENT
        )
        engine.run(run.run_id, WORKSPACE, should_cancel=lambda: True)
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["stat"] = "cost"
        resolved = resolve_definition(defn, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)
        blocks = {b.component: b for b in resolved.blocks}
        summary = blocks["metric_summary"].payload
        assert summary["available"] is False
        assert "never scan" in summary["reason"]

    def test_partial_run_never_renders_final(self, engine, store):
        """A cancelled run renders partial: metrics_complete False,
        render_final False, aggregation_state PARTIAL on the summary."""
        run = engine.create_run(
            WORKSPACE, make_spec(), tier="quick", repeats=1, retry_max=0, entrypoint=AGENT
        )
        result = engine.run(run.run_id, WORKSPACE, should_cancel=lambda: True)
        assert result.status == "cancelled"
        resolved = resolve_definition(CANONICAL, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)
        assert resolved.metrics_complete is False
        assert resolved.render_final is False
        blocks = {b.component: b for b in resolved.blocks}
        summary = blocks["metric_summary"].payload
        assert summary["aggregation_state"] == "PARTIAL"
        assert summary["render_final"] is False

    def test_no_run_in_workspace_is_placeholder_not_crash(self, store):
        resolved = resolve_definition(CANONICAL, {}, {}, store, workspace_id=WORKSPACE)
        assert resolved.run is None
        assert resolved.render_final is False
        for block in resolved.blocks:
            assert block.payload.get("available") is False
            assert "reason" in block.payload

    def test_unknown_metric_in_run_is_unavailable(self, engine, store):
        run = completed_run(engine, store)
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["bind"]["metric"] = "ghost_metric"
        resolved = resolve_definition(defn, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)
        blocks = {b.component: b for b in resolved.blocks}
        summary = blocks["metric_summary"].payload
        assert summary["available"] is False
        assert "not in the run's frozen spec" in summary["reason"]

    def test_invalid_definition_never_resolves(self, engine, store):
        run = completed_run(engine, store)
        defn = json.loads(json.dumps(CANONICAL))
        defn["blocks"][0]["component"] = "nope"
        with pytest.raises(DefinitionValidationError):
            resolve_definition(defn, {"run": run.run_id}, {}, store, workspace_id=WORKSPACE)

    def test_resolution_never_scans_trace_events(self, engine, store):
        """§37B.2 rule 6, enforced in tests: resolution touches aggregation
        tables only. A store that raises on any trace_events read must be
        invisible to the resolver."""

        class GuardStorage:
            def get_trace_events(self, *args, **kwargs):
                raise AssertionError("the dashboard resolver scanned trace_events")

            def __getattr__(self, name):
                return getattr(store, name)

        run = completed_run(engine, store)
        resolved = resolve_definition(
            CANONICAL, {"run": run.run_id}, {"case": "c0"}, GuardStorage(),
            workspace_id=WORKSPACE,
        )
        evidence = {b.component: b for b in resolved.blocks}["trace_evidence"].payload
        assert evidence["metrics"][0]["evidence_count"] == 1
