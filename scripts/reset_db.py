"""Wipe the board for a clean demo and re-seed the config knobs.

Everything durable lives in Postgres, so a reset is one TRUNCATE across the
schema in docs/CONTRACT.md plus the config seeds, and then the two runtime
directories (``agents/`` and ``shared/``) are emptied because they are caches
of what the database already knows (design doc, crashes and recovery).

Usage: python scripts/reset_db.py [--database-url URL] [--keep-runtime]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"

# Dependency order does not matter with CASCADE, but the list is the contract's
# table set, so a table missing from the database shows up as an error here
# rather than as silent stale rows later.
TABLES = [
    "operator_acks", "operator_pins", "broadcast_seen", "session_reads",
    "handoffs", "events", "votes", "tasks", "messages", "agents", "config",
]

# knob, value, min, max, step (from docs/CONTRACT.md); cooldown keeps its default.
CONFIG_SEEDS = [
    ("post_rate_per_h", 30, 5, 120, 5),
    ("vote_rate_per_h", 20, 5, 60, 5),
    ("vote_quota", 1, 0, 3, 1),
    ("lease_length_s", 1800, 300, 7200, 300),
    ("max_attempts", 3, 1, 6, 1),
    ("backoff_base_s", 60, 10, 600, 10),
    ("top_k", 10, 5, 50, 5),
    ("long_body_chars", 2000, 500, 8000, 500),
    ("decay_offset_h", 2, 1, 12, 1),
    ("decay_power", 1.5, 1, 3, 0.25),
    ("handoff_pct", 80, 50, 90, 5),
    ("kill_pct", 90, 60, 95, 5),
    ("max_handoff_tokens", 4000, 1000, 8000, 500),
]


def reset_database(db_url: str) -> None:
    import psycopg

    with psycopg.connect(db_url) as conn:
        existing = {r[0] for r in conn.execute("select tablename from pg_tables where schemaname = 'public'")}
        present = [t for t in TABLES if t in existing]
        if present:
            conn.execute("truncate " + ", ".join(present) + " restart identity cascade")
        if "config" in existing:
            conn.executemany(
                "insert into config (knob, value, min, max, step) values (%s, %s, %s, %s, %s)",
                CONFIG_SEEDS,
            )
        conn.commit()
    missing = [t for t in TABLES if t not in existing]
    if missing:
        print(f"warning: tables not yet migrated, skipped: {', '.join(missing)}")


def clear_runtime(root: Path) -> None:
    for name in ("agents", "shared"):
        directory = root / name
        directory.mkdir(exist_ok=True)
        for child in directory.iterdir():
            if child.name == ".gitkeep":
                continue
            shutil.rmtree(child) if child.is_dir() else child.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    parser.add_argument("--keep-runtime", action="store_true", help="leave agents/ and shared/ alone")
    args = parser.parse_args(argv)
    reset_database(args.database_url)
    if not args.keep_runtime:
        clear_runtime(Path(os.environ.get("CIV_ROOT", ROOT)))
    print(f"reset {args.database_url}" + ("" if args.keep_runtime else " and cleared agents/ and shared/"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
