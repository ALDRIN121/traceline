"""Lifecycle state machines: runs, cases, attempts, and the smoke gate.

The single source of truth for every status enum and legal transition in the
execution spine. Values are taken verbatim from the design sections:

- **Run statuses** — engine §11A/§13A.3 and semantics §18's ``runs.status``
  CHECK: ``draft → queued → provisioning → running → aggregating → complete``,
  terminal ``failed | cancelled``, plus the run-level ``incomplete`` marker — a
  status value, never a final state (§11B.8). Lowercase, exactly as §18 spells
  them.
- **Case statuses** — §18 ``run_cases.status``: ``queued | running | completed
  | failed | cancelled | skipped``.
- **Attempt statuses** — §18 ``run_case_attempts.status``: ``queued | running
  | completed`` with terminal ``timed_out | budget_exceeded | cancelled |
  orphaned | errored`` (§13A.2: per-attempt lifecycle statuses are engine
  §11A's; runner-applied kills are statuses, never agent exit codes).
- **Smoke states** — harness §32A: the five-state lifecycle
  ``INGESTED → ANALYZED → RUNTIME_PREPARED → SMOKE_PASSED → EVALUABLE`` plus
  the six smoke-failure states (§32A.4, canonical block, verbatim):
  ``entrypoint_missing | install_failed | invocation_failed |
  provider_unreachable | no_trace | timeout``.

``transition(current, target, table)`` rejects illegal jumps with
:class:`IllegalTransition` — an attempt to set a status the section files do
not allow is a bug in the caller, not a data state to be persisted.

**C1 (harness §32A.2)**: smoke gates RUNNING only. Authoring and
spec-creation are never blocked by the smoke state — the engine validates the
spec at run submission, never at authoring time. The run-status and smoke
machines are deliberately separate: nothing in the run lifecycle consults the
smoke state (the project gate lives in ``engine.create_run``).
"""

from __future__ import annotations

from enum import Enum
from typing import TypeVar

__all__ = [
    "RunStatus",
    "RunCaseStatus",
    "AttemptStatus",
    "SmokeState",
    "SmokeFailureState",
    "SMOKE_FAILURE_STATES",
    "RUN_TRANSITIONS",
    "RUN_CASE_TRANSITIONS",
    "ATTEMPT_TRANSITIONS",
    "SMOKE_TRANSITIONS",
    "IllegalTransition",
    "is_run_terminal",
    "is_attempt_terminal",
    "is_smoke_failure",
    "transition",
]

_S = TypeVar("_S", bound=Enum)


class RunStatus(str, Enum):
    """``runs.status`` (engine §11A lifecycle, §18 DDL — lowercase verbatim)."""

    DRAFT = "draft"
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    AGGREGATING = "aggregating"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: §11B.8: the run-level marker when a run ends without every scheduled
    #: case reaching a terminal attempt status. A status value, never a final
    #: state — distinct from metric-row aggregation_state (semantics §13A.3).
    INCOMPLETE = "incomplete"


class RunCaseStatus(str, Enum):
    """``run_cases.status`` (§18 DDL)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class AttemptStatus(str, Enum):
    """``run_case_attempts.status`` (§18 DDL; §13A.2: per-attempt lifecycle
    statuses, never assertion outcomes)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    TIMED_OUT = "timed_out"  # runner-applied kill — never an agent exit code
    BUDGET_EXCEEDED = "budget_exceeded"  # proxy budget breach (§11A)
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"  # worker crash detected by the sweeper (§11B.6)
    ERRORED = "errored"  # execution failure (§11A: never scored as an assertion)


class SmokeState(str, Enum):
    """A project's runtime-preparation state (harness §32A).

    The five-state lifecycle ``INGESTED → ANALYZED → RUNTIME_PREPARED →
    SMOKE_PASSED → EVALUABLE`` plus the six smoke-failure states (§32A.4,
    consumed verbatim — lowercase ids, exactly as the canonical block spells
    them). A project in a failure state is a project whose last smoke attempt
    failed; each failure state carries its own recovery path (§32A.4).
    """

    INGESTED = "INGESTED"
    ANALYZED = "ANALYZED"
    RUNTIME_PREPARED = "RUNTIME_PREPARED"
    SMOKE_PASSED = "SMOKE_PASSED"
    EVALUABLE = "EVALUABLE"
    ENTRYPOINT_MISSING = "entrypoint_missing"
    INSTALL_FAILED = "install_failed"
    INVOCATION_FAILED = "invocation_failed"
    PROVIDER_UNREACHABLE = "provider_unreachable"
    NO_TRACE = "no_trace"
    TIMEOUT = "timeout"


class SmokeFailureState(str, Enum):
    """The six smoke-failure states (harness §32A.4, single source of truth).

    Each carries ``meaning``, ``detected_by``, and ``recovery`` in the section
    file; this enum is the machine form used by the engine and storage.
    """

    ENTRYPOINT_MISSING = "entrypoint_missing"
    INSTALL_FAILED = "install_failed"
    INVOCATION_FAILED = "invocation_failed"
    PROVIDER_UNREACHABLE = "provider_unreachable"
    NO_TRACE = "no_trace"
    TIMEOUT = "timeout"


#: The six failure states as a tuple of SmokeState values.
SMOKE_FAILURE_STATES: tuple[SmokeState, ...] = tuple(
    SmokeState(s.value) for s in SmokeFailureState
)


class IllegalTransition(ValueError):
    """``transition()`` rejected a status jump the section files do not allow.

    A status update that raises this is a bug in the caller's flow, not a data
    condition — the engine never persists an illegal status.
    """


def is_run_terminal(status: RunStatus) -> bool:
    """§13A.3: terminal run statuses — ``complete | failed | cancelled``.
    ``incomplete`` is a marker, never a final state (§11B.8)."""
    return status in (RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.CANCELLED)


def is_attempt_terminal(status: AttemptStatus) -> bool:
    """§13A.2: terminal attempt statuses."""
    return status in (
        AttemptStatus.COMPLETED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.BUDGET_EXCEEDED,
        AttemptStatus.CANCELLED,
        AttemptStatus.ORPHANED,
        AttemptStatus.ERRORED,
    )


def is_smoke_failure(state: SmokeState) -> bool:
    return state in SMOKE_FAILURE_STATES


# ---------------------------------------------------------------------------
# Legal transition tables (the section files are authoritative)
# ---------------------------------------------------------------------------

#: runs.status (§11A lifecycle, §11B.8, §13A.3). ``draft → queued →
#: provisioning → running → aggregating → complete``; terminal
#: ``failed | cancelled``; ``incomplete`` when a run ends without every case
#: reaching a terminal attempt status. Same-status sets are legal no-ops
#: (idempotent re-assertion is harmless).
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.DRAFT: frozenset(
        {RunStatus.QUEUED, RunStatus.CANCELLED}
    ),
    RunStatus.QUEUED: frozenset(
        {RunStatus.PROVISIONING, RunStatus.CANCELLED}
    ),
    RunStatus.PROVISIONING: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {RunStatus.AGGREGATING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.INCOMPLETE}
    ),
    RunStatus.AGGREGATING: frozenset(
        {RunStatus.COMPLETE, RunStatus.FAILED, RunStatus.INCOMPLETE}
    ),
    RunStatus.COMPLETE: frozenset(),
    RunStatus.FAILED: frozenset(),
    # CANCELLED → QUEUED is the explicit resume path (§35B.3): user
    # cancellation is a terminal *outcome*, but an explicit resume requeues
    # the run record and re-runs the cases it left unscored. Never automatic
    # — the resume verb alone triggers it.
    RunStatus.CANCELLED: frozenset({RunStatus.QUEUED}),
    RunStatus.INCOMPLETE: frozenset(),
}

#: run_cases.status (§18).
RUN_CASE_TRANSITIONS: dict[RunCaseStatus, frozenset[RunCaseStatus]] = {
    RunCaseStatus.QUEUED: frozenset(
        {RunCaseStatus.RUNNING, RunCaseStatus.CANCELLED, RunCaseStatus.SKIPPED}
    ),
    RunCaseStatus.RUNNING: frozenset(
        {RunCaseStatus.COMPLETED, RunCaseStatus.FAILED, RunCaseStatus.CANCELLED}
    ),
    RunCaseStatus.COMPLETED: frozenset(),
    # FAILED → QUEUED is §35B.3's explicit resume act: a case whose attempts
    # all failed has nothing to re-score, so a resume re-runs it fresh with
    # first-attempt authority (§11C). Never automatic — the resume verb alone
    # triggers it.
    RunCaseStatus.FAILED: frozenset({RunCaseStatus.QUEUED}),
    # CANCELLED → QUEUED is the resume/requeue path (§11B.8): a runner crash
    # leaves the case CANCELLED as evidence; a later resume explicitly
    # requeues it (via Engine._reconcile_stale) to run fresh. Deliberate
    # re-queueing of a user-cancelled case is the same explicit act.
    RunCaseStatus.CANCELLED: frozenset({RunCaseStatus.QUEUED}),
    RunCaseStatus.SKIPPED: frozenset(),
}

#: run_case_attempts.status (§13A.2: queued → running → completed, terminal
#: timed_out | budget_exceeded | cancelled | orphaned | errored).
ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.QUEUED: frozenset(
        {AttemptStatus.RUNNING, AttemptStatus.CANCELLED}
    ),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.COMPLETED,
            AttemptStatus.TIMED_OUT,
            AttemptStatus.BUDGET_EXCEEDED,
            AttemptStatus.CANCELLED,
            AttemptStatus.ORPHANED,
            AttemptStatus.ERRORED,
        }
    ),
    AttemptStatus.COMPLETED: frozenset(),
    AttemptStatus.TIMED_OUT: frozenset(),
    AttemptStatus.BUDGET_EXCEEDED: frozenset(),
    AttemptStatus.CANCELLED: frozenset(),
    AttemptStatus.ORPHANED: frozenset(),
    AttemptStatus.ERRORED: frozenset(),
}

#: Smoke state machine (harness §32A). Forward progress through the five-state
#: lifecycle; a failed smoke attempt lands the project in exactly one of the
#: six failure states; recovery re-prepares (→ RUNTIME_PREPARED) or re-runs
#: smoke (→ SMOKE_PASSED), and a re-smoke can fail into another failure state.
#: A project that has not been runtime-prepared cannot fail smoke (INGESTED has
#: no failure edges), and EVALUABLE is terminal for the MVP (a re-smoke after
#: EVALUABLE is out of scope — the recovery UI re-prepares instead).
SMOKE_TRANSITIONS: dict[SmokeState, frozenset[SmokeState]] = {
    SmokeState.INGESTED: frozenset({SmokeState.ANALYZED}),
    SmokeState.ANALYZED: frozenset(
        {SmokeState.RUNTIME_PREPARED, *SMOKE_FAILURE_STATES}
    ),
    SmokeState.RUNTIME_PREPARED: frozenset(
        {SmokeState.SMOKE_PASSED, *SMOKE_FAILURE_STATES}
    ),
    SmokeState.SMOKE_PASSED: frozenset({SmokeState.EVALUABLE}),
    SmokeState.EVALUABLE: frozenset(),
    SmokeState.ENTRYPOINT_MISSING: frozenset(
        {SmokeState.RUNTIME_PREPARED, SmokeState.SMOKE_PASSED, *SMOKE_FAILURE_STATES}
    ),
    SmokeState.INSTALL_FAILED: frozenset(
        {SmokeState.RUNTIME_PREPARED, SmokeState.SMOKE_PASSED, *SMOKE_FAILURE_STATES}
    ),
    SmokeState.INVOCATION_FAILED: frozenset(
        {SmokeState.RUNTIME_PREPARED, SmokeState.SMOKE_PASSED, *SMOKE_FAILURE_STATES}
    ),
    SmokeState.PROVIDER_UNREACHABLE: frozenset(
        {SmokeState.RUNTIME_PREPARED, SmokeState.SMOKE_PASSED, *SMOKE_FAILURE_STATES}
    ),
    SmokeState.NO_TRACE: frozenset(
        {SmokeState.RUNTIME_PREPARED, SmokeState.SMOKE_PASSED, *SMOKE_FAILURE_STATES}
    ),
    SmokeState.TIMEOUT: frozenset(
        {SmokeState.RUNTIME_PREPARED, SmokeState.SMOKE_PASSED, *SMOKE_FAILURE_STATES}
    ),
}


def transition(
    current: _S | str,
    target: _S | str,
    table: dict[_S, frozenset[_S]],
) -> _S:
    """Validate (and apply) one status transition under ``table``.

    Same-status updates are legal no-ops (idempotent re-assertion). Anything
    not in the table's allowed set raises :class:`IllegalTransition`.

    Returns the target member (coerced to the table's enum type) so callers
    can persist the canonical value.
    """
    if not table:
        raise TypeError("transition table must be a non-empty mapping")
    current_member = _coerce(current, table)
    target_member = _coerce(target, table)
    if current_member == target_member:
        return target_member  # no-op: idempotent re-assertion
    allowed = table[current_member]
    if target_member not in allowed:
        raise IllegalTransition(
            f"{current_member.value!r} -> {target_member.value!r} is not a legal "
            f"transition; allowed from {current_member.value!r}: "
            f"{sorted(s.value for s in allowed)}"
        )
    return target_member


def _coerce(value: _S | str, table: dict[_S, frozenset[_S]]) -> _S:
    if isinstance(value, str):
        for member in table:
            if member.value == value:
                return member
        raise IllegalTransition(
            f"{value!r} is not a status in {type(next(iter(table))).__name__}"
        )
    if value not in table:
        raise IllegalTransition(
            f"{value!r} is not a status in {type(next(iter(table))).__name__}"
        )
    return value
