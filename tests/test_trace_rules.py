"""Tests for trace-rule evaluation (src/llm_agent_eval/trace_rules.py).

Covers the §9A.3 closed operator set: every operator's happy and edge
semantics, the §9A.4 concurrency contract (causal precedence via
parent_event_id; deterministic (timestamp, sequence) fallback), anchored
operators (root-level anchored op -> TraceRuleError), the match filters
(tool column, where predicate, source default proxy+adapter), and the
evidence model (matched on pass; matched + deepest failing on fail).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from llm_agent_eval.events import make_event
from llm_agent_eval.trace_rules import RuleResult, TraceRuleError, evaluate_rule

T0 = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def ev(
    event_type,
    event_id,
    *,
    ts=0,
    sequence=0,
    source="proxy",
    tool=None,
    parent=None,
    payload=None,
):
    """A §12B envelope event; tool is set only on tool_call/tool_result (the
    strict typed-column reading)."""
    kw = {}
    if tool is not None:
        kw["tool"] = tool
    if parent is not None:
        kw["parent_event_id"] = parent
    if payload is not None:
        kw["payload"] = payload
    return make_event(
        event_type=event_type,
        run_id="run1",
        case_id="c1",
        workspace_id="ws1",
        attempt_id="a1",
        event_id=event_id,
        timestamp=T0 + timedelta(milliseconds=ts),
        sequence=sequence,
        source=source,
        **kw,
    )


def ids(result: RuleResult):
    return set(result.matched)


# ---------------------------------------------------------------------------
# exists / never / count — the match-bearing operators
# ---------------------------------------------------------------------------


def test_exists_passes_on_match():
    events = [ev("tool_call", "e1", tool="lookup")]
    result = evaluate_rule({"op": "exists", "match": {"event": "tool_call"}}, events)
    assert result.result is True
    assert ids(result) == {"e1"}


def test_exists_fails_on_no_match():
    events = [ev("llm_call", "e1")]
    result = evaluate_rule({"op": "exists", "match": {"event": "tool_call"}}, events)
    assert result.result is False
    assert result.matched == ()


def test_never_passes_on_no_match():
    events = [ev("llm_call", "e1")]
    result = evaluate_rule({"op": "never", "match": {"event": "budget_exceeded"}}, events)
    assert result.result is True
    assert result.matched == ()


def test_never_fails_with_match_evidence():
    events = [ev("budget_exceeded", "e1"), ev("llm_call", "e2")]
    result = evaluate_rule({"op": "never", "match": {"event": "budget_exceeded"}}, events)
    assert result.result is False
    # The matched events are the failure evidence (what would have matched).
    assert ids(result) == {"e1"}


def test_never_passes_on_empty_trace():
    # §9A.5: a rule over an empty trace is evaluated here (never passes); the
    # metric-level on_missing governs a fully empty trace at the scoring layer.
    assert evaluate_rule({"op": "never", "match": {"event": "error"}}, []).result is True


def test_count_is_terminal_numeric():
    events = [ev("tool_call", "e1", tool="a"), ev("tool_call", "e2", tool="b")]
    result = evaluate_rule({"op": "count", "match": {"event": "tool_call"}}, events)
    assert result.result is True  # count is not a boolean verdict
    assert result.value == 2
    assert ids(result) == {"e1", "e2"}


# ---------------------------------------------------------------------------
# and / or / not / implies
# ---------------------------------------------------------------------------


def test_and_passes_when_all_pass():
    events = [ev("tool_call", "e1", tool="a"), ev("tool_result", "e2", tool="a")]
    rule = {
        "op": "and",
        "rules": [
            {"op": "exists", "match": {"event": "tool_call"}},
            {"op": "exists", "match": {"event": "tool_result"}},
        ],
    }
    result = evaluate_rule(rule, events)
    assert result.result is True
    assert ids(result) == {"e1", "e2"}


def test_and_fails_with_failing_child_evidence():
    events = [ev("error", "e1"), ev("llm_call", "e2")]
    rule = {
        "op": "and",
        "rules": [
            {"op": "never", "match": {"event": "error"}},
            {"op": "exists", "match": {"event": "llm_call"}},
        ],
    }
    result = evaluate_rule(rule, events)
    assert result.result is False
    # Evidence is the deepest failing subnode's matched events.
    assert set(result.matched) == {"e1", "e2"}
    assert set(result.failing) == {"e1"}


def test_or_passes_on_any():
    events = [ev("tool_call", "e1", tool="a")]
    rule = {
        "op": "or",
        "rules": [
            {"op": "exists", "match": {"event": "tool_call"}},
            {"op": "exists", "match": {"event": "error"}},
        ],
    }
    assert evaluate_rule(rule, events).result is True


def test_or_fails_when_all_fail():
    events = [ev("llm_call", "e1")]
    rule = {
        "op": "or",
        "rules": [
            {"op": "exists", "match": {"event": "tool_call"}},
            {"op": "exists", "match": {"event": "error"}},
        ],
    }
    result = evaluate_rule(rule, events)
    assert result.result is False
    assert result.failing == ()  # no matches anywhere -> no evidence


def test_or_failing_evidence_from_matching_children():
    # A child that failed by *matching* (never) contributes its matches.
    events = [ev("error", "e1")]
    rule = {
        "op": "or",
        "rules": [
            {"op": "never", "match": {"event": "error"}},
            {"op": "exists", "match": {"event": "tool_call"}},
        ],
    }
    result = evaluate_rule(rule, events)
    assert result.result is False
    assert set(result.failing) == {"e1"}


def test_not_inverts():
    events = [ev("llm_call", "e1")]
    assert evaluate_rule({"op": "not", "rule": {"op": "exists", "match": {"event": "error"}}}, events).result is True
    assert evaluate_rule({"op": "not", "rule": {"op": "exists", "match": {"event": "llm_call"}}}, events).result is False


def test_implies_truth_table():
    events = [ev("tool_call", "e1", tool="a")]
    antecedent = {"op": "exists", "match": {"event": "tool_call"}}
    consequent = {"op": "never", "match": {"event": "error"}}
    # A true, B true -> true
    assert evaluate_rule({"op": "implies", "rule": antecedent, "rules": [consequent]}, events).result is True
    # A true, B false -> false
    events_with_error = [*events, ev("error", "e2")]
    assert evaluate_rule({"op": "implies", "rule": antecedent, "rules": [consequent]}, events_with_error).result is False
    # A false -> true (vacuous)
    no_tool_events = [ev("error", "e2")]
    assert evaluate_rule({"op": "implies", "rule": antecedent, "rules": [consequent]}, no_tool_events).result is True


def test_implies_requires_single_consequent():
    events = []
    with pytest.raises(TraceRuleError):
        evaluate_rule(
            {"op": "implies", "rule": {"op": "exists", "match": {"event": "tool_call"}}, "rules": []},
            events,
        )


# ---------------------------------------------------------------------------
# for_all
# ---------------------------------------------------------------------------


def test_for_all_passes_when_every_candidate_satisfies_assert():
    events = [
        ev("tool_call", "e1", tool="a"),
        ev("tool_result", "e2", tool="a", parent="e1"),
        ev("tool_call", "e3", tool="b"),
        ev("tool_result", "e4", tool="b", parent="e3"),
    ]
    rule = {
        "op": "for_all",
        "match": {"event": "tool_call"},
        "assert": {"op": "exists_after", "match": {"event": "tool_result"}},
    }
    result = evaluate_rule(rule, events)
    assert result.result is True
    assert ids(result) == {"e1", "e3"}


def test_for_all_fails_with_assert_evidence():
    # e3 causally follows e1 (parent edge) but precedes e2 in time, so the
    # exists_after assert passes for e1 and fails for e2 — with e3 the
    # near-miss candidate the assert examined (the "eligibility events
    # examined" of §9A.5).
    events = [
        ev("tool_call", "e1", tool="a", ts=0),
        ev("tool_result", "e3", tool="a", ts=50, parent="e1"),
        ev("tool_call", "e2", tool="b", ts=200),
    ]
    rule = {
        "op": "for_all",
        "match": {"event": "tool_call"},
        "assert": {"op": "exists_after", "match": {"event": "tool_result"}},
    }
    result = evaluate_rule(rule, events)
    assert result.result is False
    assert result.matched == ("e1", "e2")  # root matched events
    # failing evidence: the near-miss candidates of the failing assert
    assert set(result.failing) == {"e3"}


def test_for_all_vacuous_true_on_zero_candidates():
    events = [ev("llm_call", "e1")]
    rule = {
        "op": "for_all",
        "match": {"event": "tool_call"},
        "assert": {"op": "never", "match": {"event": "error"}},
    }
    assert evaluate_rule(rule, events).result is True


def test_for_all_anchors_assert_on_each_matched_event():
    # The canonical §9A.5 shape: for_all tool_call assert exists_before
    # (a llm_call on the causal chain). e2 causally follows e1 even though the
    # timestamps say otherwise (parent edge beats the clock, §9A.4); e3 has no
    # parent and the llm_call is *after* it in time, so its assert fails with
    # the examined llm_call (e1) as near-miss evidence.
    events = [
        ev("tool_call", "e2", tool="a", ts=10, parent="e1"),
        ev("llm_call", "e1", ts=100),
        ev("tool_call", "e3", tool="b", ts=10),  # no causal predecessor
    ]
    rule = {
        "op": "for_all",
        "match": {"event": "tool_call"},
        "assert": {"op": "exists_before", "match": {"event": "llm_call"}},
    }
    result = evaluate_rule(rule, events)
    assert result.result is False
    assert set(result.failing) == {"e1"}


# ---------------------------------------------------------------------------
# anchored operators: exists_before / exists_after / immediately_precedes
# ---------------------------------------------------------------------------


def test_exists_before_causal_precedence():
    events = [
        ev("llm_call", "e1"),
        ev("tool_call", "e2", tool="a", parent="e1"),
    ]
    rule = {
        "op": "exists_before",
        "match": {"event": "llm_call"},
    }
    # anchored on e2: e1 is on the causal chain -> passes
    assert evaluate_rule(rule, events, anchor=events[1]).result is True
    # anchored on e1: nothing precedes it -> fails
    assert evaluate_rule(rule, events, anchor=events[0]).result is False


def test_exists_before_fallback_total_order():
    # No causal edges: (timestamp, sequence) orders them.
    events = [
        ev("llm_call", "e1", ts=100, sequence=1),
        ev("tool_call", "e2", tool="a", ts=200, sequence=2),
    ]
    rule = {"op": "exists_before", "match": {"event": "llm_call"}}
    assert evaluate_rule(rule, events, anchor=events[1]).result is True
    assert evaluate_rule(rule, events, anchor=events[0]).result is False


def test_exists_before_ignores_descendants():
    # A descendant of the anchor is causally after it and must not count.
    events = [
        ev("tool_call", "e1", tool="a"),
        ev("llm_call", "e2", parent="e1"),  # descendant of the anchor
    ]
    rule = {"op": "exists_before", "match": {"event": "llm_call"}}
    # fallback order would say e2 before e1 by (ts, seq) — but the descendant
    # rule applies and no other llm_call precedes e1
    assert evaluate_rule(rule, events, anchor=events[0]).result is False


def test_exists_after_uses_descendant_chain():
    events = [
        ev("tool_call", "e1", tool="a"),
        ev("tool_result", "e2", tool="a", parent="e1"),
    ]
    rule = {"op": "exists_after", "match": {"event": "tool_result"}}
    assert evaluate_rule(rule, events, anchor=events[0]).result is True
    assert evaluate_rule(rule, events, anchor=events[1]).result is False


def test_immediately_precedes_prefers_causal_edge():
    events = [
        ev("llm_call", "e1"),
        ev("llm_call", "e2", parent="e1"),
        ev("tool_call", "e3", tool="a", parent="e2"),
    ]
    rule = {"op": "immediately_precedes", "match": {"event": "llm_call"}}
    result = evaluate_rule(rule, events, anchor=events[2])
    assert result.result is True
    assert ids(result) == {"e2"}  # the direct causal parent


def test_immediately_precedes_fallback_adjacency():
    events = [
        ev("llm_call", "e1", ts=100, sequence=1),
        ev("tool_call", "e2", tool="a", ts=200, sequence=2),
    ]
    rule = {"op": "immediately_precedes", "match": {"event": "llm_call"}}
    assert evaluate_rule(rule, events, anchor=events[1]).result is True


def test_root_anchored_operator_raises():
    events = [ev("llm_call", "e1")]
    with pytest.raises(TraceRuleError):
        evaluate_rule({"op": "exists_before", "match": {"event": "llm_call"}}, events)
    with pytest.raises(TraceRuleError):
        evaluate_rule({"op": "exists_after", "match": {"event": "llm_call"}}, events)
    with pytest.raises(TraceRuleError):
        evaluate_rule({"op": "immediately_precedes", "match": {"event": "llm_call"}}, events)


def test_anchored_within_ms_filter():
    events = [
        ev("llm_call", "e1", ts=0),
        ev("tool_call", "e2", tool="a", ts=50),
        ev("tool_call", "e3", tool="b", ts=5000),
    ]
    rule = {
        "op": "exists_after",
        "match": {"event": "tool_call"},
        "within_ms": 100,
    }
    result = evaluate_rule(rule, events, anchor=events[0])
    assert result.result is True
    assert ids(result) == {"e2"}


def test_anchored_failure_evidence_is_near_misses():
    # The anchor must be one of the attempt's events (the scoring layer anchors
    # on matched events). Here the llm_call is *after* the anchor, so the
    # exists_before fails and the examined candidates are the failure evidence.
    events = [
        ev("tool_call", "e2", tool="refund", ts=0),
        ev("llm_call", "e1", ts=100),
    ]
    rule = {"op": "exists_before", "match": {"event": "llm_call"}}
    result = evaluate_rule(rule, events, anchor=events[0])
    assert result.result is False
    assert set(result.failing) == {"e1"}  # the eligibility events examined


# ---------------------------------------------------------------------------
# within_ms
# ---------------------------------------------------------------------------


def test_within_ms_anchors_at_attempt_start_at_root():
    events = [
        ev("error", "e1", ts=4000),
        ev("llm_call", "e2", ts=10),
    ]
    rule = {"op": "within_ms", "match": {"event": "llm_call"}, "ms": 100}
    result = evaluate_rule(rule, events)
    assert result.result is True
    assert ids(result) == {"e2"}


def test_within_ms_root_window_excludes_late_events():
    # The attempt start is the earliest event's timestamp; an error 4s in is
    # outside the 100 ms window anchored there.
    events = [
        ev("llm_call", "e2", ts=10),
        ev("error", "e1", ts=4000),
    ]
    rule = {"op": "within_ms", "match": {"event": "error"}, "ms": 100}
    result = evaluate_rule(rule, events)
    assert result.result is False
    assert result.matched == ()


# ---------------------------------------------------------------------------
# match filters: tool, source, where
# ---------------------------------------------------------------------------


def test_match_tool_filters_typed_column():
    events = [
        ev("tool_call", "e1", tool="lookup"),
        ev("tool_call", "e2", tool="refund"),
    ]
    rule = {"op": "exists", "match": {"event": "tool_call", "tool": "refund"}}
    result = evaluate_rule(rule, events)
    assert result.result is True
    assert ids(result) == {"e2"}


def test_match_source_defaults_to_proxy_and_adapter():
    events = [
        ev("error", "e1", source="proxy"),
        ev("error", "e2", source="adapter"),
        ev("error", "e3", source="runner"),
    ]
    rule = {"op": "exists", "match": {"event": "error"}}
    result = evaluate_rule(rule, events)
    # runner-authored pre-trace errors only match when explicitly selected
    assert ids(result) == {"e1", "e2"}


def test_match_source_explicit_runner():
    events = [
        ev("error", "e1", source="proxy"),
        ev("error", "e2", source="runner"),
    ]
    rule = {"op": "exists", "match": {"event": "error", "source": "runner"}}
    result = evaluate_rule(rule, events)
    assert ids(result) == {"e2"}


def test_match_source_invalid_raises():
    with pytest.raises(TraceRuleError):
        evaluate_rule({"op": "exists", "match": {"event": "error", "source": "sandbox"}}, [])


def test_match_where_predicate():
    events = [
        ev("tool_result", "e1", tool="refund", payload={"output": {"amount": 10}}),
        ev("tool_result", "e2", tool="refund", payload={"output": {"amount": 99}}),
    ]
    rule = {
        "op": "exists",
        "match": {"event": "tool_result", "where": "payload.output.amount > 50"},
    }
    result = evaluate_rule(rule, events)
    assert result.result is True
    assert ids(result) == {"e2"}


def test_match_where_compile_failure_raises():
    with pytest.raises(TraceRuleError):
        evaluate_rule(
            {"op": "exists", "match": {"event": "tool_call", "where": "payload.amount =="}},
            [ev("tool_call", "e1", tool="a")],
        )


def test_match_where_invalid_identifier_raises():
    # The closed predicate grammar rejects raw_value and eval-style access.
    with pytest.raises(TraceRuleError):
        evaluate_rule(
            {"op": "exists", "match": {"event": "tool_call", "where": "raw_value > 1"}},
            [ev("tool_call", "e1", tool="a")],
        )


# ---------------------------------------------------------------------------
# structural errors and determinism
# ---------------------------------------------------------------------------


def test_unknown_op_raises():
    with pytest.raises(TraceRuleError):
        evaluate_rule({"op": "regex"}, [])


def test_malformed_match_raises():
    with pytest.raises(TraceRuleError):
        evaluate_rule({"op": "exists", "match": "tool_call"}, [])


def test_non_dict_rule_raises():
    with pytest.raises(TraceRuleError):
        evaluate_rule("exists", [])


def test_empty_rules_list_raises():
    with pytest.raises(TraceRuleError):
        evaluate_rule({"op": "and", "rules": []}, [])


def test_evaluation_is_order_independent():
    # The verdict and the evidence *sets* are input-order independent (the
    # §9A.4 total order decides); with (timestamp, sequence) ties the tuple
    # order inside `matched` follows the stable sort of the input, so the
    # contract is asserted on verdict and evidence sets.
    events_a = [
        ev("tool_call", "e1", tool="a"),
        ev("error", "e2"),
        ev("tool_call", "e3", tool="b"),
    ]
    events_b = [events_a[2], events_a[0], events_a[1]]
    rule = {
        "op": "and",
        "rules": [
            {"op": "exists", "match": {"event": "tool_call"}},
            {"op": "never", "match": {"event": "error"}},
        ],
    }
    ra = evaluate_rule(rule, events_a)
    rb = evaluate_rule(rule, events_b)
    assert ra.result == rb.result
    assert set(ra.matched) == set(rb.matched)
    assert set(ra.failing) == set(rb.failing)


def test_deep_nesting_raises():
    rule = {"op": "exists", "match": {"event": "llm_call"}}
    for _ in range(105):
        rule = {"op": "not", "rule": rule}
    with pytest.raises(TraceRuleError):
        evaluate_rule(rule, [ev("llm_call", "e1")])
