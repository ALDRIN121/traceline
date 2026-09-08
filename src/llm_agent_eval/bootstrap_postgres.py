"""One-time PostgreSQL bootstrap for the non-bypass API login.

Run this with the database maintenance URL before starting the web process.
It installs forward-only schema changes, then creates or rotates the login
that inherits the restricted ``llm_eval_app`` role.  It never prints secrets.
"""

from __future__ import annotations

import os
import re

from .storage import Storage, psycopg


_ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")


def provision_application_login(admin_url: str, username: str, password: str) -> None:
    """Create/rotate an ordinary LOGIN role and grant only the application role."""
    if psycopg is None:
        raise RuntimeError("psycopg is required for PostgreSQL bootstrap")
    if not _ROLE_NAME.fullmatch(username):
        raise ValueError("application database username must be a PostgreSQL role identifier")
    if not password:
        raise ValueError("application database password is required")
    store = Storage(admin_url)
    try:
        if not store._is_postgres:
            raise ValueError("a PostgreSQL maintenance URL is required")
        store.create_schema()
    finally:
        store.close()

    connection = psycopg.connect(admin_url, autocommit=True)
    try:
        identifier = psycopg.sql.Identifier(username)
        exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,)).fetchone()
        statement = "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}" if exists else \
            "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD {}"
        connection.execute(psycopg.sql.SQL(statement).format(identifier, psycopg.sql.Literal(password)))
        connection.execute(psycopg.sql.SQL("GRANT llm_eval_app TO {}").format(identifier))
    finally:
        connection.close()


def main() -> None:
    admin_url = os.environ.get("DATABASE_URL") or os.environ.get("LLM_AGENT_EVAL_DB")
    if not admin_url:
        raise ValueError("DATABASE_URL is required for PostgreSQL bootstrap")
    username = os.environ.get("LLM_AGENT_EVAL_APP_DB_USER", "eval_app")
    password = os.environ.get("LLM_AGENT_EVAL_APP_DB_PASSWORD", "")
    provision_application_login(admin_url, username, password)


if __name__ == "__main__":
    main()
