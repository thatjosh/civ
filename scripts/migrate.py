"""Apply numbered, forward-only SQL migrations to a civ database.

Usage: python scripts/migrate.py [--url postgresql://127.0.0.1:5432/civ]

Each file under harness/board_mcp/migrations/ named NNNN_name.sql runs once, inside its own
transaction, and is recorded in schema_migrations. Because the files are forward-only, re-running
this script is always safe: applied files are skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "harness" / "board_mcp" / "migrations"
DEFAULT_URL = os.environ.get("CIV_DATABASE_URL", "postgresql://127.0.0.1:5432/civ")


def pending_migrations(conn: psycopg.Connection) -> list[Path]:
    with conn.cursor() as cur:
        cur.execute("create table if not exists schema_migrations (name text primary key, applied_at timestamptz not null default now())")
        cur.execute("select name from schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name[:4].isdigit())
    return [p for p in files if p.name not in applied]


def apply(url: str, log=print) -> list[str]:
    """Apply pending migrations and return their names."""
    applied: list[str] = []
    with psycopg.connect(url) as conn:
        for path in pending_migrations(conn):
            # One transaction per file, so a failing migration leaves the database at the previous file.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(path.read_text())
                    cur.execute("insert into schema_migrations (name) values (%s)", (path.name,))
            applied.append(path.name)
            log(f"applied {path.name}")
    if not applied:
        log("nothing to apply")
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args(argv)
    apply(args.url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
