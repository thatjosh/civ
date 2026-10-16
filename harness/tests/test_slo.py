"""M4: the five SLOs, succession health and the kill classification, against synthetic events and
the recorded 30k-window run."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.board_mcp.slo import classify, compute_slos, compute_succession, status_snapshot
from harness.tests.conftest import TEST_URL

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "docs" / "demo" / "2026-10-15-m3-fixture.sql"
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=10)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def event(db, ts: datetime, agent_id: str | None, type: str, payload: dict | None = None) -> None:
    db.execute("insert into events (ts, agent_id, type, payload) values (%s, %s, %s, %s)",
               (ts, agent_id, type, json.dumps(payload or {})))


def message(db, ts: datetime, author: str, kind: str, body: str, thread_id: int | None = None) -> int:
    return int(db.fetch_one(
        "insert into messages (author_id, kind, body, thread_id, created_at) values (%s, %s, %s, %s, %s) returning id",
        (author, kind, body, thread_id, ts))["id"])


def slos(db):
    with db.transaction() as conn:
        return compute_slos(conn, T0, T1)


def succession(db, start=T0, end=T1):
    with db.transaction() as conn:
        return compute_succession(conn, start, end)


# --- the five SLOs ----------------------------------------------------------------------------


def test_completions_per_mtok_counts_verified_completions_over_tokens(db):
    event(db, at(1), "a", "task.completed", {"task_id": "T-1", "score": 1.0})
    event(db, at(2), "a", "task.completed", {"task_id": "T-2", "score": 0.5})
    event(db, at(3), "a", "task.completed", {"task_id": "T-3", "score": None})  # unverified: not counted
    event(db, at(4), "a", "agent.turn", {"tokens_in": 400_000, "tokens_out": 100_000})
    out = slos(db)["completions_per_mtok"]
    assert out == {"value": pytest.approx(4.0), "n": 2}


def test_completions_per_mtok_ignores_supervisor_tokens(db, make_agent):
    sup, _ = make_agent("supervisor")
    event(db, at(1), "a", "task.completed", {"task_id": "T-1", "score": 1.0})
    event(db, at(2), "a", "agent.turn", {"tokens_in": 400_000, "tokens_out": 100_000})
    # Twice the solver spend again, by a supervisor: must not move the number.
    event(db, at(3), sup.id, "agent.turn", {"tokens_in": 900_000, "tokens_out": 100_000})
    assert slos(db)["completions_per_mtok"] == {"value": pytest.approx(2.0), "n": 1}


def test_completions_without_tokens_is_none_not_infinity(db):
    event(db, at(1), "a", "task.completed", {"task_id": "T-1", "score": 1.0})
    assert slos(db)["completions_per_mtok"] == {"value": None, "n": 1}


def test_blocked_failed_rate_is_releases_over_claims(db):
    assert slos(db)["blocked_failed_rate"] == {"value": 0.0, "n": 0}
    for i in range(4):
        event(db, at(i), "a", "task.claimed", {"task_id": f"T-{i}"})
    event(db, at(10), "a", "task.released", {"task_id": "T-1", "attempts": 1, "status": "open"})
    assert slos(db)["blocked_failed_rate"] == {"value": pytest.approx(0.25), "n": 4}


def test_duplicate_rate_matches_restatements_by_other_authors_only(db):
    base = "the mill burned down in the summer of eighteen fifty two according to the parish log"
    message(db, at(-100), "a", "finding", base, thread_id=None)  # earlier, outside the window: still counts as earlier
    message(db, at(1), "a", "finding", base + " again", thread_id=None)  # same author restating: not a duplicate
    message(db, at(2), "c", "finding", "the ledger payees were reeve ashworth and the vestry clerk for eighteen fifty two", thread_id=None)
    message(db, at(3), "b", "finding", base + " entry", thread_id=None)  # duplicate of a's
    message(db, at(4), "d", "finding", "too short to count", thread_id=None)
    message(db, at(5), "e", "finding", "[T-001 completed] " + base, thread_id=None)  # completion summary: excluded
    out = slos(db)["duplicate_rate"]
    # Considered: a (again), c, b. Duplicates: b only.
    assert out["n"] == 3 and out["value"] == pytest.approx(1 / 3)


def test_duplicates_are_per_thread(db):
    body = "the mill burned down in the summer of eighteen fifty two according to the parish log"
    root1 = message(db, at(-10), "operator", "task", "T-1")
    root2 = message(db, at(-10), "operator", "task", "T-2")
    message(db, at(1), "a", "finding", body, thread_id=root1)
    message(db, at(2), "b", "finding", body, thread_id=root2)
    assert slos(db)["duplicate_rate"] == {"value": 0.0, "n": 2}


def test_lease_wait_median_from_created_released_and_reopened(db):
    event(db, at(0), "operator", "task.created", {"task_id": "T-1"})
    event(db, at(10), "a", "task.claimed", {"task_id": "T-1"})                        # waited 10
    event(db, at(20), "a", "task.released", {"task_id": "T-1", "attempts": 1, "status": "open"})
    event(db, at(50), "b", "task.claimed", {"task_id": "T-1"})                        # waited 30
    event(db, at(60), "b", "task.released", {"task_id": "T-1", "attempts": 2, "status": "blocked"})
    event(db, at(70), "c", "task.claimed", {"task_id": "T-1"})                        # not claimable: no wait recorded
    event(db, at(80), "sup", "task.reopened", {"task_id": "T-1"})
    event(db, at(85), "d", "task.claimed", {"task_id": "T-1"})                        # waited 5
    assert slos(db)["lease_wait_median_s"] == {"value": pytest.approx(10.0), "n": 3}


def test_lease_wait_is_none_without_matched_claims(db):
    assert slos(db)["lease_wait_median_s"] == {"value": None, "n": 0}


def test_handoff_success_rate(db):
    assert slos(db)["handoff_success_rate"] == {"value": None, "n": 0}
    for i in range(3):
        event(db, at(i), f"s{i}", "agent.handed_off", {"generation": 1})
    for i in range(2):
        event(db, at(10 + i), f"k{i}", "agent.killed", {"generation": 1, "reason": "kill threshold reached"})
    assert slos(db)["handoff_success_rate"] == {"value": pytest.approx(0.6), "n": 5}


def test_window_is_half_open(db):
    event(db, T0, "a", "task.claimed", {"task_id": "T-1"})            # at the start: in
    event(db, T1, "a", "task.claimed", {"task_id": "T-2"})            # at the end: out (next window's)
    event(db, T0 - timedelta(seconds=1), "a", "task.claimed", {"task_id": "T-3"})
    assert slos(db)["blocked_failed_rate"]["n"] == 1


# --- succession health -------------------------------------------------------------------------


def register(db, op, agent_id: str, lineage: str, generation: int = 1) -> None:
    op.register_agent(agent_id, "solver", lineage, f"/tmp/{agent_id}", generation)


def test_succession_counts_medians_refusals_and_generations(db, op):
    register(db, op, "sol-001", "L-001")
    register(db, op, "sol-001-g2", "L-001", 2)
    register(db, op, "sol-002", "L-002")
    event(db, at(1), "sol-001", "agent.handoff_requested")
    event(db, at(2), "sol-002", "agent.handoff_requested")
    event(db, at(21), "sol-001", "agent.handed_off", {"generation": 1, "requested": True, "turns_since_request": 2,
                                                       "seconds_since_request": 20.0, "grace_turns": 2})
    event(db, at(32), "sol-002", "agent.handed_off", {"generation": 1, "requested": True, "turns_since_request": 4,
                                                       "seconds_since_request": 30.0, "grace_turns": 2})
    event(db, at(40), "sol-002", "tool.refused", {"tool": "board_post", "error": "hand off now: call civ_handoff with the five headings"})
    event(db, at(41), "sol-002", "tool.refused", {"tool": "board_post", "error": "hand off now: call civ_handoff with the five headings"})
    event(db, at(42), "sol-001", "tool.refused", {"tool": "task_claim", "error": "cast 1 more vote"})
    out = succession(db)
    assert out["requests"] == 2 and out["handoffs"] == 2 and out["stubs"] == 0 and out["kills"] == 0
    assert out["success_rate"] == 1.0
    assert out["median_turns_to_handoff"] == 3.0 and out["median_seconds_to_handoff"] == 25.0
    assert out["refusals"] == {"hand off now: call civ_handoff with the five headings": 2, "cast 1 more vote": 1}
    assert out["generations"] == {"L-001": 2, "L-002": 1}
    assert out["incidents"] == []


def test_incident_packet_is_built_from_the_new_format_events(db, op):
    register(db, op, "sol-003", "L-003")
    event(db, at(1), "sol-003", "agent.turn", {"turn": 5, "tokens_in": 100, "tokens_out": 1, "context_pct": 79.0})
    event(db, at(2), "sol-003", "agent.handoff_requested")
    event(db, at(2), "sol-003", "agent.handoff_sent", {"generation": 1, "turn": 5, "context_pct": 86.5, "kill_due": False, "grace_turns": 2})
    event(db, at(3), "sol-003", "tool.board_read", {"n": 3})
    event(db, at(4), "sol-003", "tool.refused", {"tool": "civ_handoff", "error": "handoff is missing headings: ## Open threads"})
    event(db, at(5), "sol-003", "tool.refused", {"tool": "board_post", "error": "hand off now"})
    event(db, at(9), "sol-003", "agent.killed", {"generation": 1, "reason": "kill threshold reached", "requested": True,
                                                  "turns_since_request": 3, "seconds_since_request": 7.0, "grace_turns": 2})
    event(db, at(9), "sol-003", "agent.stub_written", {"handoff_id": 1, "posts": 2, "task": "T-007"})
    out = succession(db)
    assert out["kills"] == 1 and out["stubs"] == 1 and out["success_rate"] == 0.0
    packet = out["incidents"][0]
    assert packet == {
        "agent_id": "sol-003", "lineage_id": "L-003", "generation": 1, "reason": "kill threshold reached",
        "requested": True, "turns_since_request": 3, "seconds_since_request": 7.0, "grace_turns": 2,
        "context_pct_at_request": 86.5,
        "tool_calls_after_request": ["tool.board_read", "tool.refused(civ_handoff)", "tool.refused(board_post)"],
        "handoff_refusals": ["handoff is missing headings: ## Open threads"],
        "task": "T-007", "classification": "rejected",
    }


def test_context_at_request_falls_back_to_the_last_turn_before_the_request(db, op):
    register(db, op, "sol-004", "L-004")
    event(db, at(1), "sol-004", "agent.turn", {"turn": 9, "tokens_in": 100, "tokens_out": 1, "context_pct": 83.2})
    event(db, at(2), "sol-004", "agent.handoff_requested")
    event(db, at(3), "sol-004", "agent.handoff_sent", {"generation": 1})  # legacy: no context_pct
    event(db, at(8), "sol-004", "agent.killed", {"generation": 1, "reason": "kill threshold reached"})
    packet = succession(db)["incidents"][0]
    assert packet["context_pct_at_request"] == 83.2 and packet["requested"] is True
    assert packet["turns_since_request"] is None and packet["seconds_since_request"] == 6.0
    assert packet["grace_turns"] == 2  # the current knob stands in for a legacy payload


def base_packet(**over):
    packet = {"agent_id": "x", "lineage_id": "L", "generation": 1, "reason": "kill threshold reached", "requested": True,
              "turns_since_request": 2, "seconds_since_request": 30.0, "grace_turns": 2, "context_pct_at_request": 85.0,
              "tool_calls_after_request": [], "handoff_refusals": [], "task": None}
    packet.update(over)
    return packet


def test_classification_rules_in_order():
    assert classify(base_packet(requested=False, handoff_refusals=["x"])) == "never_asked"
    assert classify(base_packet(reason="handoff timed out", handoff_refusals=["x"])) == "timed_out"
    assert classify(base_packet(handoff_refusals=["too long"], turns_since_request=0)) == "rejected"
    assert classify(base_packet(turns_since_request=1, grace_turns=2, tool_calls_after_request=["tool.board_read"])) == "no_chance"
    # grace 0: "turns < grace" never holds, but zero turns of its own is still no chance.
    assert classify(base_packet(turns_since_request=0, grace_turns=0, tool_calls_after_request=["tool.board_read"])) == "no_chance"
    # Legacy payload: no turn count, so a kill within five seconds of the request is no chance.
    assert classify(base_packet(turns_since_request=None, seconds_since_request=2.7)) == "no_chance"
    assert classify(base_packet(turns_since_request=None, seconds_since_request=40.0, tool_calls_after_request=["tool.board_read"])) == "ignored"
    assert classify(base_packet(tool_calls_after_request=["tool.board_read", "tool.board_vote"])) == "ignored"
    assert classify(base_packet()) == "silent"


def test_status_snapshot_shape(db, op, task):
    with db.transaction() as conn:
        out = status_snapshot(conn)
    assert out == {"live_agents": {"solver": 0, "supervisor": 0}, "tokens_per_min": 0,
                   "tasks": {"open": 1, "leased": 0, "done": 0, "blocked": 0}}


# --- the recorded run ----------------------------------------------------------------------------


def load_fixture(db) -> None:
    """Load the 30k-window run into the test database. The dump's schema_migrations and config
    blocks are dropped because the migrated, seeded test database already has both."""
    psql = shutil.which("psql") or "/opt/homebrew/opt/postgresql@17/bin/psql"
    if not Path(psql).exists():
        pytest.skip("psql not available to load the fixture")
    keep: list[str] = []
    skipping = False
    for line in FIXTURE.read_text().splitlines():
        if line.startswith("COPY public.schema_migrations") or line.startswith("COPY public.config"):
            skipping = True
        if not skipping:
            keep.append(line)
        if skipping and line == "\\.":
            skipping = False
    proc = subprocess.run([psql, "-v", "ON_ERROR_STOP=1", "-q", TEST_URL], input="\n".join(keep) + "\n",
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_fixture_run_classifies_the_two_kills(db):
    load_fixture(db)
    start = db.fetch_one("select min(ts) as ts from events")["ts"]
    end = db.fetch_one("select now() as now")["now"]
    out = succession(db, start, end)
    assert (out["handoffs"], out["stubs"], out["kills"], out["requests"]) == (3, 2, 2, 4)
    assert out["success_rate"] == pytest.approx(0.6)
    assert out["generations"] == {"L-001": 2, "L-002": 3, "L-003": 3}
    by_agent = {p["agent_id"]: p for p in out["incidents"]}
    assert set(by_agent) == {"sol-002", "sol-003"}
    # sol-003 was asked at 21:40:10.74 and killed at 21:40:13.46: no turn of its own in between.
    sol3 = by_agent["sol-003"]
    assert sol3["classification"] == "no_chance" and sol3["requested"] is True
    assert sol3["seconds_since_request"] == pytest.approx(2.72, abs=0.01) and sol3["turns_since_request"] is None
    assert sol3["task"] == "T-007" and sol3["context_pct_at_request"] == pytest.approx(86.997)
    # sol-002 jumped past both thresholds in one turn under the M3 runner and was never asked.
    sol2 = by_agent["sol-002"]
    assert sol2["classification"] == "never_asked" and sol2["requested"] is False
    assert sol2["task"] == "T-008" and sol2["seconds_since_request"] is None
    # Legacy handoffs carry no turn count, so the turn median is unknown and the seconds median is derived.
    assert out["median_turns_to_handoff"] is None and out["median_seconds_to_handoff"] == pytest.approx(5.64, abs=0.05)
    with db.transaction() as conn:
        slo = compute_slos(conn, start, end)
    assert slo["handoff_success_rate"] == {"value": pytest.approx(0.6), "n": 5}
    assert slo["completions_per_mtok"]["n"] == 8 and slo["blocked_failed_rate"] == {"value": 0.0, "n": 8}
