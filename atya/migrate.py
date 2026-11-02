"""Apply migrations/NNN_*.sql in order, each in its own transaction.

Usage: DATABASE_URL=... python -m atya.migrate
"""

from __future__ import annotations

import logging
import sys

import psycopg

from .config import MIGRATIONS_DIR, database_url

log = logging.getLogger("migrate")


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations. Returns the names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.commit()
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")):
        if path.name in done:
            continue
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
        log.info("applied %s", path.name)
        applied.append(path.name)
    return applied


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with psycopg.connect(database_url()) as conn:
        applied = migrate(conn)
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")
    sys.exit(0)


if __name__ == "__main__":
    main()
