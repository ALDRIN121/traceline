"""Real PostgreSQL proof for tenant RLS, including pooled-connection reuse.

Set TEST_DATABASE_URL to a disposable PostgreSQL database.  The test uses the
operator connection only to create the schema, then adopts the unprivileged
application role for every tenant-visible query.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import pytest

from llm_agent_eval.storage import Storage
from llm_agent_eval.bootstrap_postgres import provision_application_login


PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def app_connection():
    if not PG_URL:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL RLS integration tests")
    psycopg = pytest.importorskip("psycopg")
    store = Storage(PG_URL)
    store.create_schema()
    connection = psycopg.connect(PG_URL, autocommit=True)
    try:
        connection.execute("SET ROLE llm_eval_app")
        yield connection
    finally:
        connection.close()
        store.close()


def _in_workspace(connection, workspace_id: str, statement: str, parameters=()):
    """Execute under one transaction-local tenant setting on a pooled connection."""
    with connection.transaction():
        connection.execute("SELECT set_config('app.workspace_id', %s, true)", (workspace_id,))
        cursor = connection.execute(statement, parameters)
        return cursor.fetchall() if cursor.description else None


def test_application_role_cannot_cross_workspace_after_connection_reuse(app_connection):
    workspace_a, workspace_b = (f"ws_{uuid.uuid4().hex}" for _ in range(2))
    artifact_a, artifact_b = uuid.uuid4().hex, uuid.uuid4().hex
    insert = (
        "INSERT INTO artifacts (workspace_id, artifact_id, checksum, size_bytes, media_type, created_at, state) "
        "VALUES (%s, %s, %s, 1, 'text/plain', '2026-01-01T00:00:00+00:00', 'ready')"
    )
    _in_workspace(app_connection, workspace_a, insert, (workspace_a, artifact_a, "a" * 64))
    _in_workspace(app_connection, workspace_b, insert, (workspace_b, artifact_b, "b" * 64))

    # Same connection, different transaction and tenant: no prior setting leaks.
    assert _in_workspace(app_connection, workspace_a, "SELECT artifact_id FROM artifacts ORDER BY artifact_id") == [(artifact_a,)]
    assert _in_workspace(app_connection, workspace_b, "SELECT artifact_id FROM artifacts ORDER BY artifact_id") == [(artifact_b,)]

    with pytest.raises(Exception) as rejected:
        _in_workspace(app_connection, workspace_b, insert, (workspace_a, uuid.uuid4().hex, "c" * 64))
    assert "row-level security" in str(rejected.value).lower()


def test_all_workspace_tables_have_forced_rls(app_connection):
    table_names = {
        "projects", "smoke_attempts", "runs", "run_cases", "run_case_attempts", "trace_events",
        "cost_summaries", "run_case_metric_results", "run_metric_results", "score_revisions", "custom_evals",
        "object_versions", "version_heads", "artifacts", "version_artifacts", "legacy_definition_migrations",
        "jobs", "job_outbox", "secret_refs", "secret_audit",
        "harness_sessions", "harness_messages", "datasets", "dataset_import_reports", "targets",
    }
    # Switch back only for PostgreSQL catalog metadata; no tenant data is read.
    app_connection.execute("RESET ROLE")
    rows = app_connection.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relnamespace = current_schema()::regnamespace AND relname = ANY(%s)",
        (list(table_names),),
    ).fetchall()
    assert {row[0] for row in rows} == table_names
    assert all(row[1] and row[2] for row in rows)


def test_non_bypass_app_login_can_open_an_already_migrated_store(app_connection):
    """The web process must not need the bootstrap role just to start."""
    role = f"rls_app_{uuid.uuid4().hex}"
    provision_application_login(PG_URL, role, "disposable-test-password")
    parsed = urlsplit(PG_URL)
    app_url = urlunsplit((
        parsed.scheme,
        f"{role}:disposable-test-password@{parsed.hostname}:{parsed.port}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))
    app_store = Storage(app_url)
    try:
        app_store.create_schema()
    finally:
        app_store.close()
