"""M4: the round rules on civ_tune and civ_report, task_reopen, and refusal logging in the gateway.

Every refusal path has a test that fails if its check is removed."""

from __future__ import annotations

import json

import pytest

from harness.board_mcp.server import Gateway
from harness.board_mcp.tools.civ import NO_ROUND


def claims(op, agent_id: str, n: int) -> None:
    for i in range(n):
        op.log_event(agent_id, "task.claimed", {"task_id": f"T-{i:03d}"})


def round_with_sample(op, number: int, agent_id: str = "sol-001", n: int = 3) -> int:
    """An open round whose brief shows n claims, so blocked_failed_rate clears min_sample (3)."""
    claims(op, agent_id, n)
    rid = op.open_round(number)
    op.round_brief(rid)
    return rid


@pytest.fixture
def sup(make_agent):
    return make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")[0]


@pytest.fixture
def sup2(make_agent):
    return make_agent("supervisor", agent_id="sup-002-r1", lineage="S-002")[0]


# --- civ_tune ------------------------------------------------------------------------------------


def test_tune_needs_an_open_round(civ, sup, op):
    assert civ.tune(sup, "top_k", 15, "blocked_failed_rate", "r")["error"] == NO_ROUND
    rid = round_with_sample(op, 1)
    op.close_round(rid, {})
    assert civ.tune(sup, "top_k", 15, "blocked_failed_rate", "r")["error"] == NO_ROUND


def test_one_tune_per_round_across_supervisors(civ, sup, sup2, op, db):
    round_with_sample(op, 1)
    assert civ.tune(sup, "top_k", 15, "blocked_failed_rate", "noise")["ok"]
    refused = civ.tune(sup2, "vote_quota", 2, "blocked_failed_rate", "more votes")
    assert refused["error"].startswith("round already tuned") and "top_k" in refused["error"] and sup.id in refused["error"]
    row = db.fetch_one("select tuned_knob, tuned_by from rounds")
    assert (row["tuned_knob"], row["tuned_by"]) == ("top_k", sup.id)
    assert db.fetch_one("select value from config where knob = 'vote_quota'")["value"] == 1.0


def test_min_sample_is_read_from_the_brief(civ, sup, op):
    claims(op, "sol-001", 2)
    rid = op.open_round(1)
    op.round_brief(rid)
    refused = civ.tune(sup, "top_k", 15, "blocked_failed_rate", "r")
    assert refused["error"] == "below min_sample: blocked_failed_rate has n=2 in this window, needs 3"
    # A third claim after the brief was built does not count: the brief is what the round saw.
    claims(op, "sol-001", 1)
    assert "below min_sample" in civ.tune(sup, "top_k", 15, "blocked_failed_rate", "r")["error"]
    # Another SLO with no sample at all is refused on its own numbers.
    assert "lease_wait_median_s has n=0" in civ.tune(sup, "top_k", 15, "lease_wait_median_s", "r")["error"]


def test_round_without_a_brief_is_measured_live(civ, sup, op):
    claims(op, "sol-001", 3)
    op.open_round(1)  # no round_brief call
    assert civ.tune(sup, "top_k", 15, "blocked_failed_rate", "r")["ok"]


def test_handoff_success_rate_sample_is_handoffs_plus_stubs(civ, sup, op):
    op.log_event("sol-001", "agent.handed_off", {"generation": 1})
    op.log_event("sol-002", "agent.killed", {"generation": 1, "reason": "kill threshold reached"})
    op.log_event("sol-002", "agent.stub_written", {"handoff_id": 1})
    rid = op.open_round(1)
    op.round_brief(rid)
    refused = civ.tune(sup, "kill_grace_turns", 3, "handoff_success_rate", "one kill")
    assert refused["error"] == "below min_sample: handoff_success_rate has n=2 in this window, needs 3"
    op.close_round(rid, {})
    op.log_event("sol-003", "agent.handed_off", {"generation": 1})
    rid = op.open_round(2)
    op.round_brief(rid)
    # The second window has one handoff only (n=1); the sample is per window, not cumulative.
    assert "n=1" in civ.tune(sup, "kill_grace_turns", 3, "handoff_success_rate", "r")["error"]


def test_tune_event_carries_the_round_and_the_sample(civ, sup, op, db):
    rid = round_with_sample(op, 1, n=4)
    assert civ.tune(sup, "kill_grace_turns", 3, "blocked_failed_rate", "give them a turn")["ok"]
    ev = db.fetch_one("select payload from events where type = 'tool.civ_tune'")["payload"]
    assert ev == {"knob": "kill_grace_turns", "from": 2.0, "to": 3.0, "slo": "blocked_failed_rate", "n": 4,
                  "reason": "give them a turn", "big_move": False, "round_id": rid}


def test_operator_preset_does_not_start_a_cooldown(civ, sup, op, db):
    # The fault-injection path: the operator presets a knob and a supervisor fixes it in the same minute.
    op.set_knob("kill_grace_turns", 0, "fault")
    round_with_sample(op, 1)
    assert civ.tune(sup, "kill_grace_turns", 1, "blocked_failed_rate", "no chance kills")["ok"]
    assert db.fetch_one("select value from config where knob = 'kill_grace_turns'")["value"] == 1.0


def test_supervisor_tune_does_start_a_cooldown(civ, sup, op):
    round_with_sample(op, 1)
    assert civ.tune(sup, "kill_grace_turns", 3, "blocked_failed_rate", "r")["ok"]
    round_with_sample(op, 2)
    assert "cooldown" in civ.tune(sup, "kill_grace_turns", 4, "blocked_failed_rate", "r")["error"]


def test_big_move_needs_a_second_supervisor(civ, sup, sup2, op, db, knobs):
    # top_k: seed 10, step 5, big_move_steps 2, so 25 is a big move; preset to 20 to get within a step.
    op.set_knob("top_k", 20, "preset")
    rid = round_with_sample(op, 1)
    first = civ.tune(sup, "top_k", 25, "blocked_failed_rate", "boards are long")
    assert first == {"ok": True, "pending": True, "knob": "top_k", "value": 25.0, "needs": "a second supervisor"}
    assert knobs.get("top_k") == 20.0
    assert db.fetch_one("select tuned_knob from rounds where id = %s", (rid,))["tuned_knob"] is None  # the round's tune is unused
    # The same supervisor again is one opinion: still pending, no second proposal row.
    assert civ.tune(sup, "top_k", 25, "blocked_failed_rate", "still think so")["pending"] is True
    assert db.fetch_one("select count(*) as n from tune_proposals")["n"] == 1
    # A different value from the second supervisor is a new proposal, not a commit.
    assert civ.tune(sup2, "top_k", 15, "blocked_failed_rate", "shorter")["ok"] and db.fetch_one("select tuned_knob from rounds")["tuned_knob"] == "top_k"
    # (15 is within two steps of the seed: an ordinary tune, which used the round's one tune.)


def test_big_move_commits_on_the_matching_second_call(civ, sup, sup2, op, db, knobs):
    op.set_knob("top_k", 20, "preset")
    rid = round_with_sample(op, 1)
    assert civ.tune(sup, "top_k", 25, "blocked_failed_rate", "boards are long")["pending"] is True
    out = civ.tune(sup2, "top_k", 25, "blocked_failed_rate", "agreed")
    assert out["ok"] and "pending" not in out and knobs.get("top_k") == 25.0
    proposal = db.fetch_one("select proposed_by, committed_by, committed_at from tune_proposals")
    assert (proposal["proposed_by"], proposal["committed_by"]) == (sup.id, sup2.id) and proposal["committed_at"] is not None
    assert db.fetch_one("select tuned_knob, tuned_by from rounds where id = %s", (rid,)) == {"tuned_knob": "top_k", "tuned_by": sup2.id}
    ev = db.fetch_one("select payload from events where type = 'tool.civ_tune'")["payload"]
    assert ev["big_move"] is True and ev["to"] == 25.0
    proposed = db.fetch_one("select payload from events where type = 'tool.civ_tune_proposed'")["payload"]
    assert proposed["knob"] == "top_k" and proposed["round_id"] == rid


def test_pending_proposal_counts_in_the_next_round_but_not_later(civ, sup, sup2, op, db, make_agent, knobs):
    op.set_knob("top_k", 20, "preset")
    r1 = round_with_sample(op, 1)
    assert civ.tune(sup, "top_k", 25, "blocked_failed_rate", "r")["pending"] is True
    op.close_round(r1, {})
    r2 = round_with_sample(op, 2)
    sup2_r2 = make_agent("supervisor", agent_id="sup-002-r2", lineage="S-002", generation=2)[0]
    assert civ.tune(sup2_r2, "top_k", 25, "blocked_failed_rate", "agreed")["ok"] and knobs.get("top_k") == 25.0
    op.close_round(r2, {})
    # A fresh proposal in round 3 is not matched by anything from round 1.
    db.execute("update config set set_by = 'operator', set_at = now() where knob = 'top_k'")  # clear the cooldown for the test
    op.set_knob("top_k", 30, "preset")
    r3 = round_with_sample(op, 3)
    sup_r3 = make_agent("supervisor", agent_id="sup-001-r3", lineage="S-001", generation=3)[0]
    assert civ.tune(sup_r3, "top_k", 35, "blocked_failed_rate", "r")["pending"] is True
    op.close_round(r3, {})
    r4 = round_with_sample(op, 4)
    op.close_round(r4, {})
    round_with_sample(op, 5)
    sup2_r5 = make_agent("supervisor", agent_id="sup-002-r5", lineage="S-002", generation=5)[0]
    # Two rounds later the round-3 proposal is stale: this is a new pending proposal, not a commit.
    assert civ.tune(sup2_r5, "top_k", 35, "blocked_failed_rate", "r")["pending"] is True
    assert knobs.get("top_k") == 30.0


# --- civ_report ----------------------------------------------------------------------------------


def test_report_needs_a_round_and_happens_once_per_supervisor_per_round(civ, sup, sup2, op, db):
    assert civ.report(sup, "x")["error"] == NO_ROUND
    r1 = op.open_round(1)
    assert civ.report(sup, "first")["ok"]
    assert civ.report(sup, "second")["error"] == "you already reported this round"
    assert civ.report(sup2, "theirs")["ok"]  # a different supervisor's report is fine
    op.close_round(r1, {})
    op.open_round(2)
    assert civ.report(sup, "next round")["ok"]
    ev = db.fetch_one("select payload from events where type = 'tool.civ_report' order by seq limit 1")["payload"]
    assert ev["round_id"] == r1 and ev["trigger"] is None


def test_unknown_trigger_and_ceiling_pinned_are_refused(civ, sup, op):
    op.open_round(1)
    assert "trigger must be one of" in civ.report(sup, "x", trigger="vibes")["error"]
    assert civ.report(sup, "x", trigger="ceiling_pinned")["error"] == "no ceilings until M5"


def kill(op, agent_id: str) -> None:
    op.log_event(agent_id, "agent.killed", {"generation": 1, "reason": "kill threshold reached", "requested": True,
                                            "turns_since_request": 2, "seconds_since_request": 9.0, "grace_turns": 2})
    op.log_event(agent_id, "agent.stub_written", {"handoff_id": 1})


def test_succession_failing_needs_kills_in_two_consecutive_windows(civ, sup, op, db, make_agent):
    kill(op, "sol-001")
    r1 = op.open_round(1)
    op.round_brief(r1)
    refused = civ.report(sup, "kills", trigger="succession_failing")
    assert refused["error"] == "trigger succession_failing is not in the data: kills this window = 1, previous window = 0"
    op.close_round(r1, {})
    kill(op, "sol-002")
    r2 = op.open_round(2)
    op.round_brief(r2)
    sup_r2 = make_agent("supervisor", agent_id="sup-001-r2", lineage="S-001", generation=2)[0]
    out = civ.report(sup_r2, "two windows of kills", trigger="succession_failing")
    assert out["ok"]
    body = db.fetch_one("select body from messages where id = %s", (out["id"],))["body"]
    assert body == "[ping: succession_failing] two windows of kills"
    ping = db.fetch_one("select payload from events where type = 'report.ping'")["payload"]
    assert ping == {"id": out["id"], "round_id": r2, "trigger": "succession_failing"}
    # Without the brief the server has nothing to verify against.
    op.close_round(r2, {})
    r3 = op.open_round(3)
    sup_r3 = make_agent("supervisor", agent_id="sup-001-r3", lineage="S-001", generation=3)[0]
    assert "kills this window = 0" in civ.report(sup_r3, "x", trigger="succession_failing")["error"]


def set_brief_slo(db, round_id: int, slo: str, value: float) -> None:
    db.execute("update rounds set brief = jsonb_set(coalesce(nullif(brief, '{}'::jsonb), '{\"slo\": {\"current\": {}}}'::jsonb), "
               "%s, %s, true) where id = %s", (["slo", "current", slo], json.dumps({"value": value, "n": 5}), round_id))


def test_slo_degraded_needs_three_worsening_windows_of_the_named_slo(civ, sup, op, db, make_agent):
    for number, value in ((1, 0.1), (2, 0.2)):
        rid = op.open_round(number)
        set_brief_slo(db, rid, "blocked_failed_rate", value)
        op.close_round(rid, {})
    r3 = op.open_round(3)
    set_brief_slo(db, r3, "blocked_failed_rate", 0.3)
    assert "name the SLO as slo=<name>" in civ.report(sup, "things are worse", trigger="slo_degraded")["error"]
    assert "name the SLO" in civ.report(sup, "slo=vibes", trigger="slo_degraded")["error"]
    out = civ.report(sup, "slo=blocked_failed_rate rising three windows running", trigger="slo_degraded")
    assert out["ok"]
    # A higher-is-better SLO that rose is not degraded, and a dip in the middle breaks the run.
    sup2 = make_agent("supervisor", agent_id="sup-002-r3", lineage="S-002", generation=3)[0]
    set_brief_slo(db, r3, "handoff_success_rate", 0.9)
    assert "needs 3 windows" in civ.report(sup2, "slo=handoff_success_rate", trigger="slo_degraded")["error"]
    sup3 = make_agent("supervisor", agent_id="sup-003-r3", lineage="S-003", generation=3)[0]
    db.execute("update rounds set brief = jsonb_set(brief, '{slo,current,blocked_failed_rate,value}', '0.05') where number = 2")
    refused = civ.report(sup3, "slo=blocked_failed_rate", trigger="slo_degraded")
    assert refused["error"] == "trigger slo_degraded is not in the data: blocked_failed_rate over the last 3 windows was [0.1, 0.05, 0.3]"


def test_slo_degraded_direction_follows_the_slo(civ, sup, op, db):
    for number, value in ((1, 0.9), (2, 0.8)):
        rid = op.open_round(number)
        set_brief_slo(db, rid, "handoff_success_rate", value)
        op.close_round(rid, {})
    r3 = op.open_round(3)
    set_brief_slo(db, r3, "handoff_success_rate", 0.5)
    assert civ.report(sup, "slo=handoff_success_rate falling", trigger="slo_degraded")["ok"]


def test_big_move_disputed_needs_an_old_pending_proposal_with_a_conflict(civ, sup, op, db, make_agent):
    r1 = op.open_round(1)
    db.execute("insert into tune_proposals (knob, value, proposed_by, round_id) values ('top_k', 25, 'sup-001-r1', %s)", (r1,))
    op.close_round(r1, {})
    r2 = op.open_round(2)
    sup_r2 = make_agent("supervisor", agent_id="sup-001-r2", lineage="S-001", generation=2)[0]
    assert "no pending proposal at least 2 rounds old" in civ.report(sup_r2, "x", trigger="big_move_disputed")["error"]
    op.close_round(r2, {})
    op.open_round(3)
    sup_r3 = make_agent("supervisor", agent_id="sup-001-r3", lineage="S-001", generation=3)[0]
    # Old enough now, but nobody disagrees yet.
    assert "conflicting proposal" in civ.report(sup_r3, "x", trigger="big_move_disputed")["error"]
    db.execute("insert into tune_proposals (knob, value, proposed_by, round_id) values ('top_k', 15, 'sup-002-r1', %s)", (r1,))
    sup2_r3 = make_agent("supervisor", agent_id="sup-002-r3", lineage="S-002", generation=3)[0]
    assert civ.report(sup2_r3, "we disagree on top_k", trigger="big_move_disputed")["ok"]


# --- task_reopen ---------------------------------------------------------------------------------


def block(db, tasks, board, solver, other, task_id: str) -> None:
    for who, reason in ((solver, "no ledger"), (other, "still none"), (solver, "gave up")):
        assert tasks.claim(who)["task"]["id"] == task_id
        tasks.release(who, task_id, reason)
        # A release starts a backoff; clear it so the next claim in the test finds the task.
        db.execute("update tasks set next_attempt_at = now() where id = %s", (task_id,))
        msg = board.post(other if who is solver else solver, "finding", f"vote fodder {reason}")["id"]
        board.vote(who, msg, 1, "fine")


def test_reopen_is_supervisor_only_and_only_for_blocked_tasks(tasks, board, civ, sup, solver, other, op, db, task):
    assert tasks.reopen(solver, "T-001", "please")["error"] == "supervisor only"
    assert tasks.reopen(sup, "T-001", "")["error"].startswith("say why")
    assert tasks.reopen(sup, "T-404", "x")["error"] == "unknown task T-404"
    assert tasks.reopen(sup, "T-001", "x")["error"] == "T-001 is not blocked (it is open)"
    block(db, tasks, board, solver, other, "T-001")
    assert db.fetch_one("select status, attempts from tasks where id = 'T-001'") == {"status": "blocked", "attempts": 3}
    out = tasks.reopen(sup, "T-001", "the ledger is in materials/ledger_1852.txt, read it first")
    assert out == {"ok": True, "task_id": "T-001", "status": "open"}
    row = db.fetch_one("select status, attempts, lease_owner, next_attempt_at <= now() as ready from tasks where id = 'T-001'")
    assert (row["status"], row["attempts"], row["lease_owner"], row["ready"]) == ("open", 0, None, True)
    last = db.fetch_one("select kind, body, author_id, thread_id from messages order by id desc limit 1")
    assert last["kind"] == "blocker" and last["body"].startswith("[T-001 reopened] the ledger") and last["author_id"] == sup.id
    assert last["thread_id"] == task["thread_id"]
    ev = db.fetch_one("select payload from events where type = 'task.reopened'")["payload"]
    assert ev["task_id"] == "T-001" and ev["reason"].startswith("the ledger")
    # And a solver can claim it again.
    msg = board.post(other, "finding", "one more to vote on")["id"]
    board.vote(solver, msg, 1, "ok")
    assert tasks.claim(solver)["task"]["id"] == "T-001"


# --- refusal logging -----------------------------------------------------------------------------


class FakeCtx:
    def __init__(self, token: str) -> None:
        self.headers = {"x-civ-token": token}


async def test_gateway_logs_every_refusal_with_the_tool_name(db, civ, make_agent):
    solver, token = make_agent("solver")
    gw = Gateway(db)
    out = await gw.call(FakeCtx(token), "civ_tune", civ.tune, knob="top_k", value=15, slo="blocked_failed_rate", reason="r")
    assert out["ok"] is False and out["error"] == "supervisor only" and "head_seq" in out
    refused = db.fetch_all("select agent_id, payload from events where type = 'tool.refused'")
    assert [(r["agent_id"], r["payload"]) for r in refused] == [(solver.id, {"tool": "civ_tune", "error": "supervisor only"})]
    # A success logs no refusal, and an unknown token (no agent to attribute it to) logs nothing either.
    ok = await gw.call(FakeCtx(token), "civ_status", civ.status)
    assert ok["ok"] is True
    unknown = await gw.call(FakeCtx("nope"), "civ_status", civ.status)
    assert unknown == {"ok": False, "error": "unknown agent token"}
    assert db.fetch_one("select count(*) as n from events where type = 'tool.refused'")["n"] == 1


def test_report_is_stored_whole_even_when_long(civ, sup, op, db):
    # Reports are exempt from the long-body spill: a replayed Opus report ran to 3,600 characters
    # and a 300-character preview of it scored 3 of 8 on a rubric the full text passed.
    op.open_round(1)
    body = "## Window\n" + ("x" * 3000)
    out = civ.report(sup, body)
    assert out["ok"]
    row = db.fetch_one("select body, refs from messages where id = %s", (out["id"],))
    assert row["body"] == body and row["refs"] == [] and "[full text:" not in row["body"]
