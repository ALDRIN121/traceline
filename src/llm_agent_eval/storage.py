"""SQLite persistence for the execution spine (semantics §18 hot path, adapted).

R10 divergence, stated: SQLite (single-user local) instead of Postgres + RLS.
The DDL mirrors §18's hot-path tables — **column names, status enums, and CHECK
constraints are taken verbatim from the section file** — with these documented
SQLite divergences:

- **No LIST partitioning** (SQLite has none). Per-run trace sets are modelled
  with ``run_id`` in the primary key plus per-run indexes; the partition story
  (``DROP PARTITION`` retention) stays a design-level concern (§18.3/18.4).
- **UUIDs are TEXT** (SQLite has no uuid type). ``run_id``/``case_id``/
  ``attempt_id``/``event_id``/``metric_id``/``project_id`` are 32-hex uuid4
  strings (matching ``events.make_event``'s ``event_id`` style).
- **``jsonb`` becomes TEXT** holding the JSON document; booleans become
  INTEGER (SQLite affinity accepts the ``BOOLEAN`` keyword, stored 0/1).
- **Timestamps are ISO-8601 UTC strings** (``datetime.isoformat()``), which
  sort lexicographically in the same timezone.
- **No foreign keys** (matches §18's deliberate non-FK stance on
  ``trace_events``: "do not add the FK" — reference integrity is enforced by
  the engine's flows, and the MVP defines no ``workspaces``/``users`` tables
  for FKs to target).
- **MVP additions**, each flagged: ``runs.idempotency_key`` (idempotent
  ``create_run``); ``runs.spec_json`` (the frozen EvaluationSpec in place of
  ``evaluation_versions.spec`` indirection); ``run_case_attempts.error``
  (the failure message, so an errored attempt is explainable);
  ``trace_events.cost_json`` (the full §12B cost block preserved for re-score
  alongside the typed ``cost_usd_micros``/``price_version`` columns — §18's
  "typed columns for every queryable dimension" stays); the ``projects`` and
  ``smoke_attempts`` tables (the smoke gate, harness §32A); the
  ``score_revisions`` table (§13A.1: overrides stored ALONGSIDE machine
  scores, never replacing them — the MVP form of §18's ``annotations``
  ``kind: override_score`` rows plus the revision ledger).
- **``trace_events.price_version`` is TEXT** — §12B.2 (and ``events.py``, the
  built layer this chunk consumes) says ``str``; §18's DDL says ``integer``.
  The built layer wins; the ``price_versions`` table is not in the MVP hot
  path.
- **``trace_events.tool``** is a typed column (§12B.2's envelope; §18's DDL
  omits it but its index list names it) — required by tool-scoped metrics.
- **``run_metric_results.value`` is nullable** — an empty-denominator
  aggregate (all cases skipped) stores NULL with ``aggregation_state
  COMPLETE`` rather than a fabricated number (invariant: never report success
  you did not observe).
- **Payloads are stored inline** — the 32 KB in-row cap and ``payload_ref``
  object-storage overflow (§12B.5) need object storage, which the MVP does not
  have. The per-attempt 1,000-event cap IS enforced (engine), with
  ``run_case_attempts.trace_truncated``.

Status transitions are enforced here via :mod:`lifecycle`'s legal transition
tables: a status write that is not a legal transition raises
:class:`~llm_agent_eval.lifecycle.IllegalTransition` and is never persisted.

Single-writer, single-threaded sync. The API layer (harness §17) runs runs in
background threads while request threads read the same store, so the single
connection is created ``check_same_thread=False`` and every operation is
serialized by :data:`_CONNECTION_LOCK` (the brief: "use threads with a lock
around the engine store"; SQLite serializes statements internally, and the
coarse lock additionally protects transaction boundaries). No ORM — stdlib
``sqlite3``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

try:
    import psycopg
except ImportError:
    psycopg = None

from .events import EVENT_TYPES, CostBlock, ErrorBlock, OtelContext, RedactionState, Source, TraceEvent
from .redaction import redact
from .evaluators import CaseScore, CaseStatus, RunAggregate
from .lifecycle import (
    ATTEMPT_TRANSITIONS,
    RUN_CASE_TRANSITIONS,
    RUN_TRANSITIONS,
    AttemptStatus,
    RunCaseStatus,
    RunStatus,
    SmokeState,
    transition,
)
from .spec import EvaluationSpec, Metric

__all__ = [
    "Storage",
    "ProjectRecord",
    "SmokeAttemptRecord",
    "RunRecord",
    "CaseRecord",
    "AttemptRecord",
    "CaseMetricResultRecord",
    "RunMetricResultRecord",
    "ScoreRevisionRecord",
    "CustomEvalRecord",
    "case_key",
]

#: Serializes every connection operation. The API (harness §17) runs runs in
#: background threads while request threads read the same store; SQLite
#: connections are not thread-safe to share, so one lock guards the shared
#: connection — the coarse lock also protects transaction boundaries (the
#: brief: "use threads with a lock around the engine store"). RLock so a
#: locked operation may nest (e.g. ``_tx``'s commit after an execute).
_CONNECTION_LOCK = threading.RLock()


class _LockedCursor:
    """A cursor that holds :data:`_CONNECTION_LOCK` from the statement's
    execute until its result set is consumed.

    pysqlite steps statements lazily: ``fetchone()``/``fetchall()``/iteration
    walk the statement on the C side, and stepping is exactly what races when
    another thread executes on the same connection. Locking only ``execute()``
    leaves the fetch gap open — observed failure modes under contention: torn
    row values (a JSON column coming back as the empty string), ``SELECT``s
    returning no rows for an existing record, and rows materializing as plain
    tuples. So the lock is acquired in ``execute()`` and released only when
    the result is fully materialized (``fetchall()``, iterator exhaustion),
    when the statement is exhausted (``fetchone()`` returning ``None``), when
    the cursor is closed, or when it is discarded (``__del__``). Statements
    with no result columns (DML/DDL) release immediately after executing."""

    __slots__ = ("_cursor", "_lock", "_held")

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock, held: bool):
        self._cursor = cursor
        self._lock = lock
        self._held = held
        if held and cursor.description is None:
            self._release()

    def _release(self) -> None:
        if getattr(self, "_held", False):
            self._held = False
            try:
                self._lock.release()
            except RuntimeError:
                pass  # interpreter shutdown may have torn the lock down

    def execute(self, *args: Any, **kwargs: Any) -> "_LockedCursor":
        if not self._held:
            self._lock.acquire()
            self._held = True
        try:
            self._cursor.execute(*args, **kwargs)
        except BaseException:
            self._release()
            raise
        if self._cursor.description is None:
            self._release()
        return self

    def fetchone(self) -> Any:
        try:
            row = self._cursor.fetchone()
        except BaseException:
            self._release()
            raise
        if row is None:
            # Statement exhausted — no further stepping can race.
            self._release()
        return row

    def fetchmany(self, size: int | None = None) -> list[Any]:
        try:
            if size is None:
                size = self._cursor.arraysize
            return self._cursor.fetchmany(size)
        except BaseException:
            self._release()
            raise

    def fetchall(self) -> list[Any]:
        try:
            rows = self._cursor.fetchall()
        except BaseException:
            self._release()
            raise
        self._release()  # fully materialized — the lock's job is done
        return rows

    def __iter__(self) -> Iterator[Any]:
        try:
            for row in self._cursor:
                yield row
        finally:
            self._release()

    def close(self) -> None:
        try:
            self._cursor.close()
        finally:
            self._release()

    def __del__(self) -> None:
        self._release()

    def __enter__(self) -> "_LockedCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        self._release()

    def __getattr__(self, name: str) -> Any:
        # description, rowcount, lastrowid, arraysize, connection, ...
        return getattr(self._cursor, name)


class _LockedConnection(sqlite3.Connection):
    """A :class:`sqlite3.Connection` shared across the API's request threads
    and background run threads. Every operation takes :data:`_CONNECTION_LOCK`;
    ``execute``/``executemany``/``executescript`` transfer the lock to a
    :class:`_LockedCursor` so the statement's lazy fetch is serialized too."""

    def execute(self, *args: Any, **kwargs: Any) -> Any:  # _LockedCursor (duck-typed Cursor proxy)
        _CONNECTION_LOCK.acquire()
        try:
            cur = super().execute(*args, **kwargs)
        except BaseException:
            _CONNECTION_LOCK.release()
            raise
        return _LockedCursor(cur, _CONNECTION_LOCK, held=True)

    def executemany(self, *args: Any, **kwargs: Any) -> Any:  # _LockedCursor (duck-typed Cursor proxy)
        _CONNECTION_LOCK.acquire()
        try:
            cur = super().executemany(*args, **kwargs)
        except BaseException:
            _CONNECTION_LOCK.release()
            raise
        return _LockedCursor(cur, _CONNECTION_LOCK, held=True)

    def executescript(self, *args: Any, **kwargs: Any) -> Any:  # _LockedCursor (duck-typed Cursor proxy)
        _CONNECTION_LOCK.acquire()
        try:
            cur = super().executescript(*args, **kwargs)
        except BaseException:
            _CONNECTION_LOCK.release()
            raise
        return _LockedCursor(cur, _CONNECTION_LOCK, held=True)

    def commit(self) -> None:
        with _CONNECTION_LOCK:
            return super().commit()

    def rollback(self) -> None:
        with _CONNECTION_LOCK:
            return super().rollback()

    def close(self) -> None:
        with _CONNECTION_LOCK:
            return super().close()


#: The 14 event types, §12B.1 (for the trace_events CHECK constraint).
_EVENT_TYPE_LIST = ", ".join(repr(t) for t in EVENT_TYPES)

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS projects (
  project_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  smoke_state TEXT NOT NULL CHECK (smoke_state IN
    ('INGESTED','ANALYZED','RUNTIME_PREPARED','SMOKE_PASSED','EVALUABLE',
     'entrypoint_missing','install_failed','invocation_failed',
     'provider_unreachable','no_trace','timeout')),
  smoke_failure_detail TEXT,
  entrypoint TEXT NOT NULL DEFAULT '[]',   -- JSON array; the declared entrypoint (§32A.5)
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS smoke_attempts (
  attempt_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  entrypoint TEXT NOT NULL,       -- JSON array: the command at attempt time
  exit_code INTEGER,
  state TEXT NOT NULL,            -- outcome: SMOKE_PASSED or one of the six failure states
  failure_detail TEXT,
  trace_path TEXT,                -- the smoke trace file (persisted evidence, §32A.3.4)
  reproduce_locally TEXT,         -- §32A.3.6: the exact command to reproduce
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  project_id TEXT,                -- nullable: MVP runs may be project-less
  spec_json TEXT NOT NULL,        -- frozen EvaluationSpec (evaluation_versions.spec equivalent)
  agent_version TEXT NOT NULL,    -- JSON {{source_digest, entrypoint, cwd}}
  status TEXT NOT NULL CHECK (status IN
    ('draft','queued','provisioning','running','aggregating',
     'complete','failed','cancelled','incomplete')),
  tier TEXT NOT NULL CHECK (tier IN ('quick','standard','full')),
  repeat_config TEXT NOT NULL,    -- JSON {{repeats, retry:{{max,authoritative}}, flakiness}} (§11C)
  world_config TEXT NOT NULL,     -- JSON world model snapshot (§10A)
  judge_binding TEXT,             -- JSON; null when the spec has no judge metrics
  mixed_binding INTEGER NOT NULL DEFAULT 0,
  case_count INTEGER NOT NULL,
  concurrency INTEGER NOT NULL,
  estimated_cost_usd TEXT,        -- JSON {{min,max}}; null in the MVP (no price table)
  budget_usd_micros INTEGER NOT NULL,
  progress TEXT NOT NULL DEFAULT '{{}}',
  created_by TEXT,
  run_manifest_ref TEXT,
  run_started_at TEXT,
  run_completed_at TEXT,
  created_at TEXT NOT NULL,
  idempotency_key TEXT,           -- MVP: idempotent create_run (§31C.2)
  UNIQUE (workspace_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS runs_workspace_created_idx ON runs (workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS run_cases (
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  case_key TEXT NOT NULL,         -- §11C.5: sha256(normalized input); stable across versions
  workspace_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('queued','running','completed','failed','cancelled','skipped')),
  repeat_count INTEGER NOT NULL,
  classification TEXT,            -- PASSING | FLAKY | FAILING | ERRORED (after repeats)
  error_category TEXT,            -- infra | agent | evaluator
  started_at TEXT,
  completed_at TEXT,
  PRIMARY KEY (run_id, case_id)
);
CREATE INDEX IF NOT EXISTS run_cases_case_key_idx ON run_cases (case_key, run_id);

CREATE TABLE IF NOT EXISTS run_case_attempts (
  attempt_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  repeat_index INTEGER NOT NULL,  -- 0..repeats-1
  attempt INTEGER NOT NULL DEFAULT 0,  -- 0 = first; >0 = retry
  is_first_attempt INTEGER NOT NULL,   -- attempt == 0; "passed on retry" cannot re-enter
  status TEXT NOT NULL CHECK (status IN
    ('queued','running','completed','timed_out','budget_exceeded',
     'cancelled','orphaned','errored')),
  exit_code INTEGER,
  event_count INTEGER NOT NULL DEFAULT 0,
  trace_truncated INTEGER NOT NULL DEFAULT 0,
  ingest_token_hash TEXT,
  container_id TEXT,
  error TEXT,                     -- MVP: the failure message (why the attempt is not completed)
  started_at TEXT,
  finished_at TEXT,
  UNIQUE (run_id, case_id, repeat_index, attempt)
);
CREATE INDEX IF NOT EXISTS run_case_attempts_case_idx ON run_case_attempts (run_id, case_id);

-- trace_events — the hottest table. SQLite: no LIST partitioning; per-run
-- trace sets are modelled with run_id in the PK + per-run indexes (§18.3).
CREATE TABLE IF NOT EXISTS trace_events (
  event_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,          -- MVP: every event carries a case (no run-level events yet)
  attempt_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  repeat_index INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  sequence INTEGER NOT NULL,      -- ingest order within the attempt
  type TEXT NOT NULL CHECK (type IN ({_EVENT_TYPE_LIST})),
  source TEXT NOT NULL CHECK (source IN ('proxy','adapter','runner')),
  timestamp TEXT NOT NULL,        -- proxy wall clock (authority, engine §12B)
  duration_ms INTEGER,
  parent_event_id TEXT,
  provider_request_id TEXT,
  tool TEXT,                      -- typed column for tool_call/tool_result (§12B.2)
  otel TEXT,                      -- JSON {{trace_id, span_id}}
  cost_usd_micros INTEGER,        -- typed column (L10)
  price_version TEXT,             -- §12B.2 str; §18 DDL says integer — the built layer wins
  cost_json TEXT,                 -- MVP: full §12B cost block preserved for re-score
  redaction_state TEXT NOT NULL,  -- JSON {{status, rules[]}}; the engine rejects events without it
  payload TEXT,                   -- JSON, redacted; stored inline in the MVP (§12B.5)
  payload_ref TEXT,
  error TEXT,                     -- JSON {{type, message, retryable}}
  PRIMARY KEY (run_id, event_id)
);
CREATE INDEX IF NOT EXISTS trace_events_case_idx ON trace_events (run_id, case_id, sequence);
CREATE INDEX IF NOT EXISTS trace_events_attempt_idx ON trace_events (run_id, attempt_id);

CREATE TABLE IF NOT EXISTS cost_summaries (
  run_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  price_version TEXT NOT NULL,        -- §12A.5: totals survive price drift only keyed by version
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  usd_micros INTEGER NOT NULL,
  PRIMARY KEY (run_id, price_version)
);

CREATE TABLE IF NOT EXISTS run_case_metric_results (
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  score_revision INTEGER NOT NULL DEFAULT 1,  -- idempotent; last 5 retained (§13A.1.3)
  status TEXT NOT NULL CHECK (status IN ('PASS','FAIL','ERROR','SKIPPED')),
  score REAL,
  raw_value TEXT,                 -- JSON; NULL in the MVP (CaseScore does not carry it yet)
  evidence_snapshot_id TEXT,
  evidence_event_ids TEXT,        -- JSON array of event ids (§9A evidence link)
  evaluator_version TEXT,
  judge_binding TEXT,             -- JSON when judge-sourced (§15A)
  is_authoritative INTEGER NOT NULL DEFAULT 1,  -- computed from the first attempt
  on_retry_override INTEGER NOT NULL DEFAULT 0, -- "passed on retry" marker
  overridden INTEGER NOT NULL DEFAULT 0,        -- §13A.1.2 ordering flag; row itself never modified
  computed_at TEXT NOT NULL,
  PRIMARY KEY (run_id, case_id, metric_id, score_revision)
);
CREATE INDEX IF NOT EXISTS rcmr_metric_idx ON run_case_metric_results (run_id, metric_id, score_revision);

CREATE TABLE IF NOT EXISTS run_metric_results (
  run_id TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  score_revision INTEGER NOT NULL DEFAULT 1,
  value REAL,                     -- §18 says NOT NULL; nullable here for empty denominators
  ci_lower REAL,
  ci_upper REAL,
  unit TEXT,
  aggregation_state TEXT NOT NULL CHECK (aggregation_state IN
    ('IN_PROGRESS','PARTIAL','COMPLETE')),   -- L11; separate from runs.status
  method TEXT NOT NULL CHECK (method IN ('pass_rate','mean','p50','p95','min','sum')),
  on_error TEXT NOT NULL CHECK (on_error IN ('fail','exclude')),
  sample_n INTEGER NOT NULL,
  error_n INTEGER NOT NULL DEFAULT 0,
  skipped_n INTEGER NOT NULL DEFAULT 0,
  gate_status TEXT CHECK (gate_status IN ('PASS','FAIL','NOT_APPLICABLE')),
  no_ci INTEGER NOT NULL DEFAULT 0,   -- repeats < 3 (§11C.4)
  computed_at TEXT NOT NULL,
  PRIMARY KEY (run_id, metric_id, score_revision)
);

-- score_revisions — §13A.1.2: overrides stored ALONGSIDE machine scores,
-- never replacing them. One row per (run, case, metric, revision); appends
-- are idempotent by PK; a diff between revisions is the standard re-score
-- output (§13A.1.3).
CREATE TABLE IF NOT EXISTS score_revisions (
  run_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  metric_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  score_revision INTEGER NOT NULL,
  machine_score_snapshot TEXT NOT NULL,   -- JSON: the machine value, preserved
  override_value TEXT,                    -- JSON: the human value, rendered side by side
  dispute_path TEXT CHECK (dispute_path IN ('rule_wrong','evidence_missing','judgment_wrong')),
  reason TEXT,
  author_id TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, case_id, metric_id, score_revision)
);

CREATE TABLE IF NOT EXISTS custom_evals (
  eval_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  name TEXT NOT NULL,
  project_id TEXT,
  spec_json TEXT NOT NULL,
  dashboard_json TEXT NOT NULL,
  dataset_json TEXT NOT NULL,
  source_path TEXT,
  entrypoint TEXT NOT NULL DEFAULT '[]',
  cwd TEXT,
  run_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS custom_evals_workspace_idx ON custom_evals (workspace_id, created_at DESC);
"""

#: gate_status values (§18 DDL).
GATE_STATUSES = ("PASS", "FAIL", "NOT_APPLICABLE")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def case_key(case_input: Mapping[str, Any]) -> str:
    """§11C.5: ``sha256(normalized input)`` — JSON keys sorted, whitespace
    collapsed. Stable across dataset versions (depends only on the input)."""
    normalized = json.dumps(case_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    workspace_id: str
    name: str
    smoke_state: str
    smoke_failure_detail: str | None
    entrypoint: tuple[str, ...]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class SmokeAttemptRecord:
    attempt_id: str
    workspace_id: str
    project_id: str
    entrypoint: tuple[str, ...]
    exit_code: int | None
    state: str
    failure_detail: str | None
    trace_path: str | None
    reproduce_locally: str
    created_at: str


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    workspace_id: str
    project_id: str | None
    spec: EvaluationSpec
    spec_json: str
    agent_version: dict[str, Any]
    status: str
    tier: str
    repeat_config: dict[str, Any]
    world_config: dict[str, Any]
    judge_binding: dict[str, Any] | None
    mixed_binding: bool
    case_count: int
    concurrency: int
    estimated_cost_usd: dict[str, Any] | None
    budget_usd_micros: int
    progress: dict[str, Any]
    entrypoint: tuple[str, ...]  # from agent_version; the §11A command
    cwd: str | None
    run_started_at: str | None
    run_completed_at: str | None
    created_at: str
    idempotency_key: str | None

    @property
    def repeats(self) -> int:
        return int(self.repeat_config.get("repeats", 1))

    @property
    def retry_max(self) -> int:
        retry = self.repeat_config.get("retry") or {}
        return int(retry.get("max", 0))

    @property
    def timeout_seconds(self) -> float:
        return float(self.agent_version.get("timeout_seconds", 120.0))


@dataclass(frozen=True)
class CaseRecord:
    run_id: str
    case_id: str
    case_key: str
    workspace_id: str
    status: str
    repeat_count: int
    classification: str | None
    error_category: str | None
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    run_id: str
    case_id: str
    workspace_id: str
    repeat_index: int
    attempt: int
    is_first_attempt: bool
    status: str
    exit_code: int | None
    event_count: int
    trace_truncated: bool
    error: str | None
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class CaseMetricResultRecord:
    run_id: str
    case_id: str
    metric_id: str
    workspace_id: str
    score_revision: int
    status: str
    score: float | None
    raw_value: dict[str, Any] | None
    evidence_event_ids: tuple[str, ...]
    judge_binding: dict[str, Any] | None
    is_authoritative: bool
    on_retry_override: bool
    overridden: bool
    computed_at: str


@dataclass(frozen=True)
class RunMetricResultRecord:
    run_id: str
    metric_id: str
    workspace_id: str
    score_revision: int
    value: float | None
    ci_lower: float | None
    ci_upper: float | None
    unit: str | None
    aggregation_state: str
    method: str
    on_error: str
    sample_n: int
    error_n: int
    skipped_n: int
    gate_status: str | None
    no_ci: bool
    computed_at: str


@dataclass(frozen=True)
class ScoreRevisionRecord:
    run_id: str
    case_id: str
    metric_id: str
    workspace_id: str
    score_revision: int
    machine_score_snapshot: dict[str, Any]
    override_value: dict[str, Any] | None
    dispute_path: str | None
    reason: str | None
    author_id: str | None
    status: str
    created_at: str


@dataclass(frozen=True)
class RunPlanRecord:
    plan_id: str
    workspace_id: str
    content_digest: str
    state: str
    content: dict[str, Any]
    blockers: tuple[str, ...]
    actor_id: str
    created_at: str


@dataclass(frozen=True)
class RunAuthorizationRecord:
    authorization_id: str
    workspace_id: str
    plan_id: str
    plan_hash: str
    actor_id: str
    state: str
    expires_at: str
    created_at: str


@dataclass(frozen=True)
class CustomEvalRecord:
    eval_id: str
    workspace_id: str
    name: str
    project_id: str | None
    spec: dict[str, Any]
    dashboard: dict[str, Any]
    dataset: list[dict[str, Any]]
    source_path: str | None
    entrypoint: list[str]
    cwd: str | None
    run_id: str | None
    created_at: str
    updated_at: str


def _psycopg_row_factory(cursor: Any) -> Any:
    if cursor.description is None:
        return lambda values: values
    cols = [c.name for c in cursor.description]
    def make_row(values: tuple[Any, ...]) -> Any:
        d = dict(zip(cols, values))
        class RowProxy(dict):
            def __getitem__(self, key: Any) -> Any:
                if isinstance(key, int):
                    return values[key]
                return super().__getitem__(key)
        return RowProxy(d)
    return make_row


class _PostgresConnectionWrapper:
    """A wrapper around psycopg connection that accepts SQLite-style '?' placeholders
    and converts them to PostgreSQL '%s' placeholders, handling ON CONFLICT."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @staticmethod
    def _convert_sql(sql: str) -> str:
        sql_pg = sql.replace("?", "%s")
        if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO\s+", sql_pg, flags=re.IGNORECASE):
            sql_pg = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO\s+", "INSERT INTO ", sql_pg, flags=re.IGNORECASE).rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        sql_pg = sql_pg.replace("ORDER BY rowid", "ORDER BY case_id")
        return sql_pg

    def execute(self, sql: str, params: Any = ()) -> Any:
        sql_pg = self._convert_sql(sql)
        cur = self._conn.cursor()
        cur.execute(sql_pg, params)
        return cur

    def executemany(self, sql: str, params_seq: Any) -> Any:
        sql_pg = self._convert_sql(sql)
        cur = self._conn.cursor()
        cur.executemany(sql_pg, params_seq)
        return cur

    def executescript(self, sql: str) -> Any:
        cur = self._conn.cursor()
        cur.execute(sql)
        return cur

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _workspace_scoped(method):
    """Scope existing repository calls as well as new workflow repositories.

    Materialize streaming reads before releasing the transaction. Explicit
    workspace arguments remain the trusted service boundary; HTTP obtains
    them from authenticated context, never from request content.
    """
    signature = inspect.signature(method)

    @wraps(method)
    def scoped(self, *args, **kwargs):
        workspace_id = signature.bind(self, *args, **kwargs).arguments["workspace_id"]
        with self.workspace_transaction(workspace_id):
            result = method(self, *args, **kwargs)
            return list(result) if inspect.isgeneratorfunction(method) else result
    return scoped


class Storage:
    """Persistence layer supporting both local SQLite and production PostgreSQL (§18)."""

    def __init__(self, db_path: str | Path):
        self._db_path = str(db_path)
        self._tx_depth = 0
        self._scope_workspace = None
        if self._db_path.startswith(("postgresql://", "postgres://")):
            if psycopg is None:
                raise RuntimeError("psycopg is required for PostgreSQL storage (pip install 'psycopg[binary]')")
            self._is_postgres = True
            pg_conn = psycopg.connect(self._db_path, row_factory=_psycopg_row_factory, autocommit=True)
            self._conn = _PostgresConnectionWrapper(pg_conn)
        else:
            self._is_postgres = False
            self._conn = sqlite3.connect(
                self._db_path, check_same_thread=False, factory=_LockedConnection
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterable[None]:
        # The lock spans the whole transaction, not just the DML: otherwise a
        # second thread's statement can join this thread's implicit
        # transaction, and a rollback here would silently destroy that
        # thread's uncommitted write.
        with _CONNECTION_LOCK:
            depth = self._tx_depth
            savepoint = f"storage_nested_{depth}"
            self._conn.execute(
                ("BEGIN" if self._is_postgres else "BEGIN IMMEDIATE") if depth == 0
                else f"SAVEPOINT {savepoint}"
            )
            self._tx_depth += 1
            try:
                yield
                self._conn.execute("COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                if depth == 0:
                    self._conn.rollback()
                else:
                    self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            finally:
                self._tx_depth -= 1

    @contextmanager
    def workspace_transaction(self, workspace_id: str):
        """Transaction-local PostgreSQL RLS context; SQLite is application scoping.

        No session-level SET survives connection reuse. Nested service calls
        cannot switch tenant midway through an operation.
        """
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace identity is required")
        with _CONNECTION_LOCK:
            previous = self._scope_workspace
            if previous is not None and previous != workspace_id:
                raise ValueError("cannot switch workspace inside a transaction")
            self._scope_workspace = workspace_id
            try:
                with self._tx():
                    if self._is_postgres and previous is None:
                        self._conn.execute("SELECT set_config('app.workspace_id', ?, true)", (workspace_id,)).fetchall()
                    yield self._conn
            finally:
                self._scope_workspace = previous

    def create_schema(self, *, migrate: bool = False) -> None:
        """Create tables under maintenance credentials, or open a ready app DB.

        The running API uses a non-bypass role.  PostgreSQL checks CREATE
        privileges even for ``CREATE TABLE IF NOT EXISTS``, so an app process
        must only verify that its already-migrated schema exists rather than
        attempting the bootstrap DDL at every startup.
        """
        if self._is_postgres and not migrate:
            existing_base = self._conn.execute("SELECT to_regclass('projects')").fetchone()[0] is not None
            existing_workflow = self._conn.execute("SELECT to_regclass('object_versions')").fetchone()[0] is not None
            existing_jobs = self._conn.execute("SELECT to_regclass('jobs')").fetchone()[0] is not None
            existing_plans = self._conn.execute("SELECT to_regclass('run_plans')").fetchone()[0] is not None
            if existing_base and existing_workflow and existing_jobs and existing_plans:
                return
            if existing_base and existing_workflow and existing_jobs and not existing_plans:
                raise RuntimeError("database migration required; run the maintenance bootstrap")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        from .migrations import install_workflow_schema
        exists = self._is_postgres and self._conn.execute("SELECT to_regclass('jobs')").fetchone()[0] is not None
        if not exists or migrate or not self._is_postgres:
            install_workflow_schema(self._conn, postgres=self._is_postgres)
            self._conn.commit()

    @_workspace_scoped
    def create_run_plan(self, *, workspace_id: str, plan_id: str, content_digest: str,
                        state: str, content: dict[str, Any], blockers: Sequence[str],
                        actor_id: str, created_at: str | None = None) -> RunPlanRecord:
        if state not in {"validated", "blocked"}:
            raise ValueError("invalid run plan state")
        created_at = created_at or _now()
        with self._tx():
            self._conn.execute(
                "INSERT INTO run_plans (workspace_id,plan_id,content_digest,state,content_json,blockers_json,actor_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (workspace_id, plan_id, content_digest, state, json.dumps(content, sort_keys=True),
                 json.dumps(list(blockers)), actor_id, created_at),
            )
        return self.get_run_plan(plan_id, workspace_id)

    @_workspace_scoped
    def get_run_plan(self, plan_id: str, workspace_id: str) -> RunPlanRecord | None:
        row = self._conn.execute(
            "SELECT * FROM run_plans WHERE workspace_id=? AND plan_id=?", (workspace_id, plan_id)
        ).fetchone()
        if row is None:
            return None
        return RunPlanRecord(
            plan_id=row["plan_id"], workspace_id=row["workspace_id"],
            content_digest=row["content_digest"], state=row["state"],
            content=json.loads(row["content_json"]),
            blockers=tuple(json.loads(row["blockers_json"])),
            actor_id=row["actor_id"], created_at=row["created_at"],
        )

    @_workspace_scoped
    def create_run_authorization(self, *, workspace_id: str, authorization_id: str,
                                 plan_id: str, plan_hash: str, actor_id: str,
                                 state: str, expires_at: str, created_at: str | None = None) -> RunAuthorizationRecord:
        if state not in {"authorized", "expired", "revoked"}:
            raise ValueError("invalid authorization state")
        created_at = created_at or _now()
        with self._tx():
            self._conn.execute(
                "INSERT INTO run_authorizations (workspace_id,authorization_id,plan_id,plan_hash,actor_id,state,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (workspace_id, authorization_id, plan_id, plan_hash, actor_id, state, expires_at, created_at),
            )
        return self.get_run_authorization(authorization_id, workspace_id)

    @_workspace_scoped
    def get_run_authorization(self, authorization_id: str, workspace_id: str) -> RunAuthorizationRecord | None:
        row = self._conn.execute(
            "SELECT * FROM run_authorizations WHERE workspace_id=? AND authorization_id=?",
            (workspace_id, authorization_id),
        ).fetchone()
        if row is None:
            return None
        return RunAuthorizationRecord(
            authorization_id=row["authorization_id"], workspace_id=row["workspace_id"],
            plan_id=row["plan_id"], plan_hash=row["plan_hash"], actor_id=row["actor_id"],
            state=row["state"], expires_at=row["expires_at"], created_at=row["created_at"],
        )

    @_workspace_scoped
    def expire_run_authorization(self, authorization_id: str, workspace_id: str) -> None:
        with self._tx():
            self._conn.execute(
                "UPDATE run_authorizations SET state='expired' WHERE workspace_id=? AND authorization_id=? AND state='authorized'",
                (workspace_id, authorization_id),
            )

    # ------------------------------------------------------------------
    # Projects and the smoke gate (harness §32A)
    # ------------------------------------------------------------------

    @_workspace_scoped
    def create_project(
        self,
        *,
        workspace_id: str,
        name: str,
        entrypoint: Sequence[str] = (),
    ) -> ProjectRecord:
        now = _now()
        project_id = uuid.uuid4().hex
        with self._tx():
            self._conn.execute(
                "INSERT INTO projects (project_id, workspace_id, name, smoke_state,"
                "  entrypoint, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, workspace_id, name, SmokeState.INGESTED.value,
                 json.dumps(list(entrypoint)), now, now),
            )
        return self.get_project(project_id, workspace_id)

    @_workspace_scoped
    def get_project(self, project_id: str, workspace_id: str) -> ProjectRecord | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE project_id = ? AND workspace_id = ?",
            (project_id, workspace_id),
        ).fetchone()
        return _project_from_row(row) if row is not None else None

    @_workspace_scoped
    def set_project_smoke_state(
        self,
        project_id: str,
        workspace_id: str,
        to: SmokeState | str,
        *,
        failure_detail: str | None = None,
    ) -> ProjectRecord:
        """Advance the project's smoke state under §32A's legal transitions.
        Illegal jumps raise IllegalTransition and are never persisted."""
        current = self.get_project(project_id, workspace_id)
        if current is None:
            raise KeyError(f"project {project_id!r} not found in workspace {workspace_id!r}")
        target = transition(SmokeState(current.smoke_state), to, _SMOKE_TABLE)
        now = _now()
        with self._tx():
            self._conn.execute(
                "UPDATE projects SET smoke_state = ?, smoke_failure_detail = ?,"
                "  updated_at = ? WHERE project_id = ? AND workspace_id = ?",
                (target.value, failure_detail, now, project_id, workspace_id),
            )
        return self.get_project(project_id, workspace_id)

    @_workspace_scoped
    def set_project_entrypoint(
        self, project_id: str, workspace_id: str, entrypoint: Sequence[str]
    ) -> ProjectRecord:
        now = _now()
        with self._tx():
            self._conn.execute(
                "UPDATE projects SET entrypoint = ?, updated_at = ?"
                " WHERE project_id = ? AND workspace_id = ?",
                (json.dumps(list(entrypoint)), now, project_id, workspace_id),
            )
        return self.get_project(project_id, workspace_id)

    @_workspace_scoped
    def append_smoke_attempt(
        self,
        *,
        workspace_id: str,
        project_id: str,
        entrypoint: Sequence[str],
        state: SmokeState | str,
        exit_code: int | None = None,
        failure_detail: str | None = None,
        trace_path: str | None = None,
        reproduce_locally: str | None = None,
    ) -> SmokeAttemptRecord:
        """§32A.3.5: every smoke attempt appends a row; the full history shows
        in the recovery UI, never just the latest failure."""
        attempt_id = uuid.uuid4().hex
        with self._tx():
            self._conn.execute(
                "INSERT INTO smoke_attempts (attempt_id, workspace_id, project_id,"
                "  entrypoint, exit_code, state, failure_detail, trace_path,"
                "  reproduce_locally, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, workspace_id, project_id, json.dumps(list(entrypoint)),
                 exit_code, SmokeState(state).value, failure_detail, trace_path,
                 reproduce_locally, _now()),
            )
        row = self._conn.execute(
            "SELECT * FROM smoke_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        return _smoke_attempt_from_row(row)

    @_workspace_scoped
    def list_smoke_attempts(
        self, project_id: str, workspace_id: str, *, limit: int = 50
    ) -> list[SmokeAttemptRecord]:
        rows = self._conn.execute(
            "SELECT * FROM smoke_attempts WHERE project_id = ? AND workspace_id = ?"
            " ORDER BY created_at DESC LIMIT ?",
            (project_id, workspace_id, limit),
        ).fetchall()
        return [_smoke_attempt_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    @_workspace_scoped
    def create_run(
        self,
        *,
        workspace_id: str,
        spec_json: str,
        agent_version: dict[str, Any],
        tier: str,
        repeat_config: dict[str, Any],
        world_config: dict[str, Any],
        case_count: int,
        concurrency: int,
        budget_usd_micros: int,
        project_id: str | None = None,
        judge_binding: dict[str, Any] | None = None,
        mixed_binding: bool = False,
        estimated_cost_usd: dict[str, Any] | None = None,
        created_by: str | None = None,
        run_manifest_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> RunRecord:
        """Insert a run. Idempotency-key guarded (§31C.2): a retried create
        with the same ``(workspace_id, idempotency_key)`` returns the existing
        run, never a duplicate."""
        if idempotency_key is not None:
            existing = self._get_run_by_idempotency_key(workspace_id, idempotency_key)
            if existing is not None:
                return existing
        run_id = uuid.uuid4().hex
        now = _now()
        values = (
            run_id, workspace_id, project_id, spec_json, json.dumps(agent_version),
            RunStatus.DRAFT.value, tier, json.dumps(repeat_config),
            json.dumps(world_config),
            json.dumps(judge_binding) if judge_binding is not None else None,
            1 if mixed_binding else 0,
            case_count, concurrency,
            json.dumps(estimated_cost_usd) if estimated_cost_usd is not None else None,
            budget_usd_micros, "{}", created_by, run_manifest_ref, now, idempotency_key,
        )
        try:
            with self._tx():
                self._conn.execute(
                    "INSERT INTO runs (run_id, workspace_id, project_id, spec_json,"
                    "  agent_version, status, tier, repeat_config, world_config,"
                    "  judge_binding, mixed_binding, case_count, concurrency,"
                    "  estimated_cost_usd, budget_usd_micros, progress, created_by,"
                    "  run_manifest_ref, created_at, idempotency_key)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
        except sqlite3.IntegrityError:
            # UNIQUE (workspace_id, idempotency_key) hit between the check and
            # the insert (or, with no key, a genuine constraint violation).
            if idempotency_key is None:
                raise
            existing = self._get_run_by_idempotency_key(workspace_id, idempotency_key)
            if existing is None:
                raise
            return existing
        run = self.get_run(run_id, workspace_id)
        if run is None:  # pragma: no cover - defensive
            raise RuntimeError(f"run {run_id!r} was not persisted")
        return run

    @_workspace_scoped
    def _get_run_by_idempotency_key(
        self, workspace_id: str, idempotency_key: str
    ) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE workspace_id = ? AND idempotency_key = ?",
            (workspace_id, idempotency_key),
        ).fetchone()
        return _run_from_row(row) if row is not None else None

    @_workspace_scoped
    def get_run_by_idempotency_key(
        self, workspace_id: str, idempotency_key: str
    ) -> RunRecord | None:
        """§17.6 — the run created under ``idempotency_key``, if any. The API
        uses this to detect replays (identical payloads return the original)
        versus conflicts (the same key with a different payload is a 409)."""
        return self._get_run_by_idempotency_key(workspace_id, idempotency_key)

    @_workspace_scoped
    def get_run(self, run_id: str, workspace_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ? AND workspace_id = ?",
            (run_id, workspace_id),
        ).fetchone()
        return _run_from_row(row) if row is not None else None

    @_workspace_scoped
    def list_runs(
        self, workspace_id: str, *, limit: int = 50, status: str | None = None
    ) -> list[RunRecord]:
        if status is not None:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE workspace_id = ? AND status = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (workspace_id, status, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE workspace_id = ?"
                " ORDER BY created_at DESC LIMIT ?",
                (workspace_id, limit),
            ).fetchall()
        return [_run_from_row(r) for r in rows]

    @_workspace_scoped
    def list_runs_cursor(
        self,
        workspace_id: str,
        *,
        limit: int = 50,
        cursor: tuple[str, str] | None = None,
    ) -> tuple[list[RunRecord], tuple[str, str] | None]:
        """§17.9 — cursor pagination over runs with a stable order
        ``(created_at DESC, run_id DESC)``.

        ``cursor`` is the ``(created_at, run_id)`` pair of the last item of
        the previous page (the API encodes it opaquely as ``next_cursor``).
        Returns ``(page, next_cursor)`` — ``next_cursor`` is None when the
        page is the last. The keyset comparison keeps pages stable across
        concurrent creates (no offset drift).
        """
        if not 1 <= limit <= 500:
            raise ValueError("limit must be within 1..500")
        sql = "SELECT * FROM runs WHERE workspace_id = ?"
        params: list[Any] = [workspace_id]
        if cursor is not None:
            sql += " AND (created_at < ? OR (created_at = ? AND run_id < ?))"
            params += [cursor[0], cursor[0], cursor[1]]
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT ?"
        params.append(limit + 1)
        rows = self._conn.execute(sql, params).fetchall()
        page = [_run_from_row(r) for r in rows[:limit]]
        next_cursor: tuple[str, str] | None = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = (last.created_at, last.run_id)
        return page, next_cursor

    @_workspace_scoped
    def set_run_status(
        self, run_id: str, workspace_id: str, to: RunStatus | str
    ) -> RunRecord:
        """Legal-transition-enforcing status update (engine §11A lifecycle).
        ``run_started_at``/``run_completed_at`` are stamped by the machine."""
        run = self.get_run(run_id, workspace_id)
        if run is None:
            raise KeyError(f"run {run_id!r} not found in workspace {workspace_id!r}")
        target = transition(RunStatus(run.status), to, RUN_TRANSITIONS)
        now = _now()
        started_at = run.run_started_at
        completed_at = run.run_completed_at
        if target is RunStatus.RUNNING and started_at is None:
            started_at = now
        if target.value in _RUN_TERMINAL:
            completed_at = now
        with self._tx():
            self._conn.execute(
                "UPDATE runs SET status = ?, run_started_at = ?, run_completed_at = ?"
                " WHERE run_id = ? AND workspace_id = ?",
                (target.value, started_at, completed_at, run_id, workspace_id),
            )
        return self.get_run(run_id, workspace_id)

    @_workspace_scoped
    def set_run_progress(
        self, run_id: str, workspace_id: str, progress: dict[str, Any]
    ) -> RunRecord:
        with self._tx():
            self._conn.execute(
                "UPDATE runs SET progress = ? WHERE run_id = ? AND workspace_id = ?",
                (json.dumps(progress), run_id, workspace_id),
            )
        return self.get_run(run_id, workspace_id)

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------

    @_workspace_scoped
    def insert_cases(
        self, run_id: str, workspace_id: str, cases: Sequence[TestCase], *, repeat_count: int
    ) -> None:
        """Batch insert of a run's cases (idempotent by PK (run_id, case_id)).
        ``case_key`` is §11C.5's sha256 of the normalized input."""
        rows = [
            (run_id, c.case_id, case_key(c.input), workspace_id,
             RunCaseStatus.QUEUED.value, repeat_count, None, None, None, None)
            for c in cases
        ]
        with self._tx():
            self._conn.executemany(
                "INSERT OR IGNORE INTO run_cases (run_id, case_id, case_key,"
                "  workspace_id, status, repeat_count, classification, error_category,"
                "  started_at, completed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    @_workspace_scoped
    def get_case(self, run_id: str, case_id: str, workspace_id: str) -> CaseRecord | None:
        row = self._conn.execute(
            "SELECT * FROM run_cases WHERE run_id = ? AND case_id = ? AND workspace_id = ?",
            (run_id, case_id, workspace_id),
        ).fetchone()
        return _case_from_row(row) if row is not None else None

    @_workspace_scoped
    def list_cases(self, run_id: str, workspace_id: str) -> list[CaseRecord]:
        rows = self._conn.execute(
            "SELECT * FROM run_cases WHERE run_id = ? AND workspace_id = ?"
            " ORDER BY rowid",
            (run_id, workspace_id),
        ).fetchall()
        return [_case_from_row(r) for r in rows]

    @_workspace_scoped
    def set_case_status(
        self, run_id: str, case_id: str, workspace_id: str, to: RunCaseStatus | str
    ) -> CaseRecord:
        current = self.get_case(run_id, case_id, workspace_id)
        if current is None:
            raise KeyError(f"case {case_id!r} not in run {run_id!r}")
        target = transition(RunCaseStatus(current.status), to, RUN_CASE_TRANSITIONS)
        now = _now()
        started_at = current.started_at
        completed_at = current.completed_at
        if target is RunCaseStatus.RUNNING and started_at is None:
            started_at = now
        if target.value in _CASE_TERMINAL:
            completed_at = now
        with self._tx():
            self._conn.execute(
                "UPDATE run_cases SET status = ?, started_at = ?, completed_at = ?"
                " WHERE run_id = ? AND case_id = ? AND workspace_id = ?",
                (target.value, started_at, completed_at, run_id, case_id, workspace_id),
            )
        return self.get_case(run_id, case_id, workspace_id)

    @_workspace_scoped
    def set_case_classification(
        self,
        run_id: str,
        case_id: str,
        workspace_id: str,
        classification: str | None,
        error_category: str | None = None,
    ) -> CaseRecord:
        with self._tx():
            self._conn.execute(
                "UPDATE run_cases SET classification = ?, error_category = ?"
                " WHERE run_id = ? AND case_id = ? AND workspace_id = ?",
                (classification, error_category, run_id, case_id, workspace_id),
            )
        return self.get_case(run_id, case_id, workspace_id)

    # ------------------------------------------------------------------
    # Attempts
    # ------------------------------------------------------------------

    @_workspace_scoped
    def create_attempt(
        self,
        *,
        run_id: str,
        case_id: str,
        workspace_id: str,
        repeat_index: int,
        attempt: int,
    ) -> AttemptRecord:
        attempt_id = uuid.uuid4().hex
        with self._tx():
            self._conn.execute(
                "INSERT INTO run_case_attempts (attempt_id, run_id, case_id,"
                "  workspace_id, repeat_index, attempt, is_first_attempt, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, run_id, case_id, workspace_id, repeat_index, attempt,
                 1 if attempt == 0 else 0, AttemptStatus.QUEUED.value),
            )
        return self.get_attempt_by_id(attempt_id, workspace_id)

    @_workspace_scoped
    def get_attempt_by_id(self, attempt_id: str, workspace_id: str) -> AttemptRecord | None:
        row = self._conn.execute(
            "SELECT * FROM run_case_attempts WHERE attempt_id = ? AND workspace_id = ?",
            (attempt_id, workspace_id),
        ).fetchone()
        return _attempt_from_row(row) if row is not None else None

    @_workspace_scoped
    def get_attempt(
        self,
        *,
        run_id: str,
        case_id: str,
        workspace_id: str,
        repeat_index: int,
        attempt: int,
    ) -> AttemptRecord | None:
        row = self._conn.execute(
            "SELECT * FROM run_case_attempts WHERE run_id = ? AND case_id = ?"
            " AND workspace_id = ? AND repeat_index = ? AND attempt = ?",
            (run_id, case_id, workspace_id, repeat_index, attempt),
        ).fetchone()
        return _attempt_from_row(row) if row is not None else None

    @_workspace_scoped
    def list_attempts(
        self, run_id: str, workspace_id: str, case_id: str | None = None
    ) -> list[AttemptRecord]:
        if case_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM run_case_attempts WHERE run_id = ? AND workspace_id = ?"
                " AND case_id = ? ORDER BY repeat_index, attempt",
                (run_id, workspace_id, case_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM run_case_attempts WHERE run_id = ? AND workspace_id = ?"
                " ORDER BY repeat_index, attempt",
                (run_id, workspace_id),
            ).fetchall()
        return [_attempt_from_row(r) for r in rows]

    @_workspace_scoped
    def set_attempt_status(
        self,
        attempt_id: str,
        workspace_id: str,
        to: AttemptStatus | str,
        *,
        exit_code: int | None = None,
        event_count: int | None = None,
        trace_truncated: bool | None = None,
        error: str | None = None,
    ) -> AttemptRecord:
        """Legal-transition-enforcing attempt status update (§13A.2).
        ``started_at``/``finished_at`` are stamped by the machine."""
        current = self.get_attempt_by_id(attempt_id, workspace_id)
        if current is None:
            raise KeyError(f"attempt {attempt_id!r} not found in workspace {workspace_id!r}")
        target = transition(AttemptStatus(current.status), to, ATTEMPT_TRANSITIONS)
        now = _now()
        started_at = current.started_at
        finished_at = current.finished_at
        if target is AttemptStatus.RUNNING and started_at is None:
            started_at = now
        if _attempt_is_terminal(target):
            finished_at = now
        with self._tx():
            self._conn.execute(
                "UPDATE run_case_attempts SET status = ?, exit_code = ?,"
                "  event_count = ?, trace_truncated = ?, error = ?,"
                "  started_at = ?, finished_at = ? WHERE attempt_id = ? AND workspace_id = ?",
                (target.value,
                 exit_code if exit_code is not None else current.exit_code,
                 event_count if event_count is not None else current.event_count,
                 1 if (trace_truncated if trace_truncated is not None
                       else current.trace_truncated) else 0,
                 error if error is not None else current.error,
                 started_at, finished_at, attempt_id, workspace_id),
            )
        return self.get_attempt_by_id(attempt_id, workspace_id)

    # ------------------------------------------------------------------
    # Trace events
    # ------------------------------------------------------------------

    def insert_trace_events(self, events: Sequence[TraceEvent]) -> None:
        """Batch insert; idempotent per (run_id, event_id) (INSERT OR IGNORE —
        a re-ingested attempt's events never duplicate)."""
        if not events:
            return
        workspace_id = events[0].workspace_id
        if any(e.workspace_id != workspace_id for e in events):
            raise ValueError("trace batches must belong to one workspace")
        rows = [_event_row(e) for e in events]
        with self.workspace_transaction(workspace_id):
            self._conn.executemany(
                "INSERT OR IGNORE INTO trace_events (event_id, run_id, case_id,"
                "  attempt_id, workspace_id, repeat_index, attempt, sequence, type,"
                "  source, timestamp, duration_ms, parent_event_id,"
                "  provider_request_id, tool, otel, cost_usd_micros, price_version,"
                "  cost_json, redaction_state, payload, payload_ref, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    @_workspace_scoped
    def get_trace_events(
        self,
        *,
        run_id: str,
        workspace_id: str,
        case_id: str | None = None,
        attempt_id: str | None = None,
    ) -> list[TraceEvent]:
        """Reconstruct the attempt's TraceEvents from the typed columns.
        Ordering: (timestamp, sequence) — the deterministic §12B.4 order."""
        sql = "SELECT * FROM trace_events WHERE run_id = ? AND workspace_id = ?"
        params: list[Any] = [run_id, workspace_id]
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if attempt_id is not None:
            sql += " AND attempt_id = ?"
            params.append(attempt_id)
        sql += " ORDER BY timestamp, sequence"
        rows = self._conn.execute(sql, params).fetchall()
        return [_event_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Metric results
    # ------------------------------------------------------------------

    @_workspace_scoped
    def upsert_case_metric_results(
        self,
        *,
        run_id: str,
        case_id: str,
        workspace_id: str,
        scores: Sequence[CaseScore],
        metric_by_id: Mapping[str, Metric],
        score_revision: int = 1,
    ) -> None:
        """Store per-case metric results (§18 run_case_metric_results). The
        machine row is inserted once per (run, case, metric, revision) and
        updated in place by re-scoring — an override never modifies it
        (§13A.1.2; the ``overridden`` flag is set only by the score-revision
        path)."""
        now = _now()
        rows = []
        for s in scores:
            metric = metric_by_id.get(s.metric_id)
            judge_binding = None
            if metric is not None and metric.judge_binding is not None:
                judge_binding = json.dumps(metric.judge_binding.model_dump())
            rows.append((
                run_id, case_id, s.metric_id, workspace_id, score_revision,
                s.status.value, s.score, None, json.dumps(list(s.evidence_event_ids)),
                judge_binding,
                1 if s.attempt == 0 else 0,
                1 if s.on_retry_override else 0,
                now,
            ))
        with self._tx():
            self._conn.executemany(
                "INSERT INTO run_case_metric_results (run_id, case_id, metric_id,"
                "  workspace_id, score_revision, status, score, raw_value,"
                "  evidence_event_ids, judge_binding, is_authoritative,"
                "  on_retry_override, computed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (run_id, case_id, metric_id, score_revision)"
                " DO UPDATE SET status = excluded.status, score = excluded.score,"
                "   raw_value = excluded.raw_value,"
                "   evidence_event_ids = excluded.evidence_event_ids,"
                "   judge_binding = excluded.judge_binding,"
                "   is_authoritative = excluded.is_authoritative,"
                "   on_retry_override = excluded.on_retry_override,"
                "   computed_at = excluded.computed_at",
                rows,
            )

    # ------------------------------------------------------------------
    # Cost summaries (§12A.5) — per-run totals keyed by price_version
    # ------------------------------------------------------------------

    @_workspace_scoped
    def add_cost_summaries(
        self, run_id: str, workspace_id: str, rows: Iterable[Mapping[str, Any]]
    ) -> None:
        """Write per-price-version cost totals. Rows are mappings with
        ``price_version``, ``input_tokens``, ``output_tokens``, ``usd_micros``.
        Idempotent (UPSERT on the primary key)."""
        with self._tx():
            self._conn.executemany(
                "INSERT INTO cost_summaries "
                "(run_id, workspace_id, price_version, input_tokens, output_tokens, usd_micros) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run_id, price_version) DO UPDATE SET "
                "input_tokens = excluded.input_tokens, "
                "output_tokens = excluded.output_tokens, "
                "usd_micros = excluded.usd_micros",
                [
                    (
                        run_id,
                        workspace_id,
                        r["price_version"],
                        r["input_tokens"],
                        r["output_tokens"],
                        r["usd_micros"],
                    )
                    for r in rows
                ],
            )

    @_workspace_scoped
    def get_cost_summaries(self, run_id: str, workspace_id: str) -> list[dict[str, Any]]:
        """Per-price-version totals for the run (the dashboard's cost stat
        resolves against this — aggregates-only, never a trace scan)."""
        rows = self._conn.execute(
            "SELECT price_version, input_tokens, output_tokens, usd_micros "
            "FROM cost_summaries WHERE run_id = ? AND workspace_id = ? "
            "ORDER BY price_version",
            (run_id, workspace_id),
        ).fetchall()
        return [
            {
                "price_version": r[0],
                "input_tokens": r[1],
                "output_tokens": r[2],
                "usd_micros": r[3],
            }
            for r in rows
        ]

    @_workspace_scoped
    def iter_cost_json(self, run_id: str, workspace_id: str) -> Iterable[dict[str, Any]]:
        """Yield parsed §12B cost blocks from cost-bearing trace_events. The
        engine aggregates these into ``cost_summaries`` at run completion."""
        rows = self._conn.execute(
            "SELECT cost_json FROM trace_events "
            "WHERE run_id = ? AND workspace_id = ? AND cost_json IS NOT NULL",
            (run_id, workspace_id),
        ).fetchall()
        for r in rows:
            yield json.loads(r[0])

    @_workspace_scoped
    def get_case_scores(
        self,
        *,
        run_id: str,
        workspace_id: str,
        case_id: str | None = None,
        metric_id: str | None = None,
        score_revision: int | None = None,
    ) -> list[CaseMetricResultRecord]:
        sql = "SELECT * FROM run_case_metric_results WHERE run_id = ? AND workspace_id = ?"
        params: list[Any] = [run_id, workspace_id]
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if metric_id is not None:
            sql += " AND metric_id = ?"
            params.append(metric_id)
        if score_revision is not None:
            sql += " AND score_revision = ?"
            params.append(score_revision)
        sql += " ORDER BY case_id, metric_id"
        rows = self._conn.execute(sql, params).fetchall()
        return [_case_metric_result_from_row(r) for r in rows]

    @_workspace_scoped
    def upsert_run_metric_result(
        self,
        *,
        run_id: str,
        workspace_id: str,
        metric: Metric,
        aggregate: RunAggregate,
        score_revision: int = 1,
        gate_status: str | None = None,
        no_ci: bool = False,
    ) -> None:
        """Run-level rollup with aggregation_state (§13A.3, L11). The row is
        upserted incrementally (write-time aggregation, §13A.4); the dashboard
        reads only this row, never raw events."""
        if gate_status is not None and gate_status not in GATE_STATUSES:
            raise ValueError(
                f"gate_status must be one of {GATE_STATUSES}, got {gate_status!r}"
            )
        with self._tx():
            self._conn.execute(
                "INSERT INTO run_metric_results (run_id, metric_id, workspace_id,"
                "  score_revision, value, ci_lower, ci_upper, unit,"
                "  aggregation_state, method, on_error, sample_n, error_n,"
                "  skipped_n, gate_status, no_ci, computed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (run_id, metric_id, score_revision)"
                " DO UPDATE SET value = excluded.value, ci_lower = excluded.ci_lower,"
                "   ci_upper = excluded.ci_upper, unit = excluded.unit,"
                "   aggregation_state = excluded.aggregation_state,"
                "   method = excluded.method, on_error = excluded.on_error,"
                "   sample_n = excluded.sample_n, error_n = excluded.error_n,"
                "   skipped_n = excluded.skipped_n, gate_status = excluded.gate_status,"
                "   no_ci = excluded.no_ci, computed_at = excluded.computed_at",
                (
                    run_id, metric.metric_id, workspace_id, score_revision,
                    aggregate.value, None, None, None,
                    aggregate.aggregation_state.value,
                    metric.aggregation.method, metric.aggregation.on_error,
                    aggregate.n_cases, aggregate.error_n, aggregate.skipped_n,
                    gate_status, 1 if no_ci else 0, _now(),
                ),
            )

    @_workspace_scoped
    def get_run_metric_results(
        self, run_id: str, workspace_id: str, *, score_revision: int | None = None
    ) -> list[RunMetricResultRecord]:
        if score_revision is not None:
            rows = self._conn.execute(
                "SELECT * FROM run_metric_results WHERE run_id = ? AND workspace_id = ?"
                " AND score_revision = ? ORDER BY metric_id",
                (run_id, workspace_id, score_revision),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM run_metric_results WHERE run_id = ? AND workspace_id = ?"
                " ORDER BY metric_id",
                (run_id, workspace_id),
            ).fetchall()
        return [_run_metric_result_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Score revisions (§13A.1) — overrides alongside machine scores
    # ------------------------------------------------------------------

    @_workspace_scoped
    def append_score_revision(
        self,
        *,
        run_id: str,
        case_id: str,
        metric_id: str,
        workspace_id: str,
        score_revision: int,
        machine_score_snapshot: dict[str, Any],
        override_value: dict[str, Any] | None = None,
        dispute_path: str | None = None,
        reason: str | None = None,
        author_id: str | None = None,
        status: str = "open",
    ) -> ScoreRevisionRecord:
        """Append (or re-append — idempotent by PK) one revision row. An
        override is stored ALONGSIDE the machine score: the machine row in
        run_case_metric_results is never modified or deleted, only flagged
        ``overridden`` for ordering (§13A.1.2)."""
        if status not in ("open", "resolved"):
            raise ValueError(f"revision status must be open|resolved, got {status!r}")
        now = _now()
        with self._tx():
            self._conn.execute(
                "INSERT OR IGNORE INTO score_revisions (run_id, case_id, metric_id,"
                "  workspace_id, score_revision, machine_score_snapshot,"
                "  override_value, dispute_path, reason, author_id, status, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, case_id, metric_id, workspace_id, score_revision,
                 json.dumps(machine_score_snapshot),
                 json.dumps(override_value) if override_value is not None else None,
                 dispute_path, reason, author_id, status, now),
            )
            if override_value is not None:
                # §13A.1.2: the machine row is flagged (for ordering), never
                # modified or deleted. The flag marks the row(s) the override
                # supersedes — at whatever revision they sit (the current
                # machine row is at the last computed revision, which is not
                # necessarily the revision being appended).
                self._conn.execute(
                    "UPDATE run_case_metric_results SET overridden = 1"
                    " WHERE run_id = ? AND case_id = ? AND metric_id = ?",
                    (run_id, case_id, metric_id),
                )
        return self.get_score_revision(
            run_id, case_id, metric_id, score_revision, workspace_id
        )

    @_workspace_scoped
    def get_score_revision(
        self, run_id: str, case_id: str, metric_id: str, score_revision: int, workspace_id: str
    ) -> ScoreRevisionRecord:
        row = self._conn.execute(
            "SELECT * FROM score_revisions WHERE run_id = ? AND case_id = ?"
            " AND metric_id = ? AND score_revision = ? AND workspace_id = ?",
            (run_id, case_id, metric_id, score_revision, workspace_id),
        ).fetchone()
        if row is None:
            raise KeyError(
                f"no score revision {score_revision} for ({run_id}, {case_id}, {metric_id})"
            )
        return _score_revision_from_row(row)

    @_workspace_scoped
    def set_score_revision_status(
        self,
        *,
        run_id: str,
        case_id: str,
        metric_id: str,
        score_revision: int,
        workspace_id: str,
        status: str,
    ) -> ScoreRevisionRecord:
        if status not in ("open", "resolved"):
            raise ValueError(f"revision status must be open|resolved, got {status!r}")
        with self._tx():
            self._conn.execute(
                "UPDATE score_revisions SET status = ? WHERE run_id = ? AND case_id = ?"
                " AND metric_id = ? AND score_revision = ? AND workspace_id = ?",
                (status, run_id, case_id, metric_id, score_revision, workspace_id),
            )
        return self.get_score_revision(run_id, case_id, metric_id, score_revision, workspace_id)

    @_workspace_scoped
    def get_score_revisions(
        self,
        *,
        run_id: str,
        workspace_id: str,
        case_id: str | None = None,
        metric_id: str | None = None,
    ) -> list[ScoreRevisionRecord]:
        sql = "SELECT * FROM score_revisions WHERE run_id = ? AND workspace_id = ?"
        params: list[Any] = [run_id, workspace_id]
        if case_id is not None:
            sql += " AND case_id = ?"
            params.append(case_id)
        if metric_id is not None:
            sql += " AND metric_id = ?"
            params.append(metric_id)
        sql += " ORDER BY case_id, metric_id, score_revision"
        rows = self._conn.execute(sql, params).fetchall()
        return [_score_revision_from_row(r) for r in rows]

    @_workspace_scoped
    def create_custom_eval(
        self,
        *,
        workspace_id: str,
        name: str,
        spec: dict[str, Any],
        dashboard: dict[str, Any],
        dataset: Sequence[Mapping[str, Any]] | None = None,
        project_id: str | None = None,
        source_path: str | None = None,
        entrypoint: Sequence[str] = (),
        cwd: str | None = None,
        eval_id: str | None = None,
    ) -> CustomEvalRecord:
        now = _now()
        eid = eval_id or uuid.uuid4().hex
        cases = list(dataset) if dataset is not None else list(spec.get("cases") or [])
        with self._tx():
            self._conn.execute(
                "INSERT INTO custom_evals (eval_id, workspace_id, name, project_id,"
                "  spec_json, dashboard_json, dataset_json, source_path, entrypoint,"
                "  cwd, run_id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (
                    eid, workspace_id, name, project_id,
                    json.dumps(spec), json.dumps(dashboard), json.dumps(cases),
                    source_path, json.dumps(list(entrypoint)), cwd, now, now,
                ),
            )
        record = self.get_custom_eval(eid, workspace_id)
        assert record is not None
        return record

    @_workspace_scoped
    def get_custom_eval(self, eval_id: str, workspace_id: str) -> CustomEvalRecord | None:
        row = self._conn.execute(
            "SELECT * FROM custom_evals WHERE eval_id = ? AND workspace_id = ?",
            (eval_id, workspace_id),
        ).fetchone()
        return _custom_eval_from_row(row) if row is not None else None

    @_workspace_scoped
    def list_custom_evals(self, workspace_id: str) -> list[CustomEvalRecord]:
        rows = self._conn.execute(
            "SELECT * FROM custom_evals WHERE workspace_id = ? ORDER BY created_at DESC",
            (workspace_id,),
        ).fetchall()
        return [_custom_eval_from_row(r) for r in rows]

    @_workspace_scoped
    def set_custom_eval_run(
        self, eval_id: str, workspace_id: str, run_id: str
    ) -> CustomEvalRecord:
        now = _now()
        with self._tx():
            self._conn.execute(
                "UPDATE custom_evals SET run_id = ?, updated_at = ?"
                " WHERE eval_id = ? AND workspace_id = ?",
                (run_id, now, eval_id, workspace_id),
            )
        record = self.get_custom_eval(eval_id, workspace_id)
        if record is None:
            raise KeyError(f"eval {eval_id!r} not found in workspace {workspace_id!r}")
        return record

    @_workspace_scoped
    def update_custom_eval_dashboard(
        self, eval_id: str, workspace_id: str, dashboard: dict[str, Any]
    ) -> CustomEvalRecord:
        now = _now()
        with self._tx():
            self._conn.execute(
                "UPDATE custom_evals SET dashboard_json = ?, updated_at = ?"
                " WHERE eval_id = ? AND workspace_id = ?",
                (json.dumps(dashboard), now, eval_id, workspace_id),
            )
        record = self.get_custom_eval(eval_id, workspace_id)
        if record is None:
            raise KeyError(f"eval {eval_id!r} not found in workspace {workspace_id!r}")
        return record


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------
# Status transition tables are imported from :mod:`lifecycle` (single source
# of truth) — storage enforces them but never redefines them.
_SMOKE_TABLE = {
    SmokeState.INGESTED: {SmokeState.ANALYZED},
    SmokeState.ANALYZED: {
        SmokeState.RUNTIME_PREPARED, SmokeState.ENTRYPOINT_MISSING,
        SmokeState.INSTALL_FAILED, SmokeState.INVOCATION_FAILED,
        SmokeState.PROVIDER_UNREACHABLE, SmokeState.NO_TRACE, SmokeState.TIMEOUT,
    },
    SmokeState.RUNTIME_PREPARED: {
        SmokeState.SMOKE_PASSED, SmokeState.ENTRYPOINT_MISSING,
        SmokeState.INSTALL_FAILED, SmokeState.INVOCATION_FAILED,
        SmokeState.PROVIDER_UNREACHABLE, SmokeState.NO_TRACE, SmokeState.TIMEOUT,
    },
    SmokeState.SMOKE_PASSED: {SmokeState.EVALUABLE},
    SmokeState.EVALUABLE: set(),
}
for _f in (
    SmokeState.ENTRYPOINT_MISSING, SmokeState.INSTALL_FAILED,
    SmokeState.INVOCATION_FAILED, SmokeState.PROVIDER_UNREACHABLE,
    SmokeState.NO_TRACE, SmokeState.TIMEOUT,
):
    _SMOKE_TABLE[_f] = {
        SmokeState.RUNTIME_PREPARED, SmokeState.SMOKE_PASSED,
        SmokeState.ENTRYPOINT_MISSING, SmokeState.INSTALL_FAILED,
        SmokeState.INVOCATION_FAILED, SmokeState.PROVIDER_UNREACHABLE,
        SmokeState.NO_TRACE, SmokeState.TIMEOUT,
    }

#: Terminal run statuses (timestamps are stamped on these).
_RUN_TERMINAL = frozenset(
    {RunStatus.COMPLETE.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)
_CASE_TERMINAL = frozenset(
    {RunCaseStatus.COMPLETED.value, RunCaseStatus.FAILED.value,
     RunCaseStatus.CANCELLED.value, RunCaseStatus.SKIPPED.value}
)


def _attempt_is_terminal(status: AttemptStatus) -> bool:
    return status is not AttemptStatus.QUEUED and status is not AttemptStatus.RUNNING


def _j(v: str | None) -> Any:
    return json.loads(v) if v is not None else None


def _project_from_row(row: sqlite3.Row) -> ProjectRecord:
    return ProjectRecord(
        project_id=row["project_id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        smoke_state=row["smoke_state"],
        smoke_failure_detail=row["smoke_failure_detail"],
        entrypoint=tuple(_j(row["entrypoint"]) or ()),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _smoke_attempt_from_row(row: sqlite3.Row) -> SmokeAttemptRecord:
    return SmokeAttemptRecord(
        attempt_id=row["attempt_id"],
        workspace_id=row["workspace_id"],
        project_id=row["project_id"],
        entrypoint=tuple(_j(row["entrypoint"]) or ()),
        exit_code=row["exit_code"],
        state=row["state"],
        failure_detail=row["failure_detail"],
        trace_path=row["trace_path"],
        reproduce_locally=row["reproduce_locally"],
        created_at=row["created_at"],
    )


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    spec_json = row["spec_json"]
    spec = EvaluationSpec.model_validate_json(spec_json)
    agent_version = _j(row["agent_version"]) or {}
    entrypoint = tuple(agent_version.get("entrypoint") or ())
    cwd = agent_version.get("cwd")
    timeout_seconds = float(agent_version.get("timeout_seconds", 120.0))
    # The timeout lives with the resolved runtime config; the RunRecord
    # property reads it. agent_version is stored as given plus the timeout.
    return RunRecord(
        run_id=row["run_id"],
        workspace_id=row["workspace_id"],
        project_id=row["project_id"],
        spec=spec,
        spec_json=spec_json,
        agent_version=agent_version,
        status=row["status"],
        tier=row["tier"],
        repeat_config=_j(row["repeat_config"]) or {},
        world_config=_j(row["world_config"]) or {},
        judge_binding=_j(row["judge_binding"]),
        mixed_binding=bool(row["mixed_binding"]),
        case_count=row["case_count"],
        concurrency=row["concurrency"],
        estimated_cost_usd=_j(row["estimated_cost_usd"]),
        budget_usd_micros=row["budget_usd_micros"],
        progress=_j(row["progress"]) or {},
        entrypoint=entrypoint,
        cwd=cwd,
        run_started_at=row["run_started_at"],
        run_completed_at=row["run_completed_at"],
        created_at=row["created_at"],
        idempotency_key=row["idempotency_key"],
    )


def _case_from_row(row: sqlite3.Row) -> CaseRecord:
    return CaseRecord(
        run_id=row["run_id"],
        case_id=row["case_id"],
        case_key=row["case_key"],
        workspace_id=row["workspace_id"],
        status=row["status"],
        repeat_count=row["repeat_count"],
        classification=row["classification"],
        error_category=row["error_category"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=row["attempt_id"],
        run_id=row["run_id"],
        case_id=row["case_id"],
        workspace_id=row["workspace_id"],
        repeat_index=row["repeat_index"],
        attempt=row["attempt"],
        is_first_attempt=bool(row["is_first_attempt"]),
        status=row["status"],
        exit_code=row["exit_code"],
        event_count=row["event_count"],
        trace_truncated=bool(row["trace_truncated"]),
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _event_row(e: TraceEvent) -> tuple[Any, ...]:
    cost_usd_micros = None
    price_version = None
    cost_json = None
    if e.cost is not None:
        cost_usd_micros = int(round(e.cost.cost_usd * 1_000_000))
        price_version = e.cost.price_version
        cost_json = e.cost.model_dump_json()
    safe_payload = redact(e.payload) if e.payload is not None else None
    safe_error = redact(e.error.model_dump()) if e.error is not None else None
    flags = set((safe_payload.detector_flags if safe_payload else ()) + (safe_error.detector_flags if safe_error else ()))
    truncated = (safe_payload and safe_payload.truncated) or (safe_error and safe_error.truncated)
    state = e.redaction_state.model_copy(deep=True)
    if flags or truncated:
        state.status = "truncated" if truncated else "redacted"
        state.rules = sorted(set(state.rules) | {f"storage:{flag}" for flag in flags} | ({"storage:truncated"} if truncated else set()))
    return (
        e.event_id, e.run_id, e.case_id, e.attempt_id, e.workspace_id,
        e.repeat_index, e.attempt, e.sequence, e.type.value, e.source.value,
        e.timestamp.isoformat(), e.duration_ms, e.parent_event_id,
        e.provider_request_id, e.tool,
        e.otel.model_dump_json() if e.otel is not None else None,
        cost_usd_micros, price_version, cost_json,
        state.model_dump_json(),
        json.dumps(safe_payload.content) if safe_payload is not None else None,
        e.payload_ref,
        json.dumps(safe_error.content) if safe_error is not None else None,
    )


def _event_from_row(row: sqlite3.Row) -> TraceEvent:
    otel = OtelContext.model_validate(_j(row["otel"])) if row["otel"] is not None else None
    cost = CostBlock.model_validate(_j(row["cost_json"])) if row["cost_json"] is not None else None
    redaction = RedactionState.model_validate(_j(row["redaction_state"]))
    error = ErrorBlock.model_validate(_j(row["error"])) if row["error"] is not None else None
    return TraceEvent(
        event_id=row["event_id"],
        run_id=row["run_id"],
        case_id=row["case_id"],
        attempt_id=row["attempt_id"],
        workspace_id=row["workspace_id"],
        repeat_index=row["repeat_index"],
        attempt=row["attempt"],
        sequence=row["sequence"],
        type=row["type"],
        source=Source(row["source"]),
        timestamp=datetime.fromisoformat(row["timestamp"]),
        duration_ms=row["duration_ms"],
        parent_event_id=row["parent_event_id"],
        provider_request_id=row["provider_request_id"],
        tool=row["tool"],
        otel=otel,
        cost=cost,
        redaction_state=redaction,
        payload=_j(row["payload"]),
        payload_ref=row["payload_ref"],
        error=error,
    )


def _case_metric_result_from_row(row: sqlite3.Row) -> CaseMetricResultRecord:
    return CaseMetricResultRecord(
        run_id=row["run_id"],
        case_id=row["case_id"],
        metric_id=row["metric_id"],
        workspace_id=row["workspace_id"],
        score_revision=row["score_revision"],
        status=row["status"],
        score=row["score"],
        raw_value=_j(row["raw_value"]),
        evidence_event_ids=tuple(_j(row["evidence_event_ids"]) or ()),
        judge_binding=_j(row["judge_binding"]),
        is_authoritative=bool(row["is_authoritative"]),
        on_retry_override=bool(row["on_retry_override"]),
        overridden=bool(row["overridden"]),
        computed_at=row["computed_at"],
    )


def _run_metric_result_from_row(row: sqlite3.Row) -> RunMetricResultRecord:
    return RunMetricResultRecord(
        run_id=row["run_id"],
        metric_id=row["metric_id"],
        workspace_id=row["workspace_id"],
        score_revision=row["score_revision"],
        value=row["value"],
        ci_lower=row["ci_lower"],
        ci_upper=row["ci_upper"],
        unit=row["unit"],
        aggregation_state=row["aggregation_state"],
        method=row["method"],
        on_error=row["on_error"],
        sample_n=row["sample_n"],
        error_n=row["error_n"],
        skipped_n=row["skipped_n"],
        gate_status=row["gate_status"],
        no_ci=bool(row["no_ci"]),
        computed_at=row["computed_at"],
    )


def _score_revision_from_row(row: sqlite3.Row) -> ScoreRevisionRecord:
    return ScoreRevisionRecord(
        run_id=row["run_id"],
        case_id=row["case_id"],
        metric_id=row["metric_id"],
        workspace_id=row["workspace_id"],
        score_revision=row["score_revision"],
        machine_score_snapshot=_j(row["machine_score_snapshot"]),
        override_value=_j(row["override_value"]),
        dispute_path=row["dispute_path"],
        reason=row["reason"],
        author_id=row["author_id"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _custom_eval_from_row(row: sqlite3.Row) -> CustomEvalRecord:
    return CustomEvalRecord(
        eval_id=row["eval_id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        project_id=row["project_id"],
        spec=_j(row["spec_json"]) or {},
        dashboard=_j(row["dashboard_json"]) or {},
        dataset=_j(row["dataset_json"]) or [],
        source_path=row["source_path"],
        entrypoint=list(_j(row["entrypoint"]) or []),
        cwd=row["cwd"],
        run_id=row["run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
