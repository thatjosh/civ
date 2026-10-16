"""M1: civ_handoff, civ_alert, civ_status, civ_tune, civ_report."""

GOOD_HANDOFF = """## State of work
Half of T-003 done.
## Open threads
seq 14 has the open question.
## What to try next
Finish the census cross-reference.
## What not to retry
The regex approach on the ledger.
## Files worth reading
work/notes.md
"""


def test_handoff_requires_request_headings_and_size(civ, solver, op, db):
    assert "not requested" in civ.handoff(solver, GOOD_HANDOFF)["error"]
    op.set_agent_status(solver.id, "handing_off")
    missing = civ.handoff(solver, "## State of work\nx\n")
    assert "missing headings" in missing["error"] and "## Open threads" in missing["error"]
    db.execute("update config set value = 1000 where knob = 'max_handoff_tokens'")
    too_big = civ.handoff(solver, GOOD_HANDOFF + "x" * 5000)
    assert "ceiling is 1000" in too_big["error"]
    out = civ.handoff(solver, GOOD_HANDOFF)
    assert out["ok"] and out["handoff_id"] == 1
    row = db.fetch_one("select lineage_id, from_agent, generation, stub from handoffs")
    assert (row["lineage_id"], row["from_agent"], row["generation"], row["stub"]) == (solver.lineage_id, solver.id, 1, False)
    assert db.fetch_one("select kind from messages order by id desc limit 1")["kind"] == "handoff"


def test_alert_posts_alert_kind(civ, solver, db):
    assert "empty" in civ.alert(solver, " ")["error"]
    out = civ.alert(solver, "The verifier seems to reject every CSV")
    assert out["ok"]
    assert db.fetch_one("select kind, author_id from messages where id = %s", (out["id"],)) == {"kind": "alert", "author_id": solver.id}


def test_status_shape(civ, tasks, board, solver, other, supervisor, op, task):
    tasks.claim(solver)
    op.log_event(solver.id, "agent.turn", {"tokens_in": 1200, "tokens_out": 300})
    op.set_agent_usage(solver.id, 1200, 300, 12.5)
    out = civ.status(solver)
    assert out["live_agents"] == {"solver": 2, "supervisor": 1}
    assert out["tokens_per_min"] == 1500
    assert out["tasks"] == {"open": 0, "leased": 1, "done": 0, "blocked": 0}
    assert out["you"] == {"id": solver.id, "generation": 1, "context_pct": 12.5, "lease": "T-001", "votes_since_claim": 0}
    msg = board.post(other, "finding", "x")["id"]
    board.vote(solver, msg, 1, "good")
    assert civ.status(solver)["you"]["votes_since_claim"] == 1


def test_tune_is_supervisor_only_and_validated(civ, solver, supervisor, db, knobs, op):
    # The M1 rules, inside an open round with enough claims for the sample (the round rules
    # themselves are covered in test_supervisor_tools.py).
    for _ in range(3):
        op.log_event(solver.id, "task.claimed", {"task_id": "T-001"})
    op.round_brief(op.open_round(1))
    assert civ.tune(solver, "lease_length_s", 2100, "blocked_failed_rate", "r")["error"] == "supervisor only"
    assert "unknown knob" in civ.tune(supervisor, "nope", 1, "blocked_failed_rate", "r")["error"]
    assert "slo must be" in civ.tune(supervisor, "lease_length_s", 2100, "vibes", "r")["error"]
    assert "reason" in civ.tune(supervisor, "lease_length_s", 2100, "blocked_failed_rate", "")["error"]
    assert "one step" in civ.tune(supervisor, "lease_length_s", 2700, "blocked_failed_rate", "r")["error"]
    assert "within" in civ.tune(supervisor, "lease_length_s", 100, "blocked_failed_rate", "r")["error"]
    out = civ.tune(supervisor, "lease_length_s", 2100, "blocked_failed_rate", "tasks are legitimately long", ttl_s=600)
    assert out["ok"] and out["value"] == 2100 and knobs.get("lease_length_s") == 2100
    # A second tune in the same round is refused for the round before the cooldown is reached.
    assert "round already tuned" in civ.tune(supervisor, "lease_length_s", 2400, "blocked_failed_rate", "again")["error"]
    op.round_brief(op.open_round(2))
    assert "cooldown" in civ.tune(supervisor, "lease_length_s", 2400, "blocked_failed_rate", "again")["error"]
    row = db.fetch_one("select set_by, reason, ttl_s, expires_at > now() as live from config where knob = 'lease_length_s'")
    assert (row["set_by"], row["reason"], row["ttl_s"], row["live"]) == (supervisor.id, "tasks are legitimately long", 600, True)
    ev = db.fetch_one("select payload from events where type = 'tool.civ_tune'")["payload"]
    assert ev["slo"] == "blocked_failed_rate" and ev["from"] == 1800.0 and ev["to"] == 2100.0 and ev["n"] == 3


def test_report_is_supervisor_only_and_needs_a_round(civ, solver, supervisor, db, op):
    assert civ.report(solver, "x")["error"] == "supervisor only"
    assert civ.report(supervisor, "x")["error"] == "no supervisor round is open"
    op.open_round(1)
    assert "trigger must be one of" in civ.report(supervisor, "x", trigger="slo_degraded_3w")["error"]
    out = civ.report(supervisor, "All quiet, 3 tasks done")
    assert out["ok"] and db.fetch_one("select kind from messages where id = %s", (out["id"],))["kind"] == "report"


def test_pin_blocks_civ_writes_too(civ, board, solver, other, op):
    target = board.post(other, "finding", "steer")["id"]
    op.vote(target, 1, "read this")
    assert civ.alert(solver, "help")["error"].startswith("acknowledge")
    assert civ.status(solver)["ok"]
