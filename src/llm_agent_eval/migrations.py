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


def install_workflow_schema(connection, *, postgres: bool) -> None:
    for migration in workflow_migrations():
        sql = migration.read_text(encoding="utf-8")
        if not postgres:
            sql = sql.split("-- POSTGRESQL SECURITY", 1)[0]
        connection.executescript(sql)
    if not postgres:
        connection.executescript("""
        CREATE TRIGGER IF NOT EXISTS immutable_versions_update BEFORE UPDATE ON object_versions
          BEGIN SELECT RAISE(ABORT, 'Object versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_versions_delete BEFORE DELETE ON object_versions
          BEGIN SELECT RAISE(ABORT, 'Object versions are immutable'); END;
        """
        )
