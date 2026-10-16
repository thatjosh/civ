"""M4: supervisor rounds on the Operator: opening, the brief, closing, presets, reports and pings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

BRIEF_KEYS = {"round", "status", "slo", "succession", "blockers", "blocked_tasks", "alerts", "previous_reports", "tunes", "verifier"}


def old_event(db, seconds_ago: int, type: str = "task.created", payload: dict | None = None) -> datetime:
    ts = db.fetch_one("select now() - make_interval(secs => %s) as ts", (seconds_ago,))["ts"]
    db.execute("insert into events (ts, agent_id, type, payload) values (%s, 'operator', %s, %s)", (ts, type, json.dumps(payload or {})))
    return ts


def test_first_round_window_starts_at_the_earliest_event(db, op):
    first = old_event(db, 600)
    old_event(db, 300)
    rid = op.open_round(1)
    row = db.fetch_one("select number, window_start, final, ended_at from rounds where id = %s", (rid,))
    assert row["number"] == 1 and row["window_start"] == first and row["final"] is False and row["ended_at"] is None
    opened = db.fetch_one("select payload from events where type = 'round.opened'")["payload"]
    assert opened["round_id"] == rid and opened["number"] == 1 and opened["final"] is False


def test_first_round_without_events_starts_now(db, op):
    rid = op.open_round(1)
    row = db.fetch_one("select window_start, started_at from rounds where id = %s", (rid,))
    assert abs((row["started_at"] - row["window_start"]).total_seconds()) < 2


def test_consecutive_rounds_partition_the_timeline(db, op):
    r1 = op.open_round(1)
    op.close_round(r1, {"reports": 0})
    r2 = op.open_round(2, final=True)
    rows = {r["id"]: r for r in db.fetch_all("select id, started_at, window_start, final from rounds")}
    assert rows[r2]["window_start"] == rows[r1]["started_at"] and rows[r2]["final"] is True


def test_current_round_and_close(db, op):
    assert op.current_round() is None
    rid = op.open_round(3)
    current = op.current_round()
    assert current["id"] == rid and current["number"] == 3 and current["tuned_knob"] is None and current["final"] is False
    assert set(current) == {"id", "number", "started_at", "window_start", "tuned_knob", "final", "tuned_from", "tuned_to"}
    op.close_round(rid, {"reports": 1, "pings": 0, "tuned": None, "reopened": [], "cost_usd": 0.02, "turns": 7})
    assert op.current_round() is None
    row = db.fetch_one("select ended_at, summary from rounds where id = %s", (rid,))
    assert row["ended_at"] is not None and row["summary"]["reports"] == 1 and row["summary"]["cost_usd"] == 0.02
    closed = db.fetch_one("select payload from events where type = 'round.closed'")["payload"]
    assert closed["round_id"] == rid and closed["turns"] == 7


def test_brief_shape_on_an_empty_board(db, op):
    rid = op.open_round(1)
    brief = op.round_brief(rid, supervisors=2)
    assert set(brief) == BRIEF_KEYS
    assert brief["round"]["id"] == rid and brief["round"]["number"] == 1 and brief["round"]["supervisors"] == 2
    assert brief["round"]["window_start"] <= brief["round"]["window_end"]
    assert set(brief["status"]) == {"live_agents", "tokens_per_min", "tasks"}
    assert set(brief["slo"]) == {"current", "previous", "min_sample", "history"}
    assert brief["slo"]["previous"] is None and brief["slo"]["min_sample"] == 3
    assert brief["slo"]["history"] == [{"round": 1, "completions_per_mtok": None, "blocked_failed_rate": 0.0, "duplicate_rate": 0.0,
                                        "lease_wait_median_s": None, "handoff_success_rate": None}]
    assert brief["succession"]["incidents"] == [] and brief["succession"]["kills"] == 0
    assert brief["blockers"] == [] and brief["blocked_tasks"] == [] and brief["alerts"] == [] and brief["previous_reports"] == []
    assert brief["tunes"] == {"active": [], "pending": [], "round_tuned": False}
    assert brief["verifier"] == {"done": 0, "total": 0, "mean_score": None, "lowest": []}
    # Stored on the round as built.
    stored = db.fetch_one("select brief from rounds where id = %s", (rid,))["brief"]
    assert stored["round"]["id"] == rid


def test_brief_collects_blockers_blocked_tasks_alerts_reports_tunes_and_verifier(db, op, tasks, board, civ, make_agent, solver, other):
    supervisor, _ = make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001", generation=1)
    op.create_task("T-001", "Count", "count", priority_base=1.0)
    op.create_task("T-002", "Sum", "sum")
    r1 = op.open_round(1)
    op.round_brief(r1)
    civ.report(supervisor, "Round one: quiet")
    op.close_round(r1, {"reports": 1})
    # Inside round 2's window: block T-001 with three releases (max_attempts 3), each a blocker post, and an alert.
    for who, reason in ((solver, "no ledger"), (other, "still no ledger"), (solver, "gave up")):
        assert tasks.claim(who)["task"]["id"] == "T-001"
        tasks.release(who, "T-001", reason)
        # A release starts a backoff; clear it so the next claim in the test finds the task.
        db.execute("update tasks set next_attempt_at = now() where id = 'T-001'")
        msg = board.post(other if who is solver else solver, "finding", "something to vote on so the quota clears")["id"]
        board.vote(who, msg, 1, "fine")
    civ.alert(solver, "the archive is missing a year")
    op.set_knob("top_k", 15, "launch preset")
    make_agent("supervisor", agent_id="sup-001-r2", lineage="S-001", generation=2)
    r2 = op.open_round(2)
    brief = op.round_brief(r2)
    assert brief["round"]["supervisors"] == 1  # counted from the agents registered for round 2
    assert brief["slo"]["previous"] is not None and [h["round"] for h in brief["slo"]["history"]] == [1, 2]
    assert brief["slo"]["current"]["blocked_failed_rate"] == {"value": 1.0, "n": 3}
    assert [t["id"] for t in brief["blocked_tasks"]] == ["T-001"]
    blocked = brief["blocked_tasks"][0]
    assert blocked["attempts"] == 3 and blocked["last_release_reason"].endswith("gave up")
    # Blockers are the window's blocker posts, highest score first; all three here are unvoted.
    assert len(brief["blockers"]) == 3 and {b["task_id"] for b in brief["blockers"]} == {"T-001"}
    assert set(brief["blockers"][0]) == {"id", "thread_id", "task_id", "author_id", "body", "score", "created_at"}
    assert [a["body"] for a in brief["alerts"]] == ["the archive is missing a year"]
    assert [r["body"] for r in brief["previous_reports"]] == ["Round one: quiet"]
    assert brief["tunes"]["active"] == [{"knob": "top_k", "value": 15.0, "seed": 10.0, "set_by": "operator", "reason": "launch preset", "expires_at": None}]
    assert brief["verifier"]["total"] == 2 and brief["verifier"]["done"] == 0
    assert brief["status"]["tasks"] == {"open": 1, "leased": 0, "done": 0, "blocked": 1}


def test_brief_history_reads_stored_briefs_not_recomputations(db, op):
    r1 = op.open_round(1)
    op.round_brief(r1)
    # Tamper with the stored brief: the history must show what was stored, because that is what the
    # round's supervisors saw.
    db.execute("""update rounds set brief = jsonb_set(brief, '{slo,current,blocked_failed_rate,value}', '0.75') where id = %s""", (r1,))
    op.close_round(r1, {})
    r2 = op.open_round(2)
    brief = op.round_brief(r2)
    assert brief["slo"]["history"][0] == {"round": 1, "completions_per_mtok": None, "blocked_failed_rate": 0.75, "duplicate_rate": 0.0,
                                          "lease_wait_median_s": None, "handoff_success_rate": None}


def test_set_knob_respects_bounds_only_and_logs_a_preset(db, op, knobs):
    with pytest.raises(ValueError):
        op.set_knob("nope", 1, "x")
    with pytest.raises(ValueError):
        op.set_knob("kill_grace_turns", 99, "x")
    # Several steps at once and straight after another change: no step rule, no cooldown, no TTL.
    op.set_knob("kill_grace_turns", 0, "fault injection: the old kill rule")
    op.set_knob("kill_grace_turns", 5, "and back up")
    row = db.fetch_one("select value, set_by, reason, ttl_s, expires_at from config where knob = 'kill_grace_turns'")
    assert (row["value"], row["set_by"], row["reason"], row["ttl_s"], row["expires_at"]) == (5.0, "operator", "and back up", None, None)
    assert knobs.get("kill_grace_turns") == 5.0
    presets = [e["payload"] for e in db.fetch_all("select payload from events where type = 'knob.preset' order by seq")]
    assert presets[0] == {"knob": "kill_grace_turns", "from": 2.0, "to": 0.0, "reason": "fault injection: the old kill rule"}
    assert presets[1]["from"] == 0.0 and presets[1]["to"] == 5.0


def test_reports_for_round_and_pings_since(db, op, civ, make_agent):
    sup_a, _ = make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")
    sup_b, _ = make_agent("supervisor", agent_id="sup-002-r1", lineage="S-002")
    before = db.fetch_one("select now() - interval '1 minute' as ts")["ts"]
    r1 = op.open_round(1)
    civ.report(sup_a, "all quiet")
    # A ping stored by the server has the trigger prefix; written directly here because verifying a
    # trigger needs two rounds of data (covered in test_supervisor_tools.py).
    db.execute("insert into messages (author_id, kind, body) values (%s, 'report', %s)", (sup_b.id, "[ping: succession_failing] two windows of kills"))
    op.close_round(r1, {})
    reports = op.reports_for_round(r1)
    assert [(r["author_id"], r["ping"], r["trigger"]) for r in reports] == [(sup_a.id, False, None), (sup_b.id, True, "succession_failing")]
    assert set(reports[0]) == {"id", "author_id", "body", "created_at", "ping", "trigger"}
    pings = op.pings_since(before)
    assert len(pings) == 1 and pings[0]["trigger"] == "succession_failing"
    assert op.pings_since(db.fetch_one("select now() as ts")["ts"]) == []
    assert op.reports_for_round(999) == []


def test_current_round_carries_the_tune_and_reopened_lists_the_round_s_reopens(op, civ, tasks, supervisor, db):
    round_id = op.open_round(1)
    op.round_brief(round_id)
    # Thin window: force the tune through the operator path a supervisor would take once min_sample is met.
    with db.transaction() as conn:
        conn.execute("update rounds set tuned_knob = 'kill_grace_turns', tuned_by = %s where id = %s", (supervisor.id, round_id))
        conn.execute("insert into events (agent_id, type, payload) values (%s, 'tool.civ_tune', %s)",
                     (supervisor.id, '{"knob": "kill_grace_turns", "from": 2.0, "to": 3.0, "round_id": %d}' % round_id))
        conn.execute("insert into events (agent_id, type, payload) values (%s, 'task.reopened', %s)",
                     (supervisor.id, '{"task_id": "T-009", "reason": "x"}'))
    current = op.current_round()
    assert current["tuned_knob"] == "kill_grace_turns" and current["tuned_from"] == 2.0 and current["tuned_to"] == 3.0
    assert op.reopened_in_round(round_id) == ["T-009"]
    op.close_round(round_id, {})
    assert op.current_round() is None
