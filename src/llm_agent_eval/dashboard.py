"""Declarative dashboard backend (UX §37B) — the definition model, the closed
binding language, and server-side resolution against pre-aggregated results.

The definition is validated JSON, never frontend code (invariants: no
arbitrary LLM-generated frontend; the renderer instantiates only registry
components). Bindings are values in a closed grammar — there is no expression
language and no ``eval()``. Resolution touches only pre-aggregated tables
(``run_metric_results``, ``run_case_metric_results``, ``runs``,
``run_cases``): a bind that would need a raw ``trace_events`` scan is invalid
by construction (§37B.2 rule 6 — the registry refuses such components).

Status and badge vocabulary is closed too: per-metric rows expose
``status`` (PASS/FAIL/ERROR/SKIPPED) and the badge set
``retry | non_authoritative | overridden | provisional`` — the frontend maps
these to its own visual treatment (e.g. provisional renders as
UNCALIBRATED); it invents nothing.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .evaluators import AggregationState, CaseScore, CaseStatus, aggregate_metric
from .storage import Storage

__all__ = [
    "DEFINITION_VERSION",
    "REGISTRY_VERSION",
    "DashboardDefinition",
    "FilterDef",
    "BlockDef",
    "LayoutDef",
    "DefinitionProblem",
    "DefinitionValidationError",
    "DefinitionResolutionError",
    "ComponentSpec",
    "InputSpec",
    "register_component",
    "registry_components",
    "get_registry_version",
    "validate_definition",
    "resolve_definition",
    "ResolvedDashboard",
    "ResolvedBlock",
    "DEFAULT_DEFINITION",
]

#: The dashboard definition schema version (§37B.1 — version 3, 12-column grid).
DEFINITION_VERSION = 3

#: The registry version (§37B.4). Saved dashboards pin it: a dashboard saved
#: against an older registry is quarantined (validation error) until migrated,
#: never rendered against a registry it did not validate against.
REGISTRY_VERSION = 1

#: The closed binding grammar (§37B.2): ``$filters.<id> | $selection.<id> |
#: $run.<field> | literal``. Literals may not start with "$" — the prefix is
#: reserved for the grammar.
_BINDING_RE = re.compile(r"^\$(filters|selection|run)\.([A-Za-z_][A-Za-z0-9_]*)$")

#: Closed input-type vocabulary for component inputs (§37B.2).
#: ``run_id | case_id | metric_id`` are subtypes of ``string`` (a $filters
#: reference of subtype type satisfies a ``string`` input).
INPUT_TYPES = (
    "run_id", "case_id", "metric_id", "string", "boolean", "number", "enum",
    "selection_source",
)

#: Filter declaration types and the binding type each resolves to.
FILTER_TYPES = {
    "run_selector": "run_id",
    "tag_filter": "string",
    "metric_filter": "metric_id",
    "case_filter": "case_id",
    "string": "string",
    "boolean": "boolean",
}

#: ``$run.<field>`` — the fields of the dashboard's bound run, and the binding
#: type each resolves to. Everything here comes from the pre-aggregated
#: ``runs`` row (never a trace scan).
RUN_FIELDS = {
    "id": "run_id",
    "status": "string",
    "tier": "string",
    "case_count": "number",
    "name": "string",
    "spec_version": "string",
    "dataset_version": "string",
    "created_at": "string",
    "run_started_at": "string",
    "run_completed_at": "string",
}

#: Run statuses the dashboard treats as terminal for rendering (the engine's
#: §11B.8 ``incomplete`` marker included: it renders the interruption banner,
#: never a finished dashboard).
_TERMINAL = ("complete", "failed", "cancelled", "incomplete")

#: §37B.2 stat vocabulary — verbatim from the section file.
STATS = ("pass_rate", "mean", "p95", "count", "cost")

_STRING_LIKE = frozenset(("string", "run_id", "case_id", "metric_id"))


# ---------------------------------------------------------------------------
# Definition model (§37B.1)
# ---------------------------------------------------------------------------


class LayoutDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["grid"] = "grid"
    columns: int = Field(default=12, ge=1)


class FilterDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    default: Any = None


class BlockDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: str
    span: int
    bind: dict[str, Any]


class DashboardDefinition(BaseModel):
    """§37B.1 — the dashboard definition (version 3, grid layout, 12 columns).

    ``registry_version`` pins the registry the definition was validated
    against (§37B.4); a definition that pins a different registry is
    quarantined by :func:`validate_definition` rather than rendered against
    components it did not validate against.
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    name: str
    layout: LayoutDef = Field(default_factory=LayoutDef)
    filters: list[FilterDef] = Field(default_factory=list)
    blocks: list[BlockDef]
    registry_version: int | None = None


# ---------------------------------------------------------------------------
# Validation (§37B.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DefinitionProblem:
    field: str
    code: str
    message: str


class DefinitionValidationError(ValueError):
    """Raised by :func:`validate_definition`; carries the full problem list.

    ``.problems`` is a list of :class:`DefinitionProblem` — the API maps each
    to the ``{"field", "message"}`` envelope so the frontend can place errors
    on the offending block/input.
    """

    def __init__(self, problems: Sequence[DefinitionProblem]):
        self.problems: list[DefinitionProblem] = list(problems)
        detail = "; ".join(f"{p.field}: {p.message}" for p in self.problems)
        super().__init__(
            f"dashboard definition invalid ({len(self.problems)} problem(s)): {detail}"
        )


class DefinitionResolutionError(ValueError):
    """A bind resolved to a value that cannot be served from pre-aggregated
    results, or a binding that points at something that does not exist in the
    store. Raised by :func:`resolve_definition`."""


def _coerce_definition(defn: DashboardDefinition | dict[str, Any] | str) -> DashboardDefinition:
    try:
        if isinstance(defn, DashboardDefinition):
            return defn
        if isinstance(defn, str):
            return DashboardDefinition.model_validate_json(defn)
        return DashboardDefinition.model_validate(defn)
    except ValidationError as exc:
        problems = [
            DefinitionProblem(
                field=".".join(str(p) for p in e["loc"]) or "definition",
                code="invalid_definition",
                message=e["msg"],
            )
            for e in exc.errors()
        ]
        raise DefinitionValidationError(problems) from exc


#: Inputs a component declares (the component's inputs schema, §37B.2).
@dataclass(frozen=True)
class InputSpec:
    name: str
    type: str  # one of INPUT_TYPES
    required: bool = False
    values: tuple[str, ...] | None = None  # enum only
    default: Any = None


@dataclass(frozen=True)
class ComponentSpec:
    """A registry component (the closed component vocabulary).

    ``resolve`` produces the block's payload from the resolved bindings and
    the :class:`ResolutionContext`. Components in the MVP registry are
    aggregates-only — ``trace_required=True`` is refused at registration
    (§37B.2 rule 6: dashboards resolve against pre-aggregated results only).
    """

    name: str
    version: int
    inputs: tuple[InputSpec, ...]
    resolve: Callable[[dict[str, Any], "ResolutionContext"], dict[str, Any]]
    #: The binding type of the selection this component publishes through its
    #: ``selection_source`` input (case_table publishes a case_id selection).
    selection_type: str | None = None
    #: Never True in the MVP registry; refused at registration.
    trace_required: bool = False


@dataclass(frozen=True)
class ResolutionContext:
    """Everything a component resolver may consult. It carries only
    pre-aggregated state — never raw trace events."""

    store: Storage
    workspace_id: str
    run: Any  # the dashboard's bound RunRecord, or None
    run_summary: dict[str, Any] | None
    metrics_complete: bool  # every metric row COMPLETE (§37B.3)
    render_final: bool  # terminal status AND every metric row COMPLETE
    filters: Mapping[str, Any]
    selection: Mapping[str, Any]


_REGISTRY: dict[str, ComponentSpec] = {}


def register_component(spec: ComponentSpec) -> None:
    """Register a component for the closed vocabulary. The registry is the
    defense for §37B.2 rule 6: a component that would require scanning raw
    trace events is refused outright (the renderer can never be asked for it).
    """
    if spec.trace_required:
        raise DefinitionValidationError(
            [
                DefinitionProblem(
                    "registry",
                    "raw_scan_violation",
                    f"component {spec.name!r} requires raw trace scans — the dashboard "
                    "registry is aggregates-only (§37B.2 rule 6)",
                )
            ]
        )
    if not spec.name or spec.name in _REGISTRY:
        raise ValueError(f"component {spec.name!r} is empty or already registered")
    for inp in spec.inputs:
        if inp.type not in INPUT_TYPES:
            raise ValueError(
                f"component {spec.name!r} input {inp.name!r} has unknown type {inp.type!r}"
            )
        if inp.type == "enum" and inp.values is None:
            raise ValueError(f"component {spec.name!r} input {inp.name!r}: enum needs values")
    _REGISTRY[spec.name] = spec


def registry_components() -> dict[str, ComponentSpec]:
    return dict(_REGISTRY)


def get_registry_version() -> int:
    return REGISTRY_VERSION


def _binding_type_of(
    value: Any,
    filters: Sequence[FilterDef],
    selection_types: Mapping[str, str],
) -> tuple[str | None, Any | None, tuple[str, str] | None]:
    """Resolve a bind value's binding type under the §37B.2 grammar.

    Returns ``(binding_type, literal, problem)`` — ``literal`` is the value
    itself for literals (needed for enum membership), None for references.
    ``problem`` is None, or a ``(code, message)`` pair so semantic failures
    (unknown references) are reported distinctly from grammar failures.
    """
    if isinstance(value, str):
        m = _BINDING_RE.match(value)
        if m is None:
            if value.startswith("$"):
                return None, None, (
                    "invalid_binding",
                    f"invalid binding {value!r} — the grammar is $filters.<id> | "
                    "$selection.<id> | $run.<field> | literal (§37B.2)",
                )
            return "string", value, None
        kind, name = m.group(1), m.group(2)
        if kind == "filters":
            f = next((f for f in filters if f.id == name), None)
            if f is None:
                return None, None, (
                    "unknown_filter_ref",
                    f"unknown filter reference {value!r} (§37B.2 rule 2)",
                )
            return FILTER_TYPES.get(f.type, "string"), None, None
        if kind == "selection":
            if name not in selection_types:
                return None, None, (
                    "unknown_selection_ref",
                    f"unknown selection reference {value!r} — no block publishes a "
                    f"selection named {name!r} (§37B.2 rule 2)",
                )
            return selection_types[name], None, None
        if name not in RUN_FIELDS:
            return None, None, (
                "unknown_run_field",
                f"unknown $run field {name!r} (fields: {sorted(RUN_FIELDS)}; §37B.2 rule 2)",
            )
        return RUN_FIELDS[name], None, None
    if isinstance(value, bool):
        return "boolean", value, None
    if isinstance(value, (int, float)):
        return "number", value, None
    if value is None:
        return "null", None, None
    return None, None, (
        "invalid_binding",
        f"binding value must be a string, number, boolean, or null, "
        f"got {type(value).__name__}",
    )


def _check_input(inp: InputSpec, btype: str, literal: Any | None) -> tuple[bool, str]:
    """Type-check one binding against one input. Returns (ok, reason).

    Literals are concrete values, so a literal string satisfies any string
    input (run_id / case_id / metric_id / string); only *references* are
    constrained to their resolved binding type (a tag_filter reference, which
    resolves to a generic string, cannot satisfy a run_id input — the §37B.2
    rule-3 failure the section files call out)."""
    t = inp.type
    if t == "enum":
        if btype != "string" or literal not in (inp.values or ()):
            return False, f"must be one of {list(inp.values or ())}"
        return True, ""
    if t == "selection_source":
        # handled by the caller (literal selection ids), never via $refs
        return False, "selection inputs bind a literal selection id"
    if t == "boolean":
        return btype == "boolean", "must be a boolean"
    if t == "number":
        return btype == "number", "must be a number"
    if t in ("run_id", "case_id", "metric_id"):
        if btype == t:
            return True, ""
        if btype == "string" and literal is not None:
            return True, ""  # a literal id value
        return False, f"must be a {t}"
    if t == "string":
        return btype in _STRING_LIKE, "must be a string"
    return btype == t, f"must be a {t}"


def validate_definition(defn: DashboardDefinition | dict[str, Any] | str) -> None:
    """§37B.2 — validate a definition: version, layout grid, closed component
    vocabulary, closed binding grammar, and type-check every binding against
    the component's inputs schema.

    Raises :class:`DefinitionValidationError` with the complete problem list;
    returns None when valid. The registry is closed for the MVP — an unknown
    component or a raw-scan-requiring component is a validation error.
    """
    definition = _coerce_definition(defn)
    problems: list[DefinitionProblem] = []

    def add(field: str, code: str, message: str) -> None:
        problems.append(DefinitionProblem(field, code, message))

    # Rule: version + layout (§37B.1: version 3, grid, 12 columns).
    if definition.version != DEFINITION_VERSION:
        add(
            "version", "unsupported_version",
            f"dashboard definition version must be {DEFINITION_VERSION}, got {definition.version}",
        )
    if definition.layout.type != "grid":
        add(
            "layout.type", "grid_violation",
            f"layout.type must be 'grid', got {definition.layout.type!r}",
        )
    if definition.layout.columns != 12:
        add(
            "layout.columns", "grid_violation",
            "the layout grid has 12 columns (§37B.1); got "
            + str(definition.layout.columns),
        )
    # §37B.4: a saved dashboard pins the registry it validated against.
    if (
        definition.registry_version is not None
        and definition.registry_version != REGISTRY_VERSION
    ):
        add(
            "registry_version", "registry_mismatch",
            f"this dashboard pins registry v{definition.registry_version}; the current "
            f"registry is v{REGISTRY_VERSION} — quarantined until migrated (§37B.4)",
        )

    # Filters: unique ids, known types.
    filter_ids: dict[str, FilterDef] = {}
    for i, f in enumerate(definition.filters):
        field = f"filters[{i}]"
        if f.id in filter_ids:
            add(f"{field}.id", "duplicate_filter_id", f"duplicate filter id {f.id!r}")
            continue
        if f.type not in FILTER_TYPES:
            add(
                f"{field}.type", "unknown_filter_type",
                f"unknown filter type {f.type!r}; must be one of {sorted(FILTER_TYPES)}",
            )
        filter_ids[f.id] = f

    # Blocks: known components (rule 1), no raw-scan components (rule 6),
    # spans within the grid (rule 5), declared inputs only, required inputs
    # bound (rule 4). Selection ids published by selection_source inputs.
    selection_types: dict[str, str] = {}
    for i, block in enumerate(definition.blocks):
        comp = _REGISTRY.get(block.component)
        bfield = f"blocks[{i}]"
        if comp is None:
            add(
                f"{bfield}.component", "unknown_component",
                f"unknown component {block.component!r} — the registry is closed "
                f"for the MVP ({sorted(_REGISTRY)}; §37B.2 rule 1)",
            )
            continue
        if comp.trace_required:
            add(
                f"{bfield}.component", "raw_scan_violation",
                f"component {block.component!r} requires raw trace scans — dashboards "
                "resolve against pre-aggregated results only (§37B.2 rule 6)",
            )
        if not 1 <= block.span <= definition.layout.columns:
            add(
                f"{bfield}.span", "grid_violation",
                f"block span must be within 1..{definition.layout.columns}, "
                f"got {block.span} (§37B.2 rule 5)",
            )
        inputs = {inp.name: inp for inp in comp.inputs}
        for name in block.bind:
            if name not in inputs:
                add(
                    f"{bfield}.bind.{name}", "unknown_input",
                    f"component {block.component!r} has no input {name!r} "
                    f"(inputs: {sorted(inputs)})",
                )
        for inp in comp.inputs:
            if inp.required and inp.name not in block.bind:
                add(
                    f"{bfield}.bind", "missing_required_input",
                    f"component {block.component!r} requires input {inp.name!r} (§37B.2 rule 4)",
                )
        for inp in comp.inputs:
            if inp.type != "selection_source":
                continue
            raw = block.bind.get(inp.name)
            if not isinstance(raw, str) or not raw or raw.startswith("$"):
                add(
                    f"{bfield}.bind.{inp.name}", "type_mismatch",
                    f"input {inp.name!r} of {block.component!r} must be a literal "
                    "selection id (a string naming the selection this block publishes)",
                )
                continue
            if raw in selection_types and selection_types[raw] != (comp.selection_type or "any"):
                add(
                    f"{bfield}.bind.{inp.name}", "conflicting_selection",
                    f"selection {raw!r} is published with conflicting types",
                )
            selection_types[raw] = comp.selection_type or "any"

    # Type-check every binding (rule 3) — needs the selection types collected
    # above, so it runs after the block pass.
    for i, block in enumerate(definition.blocks):
        comp = _REGISTRY.get(block.component)
        if comp is None:
            continue
        bfield = f"blocks[{i}]"
        inputs = {inp.name: inp for inp in comp.inputs}
        for name, value in block.bind.items():
            inp = inputs.get(name)
            if inp is None or inp.type == "selection_source":
                continue
            if value is None:
                # A null literal provides nothing; required inputs are already
                # reported above, optional ones resolve to the input default.
                continue
            btype, literal, problem = _binding_type_of(
                value, definition.filters, selection_types
            )
            if problem is not None:
                code, message = problem
                add(f"{bfield}.bind.{name}", code, message)
                continue
            ok, reason = _check_input(inp, btype, literal)
            if not ok:
                add(
                    f"{bfield}.bind.{name}", "type_mismatch",
                    f"binding {value!r} (type {btype}) does not satisfy input "
                    f"{inp.name!r} (type {inp.type}{' with values ' + str(list(inp.values)) if inp.type == 'enum' else ''}): {reason}",
                )

    if problems:
        raise DefinitionValidationError(problems)


# ---------------------------------------------------------------------------
# Resolution (§37B.3 — server-side, pre-aggregated results only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedBlock:
    component: str
    span: int
    row: int
    col: int
    bind: dict[str, Any]
    payload: dict[str, Any]


@dataclass(frozen=True)
class ResolvedDashboard:
    name: str
    version: int
    layout: dict[str, Any]
    filters: list[dict[str, Any]]
    blocks: list[ResolvedBlock]
    registry_version: int
    run: dict[str, Any] | None  # the bound run's context (banner data)
    metrics_complete: bool
    render_final: bool


def _run_ctx(run: Any) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "status": run.status,
        "terminal": run.status in _TERMINAL,
        "tier": run.tier,
        "case_count": run.case_count,
        "name": run.spec.name,
        "spec_version": run.spec.spec_version,
        "dataset_version": run.spec.dataset_version,
        "created_at": run.created_at,
        "run_started_at": run.run_started_at,
        "run_completed_at": run.run_completed_at,
    }


def _run_field(run: Any, name: str) -> Any:
    if name == "id":
        return run.run_id
    if name == "status":
        return run.status
    if name == "tier":
        return run.tier
    if name == "case_count":
        return run.case_count
    if name == "name":
        return run.spec.name
    if name == "spec_version":
        return run.spec.spec_version
    if name == "dataset_version":
        return run.spec.dataset_version
    if name == "created_at":
        return run.created_at
    if name == "run_started_at":
        return run.run_started_at
    if name == "run_completed_at":
        return run.run_completed_at
    raise DefinitionResolutionError(f"unknown $run field {name!r}")


def _resolve_ref(value: str, ctx: ResolutionContext) -> Any:
    m = _BINDING_RE.match(value)
    if m is None:  # pragma: no cover - validated before resolution
        raise DefinitionResolutionError(f"invalid binding {value!r}")
    kind, name = m.group(1), m.group(2)
    if kind == "filters":
        return ctx.filters.get(name)
    if kind == "selection":
        return ctx.selection.get(name)
    if ctx.run is None:
        return None
    return _run_field(ctx.run, name)


def _layout_blocks(blocks: Sequence[BlockDef], columns: int) -> list[tuple[int, int]]:
    """Flow layout for the §37B.1 grid: blocks wrap to the next row when the
    span would overflow the 12 columns. A span within 1..12 therefore can
    never overlap another block — overlap is structurally impossible in the
    flow layout (the validator's span check is the guard)."""
    positions: list[tuple[int, int]] = []
    row, col = 0, 0
    for block in blocks:
        if col + block.span > columns:
            row += 1
            col = 0
        positions.append((row, col))
        col += block.span
    return positions


def _bound_run(ctx: ResolutionContext, bind: Mapping[str, Any]) -> Any:
    """The block's bound run: its own ``run`` binding when present, else the
    dashboard's bound run. Resolved ids are looked up in the store — a run id
    that does not exist yields a placeholder payload, never a crash."""
    run_id = bind.get("run")
    if run_id:
        return ctx.store.get_run(run_id, ctx.workspace_id)
    return ctx.run


def _placeholder(component: str, reason: str) -> dict[str, Any]:
    return {"component": component, "available": False, "reason": reason}


def _score_from_record(rec: Any, provisional: bool) -> CaseScore:
    return CaseScore(
        metric_id=rec.metric_id,
        case_id=rec.case_id,
        score=rec.score,
        passed=rec.status == CaseStatus.PASS.value,
        evidence_event_ids=rec.evidence_event_ids,
        status=CaseStatus(rec.status),
        message="",
        attempt=0,
        on_retry_override=rec.on_retry_override,
        provisional=provisional,
    )


def _badges(rec: Any, provisional: bool) -> list[str]:
    """The closed badge vocabulary for a metric row. The frontend maps
    ``provisional`` to its UNCALIBRATED treatment; nothing beyond this set is
    ever emitted (closed set — never free text)."""
    badges = [rec.status]
    if rec.on_retry_override:
        badges.append("retry")
    elif not rec.is_authoritative:
        badges.append("non_authoritative")
    if rec.overridden:
        badges.append("overridden")
    if provisional:
        badges.append("provisional")
    return badges


def _resolve_metric_summary(bind: dict[str, Any], ctx: ResolutionContext) -> dict[str, Any]:
    run = _bound_run(ctx, bind)
    base = {
        "component": "metric_summary",
        "run": _run_ctx(run) if run else None,
        "metric_id": bind.get("metric"),
        "stat": bind.get("stat"),
    }
    if run is None:
        return _placeholder(
            "metric_summary",
            "no run bound — the run filter resolved to nothing (no runs in this workspace yet)",
        )
    metric = next((m for m in run.spec.metrics if m.metric_id == bind["metric"]), None)
    if metric is None:
        return {
            **base,
            "available": False,
            "reason": f"metric {bind['metric']!r} is not in the run's frozen spec",
            "value": None,
            "aggregation_state": None,
            "provisional": False,
        }
    stored = next(
        (r for r in ctx.store.get_run_metric_results(run.run_id, ctx.workspace_id)
         if r.metric_id == metric.metric_id),
        None,
    )
    records = ctx.store.get_case_scores(
        run_id=run.run_id, workspace_id=ctx.workspace_id, metric_id=metric.metric_id
    )
    scores = [_score_from_record(r, metric.provisional) for r in records]
    agg = aggregate_metric(
        run.spec, metric, scores, expected_n=run.case_count * run.repeats
    )
    stat = bind["stat"]
    available, value, reason, cost_breakdown = True, None, None, []
    if stat == "pass_rate":
        value = agg.pass_rate
    elif stat == "mean":
        value = agg.mean
    elif stat == "count":
        value = agg.n_cases
    elif stat == "p95":
        if stored is not None and stored.method == "p95":
            value = stored.value
        else:
            available = False
            reason = (
                f"aggregation method is {stored.method if stored else 'unknown'}, "
                "not p95 — the p95 stat resolves only when the metric aggregates p95"
            )
    else:  # cost
        summaries = ctx.store.get_cost_summaries(run.run_id, ctx.workspace_id)
        if summaries:
            available = True
            value = round(
                sum(s["usd_micros"] for s in summaries) / 1_000_000.0, 6
            )
            cost_breakdown = summaries
        else:
            available = False
            reason = (
                "no cost summaries yet — cost is computed when the run completes "
                "(aggregates-only surface; cost never scans trace_events)"
            )
    return {
        **base,
        "value": value,
        "available": available,
        "reason": reason,
        "cost_breakdown": cost_breakdown,
        "aggregation_state": (
            stored.aggregation_state if stored else AggregationState.IN_PROGRESS.value
        ),
        "method": stored.method if stored else metric.aggregation.method,
        "sample_n": agg.n_cases,
        "error_n": agg.error_n,
        "skipped_n": agg.skipped_n,
        "gate_status": stored.gate_status if stored else None,
        "no_ci": stored.no_ci if stored else False,
        "provisional": metric.provisional,
        "metrics_complete": ctx.metrics_complete,
        "render_final": ctx.render_final,
    }


def _resolve_run_table(bind: dict[str, Any], ctx: ResolutionContext) -> dict[str, Any]:
    run = _bound_run(ctx, bind)
    if run is None:
        return _placeholder(
            "run_table",
            "no run bound — the run filter resolved to nothing (no runs in this workspace yet)",
        )
    rows = ctx.store.get_run_metric_results(run.run_id, ctx.workspace_id)
    metrics = []
    for m in run.spec.metrics:
        stored = next((r for r in rows if r.metric_id == m.metric_id), None)
        metrics.append(
            {
                "metric_id": m.metric_id,
                "name": m.name,
                "type": m.type,
                "value": stored.value if stored else None,
                "method": m.aggregation.method,
                "aggregation_state": (
                    stored.aggregation_state if stored else AggregationState.IN_PROGRESS.value
                ),
                "gate_status": stored.gate_status if stored else None,
                "sample_n": stored.sample_n if stored else 0,
                "error_n": stored.error_n if stored else 0,
                "skipped_n": stored.skipped_n if stored else 0,
                "no_ci": stored.no_ci if stored else False,
                "provisional": m.provisional,
            }
        )
    cases = ctx.store.list_cases(run.run_id, ctx.workspace_id)
    counts = Counter(c.status for c in cases)
    return {
        "component": "run_table",
        "run": _run_ctx(run),
        "metrics": metrics,
        "cases": {
            "total": len(cases),
            **{s: counts[s] for s in ("queued", "running", "completed", "failed", "cancelled", "skipped")},
        },
        "metrics_complete": ctx.metrics_complete,
        "render_final": ctx.render_final,
    }


def _spec_case(spec: Any, case_id: str) -> Any | None:
    for c in spec.cases:
        if c.case_id == case_id:
            return c
    return None


def _resolve_case_table(bind: dict[str, Any], ctx: ResolutionContext) -> dict[str, Any]:
    run = _bound_run(ctx, bind)
    if run is None:
        return _placeholder(
            "case_table",
            "no run bound — the run filter resolved to nothing (no runs in this workspace yet)",
        )
    metric_filter = bind.get("metric")
    tag = bind.get("tag")
    single_case = bind.get("case")
    selection_source = bind.get("selection")

    # Latest score_revision per (case, metric) — the current machine view.
    # Retry rows replace first-attempt rows in storage, so the badges carry
    # on_retry_override / is_authoritative flags to surface both sides (§37B.1).
    records = ctx.store.get_case_scores(run_id=run.run_id, workspace_id=ctx.workspace_id)
    latest: dict[tuple[str, str], Any] = {}
    for rec in records:
        if metric_filter and rec.metric_id != metric_filter:
            continue
        key = (rec.case_id, rec.metric_id)
        if key not in latest or rec.score_revision > latest[key].score_revision:
            latest[key] = rec

    out_cases = []
    for c in ctx.store.list_cases(run.run_id, ctx.workspace_id):
        tc = _spec_case(run.spec, c.case_id)
        tags = tc.metadata.get("tags") if tc is not None else None
        if isinstance(tags, str):
            tags = [tags]
        if tag is not None:
            if not (isinstance(tags, list) and tag in tags):
                continue  # tag filter: exclude cases without the tag
        if single_case and c.case_id != single_case:
            continue
        rows = []
        for m in run.spec.metrics:
            rec = latest.get((c.case_id, m.metric_id))
            if rec is None:
                continue
            rows.append(
                {
                    "metric_id": rec.metric_id,
                    "status": rec.status,
                    "score": rec.score,
                    "score_revision": rec.score_revision,
                    "is_authoritative": rec.is_authoritative,
                    "on_retry_override": rec.on_retry_override,
                    "overridden": rec.overridden,
                    "judge_binding": rec.judge_binding,
                    "provisional": m.provisional,
                    "badges": _badges(rec, m.provisional),
                }
            )
        out_cases.append(
            {
                "case_id": c.case_id,
                "name": tc.name if tc is not None else c.case_id,
                "status": c.status,
                "classification": c.classification,
                "error_category": c.error_category,
                "tags": tags if isinstance(tags, list) else [],
                "metrics": rows,
                "selection": (
                    {"source": selection_source, "value": c.case_id}
                    if selection_source
                    else None
                ),
            }
        )
    return {
        "component": "case_table",
        "run": _run_ctx(run),
        "metric": metric_filter,
        "tag": tag,
        "selection_source": selection_source,
        "cases": out_cases,
        "metrics_complete": ctx.metrics_complete,
        "render_final": ctx.render_final,
    }


def _resolve_trace_evidence(bind: dict[str, Any], ctx: ResolutionContext) -> dict[str, Any]:
    run = _bound_run(ctx, bind)
    base = {"component": "trace_evidence", "run": _run_ctx(run) if run else None}
    if run is None:
        return _placeholder(
            "trace_evidence",
            "no run bound — the run filter resolved to nothing (no runs in this workspace yet)",
        )
    case_id = bind.get("case")
    if not case_id:
        return {
            **base,
            "case_id": None,
            "placeholder": True,
            "message": "no case selected — bind a $selection.case reference or a literal case id",
            "metrics": [],
            "trace_url": None,
        }
    # Evidence ids come from the stored metric rows only — the resolver never
    # scans trace_events (the frontend fetches /runs/{id}/traces/{case} for
    # the full stream; §37B.2 rule 6).
    latest: dict[str, Any] = {}
    for rec in ctx.store.get_case_scores(
        run_id=run.run_id, workspace_id=ctx.workspace_id, case_id=case_id
    ):
        if rec.metric_id not in latest or rec.score_revision > latest[rec.metric_id].score_revision:
            latest[rec.metric_id] = rec
    metrics = []
    for m in run.spec.metrics:
        rec = latest.get(m.metric_id)
        if rec is None:
            continue
        metrics.append(
            {
                "metric_id": m.metric_id,
                "status": rec.status,
                "score": rec.score,
                "evidence_event_ids": list(rec.evidence_event_ids),
                "evidence_count": len(rec.evidence_event_ids),
                "score_revision": rec.score_revision,
                "is_authoritative": rec.is_authoritative,
                "on_retry_override": rec.on_retry_override,
                "overridden": rec.overridden,
                "judge_binding": rec.judge_binding,
                "provisional": m.provisional,
            }
        )
    return {
        **base,
        "case_id": case_id,
        "trace_url": f"/runs/{run.run_id}/traces/{case_id}",
        "metrics": metrics,
        "placeholder": False,
        "metrics_complete": ctx.metrics_complete,
        "render_final": ctx.render_final,
    }


def _register_builtins() -> None:
    _REGISTRY.clear()
    _REGISTRY["metric_summary"] = ComponentSpec(
        name="metric_summary",
        version=1,
        inputs=(
            InputSpec("run", "run_id", required=True),
            InputSpec("metric", "metric_id", required=True),
            InputSpec("stat", "enum", required=True, values=STATS),
        ),
        resolve=_resolve_metric_summary,
    )
    _REGISTRY["run_table"] = ComponentSpec(
        name="run_table",
        version=1,
        inputs=(InputSpec("run", "run_id", required=True),),
        resolve=_resolve_run_table,
    )
    _REGISTRY["case_table"] = ComponentSpec(
        name="case_table",
        version=1,
        inputs=(
            InputSpec("run", "run_id", required=True),
            InputSpec("metric", "metric_id", required=False),
            InputSpec("tag", "string", required=False),
            InputSpec("case", "case_id", required=False),
            InputSpec("selection", "selection_source", required=False),
        ),
        selection_type="case_id",
        resolve=_resolve_case_table,
    )
    _REGISTRY["trace_evidence"] = ComponentSpec(
        name="trace_evidence",
        version=1,
        inputs=(
            InputSpec("run", "run_id", required=True),
            InputSpec("case", "case_id", required=True),
        ),
        resolve=_resolve_trace_evidence,
    )


_register_builtins()


def resolve_definition(
    defn: DashboardDefinition | dict[str, Any] | str,
    filters: Mapping[str, Any] | None,
    selection: Mapping[str, Any] | None,
    store: Storage,
    *,
    workspace_id: str = "default",
) -> ResolvedDashboard:
    """§37B.3 — resolve a validated definition against the store, server-side.

    Filter values are request parameters; declared defaults fill in absent
    values; a ``run_selector`` filter's ``"latest"`` default resolves to the
    most recent run. Every block's bindings are resolved under the §37B.2
    grammar and the component's resolver produces its payload — all from
    pre-aggregated results, never a trace scan. An invalid definition is
    rejected (it is never resolved).
    """
    definition = _coerce_definition(defn)
    validate_definition(definition)
    filters = dict(filters or {})
    selection = dict(selection or {})

    resolved_filters: dict[str, Any] = {}
    for f in definition.filters:
        value = filters.get(f.id)
        if value is None:
            value = f.default
        if f.type == "run_selector" and value == "latest":
            latest = store.list_runs(workspace_id, limit=1)
            value = latest[0].run_id if latest else None
        resolved_filters[f.id] = value

    run_id = None
    for f in definition.filters:
        if f.type == "run_selector":
            run_id = resolved_filters.get(f.id)
            break
    bound_run = store.get_run(run_id, workspace_id) if run_id else None
    metric_rows = (
        store.get_run_metric_results(run_id, workspace_id) if bound_run else []
    )
    metrics_complete = bool(metric_rows) and all(
        r.aggregation_state == AggregationState.COMPLETE.value for r in metric_rows
    )
    render_final = bound_run is not None and bound_run.status in _TERMINAL and metrics_complete
    ctx = ResolutionContext(
        store=store,
        workspace_id=workspace_id,
        run=bound_run,
        run_summary=_run_ctx(bound_run) if bound_run else None,
        metrics_complete=metrics_complete,
        render_final=render_final,
        filters=resolved_filters,
        selection=selection,
    )

    positions = _layout_blocks(definition.blocks, definition.layout.columns)
    blocks: list[ResolvedBlock] = []
    for block, (row, col) in zip(definition.blocks, positions):
        bind: dict[str, Any] = {}
        for name, value in block.bind.items():
            if isinstance(value, str) and value.startswith("$"):
                bind[name] = _resolve_ref(value, ctx)
            else:
                bind[name] = value
        comp = _REGISTRY[block.component]
        payload = comp.resolve(bind, ctx)
        blocks.append(ResolvedBlock(comp.name, block.span, row, col, bind, payload))

    return ResolvedDashboard(
        name=definition.name,
        version=definition.version,
        layout=definition.layout.model_dump(),
        filters=[
            {"id": f.id, "type": f.type, "default": f.default}
            for f in definition.filters
        ],
        blocks=blocks,
        registry_version=REGISTRY_VERSION,
        run=ctx.run_summary,
        metrics_complete=metrics_complete,
        render_final=render_final,
    )


#: The out-of-the-box dashboard used by ``eval-engine dashboards`` and the
#: dashboard UI's first project: a run overview table, a case table with a
#: case selection, and a trace-evidence panel bound to the selection.
DEFAULT_DEFINITION: dict[str, Any] = {
    "version": 3,
    "name": "Default run overview",
    "layout": {"type": "grid", "columns": 12},
    "filters": [
        {"id": "run", "type": "run_selector", "default": "latest"},
        {"id": "case_tag", "type": "tag_filter", "default": None},
    ],
    "blocks": [
        {"component": "run_table", "span": 12, "bind": {"run": "$filters.run"}},
        {
            "component": "case_table",
            "span": 12,
            "bind": {"run": "$filters.run", "tag": "$filters.case_tag", "selection": "case"},
        },
        {
            "component": "trace_evidence",
            "span": 12,
            "bind": {"run": "$filters.run", "case": "$selection.case"},
        },
    ],
}
