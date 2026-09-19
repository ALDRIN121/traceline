"""Install additive workflow DDL; PostgreSQL migration requires an admin role."""

from pathlib import Path
import sysconfig


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
    for migration in workflow_migrations():
        sql = migration.read_text(encoding="utf-8")
        if not postgres:
            sql = sql.split("-- POSTGRESQL SECURITY", 1)[0]
        connection.executescript(sql)
    if not postgres:
        _upgrade_sqlite_version_kinds(connection)
        connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS immutable_versions_update BEFORE UPDATE ON object_versions
          BEGIN SELECT RAISE(ABORT, 'Object versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_versions_delete BEFORE DELETE ON object_versions
          BEGIN SELECT RAISE(ABORT, 'Object versions are immutable'); END;
        """
        )
