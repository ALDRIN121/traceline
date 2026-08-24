"""The execution spine: smoke gate, run lifecycle, invocation, ingestion,
scoring, and aggregation (engine §11A/§11B, semantics §13A, harness §32A).

The engine is the single-threaded, deterministic driver. Its invariants:

- **Explicit states** — the engine never reports success it did not observe.
  A completed attempt with no trace is scored by the metric's ``on_missing``
  (§9A.5 — a fully empty trace is exactly what on_missing governs); a
  ``no_trace`` **smoke** outcome is the ``no_trace`` failure state (§32A.4).
  No fabricated traces anywhere.
- **Fresh process per case** — every attempt is one subprocess (runner.py);
  state never leaks between cases.
- **Repeats and retries are different** (§11C.3). Each repeat is an
  independent sample; attempt 0 is authoritative. A case that passes only on
  a retry is FAILED on the first attempt and flagged ``on_retry_override`` —
  it can never re-enter the pass rate. Retries run only after a first-attempt
  FAIL (never after a timeout or an evaluator ERROR — repeating an infra
  failure masks it).
- **First-attempt authority** — ``evaluate_case`` scores only attempt-0
  events; retries are scored with ``score_attempt`` (flagged, stored, never
  aggregated).
- **No network** — the engine itself never calls out; the agent under test
  is the only subprocess.
- **``on_missing`` / ``on_error`` are consumed from the spec**, never
  re-implemented here (the spec model enforces their presence).
- **Judge metrics require an explicit ``JudgeEvaluator``** (via
  ``judge_factory``; the shipped default is the deterministic NoopJudge —
  usable-but-provisional, §15A.1). Judge bindings are verified against the
  metric's declared binding (§15A.5).
- **Redaction is verified at ingestion**: every ingested event must carry an
  explicit ``redaction_state`` (capture contract §12A.6) and must claim the
  run/case/attempt it is being ingested under — cross-case contamination is
  refused. Cost-bearing events are stamped with the config's
  ``price_version`` at ingestion.
- **Per-attempt trace cap**: ≤ 1,000 events (§18 Q8); overflow is marked
  ``trace_truncated`` and dropped, never partially trusted.
"""

from __future__ import annotations

import logging
import shlex
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings
from .events import CostBlock, TraceEvent
from .evaluators import (
    CaseScore,
    CaseStatus,
    GateResult,
    aggregate_metric,
    evaluate_case,
    evaluate_gate,
    gate_safe,
    score_attempt,
)
from .judge import JudgeBinding, JudgeEvaluator, NoopJudge
from .lifecycle import (
    AttemptStatus,
    RunCaseStatus,
    RunStatus,
    SmokeState,
)
from .runner import (
    INVOCATION_FAILED,
    NO_TRACE,
    PROVIDER_UNREACHABLE,
    TIMED_OUT,
    invoke_agent,
)
from .spec import RUN_TIERS, EvaluationSpec, TestCase, validate_spec
from .storage import (
    ProjectRecord,
    RunMetricResultRecord,
    RunRecord,
    SmokeAttemptRecord,
    Storage,
)

__all__ = [
    "Engine",
    "EngineError",
    "ProjectNotEvaluableError",
    "TierViolationError",
    "RunAlreadyTerminalError",
    "AttemptResult",
    "CaseRunResult",
    "RunResult",
    "MAX_EVENTS_PER_ATTEMPT",
]

logger = logging.getLogger(__name__)

#: Per-attempt trace cap (§18 Q8: "≤ 1,000 rows (cap)").
MAX_EVENTS_PER_ATTEMPT = 1000

#: The case classification values (§11C.3; §18 run_cases.classification).
PASSING = "PASSING"
FLAKY = "FLAKY"
FAILING = "FAILING"
ERRORED = "ERRORED"


class EngineError(Exception):
    """An engine-level contract violation (not an agent outcome)."""


class ProjectNotEvaluableError(EngineError):
    """create_run against a project that has not passed the smoke gate
    (§32A.2 C1: smoke gates RUNNING only)."""


class TierViolationError(EngineError):
    """The spec's case count is outside the tier's cap (§11B.9)."""


class RunAlreadyTerminalError(EngineError):
    """run() on a run that is already complete/failed/cancelled (§13A.3)."""


@dataclass(frozen=True)
class AttemptResult:
    """One attempt's outcome. ``status`` is an ``AttemptStatus`` value;
    ``scores`` are the attempt's per-metric CaseScores (empty when the attempt
    did not complete — metric results exist only for completed attempts,
    §13A.2)."""

    attempt_id: str
    repeat_index: int
    attempt: int
    status: str
    exit_code: int | None
    event_count: int
    trace_truncated: bool
    error: str | None
    scores: tuple[CaseScore, ...]
    result_payload: dict[str, Any] | None


@dataclass(frozen=True)
class CaseRunResult:
    """One case's full outcome: every repeat sample plus any retries."""

    run_id: str
    case_id: str
    status: str  # RunCaseStatus value
    classification: str | None  # PASSING | FLAKY | FAILING | ERRORED (§11C.3)
    error_category: str | None  # infra | agent | evaluator
    repeat_outcomes: tuple[AttemptResult, ...]
    retry_outcomes: tuple[AttemptResult, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunResult:
    """The run-level outcome. ``status`` is the final ``runs.status``
    (``complete`` — the gate result is carried in ``gate_results``, never
    smuggled into the lifecycle status)."""

    run_id: str
    status: str
    case_results: tuple[CaseRunResult, ...]
    metric_results: tuple[RunMetricResultRecord, ...]
    gate_results: tuple[GateResult, ...]
    warnings: tuple[str, ...]


class Engine:
    """Thin orchestration over Storage + runner + evaluators. All state lives
    in SQLite; the engine holds no per-run state between calls."""

    def __init__(
        self,
        storage: Storage,
        *,
        judge_factory: Callable[[JudgeBinding], JudgeEvaluator] | None = None,
        work_root: str | Path | None = None,
        price_version: str | None = None,
        tiers: Any = None,
    ):
        self.storage = storage
        # The shipped judge: deterministic, no model, no network — every score
        # it produces is provisional (§15A.1, owner decision 4J).
        self.judge_factory = judge_factory if judge_factory is not None else (
            lambda binding: NoopJudge(binding)
        )
        root = Path(work_root) if work_root is not None else (
            Path(tempfile.gettempdir()) / "llm_agent_eval"
        )
        root.mkdir(parents=True, exist_ok=True)
        self.work_root = root
        self.price_version = price_version if price_version is not None else settings.price_version
        self.tiers = tiers if tiers is not None else settings.run_tiers

    # ------------------------------------------------------------------
    # Projects and the smoke gate (harness §32A)
    # ------------------------------------------------------------------

    def create_project(
        self, workspace_id: str, name: str, *, entrypoint: Sequence[str] = ()
    ) -> ProjectRecord:
        return self.storage.create_project(
            workspace_id=workspace_id, name=name, entrypoint=entrypoint
        )

    def get_project(self, project_id: str, workspace_id: str) -> ProjectRecord | None:
        return self.storage.get_project(project_id, workspace_id)

    def advance_project(self, project_id: str, workspace_id: str, to: SmokeState | str) -> ProjectRecord:
        """Explicit smoke-state advance (e.g. SMOKE_PASSED -> EVALUABLE). Illegal
        jumps raise IllegalTransition and are never persisted."""
        return self.storage.set_project_smoke_state(project_id, workspace_id, to)

    def prepare_project(
        self,
        project_id: str,
        workspace_id: str,
        *,
        entrypoint: Sequence[str],
    ) -> ProjectRecord:
        """The smoke gate, half 1 — resolve the runtime: ANALYZED ->
        RUNTIME_PREPARED with the entrypoint stored, or ->
        entrypoint_missing when none is declared (§32A.4). Recovery from a
        failure state re-prepares (failure -> RUNTIME_PREPARED)."""
        project = self.storage.get_project(project_id, workspace_id)
        if project is None:
            raise KeyError(f"project {project_id!r} not found in workspace {workspace_id!r}")
        state = SmokeState(project.smoke_state)
        if state in (SmokeState.SMOKE_PASSED, SmokeState.EVALUABLE):
            raise EngineError(
                f"project {project_id!r} is {state.value}; re-preparing the runtime after "
                "smoke passed is out of scope in the MVP (the recovery UI re-prepares "
                "from failure states only)"
            )
        if state is SmokeState.INGESTED:
            self.storage.set_project_smoke_state(project_id, workspace_id, SmokeState.ANALYZED)
        self.storage.set_project_entrypoint(project_id, workspace_id, tuple(entrypoint or ()))
        if not entrypoint:
            return self.storage.set_project_smoke_state(
                project_id, workspace_id, SmokeState.ENTRYPOINT_MISSING,
                failure_detail="no entrypoint declared",
            )
        return self.storage.set_project_smoke_state(
            project_id, workspace_id, SmokeState.RUNTIME_PREPARED
        )

    def smoke(
        self,
        project_id: str,
        workspace_id: str,
        *,
        timeout_seconds: float = 120.0,
        cwd: str | Path | None = None,
    ) -> SmokeAttemptRecord:
        """The smoke gate, half 2 — a real single-case invocation (§32A.3)
        with the project's resolved entrypoint. The outcome lands the project
        in SMOKE_PASSED or exactly one of the six failure states (§32A.4,
        canonical). The smoke trace is persisted at the attempt's
        ``trace_path`` (evidence) but is not ingested into ``trace_events`` —
        there is no run (MVP divergence, documented)."""
        project = self.storage.get_project(project_id, workspace_id)
        if project is None:
            raise KeyError(f"project {project_id!r} not found in workspace {workspace_id!r}")
        entrypoint = project.entrypoint
        if not entrypoint:
            raise EngineError(
                f"project {project_id!r} has no entrypoint — run prepare_project first"
            )
        state = SmokeState(project.smoke_state)
        if state in (
            SmokeState.INGESTED, SmokeState.ANALYZED,
            SmokeState.SMOKE_PASSED, SmokeState.EVALUABLE,
        ):
            raise EngineError(
                f"smoke can only run from RUNTIME_PREPARED or a failure state; "
                f"project {project_id!r} is {state.value}"
            )
        attempt_id = uuid.uuid4().hex
        work_dir = self.work_root / "smoke" / project_id / attempt_id
        work_dir.mkdir(parents=True, exist_ok=True)
        probe_case = TestCase(
            case_id="__smoke__", name="smoke probe",
            description="probe invocation", input={},
        )
        probe_spec = EvaluationSpec(
            spec_version="0.0.0", name="smoke", dataset_version="0.0.0",
            cases=[], metrics=[],
        )
        result = invoke_agent(
            agent_command=entrypoint,
            run_id="__smoke__",
            workspace_id=workspace_id,
            case=probe_case,
            attempt_id=attempt_id,
            repeat_index=0,
            attempt=0,
            spec=probe_spec,
            timeout_seconds=timeout_seconds,
            work_dir=work_dir,
            cwd=cwd,
        )
        if result.status == TIMED_OUT:
            outcome = SmokeState.TIMEOUT
        elif result.status == PROVIDER_UNREACHABLE:
            outcome = SmokeState.PROVIDER_UNREACHABLE
        elif result.status == INVOCATION_FAILED:
            outcome = SmokeState.INVOCATION_FAILED
        elif result.status == NO_TRACE:
            outcome = SmokeState.NO_TRACE
        else:
            outcome = SmokeState.SMOKE_PASSED
        trace_path = work_dir / "trace.jsonl"
        record = self.storage.append_smoke_attempt(
            workspace_id=workspace_id,
            project_id=project_id,
            entrypoint=entrypoint,
            state=outcome,
            exit_code=result.exit_code,
            failure_detail=result.error,
            trace_path=str(trace_path) if trace_path.exists() else None,
            reproduce_locally=shlex.join(entrypoint),  # §32A.3.6: the exact command
        )
        self.storage.set_project_smoke_state(
            project_id, workspace_id, outcome, failure_detail=result.error
        )
        return record

    # ------------------------------------------------------------------
    # Run creation
    # ------------------------------------------------------------------

    def create_run(
        self,
        workspace_id: str,
        spec: EvaluationSpec | dict[str, Any] | str,
        *,
        tier: str | None = None,
        repeats: int = 1,
        retry_max: int = 0,
        project_id: str | None = None,
        idempotency_key: str | None = None,
        created_by: str | None = None,
        budget_usd_micros: int = 0,
        concurrency: int = 1,
        world_config: dict[str, Any] | None = None,
        source_digest: str | None = None,
        cwd: str | Path | None = None,
        timeout_seconds: float = 120.0,
        entrypoint: Sequence[str] | None = None,
    ) -> RunRecord:
        """Validate and persist a run: pydantic spec validation, the project
        smoke gate (§32A.2), the tier cap (§11B.9), idempotency-key guard
        (§31C.2). ``repeats``/``retry_max`` become the run's §11C execution
        config; attempt 0 is authoritative."""
        if isinstance(spec, EvaluationSpec):
            validated = spec
        elif isinstance(spec, (dict, str)):
            validated = validate_spec(spec)
        else:
            raise TypeError(
                "spec must be an EvaluationSpec, a spec dict, or spec JSON text"
            )
        if not validated.cases:
            raise ValueError(f"spec {validated.name!r} has no cases — a run with zero cases is meaningless")
        if not validated.metrics:
            raise ValueError(f"spec {validated.name!r} has no metrics — a run with zero metrics measures nothing")
        if repeats < 1:
            raise ValueError(f"repeats must be >= 1, got {repeats}")
        if retry_max < 0:
            raise ValueError(f"retry_max must be >= 0, got {retry_max}")
        if concurrency != 1:
            raise ValueError(
                "concurrency > 1 is not supported in the MVP — the engine is "
                "single-threaded (R10; the design's workers are a later chunk)"
            )
        if budget_usd_micros < 0:
            raise ValueError(f"budget_usd_micros must be >= 0, got {budget_usd_micros}")
        if tier is None:
            tier = validated.run_tier
        if tier is None:
            raise ValueError(
                "run tier is required: pass tier= or set the spec's run_tier hint "
                "(quick | standard | full, §11B.9)"
            )
        if tier not in RUN_TIERS:
            raise ValueError(f"unknown tier {tier!r}; must be one of {RUN_TIERS}")

        # Project gate — smoke gates RUNNING only (§32A.2 C1).
        if project_id is not None:
            project = self.storage.get_project(project_id, workspace_id)
            if project is None:
                raise KeyError(f"project {project_id!r} not found in workspace {workspace_id!r}")
            if project.smoke_state != SmokeState.EVALUABLE.value:
                raise ProjectNotEvaluableError(
                    f"project {project_id!r} is {project.smoke_state!r}; the smoke gate "
                    "must pass and the project must be marked EVALUABLE before runs "
                    "execute against it (§32A.2)"
                )
            resolved_entrypoint = project.entrypoint
            if not resolved_entrypoint:
                raise EngineError(
                    f"project {project_id!r} is EVALUABLE but has no entrypoint — schema drift"
                )
        else:
            resolved_entrypoint = tuple(entrypoint or ())
            if not resolved_entrypoint:
                raise ValueError(
                    "an agent entrypoint is required when no project_id is given"
                )

        self._check_tier_cap(tier, len(validated.cases))

        agent_version = {
            "source_digest": source_digest or "unknown",
            "entrypoint": list(resolved_entrypoint),
            "cwd": str(cwd) if cwd is not None else None,
            "timeout_seconds": float(timeout_seconds),
        }
        judge_binding = {
            m.metric_id: m.judge_binding.model_dump()
            for m in validated.metrics
            if m.type == "judge"
        } or None
        mixed_binding = any(
            m.type == "judge" and not m.provisional for m in validated.metrics
        )
        repeat_config = {
            "repeats": repeats,
            "retry": {"max": retry_max, "authoritative": "first_attempt"},
            "flakiness": {"classify": True},
        }
        record = self.storage.create_run(
            workspace_id=workspace_id,
            spec_json=validated.model_dump_json(),
            agent_version=agent_version,
            tier=tier,
            repeat_config=repeat_config,
            world_config=world_config or {},
            case_count=len(validated.cases),
            concurrency=concurrency,
            budget_usd_micros=budget_usd_micros,
            project_id=project_id,
            judge_binding=judge_binding,
            mixed_binding=mixed_binding,
            created_by=created_by,
            idempotency_key=idempotency_key,
        )
        self.storage.insert_cases(record.run_id, workspace_id, validated.cases, repeat_count=repeats)
        return record

    def get_run(self, run_id: str, workspace_id: str) -> RunRecord | None:
        return self.storage.get_run(run_id, workspace_id)

    def _check_tier_cap(self, tier: str, case_count: int) -> None:
        t = self.tiers
        if tier == "quick":
            if case_count > t.quick_max_cases:
                raise TierViolationError(
                    f"tier 'quick' allows up to {t.quick_max_cases} cases (§11B.9), "
                    f"spec has {case_count}"
                )
        elif tier == "standard":
            if case_count < t.standard_min_cases:
                raise TierViolationError(
                    f"tier 'standard' requires at least {t.standard_min_cases} cases "
                    f"(§11B.9), spec has {case_count}"
                )
            if case_count > t.standard_max_cases:
                raise TierViolationError(
                    f"tier 'standard' allows up to {t.standard_max_cases} cases (§11B.9), "
                    f"spec has {case_count}"
                )
        elif tier == "full":
            if case_count > t.full_max_cases:
                raise TierViolationError(
                    f"tier 'full' hard cap is {t.full_max_cases} cases (§11B.9), "
                    f"spec has {case_count}"
                )
        else:
            raise ValueError(f"unknown tier {tier!r}")
        if case_count >= t.warning_threshold_cases:
            logger.warning(
                "run with %d cases is at/above the %d-case warning threshold (§11B.9)",
                case_count, t.warning_threshold_cases,
            )

    # ------------------------------------------------------------------
    # Run execution
    # ------------------------------------------------------------------

    def run(
        self,
        run_id: str,
        workspace_id: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> RunResult:
        """Execute a run end to end: advance the lifecycle to running, run every
        queued case (repeats + retries + scoring), aggregate (§13A.4 write-time
        incremental), evaluate gates, and land in ``complete``. Unexpected
        engine errors land the run in ``incomplete`` (§11B.8: a marker, never a
        final state) and propagate. ``complete`` / ``failed`` runs refuse to
        re-run. A resumed run (INCOMPLETE / RUNNING / AGGREGATING, or CANCELLED
        via the explicit §35B.3 resume path) re-scores previously-finished
        cases from their stored traces (§13A.1.3 — no agent re-run) and
        re-runs only the cases left queued or requeued.

        ``should_cancel`` — an optional polled cancellation signal (§17.7:
        cancellation is a request, never a promise; the API's background worker
        passes a thread-safe flag). Checked once between cases. When it fires,
        unrun cases are marked CANCELLED (evidence of the interruption —
        nothing silently dropped), what already landed is re-aggregated
        (aggregation_state PARTIAL — the run is done executing but inputs are
        missing, never a fabricated complete) and the run lands in
        ``cancelled``. The in-flight case (if any) is allowed to finish; the
        signal is honored at the case boundary."""
        run = self.storage.get_run(run_id, workspace_id)
        if run is None:
            raise KeyError(f"run {run_id!r} not found in workspace {workspace_id!r}")
        status = RunStatus(run.status)
        if status in (RunStatus.COMPLETE, RunStatus.FAILED):
            raise RunAlreadyTerminalError(
                f"run {run_id!r} is already {status.value}; a terminal run does not re-run"
            )
        if status is RunStatus.CANCELLED:
            # §35B.3 resume of a user-cancelled run: cancellation is a
            # terminal *outcome*, not a terminal *record* — the explicit
            # resume verb requeues the run. Unrun cases were left CANCELLED
            # as evidence of the interruption (§17.7); requeue exactly those
            # (case-level CANCELLED → QUEUED is the deliberate explicit act
            # the transition table blesses). Finished cases keep their
            # attempts and re-score from stored traces at finalize.
            self.storage.set_run_status(run_id, workspace_id, RunStatus.QUEUED)
            for case in self.storage.list_cases(run_id, workspace_id):
                if case.status == RunCaseStatus.CANCELLED.value:
                    self.storage.set_case_status(
                        run_id, case.case_id, workspace_id, RunCaseStatus.QUEUED
                    )
            status = RunStatus.QUEUED
        if status in (RunStatus.DRAFT, RunStatus.QUEUED, RunStatus.PROVISIONING):
            if status is RunStatus.DRAFT:
                self.storage.set_run_status(run_id, workspace_id, RunStatus.QUEUED)
            if status in (RunStatus.DRAFT, RunStatus.QUEUED):
                self.storage.set_run_status(run_id, workspace_id, RunStatus.PROVISIONING)
            self.storage.set_run_status(run_id, workspace_id, RunStatus.RUNNING)
        requeue = self._reconcile_stale(run_id, workspace_id)
        # CANCELLED → QUEUED (§11B.8 resume path): the reconciler left the
        # crashed case CANCELLED as evidence; requeue it so the loop re-runs
        # it fresh. §35B.3: a resume also re-enqueues FAILED cases — a case
        # whose attempts all failed has nothing to re-score, so it runs fresh
        # with first-attempt authority (§11C). Finished cases are untouched
        # (they re-score from stored traces at finalize).
        if status in (
            RunStatus.RUNNING, RunStatus.AGGREGATING, RunStatus.INCOMPLETE,
        ) or run.status == RunStatus.CANCELLED.value:
            for case in self.storage.list_cases(run_id, workspace_id):
                if case.status == RunCaseStatus.FAILED.value:
                    requeue.add(case.case_id)
        for case_id in requeue:
            self.storage.set_case_status(
                run_id, case_id, workspace_id, RunCaseStatus.QUEUED
            )

        spec = run.spec
        expected_n = len(spec.cases) * run.repeats
        no_ci = run.repeats < 3  # §11C.4: repeats >= 3 required for a CI
        executed: dict[str, CaseRunResult] = {}
        warnings: list[str] = []
        landed: dict[str, list[CaseScore]] = {m.metric_id: [] for m in spec.metrics}

        try:
            for case in spec.cases:
                if should_cancel is not None and should_cancel():
                    # §17.7 worker confirmation: land the cancelled run and
                    # return normally — the except-path below would clobber
                    # CANCELLED with INCOMPLETE.
                    return self._cancel_run(
                        run_id, workspace_id, spec, executed, landed,
                        expected_n, no_ci, warnings,
                    )
                record = self.storage.get_case(run_id, case.case_id, workspace_id)
                if record is None:
                    raise EngineError(
                        f"case {case.case_id!r} missing from run {run_id!r} — schema drift"
                    )
                if record.status in (
                    RunCaseStatus.COMPLETED.value, RunCaseStatus.FAILED.value,
                    RunCaseStatus.CANCELLED.value, RunCaseStatus.SKIPPED.value,
                ):
                    continue  # resume: finished cases re-score from traces at finalize
                case_result = self._run_case_inner(run, record, workspace_id)
                executed[case.case_id] = case_result
                warnings.extend(case_result.warnings)
                for outcome in case_result.repeat_outcomes:
                    for score in outcome.scores:
                        landed[score.metric_id].append(score)
                # §13A.4 / 4H: stream the running partial after every case.
                for metric in spec.metrics:
                    aggregate = aggregate_metric(
                        spec, metric, landed[metric.metric_id],
                        expected_n=expected_n, in_progress=True,
                    )
                    self.storage.upsert_run_metric_result(
                        run_id=run_id, workspace_id=workspace_id, metric=metric,
                        aggregate=aggregate, no_ci=no_ci,
                    )
        except Exception:
            self.storage.set_run_status(run_id, workspace_id, RunStatus.INCOMPLETE)
            raise
        try:
            return self._finalize(run, workspace_id, executed, landed, expected_n, no_ci, warnings)
        except Exception:
            self.storage.set_run_status(run_id, workspace_id, RunStatus.INCOMPLETE)
            raise

    def _cancel_run(
        self,
        run_id: str,
        workspace_id: str,
        spec: EvaluationSpec,
        executed: Mapping[str, CaseRunResult],
        landed: Mapping[str, list[CaseScore]],
        expected_n: int,
        no_ci: bool,
        warnings: list[str],
    ) -> RunResult:
        """§17.7 worker-side confirmation of a cancel request.

        Every unrun case is marked CANCELLED (evidence of the interruption —
        nothing silently dropped), what already landed is re-aggregated with
        ``in_progress=False`` (aggregation_state PARTIAL when inputs are
        missing — the dashboard must never render a half-aggregated run as
        finished), and the run lands in ``cancelled`` (legal from RUNNING per
        RUN_TRANSITIONS). Returns a CANCELLED RunResult normally, so the
        caller's except-path never overrides it with INCOMPLETE.
        """
        for case in spec.cases:
            record = self.storage.get_case(run_id, case.case_id, workspace_id)
            if record is None:
                raise EngineError(
                    f"case {case.case_id!r} missing from run {run_id!r} — schema drift"
                )
            if record.status in (
                RunCaseStatus.COMPLETED.value, RunCaseStatus.FAILED.value,
                RunCaseStatus.CANCELLED.value, RunCaseStatus.SKIPPED.value,
            ):
                continue  # finished or already terminal cases are evidence, untouched
            self.storage.set_case_status(
                run_id, case.case_id, workspace_id, RunCaseStatus.CANCELLED
            )
        for metric in spec.metrics:
            aggregate = aggregate_metric(
                spec, metric, landed[metric.metric_id], expected_n=expected_n
            )
            self.storage.upsert_run_metric_result(
                run_id=run_id, workspace_id=workspace_id, metric=metric,
                aggregate=aggregate, no_ci=no_ci,
            )
        self.storage.set_run_status(run_id, workspace_id, RunStatus.CANCELLED)
        metric_results = self.storage.get_run_metric_results(run_id, workspace_id)
        return RunResult(
            run_id=run_id,
            status=RunStatus.CANCELLED.value,
            case_results=tuple(executed.values()),
            metric_results=tuple(metric_results),
            gate_results=tuple(),
            warnings=tuple(warnings),
        )

    def run_case(self, run_id: str, case_id: str, workspace_id: str) -> CaseRunResult:
        """Run one queued case (repeats, retries, scoring) without the rest of
        the run loop. Used by the API's per-case endpoints and tests."""
        run = self.storage.get_run(run_id, workspace_id)
        if run is None:
            raise KeyError(f"run {run_id!r} not found in workspace {workspace_id!r}")
        record = self.storage.get_case(run_id, case_id, workspace_id)
        if record is None:
            raise KeyError(f"case {case_id!r} not found in run {run_id!r}")
        if record.status != RunCaseStatus.QUEUED.value:
            raise EngineError(
                f"case {case_id!r} is {record.status!r}; only queued cases can be run"
            )
        return self._run_case_inner(run, record, workspace_id)

    def _reconcile_stale(self, run_id: str, workspace_id: str) -> set[str]:
        """Resume hygiene: attempts left QUEUED/RUNNING by a dead runner are
        ORPHANED (§11B.6 sweeper semantics); cases left RUNNING are cancelled
        (evidence of the crash) and returned so the caller can requeue and
        re-run them fresh."""
        cancelled: set[str] = set()
        for attempt in self.storage.list_attempts(run_id, workspace_id):
            if attempt.status in (
                AttemptStatus.QUEUED.value, AttemptStatus.RUNNING.value,
            ):
                self.storage.set_attempt_status(
                    attempt.attempt_id, workspace_id, AttemptStatus.ORPHANED,
                    error="previous runner process died; attempt orphaned on resume",
                )
        for case in self.storage.list_cases(run_id, workspace_id):
            if case.status == RunCaseStatus.RUNNING.value:
                self.storage.set_case_status(
                    run_id, case.case_id, workspace_id, RunCaseStatus.CANCELLED
                )
                cancelled.add(case.case_id)
        return cancelled

    def _run_case_inner(
        self, run: RunRecord, case: Any, workspace_id: str
    ) -> CaseRunResult:
        """One case through its repeats (independent samples) and retries
        (attempt > 0, only after a first-attempt FAIL — never after a timeout
        or evaluator ERROR). The per-case metric row is upserted per attempt
        (§18.6: rows land under the current score_revision; retry rows are
        flagged on_retry_override, never re-entered into the pass rate).

        ``case`` is the storage CaseRecord (status holder); the invocation
        payload comes from the run's frozen spec (the TestCase), so the agent
        always sees the spec's input — never storage state."""
        spec_case = self._spec_case(run, case.case_id)
        self.storage.set_case_status(
            run.run_id, case.case_id, workspace_id, RunCaseStatus.RUNNING
        )
        # Resume numbering: a requeued case may still hold orphaned/errored
        # attempts from the crashed runner, and attempts' PK is
        # (run_id, case_id, repeat_index, attempt) — fresh samples continue
        # past the highest repeat_index already present, never reusing one.
        prior = self.storage.list_attempts(run.run_id, workspace_id, case_id=case.case_id)
        repeat_offset = max((a.repeat_index for a in prior), default=-1) + 1
        repeat_outcomes: list[AttemptResult] = []
        retry_outcomes: list[AttemptResult] = []
        warnings: list[str] = []
        for i in range(run.repeats):
            repeat_index = repeat_offset + i
            first = self._run_attempt(run, spec_case, workspace_id, repeat_index, 0, warnings)
            repeat_outcomes.append(first)
            if first.status != AttemptStatus.COMPLETED.value:
                continue
            if (
                first.scores
                and any(s.status is CaseStatus.FAIL for s in first.scores)
                and run.retry_max > 0
            ):
                # Retries mask variance (§11C.3): attempt 0 is authoritative,
                # a passing retry is flagged on_retry_override and can never
                # re-enter the pass rate.
                for attempt_no in range(1, run.retry_max + 1):
                    retry = self._run_attempt(
                        run, spec_case, workspace_id, repeat_index, attempt_no, warnings
                    )
                    retry_outcomes.append(retry)
                    if (
                        retry.status == AttemptStatus.COMPLETED.value
                        and retry.scores
                        and all(s.status is CaseStatus.PASS for s in retry.scores)
                    ):
                        break
        classification, error_category = self._classify_case(repeat_outcomes)
        self.storage.set_case_classification(
            run.run_id, case.case_id, workspace_id, classification, error_category
        )
        if any(o.status == AttemptStatus.COMPLETED.value for o in repeat_outcomes):
            self.storage.set_case_status(
                run.run_id, case.case_id, workspace_id, RunCaseStatus.COMPLETED
            )
        else:
            self.storage.set_case_status(
                run.run_id, case.case_id, workspace_id, RunCaseStatus.FAILED
            )
        return CaseRunResult(
            run_id=run.run_id,
            case_id=case.case_id,
            status=self.storage.get_case(run.run_id, case.case_id, workspace_id).status,
            classification=classification,
            error_category=error_category,
            repeat_outcomes=tuple(repeat_outcomes),
            retry_outcomes=tuple(retry_outcomes),
            warnings=tuple(warnings),
        )

    def _spec_case(self, run: RunRecord, case_id: str) -> TestCase:
        """The frozen spec's TestCase for ``case_id``. The spec is the
        authoritative input source — the agent never sees storage state."""
        for case in run.spec.cases:
            if case.case_id == case_id:
                return case
        raise EngineError(
            f"case {case_id!r} is not in run {run.run_id!r}'s frozen spec — schema drift"
        )

    def _run_attempt(
        self,
        run: RunRecord,
        case: Any,
        workspace_id: str,
        repeat_index: int,
        attempt: int,
        warnings: list[str],
    ) -> AttemptResult:
        """One subprocess invocation: fresh process, §11A file protocol,
        ingestion (redaction + identity verification, price-version stamp,
        1,000-event cap), and per-attempt scoring."""
        record = self.storage.create_attempt(
            run_id=run.run_id,
            case_id=case.case_id,
            workspace_id=workspace_id,
            repeat_index=repeat_index,
            attempt=attempt,
        )
        work_dir = (
            self.work_root / "runs" / run.run_id / case.case_id
            / f"repeat{repeat_index}_attempt{attempt}"
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        self.storage.set_attempt_status(
            record.attempt_id, workspace_id, AttemptStatus.RUNNING
        )
        try:
            result = invoke_agent(
                agent_command=run.entrypoint,
                run_id=run.run_id,
                workspace_id=workspace_id,
                case=case,
                attempt_id=record.attempt_id,
                repeat_index=repeat_index,
                attempt=attempt,
                spec=run.spec,
                timeout_seconds=run.timeout_seconds,
                work_dir=work_dir,
                cwd=run.cwd,
            )
        except Exception as exc:  # an engine bug/crash, not an agent outcome
            self.storage.set_attempt_status(
                record.attempt_id, workspace_id, AttemptStatus.ERRORED,
                error=f"runner crashed: {exc}",
            )
            raise
        if result.status == TIMED_OUT:
            self.storage.set_attempt_status(
                record.attempt_id, workspace_id, AttemptStatus.TIMED_OUT,
                exit_code=result.exit_code, error=result.error,
            )
            return AttemptResult(
                record.attempt_id, repeat_index, attempt, AttemptStatus.TIMED_OUT.value,
                result.exit_code, 0, False, result.error, (), result.result_payload,
            )
        if result.status in (INVOCATION_FAILED, PROVIDER_UNREACHABLE):
            self.storage.set_attempt_status(
                record.attempt_id, workspace_id, AttemptStatus.ERRORED,
                exit_code=result.exit_code, error=result.error,
            )
            return AttemptResult(
                record.attempt_id, repeat_index, attempt, AttemptStatus.ERRORED.value,
                result.exit_code, 0, False, result.error, (), result.result_payload,
            )
        # completed | no_trace — ingest, then score.
        try:
            kept, truncated = self._ingest_events(
                run, case.case_id, record.attempt_id, result.trace_events
            )
        except EngineError as exc:
            self.storage.set_attempt_status(
                record.attempt_id, workspace_id, AttemptStatus.ERRORED, error=str(exc)
            )
            return AttemptResult(
                record.attempt_id, repeat_index, attempt, AttemptStatus.ERRORED.value,
                result.exit_code, 0, False, str(exc), (), result.result_payload,
            )
        if result.status == NO_TRACE:
            warnings.append(
                f"case {case.case_id!r} repeat {repeat_index} completed with no trace "
                "events — the empty trace is scored by on_missing (§9A.5), never "
                "fabricated"
            )
        self.storage.set_attempt_status(
            record.attempt_id, workspace_id, AttemptStatus.COMPLETED,
            exit_code=result.exit_code, event_count=len(kept),
            trace_truncated=truncated,
        )
        metric_by_id = {m.metric_id: m for m in run.spec.metrics}
        if attempt == 0:
            # First attempts are authoritative (§11C.3).
            scores = [
                evaluate_case(run.spec, case.case_id, kept, metric, judge=self._judge_for(metric))
                for metric in run.spec.metrics
            ]
        else:
            scores = [
                score_attempt(run.spec, case.case_id, kept, metric, judge=self._judge_for(metric))
                for metric in run.spec.metrics
            ]
        self.storage.upsert_case_metric_results(
            run_id=run.run_id, case_id=case.case_id, workspace_id=workspace_id,
            scores=scores, metric_by_id=metric_by_id,
        )
        return AttemptResult(
            record.attempt_id, repeat_index, attempt, AttemptStatus.COMPLETED.value,
            result.exit_code, len(kept), truncated, None, tuple(scores),
            result.result_payload,
        )

    def _ingest_events(
        self, run: RunRecord, case_id: str, attempt_id: str, events: Sequence[TraceEvent]
    ) -> tuple[list[TraceEvent], bool]:
        """Verify the capture contract (explicit redaction_state, matching
        identity claims), stamp price_version from config on cost-bearing
        events, enforce the 1,000-event cap, and persist. Returns
        (kept events, truncated)."""
        for event in events:
            if "redaction_state" not in event.model_fields_set:
                raise EngineError(
                    f"event {event.event_id!r} does not carry an explicit "
                    "redaction_state — the capture contract (§12A.6) requires "
                    "redaction at capture, and the engine refuses to ingest "
                    "unverified events"
                )
            if (
                event.run_id != run.run_id
                or event.case_id != case_id
                or event.attempt_id != attempt_id
            ):
                raise EngineError(
                    f"event {event.event_id!r} claims run/case/attempt "
                    f"{event.run_id!r}/{event.case_id!r}/{event.attempt_id!r}, expected "
                    f"{run.run_id!r}/{case_id!r}/{attempt_id!r} — cross-case "
                    "contamination refused"
                )
        kept: list[TraceEvent] = []
        for event in events:
            if event.cost is not None and event.cost.price_version != self.price_version:
                event = event.model_copy(
                    update={
                        "cost": event.cost.model_copy(
                            update={"price_version": self.price_version}
                        )
                    }
                )
            kept.append(event)
        truncated = len(kept) > MAX_EVENTS_PER_ATTEMPT
        if truncated:
            kept = kept[:MAX_EVENTS_PER_ATTEMPT]
        self.storage.insert_trace_events(kept)
        return kept, truncated

    def _judge_for(self, metric: Any) -> JudgeEvaluator | None:
        if metric.type != "judge":
            return None
        judge = self.judge_factory(metric.judge_binding)
        if judge.binding != metric.judge_binding:
            raise ValueError(
                f"judge factory returned a judge bound to {judge.binding!r}, but metric "
                f"{metric.metric_id!r} declares {metric.judge_binding!r} (§15A.5 — "
                "bindings must match for scores to be comparable)"
            )
        return judge

    @staticmethod
    def _classify_case(repeat_outcomes: Sequence[AttemptResult]) -> tuple[str | None, str | None]:
        """§11C.3: FLAKY = 0 < pass_rate < 1 across the case's repeat samples
        (first-attempt outcomes only — retry passes never re-enter). PASSING =
        pass_rate 1, FAILING = 0, ERRORED = no pass/fail sample at all.
        error_category ∈ infra | agent | evaluator."""
        passes = fails = 0
        error_categories: list[str] = []
        for outcome in repeat_outcomes:
            if outcome.attempt != 0:
                continue  # defensive: only first attempts classify
            if outcome.status != AttemptStatus.COMPLETED.value:
                error_categories.append(
                    "infra"
                    if outcome.status
                    in (AttemptStatus.TIMED_OUT.value, AttemptStatus.ORPHANED.value,
                        AttemptStatus.BUDGET_EXCEEDED.value)
                    else "agent"
                )
                continue
            statuses = [s.status for s in outcome.scores]
            if not statuses:
                error_categories.append("agent")
                continue
            if any(s is CaseStatus.ERROR for s in statuses):
                error_categories.append("evaluator")
                continue
            if any(s is CaseStatus.FAIL for s in statuses):
                fails += 1
            elif all(s is CaseStatus.PASS for s in statuses):
                passes += 1
            # all SKIPPED: excluded from pass/fail (§7A.4)
        if passes + fails == 0:
            classification = ERRORED if error_categories else None
        elif fails == 0:
            classification = PASSING
        elif passes == 0:
            classification = FAILING
        else:
            classification = FLAKY
        error_category = None
        for category in ("infra", "agent", "evaluator"):
            if category in error_categories:
                error_category = category
                break
        return classification, error_category

    # ------------------------------------------------------------------
    # Finalize: aggregation, gates, completion
    # ------------------------------------------------------------------

    def _write_cost_summaries(self, run: RunRecord, workspace_id: str) -> None:
        """Aggregate ingested cost into ``cost_summaries`` before the run lands
        COMPLETE (§12A.5/§12B.2: historical totals survive provider price drift
        only keyed by ``price_version``). Written at finalize so a mid-run
        dashboard can never show a fabricated total — no summaries, no cost
        stat."""
        totals: dict[str, list[int]] = {}
        for raw in self.storage.iter_cost_json(run.run_id, workspace_id):
            block = CostBlock.model_validate(raw)
            t = totals.setdefault(block.price_version, [0, 0, 0])
            t[0] += block.tokens.input + block.tokens.cache_read
            t[1] += block.tokens.output + block.tokens.cache_write
            t[2] += round(block.cost_usd * 1_000_000)
        rows = [
            {
                "price_version": pv,
                "input_tokens": v[0],
                "output_tokens": v[1],
                "usd_micros": v[2],
            }
            for pv, v in totals.items()
        ]
        if rows:
            self.storage.add_cost_summaries(run.run_id, workspace_id, rows)

    def _finalize(
        self,
        run: RunRecord,
        workspace_id: str,
        executed: Mapping[str, Any],
        landed: Mapping[str, list[CaseScore]],
        expected_n: int,
        no_ci: bool,
        warnings: list[str],
    ) -> RunResult:
        """Aggregate every metric (§13A.4), evaluate gates (§7A.2/§15A.1 —
        provisional judge metrics never gate), and land the run in
        ``complete``. Skipped cases (a resumed run) re-score from their stored
        first-attempt traces — no agent re-run (§13A.1.3)."""
        spec = run.spec
        for case in spec.cases:
            if case.case_id in executed:
                continue
            per_repeat = self._case_first_attempt_scores_from_trace(run, case.case_id, workspace_id)
            for scores in per_repeat:
                for score in scores:
                    landed[score.metric_id].append(score)
        current = RunStatus(self.storage.get_run(run.run_id, workspace_id).status)
        if current is RunStatus.RUNNING:
            self.storage.set_run_status(run.run_id, workspace_id, RunStatus.AGGREGATING)
        # §15A.1 (owner decision 4J): with no readiness registry there is no
        # calibration evidence — judge metrics never gate in the MVP.
        gate_safe_metrics = gate_safe(spec.metrics)
        gate_results: list[GateResult] = []
        for metric in spec.metrics:
            aggregate = aggregate_metric(
                spec, metric, landed[metric.metric_id], expected_n=expected_n
            )
            if metric in gate_safe_metrics:
                gate = evaluate_gate(metric, aggregate)
                gate_results.append(gate)
                gate_status = "PASS" if gate.passed else ("FAIL" if gate.active else "NOT_APPLICABLE")
            else:
                gate_status = "NOT_APPLICABLE"
            self.storage.upsert_run_metric_result(
                run_id=run.run_id, workspace_id=workspace_id, metric=metric,
                aggregate=aggregate, gate_status=gate_status, no_ci=no_ci,
            )
        self._write_cost_summaries(run, workspace_id)
        self.storage.set_run_status(run.run_id, workspace_id, RunStatus.COMPLETE)
        metric_results = self.storage.get_run_metric_results(run.run_id, workspace_id)
        return RunResult(
            run_id=run.run_id,
            status=RunStatus.COMPLETE.value,
            case_results=tuple(executed.values()),
            metric_results=tuple(metric_results),
            gate_results=tuple(gate_results),
            warnings=tuple(warnings),
        )

    def _case_first_attempt_scores_from_trace(
        self, run: RunRecord, case_id: str, workspace_id: str
    ) -> list[list[CaseScore]]:
        """Re-score a previously-finished case from its stored first-attempt
        traces (§13A.1.3: the headline re-score capability — stored evidence,
        no agent re-run). Returns one list of scores per repeat."""
        per_repeat: list[list[CaseScore]] = []
        attempts = self.storage.list_attempts(run.run_id, workspace_id, case_id=case_id)
        for attempt_record in attempts:
            if not attempt_record.is_first_attempt:
                continue
            events = self.storage.get_trace_events(
                run_id=run.run_id, workspace_id=workspace_id,
                case_id=case_id, attempt_id=attempt_record.attempt_id,
            )
            per_repeat.append(
                [
                    evaluate_case(run.spec, case_id, events, metric, judge=self._judge_for(metric))
                    for metric in run.spec.metrics
                ]
            )
        return per_repeat
