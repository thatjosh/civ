"""Seed the task pack onto the board. Safe to run twice.

Each task becomes a ``kind=task`` root message plus a row in ``tasks`` through
``Operator.create_task``. The root message body carries the description, the
result file name and the format line, because the body is the only thing a
solver sees when it claims, so everything it needs to produce a gradeable
result has to be in there.

Usage: python scripts/seed_tasks.py [--tasks task/tasks.json | --pack task/packs/<name>] [--database-url URL]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB_URL = "postgresql://127.0.0.1:5432/civ"


def load_tasks(path: Path) -> list[dict]:
    return json.loads(path.read_text())["tasks"]


def task_body(task: dict) -> str:
    """The text a solver reads when it claims the task."""
    return (
        f"{task['description']}\n\n"
        f"Result file: {task['result_file']} (write it under work/ in your directory and pass its path to task_complete).\n"
        f"Format: {task['format']}"
    )


def seed(op, tasks: list[dict], existing: set[str]) -> list[str]:
    """Create every task not already on the board; return the ids created."""
    created = []
    for task in tasks:
        if task["id"] in existing:
            continue
        op.create_task(task["id"], task["title"], task_body(task), priority_base=float(task.get("priority_base", 0)))
        created.append(task["id"])
    return created


def existing_task_ids(db_url: str) -> set[str]:
    import psycopg

    with psycopg.connect(db_url) as conn:
        return {row[0] for row in conn.execute("select id from tasks")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", default=None, help="a tasks.json; the default is the pack's")
    parser.add_argument("--pack", default=os.environ.get("CIV_PACK"),
                        help="a pack directory (task/packs/<name>); defaults to CIV_PACK, else the repo pack")
    parser.add_argument("--database-url", default=os.environ.get("CIV_DATABASE_URL", DEFAULT_DB_URL))
    args = parser.parse_args(argv)

    from harness.board_mcp.db import Db
    from harness.board_mcp.operator import Operator
    from task.pack import Pack

    # --tasks still wins for the odd one-off file; otherwise the pack decides, so the seeder and
    # the server (which reads CIV_PACK for the answers) cannot seed one pack and score another.
    tasks_path = Path(args.tasks) if args.tasks else (Pack(Path(args.pack)) if args.pack else Pack.default()).tasks_path
    tasks = load_tasks(tasks_path)
    existing = existing_task_ids(args.database_url)
    created = seed(Operator(Db(args.database_url)), tasks, existing)
    skipped = len(tasks) - len(created)
    print(f"seeded {len(created)} task(s), {skipped} already present: {', '.join(created) or 'none new'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
