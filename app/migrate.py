"""Apply migrations/NNN_*.sql in order. Safe to run repeatedly."""

from __future__ import annotations

from pathlib import Path

import psycopg

from .config import settings
from .db import connect

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = ROOT / "migrations"


def main() -> None:
    with connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
    for path in sorted(MIGRATIONS.glob("*.sql")):
        with connect() as conn:
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,)
            ).fetchone()
            if already:
                print(f"skip {path.name}")
                continue
            print(f"apply {path.name}")
            with conn.transaction():
                for statement in [s for s in path.read_text(encoding="utf-8").split(";") if s.strip()]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(name) VALUES (%s)", (path.name,)
                )


if __name__ == "__main__":
    try:
        main()
    except psycopg.Error as error:
        print(f"migration failed: {error}")
        raise
