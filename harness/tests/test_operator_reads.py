"""M5: the Operator reads the governor and the civ command line depend on."""

from __future__ import annotations

import pytest

from harness.board_mcp.ceilings import Ceilings
from harness.board_mcp.operator import Operator


def test_tokens_per_minute_sums_recent_turns(op, make_agent):
    a = make_agent("solver")[0]
    op.log_event(a.id, "agent.turn", {"tokens_in": 1000, "tokens_out": 20})
    op.log_event(a.id, "agent.turn", {"tokens_in": 500, "tokens_out": 30})
    assert op.tokens_per_minute() == 1550


def test_spend_counts_ended_sessions_and_estimates_live_ones(db, op, make_agent, tmp_path):
    ended = make_agent("solver")[0]
    op.log_event(ended.id, "agent.ended", {"cost_usd": 0.25, "subtype": "success"})
    op.set_agent_status(ended.id, "ended")
    live = make_agent("solver")[0]
    # 1M context tokens read from cache at $0.20/M and 1000 output tokens at $20/M for Opus.
    op.set_agent_usage(live.id, 1_000_000, 1000, 50.0)
    ceilings = tmp_path / "ceilings.toml"
    ceilings.write_text('model = "claude-opus-5-5"\n', encoding="utf-8")
    priced = Operator(db, Ceilings(ceilings))
    assert priced.spend_usd(hours=24) == pytest.approx(0.25 + 0.20 + 0.02)
    # An unknown model prices live sessions at nothing rather than guessing.
    assert Operator(db, Ceilings(tmp_path / "absent.toml")).spend_usd(hours=24) == pytest.approx(0.25)
    # Outside the window nothing counts.
    db.execute("update events set ts = now() - interval '2 days' where type = 'agent.ended'")
    db.execute("update agents set spawned_at = now() - interval '2 days' where id = %s", (live.id,))
    assert priced.spend_usd(hours=24) == pytest.approx(0.0)


def test_live_agents_by_role(op, make_agent):
    make_agent("solver"); make_agent("solver"); make_agent("supervisor")
    gone = make_agent("solver")[0]
    op.set_agent_status(gone.id, "dead")
    assert op.live_agents() == {"solver": 2, "supervisor": 1}


def test_epoch_and_control_state_roundtrip(db, op):
    assert op.epoch() == 1
    op.control_set("epoch", 3)
    assert op.epoch() == 3 and op.control_get("epoch") == 3
    op.control_set("shape", {"population": {"solvers": 5}})
    assert op.control_get("shape") == {"population": {"solvers": 5}}
    assert op.control_get("nothing") is None
    db.execute("delete from control_state where key = 'epoch'")
    assert op.epoch() == 1


def test_epoch_is_one_without_the_table(db, op):
    db.execute("drop table control_state")
    try:
        assert op.epoch() == 1 and op.control_get("paused") is None
    finally:
        db.execute("create table control_state (key text primary key, value jsonb not null, updated_at timestamptz not null default now())")


def test_lineages_to_resume_lists_only_handoffs_without_a_successor(db, op, make_agent):
    # L-001: handed off, successor spawned (to_agent set): nothing to resume.
    a1 = make_agent("solver", agent_id="sol-001", lineage="L-001")[0]
    db.execute("insert into handoffs (lineage_id, from_agent, to_agent, generation, body) values ('L-001', 'sol-001', 'sol-001-g2', 1, 'x')")
    # L-002: a stub written at restore, no successor yet: resume it.
    make_agent("solver", agent_id="sol-002", lineage="L-002")
    db.execute("insert into handoffs (lineage_id, from_agent, generation, body, stub) values ('L-002', 'sol-002', 1, 'stub body', true)")
    # L-003: an older handoff with a successor and a newer one without: the latest decides.
    make_agent("solver", agent_id="sol-003", lineage="L-003")
    db.execute("insert into handoffs (lineage_id, from_agent, to_agent, generation, body) values ('L-003', 'sol-003', 'sol-003-g2', 1, 'old')")
    db.execute("insert into handoffs (lineage_id, from_agent, generation, body) values ('L-003', 'sol-003-g2', 2, 'new')")
    out = op.lineages_to_resume()
    assert [(r["lineage_id"], r["from_agent"], r["generation"], r["stub"]) for r in out] == [
        ("L-002", "sol-002", 1, True), ("L-003", "sol-003-g2", 2, False)]
    assert out[0]["body"] == "stub body" and isinstance(out[0]["handoff_id"], int)
    assert a1.id == "sol-001"


def test_agents_without_ack_names_the_live_agents_that_never_acknowledged(db, op, board, make_agent):
    author = make_agent("solver", agent_id="sol-001")[0]
    quiet = make_agent("solver", agent_id="sol-002")[0]
    acked = make_agent("solver", agent_id="sol-003")[0]
    gone = make_agent("solver", agent_id="sol-004")[0]
    op.set_agent_status(gone.id, "ended")
    db.execute("update agents set ended_at = now() - interval '1 hour' where id = %s", (gone.id,))
    msg = board.post(author, "finding", "look here")["id"]
    op.vote(msg, 1, "everyone read this")
    board.ack(acked, msg)
    # Spawned after the pin lifts: not in the window.
    db.execute("insert into agents (id, role, lineage_id, cwd, token, spawned_at) values ('sol-009', 'solver', 'L-009', '/tmp', 't9', now() + interval '1 day')")
    assert op.agents_without_ack(msg) == ["sol-001", "sol-002"]
    assert quiet.id == "sol-002"


def test_inbox_is_pings_first_then_newest_and_honours_since_seq(db, op, make_agent, civ, board):
    sol = make_agent("solver", agent_id="sol-001")[0]
    sup = make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")[0]
    alert = civ.alert(sol, "T-003 needs the ledger convention")["id"]
    finding = board.post(sol, "finding", "not an inbox item")["id"]
    op.open_round(1)
    report = civ.report(sup, "## Window\nquiet round")["id"]
    ping_id = int(db.fetch_one(
        "insert into messages (author_id, kind, body) values (%s, 'report', '[ping: succession_failing] two windows of kills') returning id",
        (sup.id,))["id"])
    items = op.inbox(since_seq=0)
    assert [i["seq"] for i in items] == [ping_id, report, alert]
    assert items[0]["ping"] and items[0]["trigger"] == "succession_failing" and items[0]["kind"] == "report"
    assert items[1]["trigger"] is None and items[2]["kind"] == "alert" and items[2]["author_id"] == sol.id
    assert finding not in [i["seq"] for i in items]
    assert [i["seq"] for i in op.inbox(since_seq=report)] == [ping_id]
    assert op.inbox(since_seq=0, limit=1) == items[:1]


def test_lineages_to_resume_skips_supervisor_rounds_and_last_round_number(op, make_agent, db):
    sol, _ = make_agent("solver", agent_id="sol-001", lineage="L-001")
    sup, _ = make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")
    with db.transaction() as conn:
        for agent, lineage in ((sol, "L-001"), (sup, "S-001")):
            conn.execute("insert into handoffs (lineage_id, from_agent, generation, body, stub) values (%s, %s, 1, 'x', true)",
                         (lineage, agent.id))
    rows = op.lineages_to_resume()
    assert [r["lineage_id"] for r in rows] == ["L-001"]
    assert op.last_round_number() == 0
    op.open_round(3)
    assert op.last_round_number() == 3
