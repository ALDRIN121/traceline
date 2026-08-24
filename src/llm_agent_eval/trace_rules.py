"""Trace-rule evaluation (semantics §9A.3 / §9A.4) — the closed operator set.

A rule node is a JSON object with an ``op`` from the closed set (for_all,
exists, never, exists_before, exists_after, immediately_precedes, count,
within_ms, and, or, not, implies — §9A.3, verbatim in ``spec.TRACE_RULE_OPS``).
Every node evaluates to a :class:`RuleResult` carrying the events it matched —
that report **is** the evidence link (§9A.7.3). The metric's stored evidence
is the root node's matched events plus the matched events of the deepest
failing subnode (or the matched events that satisfied the rule, on pass) —
implemented via ``matched`` (this node's matches) and ``failing`` (this node's
failure evidence: its own matches plus its failing children's evidence).

Ordering is §9A.4's concurrency contract:

- **Causal precedence** is exactly the ``parent_event_id`` ancestor chain.
- Everything else is ordered by ``(timestamp, sequence)`` — a deterministic
  total order that makes every operator total and reproducible.
- ``exists_before``/``exists_after`` consider only non-descendant events.

Anchored operators (``exists_before``, ``exists_after``,
``immediately_precedes``) require an anchor event — they exist to serve as
``for_all``'s ``assert`` (anchored on each matched event) or inside a compound
that carries the anchor. At the rule root there is no anchor, so a root-level
anchored operator raises :class:`TraceRuleError` and the metric routes that to
``on_error`` (§7A) rather than silently producing a number. ``within_ms`` is
the exception: at root level its anchor is the attempt start (§9A.3).

``match.where`` is a §9A scalar predicate in the closed grammar of
``predicates.py`` — the stopgap until full CEL lands with the cel-python
dependency. A ``where`` that does not compile raises TraceRuleError: the rule
cannot be evaluated, and ``on_error`` decides the case (§7A.4). The ``tool``
key matches the envelope's typed top-level column (§12B.2, strict reading
carried from the core layer: non-null only on tool_call/tool_result); a tool
name carried inside a payload (retrieval, guardrail_check, …) is matched with
a ``where`` predicate instead. ``source`` defaults to proxy+adapter (§9A.3/
§9A.6: adapter events are first-class evidence; operators never filter by
source unless a metric explicitly asks).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .events import Source, TraceEvent
from .predicates import Predicate, PredicateError
from .spec import TRACE_RULE_OPS

__all__ = ["RuleResult", "TraceRuleError", "evaluate_rule"]

#: Mirrors spec._MAX_RULE_DEPTH — evaluation recurses no deeper than the
#: structural validator allowed.
_MAX_DEPTH = 100


class TraceRuleError(Exception):
    """The rule cannot be evaluated as written (uncompilable ``where``, an
    anchored operator at the root, a malformed node). The metric evaluator
    turns this into an ERROR case result; ``aggregation.on_error`` decides the
    run-level rollup (§7A.4)."""


@dataclass(frozen=True)
class RuleResult:
    """One rule node's evaluation: the verdict, the events it matched, and its
    failure evidence.

    ``matched`` — the event ids this node matched (for ``never``, the events
    that *would* have been matched — the failure UI's evidence; empty on pass).
    ``failing`` — this node's failure evidence: its own matches plus its
    failing children's (matched + failing); empty when the node passed. The
    metric's evidence is the root's ``matched`` on pass and the root's
    ``matched`` + ``failing`` on fail (§9A.3).
    ``value`` — the numeric result for the terminal ``count`` op (the count of
    matched events, which becomes ``raw_value`` for §7A.2 scoring); None for
    boolean ops.
    """

    result: bool
    matched: tuple[str, ...] = ()
    failing: tuple[str, ...] = ()
    value: int | None = None


def evaluate_rule(
    rule: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None = None,
) -> RuleResult:
    """Evaluate a §9A.3 trace rule over one attempt's events.

    ``events`` is a single attempt's agent-under-test trace. ``anchor`` is the
    anchor event when the rule is evaluated as (or inside) a ``for_all``
    assert; at the root it is None and the attempt start anchors ``within_ms``
    (§9A.3).

    Raises:
        TraceRuleError: the rule cannot be evaluated (anchored op at the root,
            ``where`` that does not compile in the closed predicate grammar,
            malformed node, nesting deeper than the validator's cap).
        TypeError: ``events`` contains a non-TraceEvent.
    """
    if not isinstance(rule, dict):
        raise TraceRuleError("a trace rule must be a JSON object")
    for event in events:
        if not isinstance(event, TraceEvent):
            raise TypeError(
                f"evaluate_rule expects TraceEvent objects, got {type(event).__name__}"
            )
    events_list = list(events)
    by_id = {e.event_id: e for e in events_list}
    return _eval_node(
        rule,
        events_list,
        anchor=anchor,
        by_id=by_id,
        pred_cache={},
        depth=0,
    )


# ---------------------------------------------------------------------------
# §9A.4 ordering contract
# ---------------------------------------------------------------------------


def _fallback_key(event: TraceEvent) -> tuple[Any, int]:
    return (event.timestamp, event.sequence)


def _is_ancestor(by_id: dict[str, TraceEvent], ancestor: TraceEvent, desc: TraceEvent) -> bool:
    """True iff ``ancestor`` is on desc's ``parent_event_id`` chain (§9A.4:
    causal precedence is exactly the ancestor chain)."""
    cur = by_id.get(desc.parent_event_id) if desc.parent_event_id is not None else None
    steps = 0
    while cur is not None:
        if cur.event_id == ancestor.event_id:
            return True
        if cur.parent_event_id is None:
            return False
        cur = by_id.get(cur.parent_event_id)
        steps += 1
        if steps > len(by_id):  # defensive: a malformed parent cycle must terminate
            return False
    return False


def _precedes(by_id: dict[str, TraceEvent], e1: TraceEvent, e2: TraceEvent) -> bool:
    """§9A.4: e1 precedes e2 iff e1 is an ancestor of e2 (causal), or neither
    is an ancestor and (timestamp, sequence) orders e1 earlier. Descendants of
    e2 never precede it — exists_before considers only non-descendant events.
    Total: every pair gets a defined answer."""
    if _is_ancestor(by_id, e1, e2):
        return True
    if _is_ancestor(by_id, e2, e1):
        return False
    return _fallback_key(e1) < _fallback_key(e2)


def _follows(by_id: dict[str, TraceEvent], e1: TraceEvent, e2: TraceEvent) -> bool:
    """Mirror of ``_precedes`` for exists_after: e1 follows e2 iff e1 is a
    descendant of e2 (causal), or neither is an ancestor and (timestamp,
    sequence) orders e1 later."""
    if _is_ancestor(by_id, e2, e1):
        return True
    if _is_ancestor(by_id, e1, e2):
        return False
    return _fallback_key(e2) < _fallback_key(e1)


# ---------------------------------------------------------------------------
# Match resolution
# ---------------------------------------------------------------------------


def _attempt_start(events: Sequence[TraceEvent]) -> Any | None:
    return min((e.timestamp for e in events), default=None)


def _within_window(event: TraceEvent, anchor_ts: Any, ms: int) -> bool:
    if anchor_ts is None:
        return False
    delta_ms = (event.timestamp - anchor_ts).total_seconds() * 1000.0
    return abs(delta_ms) <= ms


def _compile_where(text: str, cache: dict[str, Predicate]) -> Predicate:
    if text not in cache:
        try:
            cache[text] = Predicate.compile(text)
        except PredicateError as exc:
            raise TraceRuleError(f"match.where does not compile: {exc}") from exc
    return cache[text]


def _match(
    node_match: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    window_ms: int | None,
    pred_cache: dict[str, Predicate],
) -> list[TraceEvent]:
    """Events matching the §9A.3 ``match`` criteria (type, optional tool column,
    optional where predicate, optional source) — plus the optional ``within_ms``
    window around the anchor (attempt start at the root, §9A.3). Deterministic
    (timestamp, sequence) order."""
    if not isinstance(node_match, dict):
        raise TraceRuleError("match must be a JSON object")
    event_type = node_match.get("event")
    tool = node_match.get("tool")
    where = node_match.get("where")
    source = node_match.get("source")
    if source is not None and source not in tuple(s.value for s in Source):
        raise TraceRuleError(f"match.source must be proxy|adapter|runner, got {source!r}")
    out: list[TraceEvent] = []
    for event in events:
        if event.type.value != event_type:
            continue
        if source is not None:
            if event.source.value != source:
                continue
        elif event.source not in (Source.PROXY, Source.ADAPTER):
            # §9A.3 default: BOTH proxy+adapter — adapter events are
            # first-class evidence; runner events (worker-written pre-trace
            # errors) only match when a metric explicitly selects them.
            continue
        if tool is not None and event.tool != tool:
            continue
        if where is not None and not _compile_where(where, pred_cache).evaluate(event):
            continue
        out.append(event)
    if window_ms is not None:
        anchor_ts = anchor.timestamp if anchor is not None else _attempt_start(events)
        out = [e for e in out if _within_window(e, anchor_ts, window_ms)]
    out.sort(key=_fallback_key)
    return out


def _ids(events: Sequence[TraceEvent]) -> tuple[str, ...]:
    return tuple(e.event_id for e in events)


def _dedupe(ids: Sequence[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return tuple(out)


# ---------------------------------------------------------------------------
# Operator evaluation
# ---------------------------------------------------------------------------


def _eval_node(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    if depth > _MAX_DEPTH:
        raise TraceRuleError(f"trace-rule nesting exceeds {_MAX_DEPTH} levels")
    if not isinstance(node, dict):
        raise TraceRuleError("a trace-rule node must be a JSON object")
    op = node.get("op")
    if op not in TRACE_RULE_OPS:
        raise TraceRuleError(f"unknown trace-rule op {op!r}; closed set: {TRACE_RULE_OPS}")
    if op == "and":
        return _eval_and_or(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)
    if op == "or":
        return _eval_and_or(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)
    if op == "not":
        return _eval_not(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)
    if op == "implies":
        return _eval_implies(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)
    if op == "for_all":
        return _eval_for_all(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)
    if op == "within_ms":
        return _eval_within_ms(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)
    if op in ("exists_before", "exists_after", "immediately_precedes"):
        return _eval_anchored(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)
    return _eval_match_bearing(node, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth)


def _eval_and_or(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    op = node["op"]
    rules = node.get("rules")
    if not isinstance(rules, list) or not rules:
        raise TraceRuleError(f"op {op!r} requires a non-empty 'rules' list")
    results = [
        _eval_node(r, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth + 1)
        for r in rules
    ]
    result = all(r.result for r in results) if op == "and" else any(r.result for r in results)
    matched = _dedupe(i for r in results for i in r.matched)
    if result:
        failing: tuple[str, ...] = ()
    else:
        parts = [i for r in results if not r.result for i in (*r.matched, *r.failing)]
        failing = _dedupe(parts)
    return RuleResult(result=result, matched=matched, failing=failing)


def _eval_not(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    child = node.get("rule")
    if not isinstance(child, dict):
        raise TraceRuleError("op 'not' requires a 'rule' child")
    cr = _eval_node(child, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth + 1)
    failing = () if cr.result else _dedupe((*cr.matched, *cr.failing))
    return RuleResult(result=not cr.result, matched=cr.matched, failing=failing)


def _eval_implies(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    # §9A.3: `rule` is the antecedent, `rules` the consequent ("single child").
    antecedent = node.get("rule")
    consequent_list = node.get("rules")
    if not isinstance(antecedent, dict):
        raise TraceRuleError("op 'implies' requires a 'rule' antecedent")
    if not isinstance(consequent_list, list) or len(consequent_list) != 1:
        raise TraceRuleError(
            "op 'implies' requires 'rules' with exactly one consequent — "
            "'A implies B' needs both an antecedent (rule) and a consequent (rules) (§9A.3)"
        )
    ar = _eval_node(antecedent, events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth + 1)
    br = _eval_node(consequent_list[0], events, anchor=anchor, by_id=by_id, pred_cache=pred_cache, depth=depth + 1)
    result = (not ar.result) or br.result
    matched = _dedupe((*ar.matched, *br.matched))
    failing = () if result else _dedupe((*ar.matched, *ar.failing, *br.matched, *br.failing))
    return RuleResult(result=result, matched=matched, failing=failing)


def _eval_for_all(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    match = node.get("match")
    assert_rule = node.get("assert")
    if not isinstance(match, dict) or not isinstance(assert_rule, dict):
        raise TraceRuleError("op 'for_all' requires a 'match' and an 'assert' rule")
    window = node.get("within_ms")
    if window is not None and not isinstance(window, int):
        raise TraceRuleError("'within_ms' filter must be an int (milliseconds)")
    candidates = _match(match, events, anchor=anchor, window_ms=window, pred_cache=pred_cache)
    matched = _ids(candidates)
    failing_parts: list[str] = []
    ok = True
    for event in candidates:
        # The assert is anchored on each matched event (§9A.3/§9A.4).
        ar = _eval_node(assert_rule, events, anchor=event, by_id=by_id, pred_cache=pred_cache, depth=depth + 1)
        if not ar.result:
            ok = False
            failing_parts.extend((*ar.matched, *ar.failing))
    failing = () if ok else _dedupe(failing_parts)
    return RuleResult(result=ok, matched=matched, failing=failing)


def _eval_within_ms(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    # Terminal filter: true iff the child match found >= 1 event within ms of
    # the anchor context; at root the anchor is the attempt start (§9A.3).
    ms = node.get("ms")
    match = node.get("match")
    if not isinstance(ms, int) or not isinstance(match, dict):
        raise TraceRuleError("op 'within_ms' requires 'ms' (int milliseconds) and a 'match'")
    candidates = _match(match, events, anchor=anchor, window_ms=ms, pred_cache=pred_cache)
    matched = _ids(candidates)
    return RuleResult(result=bool(candidates), matched=matched)


def _eval_anchored(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    op = node["op"]
    if anchor is None:
        raise TraceRuleError(
            f"op {op!r} requires an anchor event — evaluate it as a for_all 'assert' "
            "or inside a compound that carries the anchor; at the rule root there is no "
            "anchor (§9A.3)"
        )
    match = node.get("match")
    if not isinstance(match, dict):
        raise TraceRuleError(f"op {op!r} requires a 'match' object")
    window = node.get("within_ms")
    if window is not None and not isinstance(window, int):
        raise TraceRuleError("'within_ms' filter must be an int (milliseconds)")
    candidates = _match(match, events, anchor=anchor, window_ms=window, pred_cache=pred_cache)
    if op == "exists_before":
        satisfying = [e for e in candidates if _precedes(by_id, e, anchor)]
    elif op == "exists_after":
        satisfying = [e for e in candidates if _follows(by_id, e, anchor)]
    else:  # immediately_precedes
        satisfying = _immediate_predecessors(candidates, events, anchor, by_id)
    matched = _ids(satisfying)
    # On failure the near-miss candidates are the evidence (the "eligibility
    # events examined" of §9A.5's canonical rule).
    failing = () if satisfying else _ids(candidates)
    return RuleResult(result=bool(satisfying), matched=matched, failing=failing)


def _immediate_predecessors(
    candidates: Sequence[TraceEvent],
    events: Sequence[TraceEvent],
    anchor: TraceEvent,
    by_id: dict[str, TraceEvent],
) -> list[TraceEvent]:
    """§9A.3/§9A.4: prefer the direct causal edge (anchor's parent_event_id ==
    matched event_id). When the anchor names a parent that is absent from the
    candidate set, [] is returned — a predecessor is never fabricated. Only
    when the anchor has no parent_event_id does the immediately preceding
    event by (timestamp, sequence) in the same attempt win."""
    if anchor.parent_event_id is not None:
        return [e for e in candidates if e.event_id == anchor.parent_event_id]
    best: TraceEvent | None = None
    for event in events:
        if event.event_id == anchor.event_id:
            continue
        if _is_ancestor(by_id, anchor, event):
            continue  # descendants of the anchor cannot precede it
        if _fallback_key(event) < _fallback_key(anchor):
            if best is None or _fallback_key(event) > _fallback_key(best):
                best = event
    if best is None:
        return []
    return [e for e in candidates if e.event_id == best.event_id]


def _eval_match_bearing(
    node: dict[str, Any],
    events: Sequence[TraceEvent],
    *,
    anchor: TraceEvent | None,
    by_id: dict[str, TraceEvent],
    pred_cache: dict[str, Predicate],
    depth: int,
) -> RuleResult:
    op = node["op"]  # exists | never | count
    match = node.get("match")
    if not isinstance(match, dict):
        raise TraceRuleError(f"op {op!r} requires a 'match' object")
    window = node.get("within_ms")
    if window is not None and not isinstance(window, int):
        raise TraceRuleError("'within_ms' filter must be an int (milliseconds)")
    candidates = _match(match, events, anchor=anchor, window_ms=window, pred_cache=pred_cache)
    matched = _ids(candidates)
    if op == "exists":
        result = bool(candidates)
        return RuleResult(result=result, matched=matched, failing=matched if not result else ())
    if op == "never":
        result = not candidates
        return RuleResult(result=result, matched=matched, failing=matched if not result else ())
    # count — terminal and numeric: the count becomes raw_value; per-case
    # pass/fail comes from scoring.condition (§9A.3/§7A.2).
    return RuleResult(result=True, matched=matched, value=len(candidates))
