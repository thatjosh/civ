"""M3, server side: the handoff lockout, the handoff ending the agent, lease transfer to the
successor, the stub handoff, and the lineage reads the orchestrator relies on."""

from __future__ import annotations

from pathlib import Path

from harness.board_mcp.auth import resolve_token
from harness.board_mcp.stub import HEADINGS, build_stub
from harness.board_mcp.tools.common import HANDOFF_LOCKOUT

VALID_HANDOFF = "\n".join(f"## {h}\nsomething" for h in HEADINGS) + "\n"


def _write(identity, name="result.txt", text="households: 12\n") -> str:
    path = Path(identity.cwd) / "work" / name
    path.write_text(text)
    return f"work/{name}"


# The lockout


def test_request_handoff_flips_live_agents_only(op, solver, db):
    assert op.request_handoff(solver.id) is True
    assert db.fetch_one("select status from agents where id = %s", (solver.id,))["status"] == "handing_off"
    # A second request is a no-op: the agent is no longer live.
    assert op.request_handoff(solver.id) is False
    types = [r["type"] for r in db.fetch_all("select type from events where agent_id = %s order by seq", (solver.id,))]
    assert types.count("agent.handoff_requested") == 1


def test_every_write_but_the_handoff_is_refused_while_handing_off(board, tasks, civ, solver, other, op, task):
    assert tasks.claim(solver)["ok"]
    posted = board.post(other, "finding", "something worth a vote", thread_id=task["thread_id"])
    op.request_handoff(solver.id)
    assert board.post(solver, "finding", "late finding", thread_id=task["thread_id"])["error"] == HANDOFF_LOCKOUT
    assert board.vote(solver, posted["id"], 1, "good")["error"] == HANDOFF_LOCKOUT
    assert tasks.claim(solver)["error"] == HANDOFF_LOCKOUT
    assert tasks.heartbeat(solver, "T-001")["error"] == HANDOFF_LOCKOUT
    assert tasks.complete(solver, "T-001", _write(solver), "done")["error"] == HANDOFF_LOCKOUT
    assert tasks.release(solver, "T-001", "stuck")["error"] == HANDOFF_LOCKOUT
    assert civ.alert(solver, "help")["error"] == HANDOFF_LOCKOUT
    # Reads still work, and the lease was not disturbed by any refusal.
    assert board.read(solver, thread_id=task["thread_id"])["ok"]
    assert db_lease(op, "T-001") == solver.id


def test_lockout_is_per_agent(board, solver, other, op, task):
    op.request_handoff(solver.id)
    assert board.post(other, "finding", "unaffected", thread_id=task["thread_id"])["ok"]


def test_ack_stays_open_during_handoff_and_the_handoff_needs_no_ack(board, civ, solver, op, task):
    # Two gates meet here. The ack stays open under the lockout so an agent that wants to clear
    # the steer can, and the handoff is exempt from the pin so one that does not still ends with
    # a real handoff rather than a stub (the M5 live check killed two agents on the old rule).
    op.vote(task["thread_id"], 1, "look at this")
    op.request_handoff(solver.id)
    assert board.ack(solver, task["thread_id"])["ok"]
    assert civ.handoff(solver, VALID_HANDOFF)["ok"]


# The handoff itself


def test_handoff_ends_the_agent_and_returns_stop(civ, tasks, solver, op, db, task):
    tasks.claim(solver)
    op.request_handoff(solver.id)
    out = civ.handoff(solver, VALID_HANDOFF)
    assert out["ok"] and out["stop"] is True
    row = db.fetch_one("select status, ended_at from agents where id = %s", (solver.id,))
    assert row["status"] == "ended" and row["ended_at"] is not None
    # The lease is kept for the successor, not released.
    assert db_lease(op, "T-001") == solver.id
    types = [r["type"] for r in db.fetch_all("select type from events where agent_id = %s order by seq", (solver.id,))]
    assert "agent.handoff_written" in types


def test_token_dies_with_the_handoff(civ, solver, op, db, make_agent):
    identity, token = make_agent("solver")
    op.request_handoff(identity.id)
    assert civ.handoff(identity, VALID_HANDOFF)["ok"]
    with db.transaction() as conn:
        assert resolve_token(conn, token) is None
    # A second handoff from the same (now ended) agent is refused rather than stored twice.
    second = civ.handoff(identity, VALID_HANDOFF)
    assert not second["ok"] and "not requested" in second["error"]
    assert db.fetch_one("select count(*) as n from handoffs")["n"] == 1


# Succession


def test_succeed_transfers_the_lease_with_a_fresh_expiry(civ, tasks, solver, op, db, task):
    tasks.claim(solver)
    db.execute("update tasks set lease_expires = now() + interval '1 minute' where id = 'T-001'")
    op.request_handoff(solver.id)
    civ.handoff(solver, VALID_HANDOFF)
    token = op.succeed(solver.id, "sol-001-g2", solver.cwd)
    assert isinstance(token, str) and token
    row = db.fetch_one("select lease_owner, status, lease_expires > now() + interval '20 minutes' as fresh from tasks where id = 'T-001'")
    assert row["lease_owner"] == "sol-001-g2" and row["status"] == "leased" and row["fresh"]


def test_successor_inherits_lineage_directory_role_and_generation(civ, solver, op, db):
    op.request_handoff(solver.id)
    civ.handoff(solver, VALID_HANDOFF)
    token = op.succeed(solver.id, "next", solver.cwd)
    with db.transaction() as conn:
        successor = resolve_token(conn, token)
    assert successor.id == "next"
    assert successor.lineage_id == solver.lineage_id
    assert successor.cwd == solver.cwd
    assert successor.generation == solver.generation + 1
    assert successor.role == "solver"
    assert successor.status == "live"
    handoff = op.latest_handoff(solver.lineage_id)
    assert handoff["to_agent"] == "next" and handoff["from_agent"] == solver.id and handoff["stub"] is False
    succeeded = db.fetch_one("select payload from events where type = 'agent.succeeded'")["payload"]
    assert succeeded["from"] == solver.id and succeeded["to"] == "next" and succeeded["generation"] == 2


def test_succeed_after_a_kill_ends_the_predecessor_and_still_moves_the_lease(tasks, solver, op, db, task):
    tasks.claim(solver)
    # The kill path: the orchestrator never got a handoff, so the predecessor is still handing_off
    # (or live) when the successor is registered. Two active generations must not coexist.
    op.request_handoff(solver.id)
    op.succeed(solver.id, "after-kill", solver.cwd)
    assert db.fetch_one("select status from agents where id = %s", (solver.id,))["status"] == "ended"
    assert db_lease(op, "T-001") == "after-kill"
    assert op.lineage_state(solver.lineage_id) == {
        "generation": 2, "head_agent": "after-kill", "status": "live", "lease": "T-001",
    }


def test_succeed_unknown_predecessor_raises(op, solver):
    import pytest

    with pytest.raises(ValueError):
        op.succeed("nobody", "x", solver.cwd)


# Lineage reads


def test_latest_handoff_and_lineage_state_before_and_after(civ, tasks, solver, op, task):
    assert op.latest_handoff(solver.lineage_id) is None
    assert op.lineage_state("L-nope") is None
    tasks.claim(solver)
    assert op.lineage_state(solver.lineage_id) == {
        "generation": 1, "head_agent": solver.id, "status": "live", "lease": "T-001",
    }
    op.request_handoff(solver.id)
    assert op.lineage_state(solver.lineage_id)["status"] == "handing_off"
    civ.handoff(solver, VALID_HANDOFF)
    latest = op.latest_handoff(solver.lineage_id)
    assert latest["from_agent"] == solver.id and latest["to_agent"] is None and latest["generation"] == 1
    assert latest["body"] == VALID_HANDOFF
    assert op.lineage_state(solver.lineage_id)["status"] == "ended"


def test_recent_posts_are_newest_first_and_limited(board, solver, op, task):
    ids = [board.post(solver, "finding", f"finding {i}", thread_id=task["thread_id"])["id"] for i in range(5)]
    posts = op.recent_posts(solver.id, n=3)
    assert [p["id"] for p in posts] == ids[-1:-4:-1]
    assert posts[0]["kind"] == "finding" and posts[0]["thread_id"] == task["thread_id"]
    assert op.recent_posts("nobody") == []


# The stub


def test_build_stub_has_the_five_headings_and_the_facts():
    posts = [
        {"id": 12, "kind": "finding", "thread_id": 3, "body": "ledger total is 125 shillings", "created_at": "t"},
        {"id": 9, "kind": "blocker", "thread_id": 3, "body": "regex on the ledger gives false matches", "created_at": "t"},
        {"id": 14, "kind": "question", "thread_id": 4, "body": "who was the miller?", "created_at": "t"},
    ]
    task = {"id": "T-003", "title": "Ledger total", "description": "Total the receipts", "thread_id": 3}
    body = build_stub(posts, task, ["work/ledger.txt", "notes/scratch.md"])
    for h in HEADINGS:
        assert f"## {h}\n" in body
    # Oldest first inside the state of work, the blocker under what not to retry, both threads listed.
    assert body.index("seq 9 blocker") < body.index("seq 12 finding") < body.index("seq 14 question")
    assert "regex on the ledger" in body.split("## What not to retry")[1].split("## Files")[0]
    assert "thread 3: read from seq 12" in body and "thread 4: read from seq 14" in body
    assert "Finish T-003" in body and "work/ledger.txt" in body
    assert "Stub handoff" in body


def test_build_stub_with_nothing_still_has_every_heading():
    body = build_stub([], None, [])
    for h in HEADINGS:
        assert f"## {h}\n" in body
    assert "held no task" in body and "posted nothing" in body


def test_write_stub_handoff_stores_a_stub_from_the_board(board, tasks, civ, solver, op, db, task):
    tasks.claim(solver)
    board.post(solver, "finding", "half the ledger is summed", thread_id=task["thread_id"])
    board.post(solver, "blocker", "the 1852 page is torn", thread_id=task["thread_id"])
    op.request_handoff(solver.id)
    handoff_id = op.write_stub_handoff(solver.id, ["work/partial.txt"])
    row = db.fetch_one("select lineage_id, from_agent, generation, stub, body from handoffs where id = %s", (handoff_id,))
    assert row["stub"] is True and row["from_agent"] == solver.id and row["lineage_id"] == solver.lineage_id
    assert "half the ledger" in row["body"] and "1852 page is torn" in row["body"] and "T-001" in row["body"]
    assert "work/partial.txt" in row["body"]
    # It validates as a handoff: the successor's prompt builder relies on the headings.
    for h in HEADINGS:
        assert f"## {h}\n" in row["body"]
    assert op.latest_handoff(solver.lineage_id)["stub"] is True
    event = db.fetch_one("select payload from events where type = 'agent.stub_written'")["payload"]
    assert event["handoff_id"] == handoff_id and event["task"] == "T-001" and event["posts"] == 2


def db_lease(op, task_id: str) -> str | None:
    row = op.db.fetch_one("select lease_owner from tasks where id = %s", (task_id,))
    return row["lease_owner"] if row else None


def test_handoff_is_not_blocked_by_a_pending_operator_pin(civ, board, solver, other, op, db):
    # An operator vote pinned while an agent is handing off must not stop the handoff: the agent
    # is about to end and can never act on the steer, and a refused handoff becomes a stub.
    target = board.post(other, "finding", "steer target")["id"]
    op.vote(target, 1, "everyone look")
    assert op.request_handoff(solver.id)
    body = "## State of work\nx\n## Open threads\n\n## What to try next\n\n## What not to retry\n\n## Files worth reading\n"
    out = civ.handoff(solver, body)
    assert out["ok"] and out["stop"] is True
    # Other writes stay gated by the pin as before.
    assert "acknowledge operator vote" in board.post(other, "finding", "blocked")["error"]
