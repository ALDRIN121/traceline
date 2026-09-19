"""Install additive workflow DDL; PostgreSQL migration requires an admin role."""

import hashlib
from pathlib import Path
import sysconfig


_SYSTEM_WORKSPACE = "__system__"


def workflow_migration() -> str:
    relative = Path("migrations/0001_workflow_objects.sql")
    source = Path(__file__).resolve().parents[2] / relative
    installed = Path(sysconfig.get_path("data")) / "share/llm-agent-eval" / relative
    return (source if source.is_file() else installed).read_text(encoding="utf-8")


def workflow_migrations() -> list[Path]:
    relative = Path("migrations")
    source = Path(__file__).resolve().parents[2] / relative
    installed = Path(sysconfig.get_path("data")) / "share/llm-agent-eval" / relative
    directory = source if source.is_dir() else installed
    return sorted(directory.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def _upgrade_sqlite_version_kinds(connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='object_versions'"
    ).fetchone()
    if row is None or "'evaluator'" in row[0]:
        return
    ddl = row[0]
    for needle in (
        "'connection','dashboard','model_profile','model_selection','judge_rubric'",
        "'connection','dashboard'",
    ):
        if needle in ddl:
            ddl = ddl.replace(needle, needle + ",'evaluator'", 1)
            break
    if ddl == row[0]:
        raise RuntimeError("Unrecognized object_versions schema; migration refused")
    connection.commit()
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    connection.execute("PRAGMA foreign_keys=OFF")
    try:
        connection.execute("BEGIN IMMEDIATE")
        triggers = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='object_versions'"
        ).fetchall()
        connection.execute(ddl.replace("object_versions", "object_versions_upgrade", 1))
        connection.execute("INSERT INTO object_versions_upgrade SELECT * FROM object_versions")
        connection.execute("DROP TABLE object_versions")
        connection.execute("ALTER TABLE object_versions_upgrade RENAME TO object_versions")
        for trigger in triggers:
            connection.execute(trigger[0])
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Version-kind migration failed foreign-key validation")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")


def install_workflow_schema(connection, *, postgres: bool) -> None:
    if postgres:
        _install_postgres_workflow_schema(connection)
        return

    for migration in workflow_migrations():
        sql = migration.read_text(encoding="utf-8")
        sql = sql.split("-- POSTGRESQL SECURITY", 1)[0]
        connection.executescript(sql)
    _upgrade_sqlite_version_kinds(connection)
    connection.executescript("""
    CREATE TRIGGER IF NOT EXISTS immutable_versions_update BEFORE UPDATE ON object_versions
      BEGIN SELECT RAISE(ABORT, 'Object versions are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS immutable_versions_delete BEFORE DELETE ON object_versions
      BEGIN SELECT RAISE(ABORT, 'Object versions are immutable'); END;
    """
    )


def _install_postgres_workflow_schema(connection) -> None:
    """Apply each PostgreSQL migration atomically and record its checksum.

    A bootstrap can be interrupted after any statement.  The ledger is written
    in the same transaction as the migration, so an interrupted migration is
    either absent from the ledger and safely retried, or present with the exact
    SQL checksum that was applied.  A changed migration is rejected rather
    than silently reinterpreted against a live database.
    """
    connection.execute("""
    CREATE TABLE IF NOT EXISTS schema_migrations (
      workspace_id TEXT NOT NULL DEFAULT '__system__',
      version TEXT PRIMARY KEY,
      checksum TEXT NOT NULL,
      applied_at TEXT NOT NULL
    )
    """)
    columns = {
        row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=?",
            ("schema_migrations",),
        ).fetchall()
    }
    if "workspace_id" not in columns:
        # Development databases created by the pre-journal prototype had no
        # tenant marker. Upgrade that local ledger before enforcing RLS.
        connection.execute(
            "ALTER TABLE schema_migrations ADD COLUMN workspace_id TEXT "
            "NOT NULL DEFAULT '__system__'"
        )
    connection.execute("ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY")
    connection.execute("ALTER TABLE schema_migrations FORCE ROW LEVEL SECURITY")
    connection.execute("DROP POLICY IF EXISTS migration_ledger_system ON schema_migrations")
    connection.execute(
        "CREATE POLICY migration_ledger_system ON schema_migrations USING "
        "(workspace_id='__system__') WITH CHECK (workspace_id='__system__')"
    )
    connection.execute("REVOKE ALL ON schema_migrations FROM PUBLIC")
    connection.commit()

    for migration in workflow_migrations():
        version = migration.stem
        sql = migration.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        recorded = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE workspace_id=? AND version=?",
            (_SYSTEM_WORKSPACE, version),
        ).fetchone()
        if recorded is not None:
            if recorded[0] != checksum:
                raise RuntimeError(
                    f"Migration {version} checksum changed after it was applied"
                )
            continue

        try:
            connection.execute("BEGIN")
            # Migration files contain PostgreSQL format tokens such as %I
            # inside server-side format() calls. Use the raw script channel;
            # the SQLite-compatible execute wrapper must not treat those
            # tokens as client-side placeholders.
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations(workspace_id,version,checksum,applied_at) "
                "VALUES (?,?,?,CURRENT_TIMESTAMP) ON CONFLICT (version) DO NOTHING",
                (_SYSTEM_WORKSPACE, version, checksum),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.rollback()
            raise
