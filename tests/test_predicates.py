"""Tests for the scalar predicate language (src/llm_agent_eval/predicates.py).

Covers the closed grammar (each comparison operator, boolean combinators,
literals, dotted paths), the missing-value rule (comparison against MISSING is
false except `== null`), three-valued combinator semantics, explicit compile
errors, and the rejection of eval-style call / `__`-attribute smuggling.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from llm_agent_eval.events import EventType, TraceEvent, make_event
from llm_agent_eval.predicates import (
    MISSING,
    UNKNOWN,
    ALLOWED_FIELDS,
    Predicate,
    PredicateError,
    PredicateSemanticError,
    PredicateSyntaxError,
)


def evt(**fields):
    """A plain event (no tool column) with a payload, plus any overrides."""
    return make_event(
        event_type=fields.pop("event_type", "llm_call"),
        run_id=fields.pop("run_id", "run_1"),
        case_id=fields.pop("case_id", "case_1"),
        workspace_id=fields.pop("workspace_id", "ws_1"),
        attempt_id=fields.pop("attempt_id", "att_1"),
        **fields,
    )


def check(text, event, expected):
    assert Predicate.compile(text).evaluate(event) is expected


# ---------------------------------------------------------------------------
# Comparison operators
# ---------------------------------------------------------------------------


def test_each_comparison_operator():
    ev = evt(payload={"amount": 49.99, "name": "Ada", "temp": -5})
    check("payload.amount == 49.99", ev, True)
    check("payload.amount == 50", ev, False)
    check("payload.amount != 49.99", ev, False)
    check("payload.amount != 50", ev, True)
    check("payload.amount < 50", ev, True)
    check("payload.amount <= 49.99", ev, True)
    check("payload.amount > 50", ev, False)
    check("payload.amount >= 49.99", ev, True)
    check("payload.amount > 49.98", ev, True)
    check("payload.temp == -5", ev, True)
    check("payload.temp < -4", ev, True)
    check("payload.temp > -6", ev, True)


def test_numeric_cross_type_comparison_int_vs_float():
    ev = evt(payload={"amount": 50})
    check("payload.amount == 50.0", ev, True)
    check("payload.amount < 50.5", ev, True)


def test_scientific_notation_literal():
    ev = evt(payload={"n": 1000})
    check("payload.n == 1e3", ev, True)
    check("payload.n >= 1.0e3", ev, True)


def test_string_comparison_and_ordering():
    ev = evt(payload={"name": "Ada", "kind": "B"})
    check("payload.name == 'Ada'", ev, True)
    check('payload.name == "Ada"', ev, True)
    check('payload.name != "Bob"', ev, True)
    check('payload.kind < "C"', ev, True)
    check('payload.kind > "A"', ev, True)
    check('payload.name == "ada"', ev, False)


def test_string_escapes():
    ev = evt(payload={"s": "line\nbreak", "q": 'say "hi"', "bs": "a\\b"})
    check(r'payload.s == "line\nbreak"', ev, True)
    check(r'payload.q == "say \"hi\""', ev, True)
    check(r"payload.s == 'line\nbreak'", ev, True)
    check(r"payload.bs == 'a\\b'", ev, True)
    check(r"payload.s == 'lineXbreak'", ev, False)


def test_boolean_literals():
    ev = evt(payload={"flag": True, "other": False})
    check("payload.flag == true", ev, True)
    check("payload.flag != false", ev, True)
    check("payload.other == false", ev, True)
    check("payload.flag", ev, True)
    check("payload.other", ev, False)


def test_bool_never_equals_number():
    ev = evt(payload={"flag": True})
    check("payload.flag == 1", ev, False)
    check("payload.flag == true", ev, True)


def test_collection_literals_are_rejected():
    # The closed grammar has string/int/float/bool/null literals only — no
    # list or dict constructors (that is indexing-adjacent syntax).
    ev = evt(payload={"tags": ["a", "b"], "meta": {"x": 1}})
    for text in (
        'payload.tags == ["a", "b"]',
        'payload.tags != ["a"]',
        'payload.meta == {"x": 1}',
        'payload.tags == []',
    ):
        with pytest.raises(PredicateSyntaxError):
            Predicate.compile(text)
        with pytest.raises(PredicateError):
            Predicate.compile(text)
    # Present values still compare fine without collection literals.
    check("payload.tags == payload.tags", ev, True)
    check("payload.meta == payload.meta", ev, True)


def test_datetime_comparison_with_iso_string():
    ev = evt(timestamp=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc))
    check('timestamp >= "2026-08-24T00:00:00Z"', ev, True)
    check('timestamp < "2026-08-24T00:00:00Z"', ev, False)
    check('timestamp == "2026-08-24T12:00:00Z"', ev, True)


# ---------------------------------------------------------------------------
# Boolean combinators and precedence
# ---------------------------------------------------------------------------


def test_and_or_not():
    ev = evt(payload={"a": 1, "b": 2, "c": 3})
    check("payload.a == 1 && payload.b == 2", ev, True)
    check("payload.a == 9 && payload.b == 2", ev, False)
    check("payload.a == 9 || payload.b == 2", ev, True)
    check("payload.a == 9 || payload.b == 9", ev, False)
    check("!payload.a == 9", ev, True)
    check("!payload.a == 1", ev, False)
    check("!!payload.a == 1", ev, True)


def test_precedence_not_over_and_over_or():
    ev = evt(payload={"a": 1, "b": 2, "c": 3})
    # && binds tighter than ||
    check("payload.a == 1 || payload.b == 9 && payload.c == 9", ev, True)
    check("payload.a == 9 || payload.b == 2 && payload.c == 3", ev, True)
    check("payload.a == 9 || payload.b == 2 && payload.c == 9", ev, False)
    # ! binds tighter than comparisons
    check("!payload.a == 9 && payload.b == 2", ev, True)
    check("!(payload.a == 1) && payload.b == 2", ev, False)
    check("!(payload.a == 9 || payload.b == 9) && payload.c == 3", ev, True)


def test_parentheses():
    ev = evt(payload={"a": 1, "b": 2, "c": 3})
    check("(payload.a == 9 || payload.b == 2) && payload.c == 3", ev, True)
    check("(payload.a == 9 || payload.b == 9) && payload.c == 3", ev, False)
    check("((payload.a == 1))", ev, True)


# ---------------------------------------------------------------------------
# Dotted paths into the event
# ---------------------------------------------------------------------------


def test_nested_payload_paths():
    ev = evt(payload={"args": {"order_id": "123", "items": [{"sku": "a"}]}})
    check('payload.args.order_id == "123"', ev, True)
    check('payload.args.order_id != "456"', ev, True)
    check("payload.args.items != null", ev, True)
    check("payload.args.items == null", ev, False)
    check("payload.args.items.sku == null", ev, True)  # lists have no sub-fields


def test_cost_block_paths():
    ev = evt(
        event_type="llm_response",
        payload={"provider": "anthropic"},
        cost={
            "tokens": {"input": 120, "output": 40, "cache_read": 30, "cache_write": 0},
            "cost_usd": 0.0012,
            "currency": "USD",
            "price_version": "42",
        },
    )
    check("cost.cost_usd > 0.001", ev, True)
    check("cost.cost_usd < 0.001", ev, False)
    check("cost.tokens.input >= 120", ev, True)
    check("cost.tokens.cache_read == 30", ev, True)
    check("cost.tokens.output == 40", ev, True)


def test_envelope_field_paths():
    ev = evt(
        event_type="llm_call",
        parent_event_id="evt_parent",
        provider_request_id="req_abc",
        redaction_state={"status": "redacted", "rules": ["rule_api_key"]},
    )
    check('provider_request_id == "req_abc"', ev, True)
    check('provider_request_id == "req_xyz"', ev, False)
    check('parent_event_id == "evt_parent"', ev, True)
    check('redaction_state.status == "redacted"', ev, True)
    check('redaction_state.status != "clean"', ev, True)


def test_allowed_fields_whitelist_is_exactly_the_closed_set():
    assert set(ALLOWED_FIELDS) == {
        "payload",
        "cost",
        "timestamp",
        "provider_request_id",
        "parent_event_id",
        "redaction_state",
    }


def test_predicate_is_reusable_across_events():
    p = Predicate.compile("payload.amount >= 50")
    assert p.evaluate(evt(payload={"amount": 60})) is True
    assert p.evaluate(evt(payload={"amount": 40})) is False


def test_evaluate_requires_a_trace_event():
    with pytest.raises(TypeError):
        Predicate.compile("payload.x == 1").evaluate({"payload": {"x": 1}})


# ---------------------------------------------------------------------------
# Missing-value semantics
# ---------------------------------------------------------------------------


def test_missing_equals_null_is_true():
    check("payload.amount == null", evt(payload={}), True)
    # symmetric
    check("null == payload.amount", evt(payload={}), True)


def test_explicit_json_null_matches_null():
    check("payload.amount == null", evt(payload={"amount": None}), True)
    check("payload.amount == null", evt(payload={"amount": 5}), False)


def test_any_other_comparison_against_missing_is_false():
    ev = evt(payload={})
    check("payload.amount == 5", ev, False)
    check("payload.amount != 5", ev, False)  # no silent truthiness
    check("payload.amount != null", ev, False)  # documented: != null is False
    check("payload.amount < 5", ev, False)
    check("payload.amount <= 5", ev, False)
    check("payload.amount > 5", ev, False)
    check("payload.amount >= 5", ev, False)
    check("payload.amount == payload.other", ev, False)  # missing == missing


def test_missing_propagates_through_combinators():
    ev = evt(payload={"b": 2})
    check("payload.a == 1 && payload.b == 2", ev, False)
    check("payload.a == 1 || payload.b == 2", ev, True)
    check("payload.a == 1 || payload.b == 9", ev, False)
    check("!payload.a == 1", ev, False)
    check("!payload.a == null", ev, False)
    check("payload.a == null && payload.b == 2", ev, True)


def test_non_boolean_in_boolean_position_is_unknown_not_truthy():
    ev = evt(payload={"name": "Ada", "flag": True})
    check("payload.name && payload.flag", ev, False)  # "Ada" is not truthy
    check("!payload.name", ev, False)
    check('payload.name || payload.flag', ev, True)


def test_top_level_unknown_is_false():
    p = Predicate.compile("payload.missing < 5")
    assert p.evaluate(evt(payload={})) is False
    assert p.tree is not None  # introspection is available


def test_missing_sentinel_is_typed_and_distinct():
    assert MISSING is not None
    assert MISSING is not False
    assert repr(MISSING) == "<MISSING>"
    assert UNKNOWN is not MISSING
    assert repr(UNKNOWN) == "<UNKNOWN>"


# ---------------------------------------------------------------------------
# Compile errors are explicit
# ---------------------------------------------------------------------------


def test_empty_predicate_is_a_compile_error():
    for text in ("", "   ", "\n\t"):
        with pytest.raises(PredicateSyntaxError):
            Predicate.compile(text)


def test_unknown_field_is_rejected_with_the_whitelist():
    for text in ("foo == 1", "test.expected.x == 1", "case.tags == 'x'", "raw_value <= 5"):
        with pytest.raises(PredicateSemanticError) as excinfo:
            Predicate.compile(text)
        assert "unknown field" in str(excinfo.value)
        assert "payload" in str(excinfo.value)


def test_underscore_identifiers_are_rejected():
    for text in (
        "payload.__class__ == 'x'",
        "payload.args.__dict__ == 1",
        "__import__('os') == 1",
        "payload._secret == 1",
        "payload.args.__class__ == 'dict'",
    ):
        with pytest.raises(PredicateError) as excinfo:
            Predicate.compile(text)
        assert "smuggling" in str(excinfo.value)


def test_function_call_syntax_is_rejected():
    for text in (
        "size(payload) > 0",
        "payload.get('x') == 1",
        "getattr(payload, 'x') == 1",
        "eval(payload) == 1",
        "contains_secret(string(payload.args))",
        "matches(payload.s, 're')",
    ):
        with pytest.raises(PredicateError):
            Predicate.compile(text)


def test_arithmetic_is_rejected():
    for text in ("payload.a + 1 == 2", "payload.a - 1 == 1", "payload.a * 2 == 4",
                 "payload.a / 2 == 1", "payload.a % 2 == 0", "payload.a + payload.b == 3"):
        with pytest.raises(PredicateError):
            Predicate.compile(text)


def test_indexing_is_rejected():
    for text in ("payload.a[0] == 1", "payload.a['k'] == 1", "payload.a[1:2] == 1"):
        with pytest.raises(PredicateError):
            Predicate.compile(text)


def test_syntax_errors_carry_position_and_message():
    with pytest.raises(PredicateSyntaxError) as excinfo:
        Predicate.compile("payload.a == ")
    err = excinfo.value
    assert err.position >= 0
    assert err.text == "payload.a == "
    assert "position" in str(err)


def test_malformed_inputs_are_syntax_errors():
    for text in (
        "(payload.a == 1",          # unbalanced paren
        "payload.a == 1 payload.b == 2",  # trailing garbage
        "payload.a = 1",            # single =
        "payload..a == 1",          # empty segment
        "payload.a === 1",          # triple =
        "payload.a > 5 < 3",        # chained comparison
        "== 1",                     # no left operand
        '"unterminated',            # unterminated string
        "payload.a == 'x",          # unterminated string
        "payload.a == 5 &&",        # dangling operator
        "payload.true == 1",        # reserved word as segment
        "@payload",                 # junk
    ):
        with pytest.raises(PredicateSyntaxError):
            Predicate.compile(text)


def test_non_string_input_is_rejected():
    with pytest.raises(TypeError):
        Predicate.compile(None)
    with pytest.raises(TypeError):
        Predicate.compile(42)


# ---------------------------------------------------------------------------
# The grammar stays closed: nothing eval-able escapes evaluation
# ---------------------------------------------------------------------------


def test_evaluate_never_reaches_python_attributes():
    """Even if a key existed with a dunder-ish name, evaluation stays on
    dict-get; and such predicates are rejected at compile time anyway."""
    ev = evt(payload={"__class__": "value"})
    assert Predicate.compile("payload.amount == 1").evaluate(ev) is False
    with pytest.raises(PredicateError):
        Predicate.compile("payload.__class__ == 'value'")


def test_paths_off_non_containers_are_missing():
    ev = evt(payload={"n": 5, "s": "hi", "ts": datetime(2026, 1, 1, tzinfo=timezone.utc)})
    check("payload.n.x == null", ev, True)  # int has no sub-fields → missing
    check("payload.s.len == null", ev, True)  # str has no sub-fields → missing
    check("payload.ts.year == 2026", ev, False)  # no datetime attribute access


def test_nesting_depth_is_capped():
    # Mirrors _MAX_RULE_DEPTH (spec / trace_rules): pathological nesting is a
    # clean compile error, never a RecursionError.
    deep_parens = "(" * 5000 + "true" + ")" * 5000
    with pytest.raises(PredicateSyntaxError):
        Predicate.compile(deep_parens)
    with pytest.raises(PredicateError):
        Predicate.compile(deep_parens)
    deep_nots = "!" * 5000 + "true"
    with pytest.raises(PredicateSyntaxError):
        Predicate.compile(deep_nots)
    # Normal nesting still compiles and evaluates.
    ev = evt(payload={"amount": 100})
    check("(" * 50 + "payload.amount >= 50" + ")" * 50, ev, True)
