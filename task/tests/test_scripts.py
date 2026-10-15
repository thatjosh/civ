"""Tests for the seed and dump helpers that do not need a live board.

The live paths (Operator, psycopg) are exercised by the integration step; here
the logic around them is checked with fakes, so a broken tasks.json or a
transcript renderer that crashes on an empty board is caught before a demo.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import board_dump  # noqa: E402
import reset_db  # noqa: E402
import seed_tasks  # noqa: E402


class FakeOperator:
    def __init__(self):
        self.created: list[tuple] = []

    def create_task(self, task_id, title, description, priority_base=0.0):
        self.created.append((task_id, title, description, priority_base))
        return len(self.created)


def test_seed_creates_only_missing_tasks():
    tasks = seed_tasks.load_tasks(ROOT / "task" / "tasks.json")
    op = FakeOperator()
    created = seed_tasks.seed(op, tasks, existing={"T-001", "T-002"})
    assert created == [t["id"] for t in tasks if t["id"] not in ("T-001", "T-002")]
    assert all(c[0] not in ("T-001", "T-002") for c in op.created)
    # Running again against everything present creates nothing: idempotent.
    assert seed_tasks.seed(FakeOperator(), tasks, existing={t["id"] for t in tasks}) == []


def test_task_body_carries_result_file_and_format():
    task = seed_tasks.load_tasks(ROOT / "task" / "tasks.json")[0]
    body = seed_tasks.task_body(task)
    assert task["result_file"] in body and task["format"] in body and task["description"] in body


def test_config_seeds_match_contract_knobs():
    # reset_db re-seeds through the one shared copy of the seeds in harness/board_mcp/seed.py.
    from harness.board_mcp.seed import CONFIG_SEEDS

    assert {"post_rate_per_h", "vote_quota", "lease_length_s", "decay_power", "max_handoff_tokens"} <= set(CONFIG_SEEDS)
    for value, lo, hi, step in CONFIG_SEEDS.values():
        assert lo <= value <= hi and step > 0
    assert "seed_config" in (ROOT / "scripts" / "reset_db.py").read_text()


def test_render_handles_empty_and_populated_board():
    assert "=== tasks" in board_dump.render({"messages": [], "votes": [], "tasks": [], "agents": []})
    now = dt.datetime(2026, 10, 15, 9, 0, 0)
    data = {
        "messages": [
            (1, "operator", "task", None, None, "Count the households", [], None, now, 0.0),
            (2, "sol-001", "finding", 1, None, "households: 12", [], 1, now, 1.5),
            (3, "operator", "broadcast", None, None, "Prefer breadth", [], None, now, 0.0),
        ],
        "votes": [(2, "sol-002", 1, 1.0, "matches my count")],
        "tasks": [("T-001", "Census headcount", "done", 1, None, 0, 1.0, "all fields correct", "sol-001", "shared/abc-census.txt")],
        "agents": [("sol-001", "solver", 1, "ended", 1200, 300, 0.4)],
    }
    text = board_dump.render(data)
    assert "T-001 Census headcount" in text and "score 1.00" in text
    assert "vote +1 by sol-002: matches my count" in text
    assert "outside threads" in text and "Prefer breadth" in text
    assert "(had read to #1)" in text


def test_tasks_json_round_trips():
    raw = json.loads((ROOT / "task" / "tasks.json").read_text())
    assert raw["pack"] == "hollowmere" and raw["materials_dir"] == "task/materials"
