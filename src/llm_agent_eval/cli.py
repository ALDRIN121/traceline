"""The eval-engine command line (interaction surface, harness §17).

Subcommands: ``init`` (project skeleton), ``smoke`` (the §32A smoke gate),
``run`` (execute a run synchronously), ``results`` (print a run's state),
``serve`` (the FastAPI app), ``dashboards`` (resolve a declarative dashboard
definition against a run — §37B).

Every mutating command prints an explicit state line — never silent success
(invariant: never report success you did not observe). Exit codes: 0 success,
1 operational failure (smoke failure states, gate failure, invalid spec
content, engine errors), 2 usage (bad arguments, missing files).

The dashboard surface is declarative-only: ``dashboards`` validates the
definition (closed binding vocabulary, §37B.2) and resolves it server-side;
it never evaluates expressions and never emits frontend code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import settings
from .ci_api import exit_code as ci_exit_code
from .dashboard import (
    DEFAULT_DEFINITION,
    DefinitionResolutionError,
    DefinitionValidationError,
    ResolvedDashboard,
    validate_definition,
    resolve_definition,
)
from .engine import Engine, EngineError
from .lifecycle import RunStatus, SmokeState
from .spec import SpecValidationError, validate_spec
from .storage import Storage

EXIT_OK = 0
EXIT_OPERATIONAL = 1
EXIT_USAGE = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_store(db_path: str | None) -> Storage:
    store = Storage(db_path or settings.db_path)
    store.create_schema()
    return store


def _load_spec(spec_path: Path) -> Any | None:
    """Load and validate a spec file; prints problems and returns None on
    failure (the caller decides the exit code — invalid content is
    operational, unreadable files are usage errors)."""
    try:
        raw = spec_path.read_text()
    except OSError as exc:
        print(f"error: cannot read {spec_path}: {exc}", file=sys.stderr)
        return None
    try:
        return validate_spec(raw)
    except SpecValidationError as exc:
        print("error: spec validation failed:", file=sys.stderr)
        for p in exc.problems:
            print(f"  {p.field}: {p.message}", file=sys.stderr)
        return None


def _source_digest(spec_path: Path) -> str:
    return hashlib.sha256(spec_path.read_bytes()).hexdigest()


def _print_spec_problems(exc: SpecValidationError) -> None:
    print("error: spec validation failed:", file=sys.stderr)
    for p in exc.problems:
        print(f"  {p.field}: {p.message}", file=sys.stderr)


#: The spec template ``eval-engine init`` writes — a valid EvaluationSpec
#: (binary scalar on the tool_result amount, pass_rate aggregation).
_SPEC_TEMPLATE: dict[str, Any] = {
    "spec_version": "0.1.0",
    "name": "my-agent-suite",
    "dataset_version": "v1",
    "run_tier": "quick",
    "cases": [
        {
            "case_id": "case_1",
            "name": "first case",
            "description": "edit me",
            "input": {"prompt": "hello"},
            "expected": {"response": {"tag": "user_stated", "value": "hi"}},
        },
    ],
    "metrics": [
        {
            "metric_id": "responds",
            "name": "agent responds",
            "type": "scalar",
            "target": {"type": "model_output", "occurrence": "last", "on_missing": "fail"},
            "evaluator": {"type": "exact_match", "expected": "hi"},
            "scoring": {"type": "binary", "range": [0, 1]},
            "aggregation": {"method": "pass_rate", "on_error": "fail"},
        },
    ],
}


def _print_dashboard(resolved: ResolvedDashboard) -> None:
    print(
        f"dashboards: {resolved.name} "
        f"(definition v{resolved.version}, registry v{resolved.registry_version})"
    )
    if resolved.run is not None:
        final = "final" if resolved.render_final else "partial"
        print(
            f"dashboards: run {resolved.run['run_id']}: status {resolved.run['status']} "
            f"({resolved.run['case_count']} cases) — renders as {final}"
        )
        if not resolved.metrics_complete:
            print(
                "dashboards: results are partial — some metric rows are not COMPLETE; "
                "a half-aggregated run is never rendered as finished (§37B.3)"
            )
    else:
        print("dashboards: no run bound — the run filter resolved to nothing")
    for block in resolved.blocks:
        print(f"dashboards: block [{block.row}:{block.col}] {block.component} (span {block.span})")
        payload = block.payload
        if payload.get("placeholder"):
            print(f"  placeholder: {payload.get('message', 'no data bound')}")
            continue
        if payload.get("available") is False:
            print(f"  unavailable: {payload.get('reason', 'no data')}")
            continue
        component = block.component
        if component == "metric_summary":
            print(
                f"  {payload['metric_id']} {payload['stat']}={payload['value']} "
                f"(n={payload['sample_n']}, {payload['aggregation_state']}, "
                f"gate {payload['gate_status']}, provisional={payload['provisional']})"
            )
        elif component == "run_table":
            for m in payload["metrics"]:
                print(
                    f"  metric {m['metric_id']}: value={m['value']} ({m['method']}, "
                    f"n={m['sample_n']}, {m['aggregation_state']}, "
                    f"gate {m['gate_status']}, provisional={m['provisional']})"
                )
            counts = payload["cases"]
            print(
                f"  cases: {counts['total']} total ({counts['completed']} completed, "
                f"{counts['failed']} failed, {counts['cancelled']} cancelled, "
                f"{counts['queued']} queued, {counts['running']} running)"
            )
        elif component == "case_table":
            for case in payload["cases"]:
                badges = " ".join(
                    f"{m['metric_id']}={m['status']}"
                    f"{'!' if m['on_retry_override'] else ''}"
                    f"{'~' if m['provisional'] else ''}"
                    for m in case["metrics"]
                )
                print(
                    f"  case {case['case_id']}: {case['status']} "
                    f"({case['classification'] or 'n/a'}) — {badges or 'no metric rows'}"
                )
        elif component == "trace_evidence":
            print(f"  case {payload.get('case_id')}: {payload.get('trace_url')}")
            for m in payload["metrics"]:
                print(
                    f"    {m['metric_id']}: {m['status']} — {m['evidence_count']} "
                    f"evidence event(s) {list(m['evidence_event_ids'][:5])}"
                )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.dir)
    try:
        target.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print(f"init: error: directory {target} already exists", file=sys.stderr)
        return EXIT_USAGE
    spec_path = target / "spec.json"
    project_path = target / "project.json"
    agent_dir = target / "agent"
    spec_path.write_text(json.dumps(_SPEC_TEMPLATE, indent=2) + "\n")
    agent_dir.mkdir()
    (agent_dir / "README.md").write_text(
        "Put the agent under test in this directory (entrypoint: python agent.py).\n"
    )
    project_path.write_text(
        json.dumps({"entrypoint": ["python", "agent.py"], "cwd": "agent"}, indent=2) + "\n"
    )
    print(f"init: created {spec_path} (spec template)")
    print(f"init: created {project_path} (entrypoint: python agent.py in agent/)")
    print(f"init: created {agent_dir}/")
    print(
        "init: state: created — edit spec.json, drop your agent into agent/, then run "
        "`eval-engine smoke .`"
    )
    return EXIT_OK


def cmd_smoke(args: argparse.Namespace) -> int:
    """The §32A smoke gate, run from the project directory: it must pass
    before any run executes against the project."""
    project_dir = Path(args.dir)
    spec_path = project_dir / "spec.json"
    project_path = project_dir / "project.json"
    if not spec_path.is_file() or not project_path.is_file():
        print(
            f"smoke: error: {project_dir} needs spec.json and project.json "
            "(run `eval-engine init` first)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        config = json.loads(project_path.read_text())
        entrypoint = config["entrypoint"]
        cwd = config.get("cwd")
    except (OSError, ValueError, KeyError) as exc:
        print(f"smoke: error: project.json is not readable: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not entrypoint:
        print("smoke: error: project.json declares no entrypoint", file=sys.stderr)
        return EXIT_USAGE
    spec = _load_spec(spec_path)
    if spec is None:
        return EXIT_OPERATIONAL

    store = _open_store(args.db)
    eng = Engine(store)
    cwd_abs = str((project_dir / cwd).resolve()) if cwd else None
    try:
        project = eng.create_project(args.workspace, spec.name, entrypoint=entrypoint)
        project = eng.prepare_project(
            project.project_id, args.workspace, entrypoint=entrypoint
        )
        attempt = eng.smoke(project.project_id, args.workspace, cwd=cwd_abs)
    except EngineError as exc:
        print(f"smoke: error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    print(f"smoke: state: {attempt.state}")
    if attempt.failure_detail:
        print(f"smoke: failure_detail: {attempt.failure_detail}")
    if attempt.reproduce_locally:
        print(f"smoke: reproduce: {attempt.reproduce_locally}")
    if attempt.trace_path:
        print(f"smoke: trace: {attempt.trace_path}")
    if attempt.state == SmokeState.SMOKE_PASSED.value:
        print("smoke: ok — the agent runs and produces a trace")
        return EXIT_OK
    print(
        f"smoke: failed — the agent did not pass smoke ({attempt.state}); "
        "see the failure detail above",
        file=sys.stderr,
    )
    return EXIT_OPERATIONAL


def _resolve_entrypoint(
    spec_path: Path, args: argparse.Namespace
) -> tuple[list[str] | None, str | None]:
    if args.entrypoint:
        return list(args.entrypoint), args.cwd
    project_path = spec_path.parent / "project.json"
    if project_path.is_file():
        try:
            config = json.loads(project_path.read_text())
            entrypoint = config["entrypoint"]
            cwd = config.get("cwd")
            cwd_abs = str((spec_path.parent / cwd).resolve()) if cwd else None
            return entrypoint, cwd_abs
        except (OSError, ValueError, KeyError):
            pass
    return None, None


def cmd_run(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"run: error: spec file {spec_path} not found", file=sys.stderr)
        return EXIT_USAGE
    spec = _load_spec(spec_path)
    if spec is None:
        return EXIT_OPERATIONAL
    entrypoint, cwd = _resolve_entrypoint(spec_path, args)
    if entrypoint is None:
        print(
            "run: error: no entrypoint — pass --entrypoint or create project.json "
            "next to the spec (`eval-engine init`)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    store = _open_store(args.db)
    eng = Engine(store)
    try:
        run = eng.create_run(
            args.workspace, spec, tier=args.tier, repeats=args.repeat,
            entrypoint=entrypoint, cwd=cwd,
            source_digest=_source_digest(spec_path),
        )
    except EngineError as exc:
        print(f"run: error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    print(
        f"run: created {run.run_id} ({len(spec.cases)} cases, tier {run.tier}, "
        f"repeats {run.repeats}, retry_max {run.retry_max})"
    )
    try:
        result = eng.run(run.run_id, args.workspace)
    except EngineError as exc:
        print(f"run: error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    print(f"run: state: {result.status}")
    for case in result.case_results:
        print(
            f"  case {case.case_id}: {case.status} "
            f"({case.classification or 'n/a'})"
        )
    for metric in result.metric_results:
        print(
            f"  metric {metric.metric_id}: value={metric.value} ({metric.method}, "
            f"n={metric.sample_n}, {metric.aggregation_state}, gate {metric.gate_status})"
        )
    for gate in result.gate_results:
        print(
            f"  gate {gate.metric_id}: {'PASS' if gate.passed else 'FAIL' if gate.active else 'n/a'} — {gate.message}"
        )
    if result.status == RunStatus.COMPLETE.value and all(
        not g.active or g.passed for g in result.gate_results
    ):
        return EXIT_OK
    if result.status == RunStatus.COMPLETE.value:
        print("run: gate failed — the run landed complete but the gate did not pass", file=sys.stderr)
    else:
        print(
            f"run: {result.status} — the run did not complete; fix the failure and re-run",
            file=sys.stderr,
        )
    return EXIT_OPERATIONAL


def cmd_results(args: argparse.Namespace) -> int:
    store = _open_store(args.db)
    run = store.get_run(args.run_id, args.workspace)
    if run is None:
        print(f"results: error: run {args.run_id!r} not found", file=sys.stderr)
        return EXIT_OPERATIONAL
    print(
        f"results: run {run.run_id}: status {run.status} "
        f"(tier {run.tier}, {run.case_count} cases, repeats {run.repeats})"
    )
    for m in store.get_run_metric_results(run.run_id, args.workspace):
        print(
            f"  metric {m.metric_id}: value={m.value} method={m.method} n={m.sample_n} "
            f"agg={m.aggregation_state} gate={m.gate_status}"
        )
    for c in store.list_cases(run.run_id, args.workspace):
        print(
            f"  case {c.case_id}: {c.status} ({c.classification or 'n/a'})"
        )
    for r in store.get_score_revisions(run_id=run.run_id, workspace_id=args.workspace):
        print(
            f"  revision {r.score_revision} {r.case_id}/{r.metric_id}: "
            f"override={r.override_value} path={r.dispute_path} "
            f"status={r.status} reason={r.reason!r}"
        )
    print("results: state: ok")
    return EXIT_OK


def cmd_ci_status(args: argparse.Namespace) -> int:
    """Print the authenticated CI contract and return its documented code."""
    store = _open_store(args.db)
    run = store.get_run(args.run_id, args.workspace)
    if run is None:
        print(f"ci-status: error: run {args.run_id!r} not found", file=sys.stderr)
        return EXIT_USAGE
    metrics = [
        {
            "metric_id": metric.metric_id,
            "value": metric.value,
            "gate_status": metric.gate_status,
            "no_ci": metric.no_ci,
        }
        for metric in store.get_run_metric_results(run.run_id, args.workspace)
    ]
    payload = {
        "run_id": run.run_id,
        "status": run.status,
        "metrics": metrics,
    }
    payload["exit_code"] = ci_exit_code(payload)
    print(json.dumps(payload, sort_keys=True))
    return payload["exit_code"]


def cmd_dashboards(args: argparse.Namespace) -> int:
    store = _open_store(args.db)
    run = store.get_run(args.run_id, args.workspace)
    if run is None:
        print(f"dashboards: error: run {args.run_id!r} not found", file=sys.stderr)
        return EXIT_OPERATIONAL
    if args.definition:
        try:
            definition = json.loads(args.definition.read_text())
        except (OSError, ValueError) as exc:
            print(f"dashboards: error: cannot read {args.definition}: {exc}", file=sys.stderr)
            return EXIT_OPERATIONAL
    else:
        definition = DEFAULT_DEFINITION
    try:
        validate_definition(definition)
        resolved = resolve_definition(
            definition, {"run": args.run_id}, {}, store, workspace_id=args.workspace
        )
    except (DefinitionValidationError, DefinitionResolutionError) as exc:
        print(f"dashboards: error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    _print_dashboard(resolved)
    return EXIT_OK


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    db_path = args.db or settings.db_path
    store = _open_store(db_path)
    app = create_app(storage=store, workspace_id=args.workspace, release_mode=True)
    print(f"serve: starting on http://{args.host}:{args.port} (db {db_path})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def cmd_worker(args: argparse.Namespace) -> int:
    import signal
    import threading
    from .auth import Actor
    from .gateway import LiteLLMGateway
    from .install_state import load_fingerprint_key
    from .worker import WorkflowWorker

    root = Path(args.artifact_root or settings.artifact_root)
    store = _open_store(args.db or settings.db_path)
    gateway = LiteLLMGateway(settings.model)
    stop = threading.Event()
    previous = {}
    try:
        worker = WorkflowWorker(
            store, root, gateway,
            fingerprint_key_source=lambda: load_fingerprint_key(root / "install-fingerprint.key"),
        )
        actor = Actor("workflow-service", args.workspace, "owner")
        actors = [actor]
        for workspace in getattr(args, "workspaces", None) or ():
            actors.append(Actor("workflow-service", workspace, "owner"))
        if args.once:
            if len(actors) == 1:
                worker.run_once(actor, stop_event=stop)
            else:
                worker.run_fair_once(actors, worker_id="workflow-service")
        else:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            if len(actors) == 1:
                worker.run_forever(actor, stop_event=stop)
            else:
                worker.run_fair_forever(actors, stop_event=stop, worker_id="workflow-service")
        return EXIT_OK
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        gateway.close()
        store.close()


def cmd_backup(args: argparse.Namespace) -> int:
    from .auth import Actor
    from .operations import OperationsService

    store = _open_store(args.db or settings.db_path)
    root = Path(args.artifact_root or settings.artifact_root)
    try:
        result = OperationsService(store, root).backup_workspace(
            Actor("cli-owner", args.workspace, "owner"), Path(args.destination),
            maintenance_database_url=getattr(args, "postgres_url", None),
        )
        print(json.dumps(result, sort_keys=True))
        return EXIT_OK
    except Exception as exc:
        print(f"backup: error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    finally:
        store.close()


def cmd_restore(args: argparse.Namespace) -> int:
    from .operations import OperationsService

    if not args.postgres_url and not args.destination_db:
        print("restore: error: --destination-db is required for SQLite restore", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.postgres_url:
            result = OperationsService.restore_postgres(
                Path(args.backup), args.postgres_url, Path(args.destination_artifact_root),
            )
        else:
            result = OperationsService.restore_sqlite(
                Path(args.backup), Path(args.destination_db), Path(args.destination_artifact_root),
            )
        print(json.dumps(result, sort_keys=True))
        return EXIT_OK
    except Exception as exc:
        print(f"restore: error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL


def cmd_retention(args: argparse.Namespace) -> int:
    from .auth import Actor
    from .operations import OperationsService

    store = _open_store(args.db or settings.db_path)
    root = Path(args.artifact_root or settings.artifact_root)
    try:
        result = OperationsService(store, root).enforce_retention(
            Actor("cli-owner", args.workspace, "owner"),
            export_days=args.export_days,
            preview_days=args.preview_days,
            abandoned_import_hours=args.abandoned_import_hours,
            dry_run=not args.apply,
        )
        print(json.dumps(result, sort_keys=True))
        return EXIT_OK
    except Exception as exc:
        print(f"retention: error: {exc}", file=sys.stderr)
        return EXIT_OPERATIONAL
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval-engine",
        description="LLM Agent Evaluation Engine — CLI (harness §17).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        # SUPPRESS defaults: the option is usable before OR after the
        # subcommand, and a subparser default never clobbers a value already
        # parsed at the top level.
        p.add_argument(
            "--db", default=argparse.SUPPRESS,
            help="SQLite database path (default: $LLM_AGENT_EVAL_DB or ./eval.db)",
        )
        p.add_argument(
            "--workspace", default=argparse.SUPPRESS,
            help="workspace id (default: default)",
        )

    # Common options also live on the top-level parser (before the subcommand).
    # set_defaults keeps the value when the option is not given; SUPPRESS on
    # the option declarations means a subparser default can never clobber a
    # value already parsed at the top level.
    parser.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.add_argument("--workspace", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    parser.set_defaults(db=None, workspace="default")

    p = sub.add_parser("init", help="create a project skeleton (spec template + agent dir)")
    p.add_argument("dir", help="target directory")
    add_common(p)
    p.set_defaults(handler=cmd_init)

    p = sub.add_parser("smoke", help="run the smoke gate against a project directory")
    p.add_argument("dir", help="project directory (spec.json + project.json)")
    add_common(p)
    p.set_defaults(handler=cmd_smoke)

    p = sub.add_parser("run", help="execute a run synchronously from a spec file")
    p.add_argument("spec", help="path to spec.json")
    p.add_argument("--tier", choices=("quick", "standard", "full"), default=None,
                   help="run tier (default: the spec's run_tier hint)")
    p.add_argument("--repeat", type=int, default=1, help="repeats per case (default: 1)")
    p.add_argument("--entrypoint", nargs="+", default=None,
                   help="agent command (overrides project.json)")
    p.add_argument("--cwd", default=None, help="agent working directory (with --entrypoint)")
    add_common(p)
    p.set_defaults(handler=cmd_run)

    p = sub.add_parser("results", help="print a run's state, scores, and revisions")
    p.add_argument("run_id", help="the run id (see `eval-engine run` output)")
    add_common(p)
    p.set_defaults(handler=cmd_results)

    p = sub.add_parser("ci-status", help="print CI gate status and return its stable exit code")
    p.add_argument("run_id", help="the run id (see `eval-engine run` output)")
    add_common(p)
    p.set_defaults(handler=cmd_ci_status)

    p = sub.add_parser("dashboards", help="resolve a declarative dashboard definition (§37B)")
    p.add_argument("run_id", help="the run id to bind")
    p.add_argument("--definition", type=Path, default=None,
                   help="definition file (default: the built-in DEFAULT_DEFINITION)")
    add_common(p)
    p.set_defaults(handler=cmd_dashboards)

    p = sub.add_parser("serve", help="start the FastAPI application")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    add_common(p)
    p.set_defaults(handler=cmd_serve)

    p = sub.add_parser("worker", help="run the durable workflow worker service")
    p.add_argument("--once", action="store_true", help="drain one eligible job and exit")
    p.add_argument("--workspaces", nargs="+", default=None,
                   help="additional operator-approved workspace ids to service round-robin")
    p.add_argument("--artifact-root", default=None, help="install state/artifact root")
    add_common(p)
    p.set_defaults(handler=cmd_worker)

    p = sub.add_parser("backup", help="create a checksummed SQLite or PostgreSQL workspace backup")
    p.add_argument("destination", type=Path, help="new backup archive path")
    p.add_argument("--postgres-url", default=None,
                   help="maintenance PostgreSQL URL used only for pg_dump")
    p.add_argument("--artifact-root", default=None, help="artifact/install root")
    add_common(p)
    p.set_defaults(handler=cmd_backup)

    p = sub.add_parser("restore", help="restore a workspace backup into fresh destinations")
    p.add_argument("backup", type=Path, help="backup archive path")
    p.add_argument("--destination-db", default=None, help="new SQLite database path")
    p.add_argument("--destination-artifact-root", required=True, help="new artifact root")
    p.add_argument("--postgres-url", default=None, help="restore as PostgreSQL instead of SQLite")
    p.set_defaults(handler=cmd_restore)

    p = sub.add_parser("retention", help="preview or apply workspace retention cleanup")
    p.add_argument("--artifact-root", default=None, help="artifact/install root")
    p.add_argument("--apply", action="store_true", help="delete expired unreferenced data")
    p.add_argument("--export-days", type=int, default=7)
    p.add_argument("--preview-days", type=int, default=30)
    p.add_argument("--abandoned-import-hours", type=int, default=24)
    add_common(p)
    p.set_defaults(handler=cmd_retention)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
