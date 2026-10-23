"""The dashboard renders from an empty database and from a seeded one (M7)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from harness.tests.conftest import TEST_URL  # noqa: E402  (the migrated civ_test database)
from harness.tests.conftest import clean_tables, db, migrated_url, op, make_agent, task  # noqa: E402,F401

import dashboard  # noqa: E402  (scripts/dashboard.py)


def _well_formed(page: str) -> None:
    assert page.startswith("<!doctype html>") and page.rstrip().endswith("</html>")
    assert page.count("<svg") == page.count("</svg>")
    assert page.count("<section") == page.count("</section>")
    assert "—" not in page
    # The palette is the mockup's: no blue series anywhere.
    for blue in ("#1f77b4", "#007aff", "#2563eb", "#3b82f6", "blue"):
        assert blue not in page.lower()


def test_renders_on_an_empty_database(db, tmp_path):
    out = tmp_path / "empty.html"
    assert dashboard.main(["--out", str(out), "--db-url", TEST_URL, "--title", "empty civ"]) == 0
    page = out.read_text(encoding="utf-8")
    _well_formed(page)
    assert "heartbeat" in page and "none" in page
    assert "no agents have run yet" in page and "no tasks completed yet" in page
    assert "no supervisor rounds yet" in page and "nothing in the inbox" in page and "no chaos injected" in page
    assert out.stat().st_size < 2 * 1024 * 1024


def test_renders_a_seeded_run_with_markers(db, op, make_agent, task, tmp_path):
    sol, _ = make_agent("solver", agent_id="sol-001", lineage="L-001")
    sup, _ = make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")
    with db.transaction() as conn:
        conn.execute("insert into control_state (key, value) values ('heartbeat', %s::jsonb) on conflict (key) do update set value = excluded.value",
                     (json.dumps({"at": "2026-10-23T10:00:00+00:00", "pid": 77, "epoch": 2}),))
        conn.execute("insert into events (agent_id, type, payload) values (%s, 'agent.turn', %s)", (sol.id, json.dumps({"tokens_in": 1200, "tokens_out": 40})))
        conn.execute("insert into events (agent_id, type, payload) values (%s, 'task.completed', %s)", (sol.id, json.dumps({"task_id": "T-001", "score": 1.0})))
        conn.execute("update tasks set status = 'done', score = 1.0, completed_at = now() where id = 'T-001'")
        conn.execute("insert into events (agent_id, type, payload) values ('operator', 'operator.chaos', %s)", (json.dumps({"action": "kill", "agent_id": "sol-001"}),))
        conn.execute("insert into events (agent_id, type, payload) values ('operator', 'knob.preset', %s)", (json.dumps({"knob": "kill_grace_turns", "from": 2, "to": 0, "reason": "fault"}),))
        conn.execute("insert into messages (author_id, kind, body) values (%s, 'report', '[ping: succession_failing] two windows of kills')", (sup.id,))
        conn.execute("insert into events (agent_id, type, payload) values (%s, 'agent.ended', %s)", (sol.id, json.dumps({"cost_usd": 0.05})))
    rid = op.open_round(1)
    op.round_brief(rid, supervisors=1)
    with db.transaction() as conn:
        conn.execute("update rounds set brief = brief || %s::jsonb where id = %s", (json.dumps({"succession": {"handoffs": 2, "stubs": 1, "kills": 1, "success_rate": 0.667}}), rid))
    out = tmp_path / "seeded.html"
    page = dashboard.render(TEST_URL, "seeded civ")
    out.write_text(page, encoding="utf-8")
    _well_formed(page)
    assert "pid 77, epoch 2" in page
    assert "1 of 1" in page and "kill sol-001" in page
    assert "real handoffs" in page and "kill x1" in page
    assert 'class="pill ping"' in page and "two windows of kills" in page
    assert "kill_grace_turns" in page and "solver $0.05" in page
    assert out.stat().st_size < 2 * 1024 * 1024
