"""Opt-in PostgreSQL migration journal and interrupted-bootstrap rehearsal."""

from __future__ import annotations

import os

import pytest

from llm_agent_eval.migrations import workflow_migrations
from llm_agent_eval.storage import Storage


@pytest.mark.integration
def test_postgres_migration_journal_recovers_after_unrecorded_ddl():
    database_url = os.environ.get("TEST_MIGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_MIGRATION_DATABASE_URL is required")

    storage = Storage(database_url)
    try:
        storage.create_schema(migrate=True)
        version = workflow_migrations()[-1].stem
        storage._conn.execute("DELETE FROM schema_migrations WHERE version=?", (version,))
        storage._conn.commit()

        # The table DDL survived, but the journal entry did not. A second
        # bootstrap must safely replay the idempotent migration and restore the
        # durable record rather than treating the database as irrecoverable.
        storage.create_schema(migrate=True)
        rows = storage._conn.execute(
            "SELECT version,checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row[0] for row in rows] == [migration.stem for migration in workflow_migrations()]
        assert storage._conn.execute(
            "SELECT to_regclass('export_snapshots')"
        ).fetchone()[0] == "export_snapshots"

        checksum = rows[-1][1]
        storage._conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            ("tampered", version),
        )
        storage._conn.commit()
        with pytest.raises(RuntimeError, match="checksum changed"):
            storage.create_schema(migrate=True)
        storage._conn.execute(
            "UPDATE schema_migrations SET checksum=? WHERE version=?",
            (checksum, version),
        )
        storage._conn.commit()
    finally:
        storage.close()
