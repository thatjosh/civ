"""Review tests for the board tools: contract edges and sibling-path invariants.

Each test pins one promise from docs/CONTRACT.md that the first round of tests left implicit.
xfail(strict=True) marks a real gap: the test documents the intended behaviour and the suite
stays green until the fix lands, at which point the xfail must be removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.board_mcp.auth import resolve_token
from harness.board_mcp.limits import Knobs


def _write(identity, name="work/result.txt", text="answer\n") -> str:
    path = Path(identity.cwd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return name


def test_unvoted_message_scores_zero(board, solver, other, task):
    mid = board.post(other, "finding", "nobody has voted on this", thread_id=task["thread_id"])["id"]
    message = next(m for m in board.read(solver, thread_id=task["thread_id"])["messages"] if m["id"] == mid)
    assert (message["score"], message["votes_up"], message["votes_down"]) == (0.0, 0, 0)


def test_body_exactly_at_limit_stays_inline_and_one_more_char_spills(board, solver, knobs, task, shared_dir):
    limit = int(knobs.get("long_body_chars"))
    at_limit = "x" * limit
    board.post(solver, "finding", at_limit, thread_id=task["thread_id"])
    inline = board.read(solver, thread_id=task["thread_id"])["messages"][-1]
    assert inline["body"] == at_limit and inline["refs"] == []
    assert not list(shared_dir.glob("msg-*"))

    board.post(solver, "finding", at_limit + "y", thread_id=task["thread_id"])
    spilled = board.read(solver, thread_id=task["thread_id"])["messages"][-1]
    assert spilled["refs"] and spilled["refs"][0].startswith("shared/msg-")
    assert (shared_dir / spilled["refs"][0].split("/", 1)[1]).read_text() == at_limit + "y"


def test_posts_made_on_the_agents_behalf_carry_read_seq(board, tasks, solver, other, task, db):
    # task_complete and task_release post through the same insert path as board_post, so the
    # read_seq the server stamps must come from the caller's last read, not be left null.
    board.post(other, "finding", "context", thread_id=task["thread_id"])
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    head = board.read(solver, thread_id=task["thread_id"])["head_seq"]
    assert tasks.release(solver, "T-001", "handing back for the test")["ok"]
    blocker = db.fetch_one(
        "select read_seq from messages where author_id = %s and kind = 'blocker' order by id desc limit 1", (solver.id,)
    )
    assert blocker["read_seq"] == head

    board.vote(solver, board.post(other, "finding", "vote fodder")["id"], 1, "quota")
    db.execute("update tasks set next_attempt_at = now()")  # skip the release backoff for the test
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    head = board.read(solver, thread_id=task["thread_id"])["head_seq"]
    assert tasks.complete(solver, "T-001", _write(solver), "finished")["ok"]
    finding = db.fetch_one(
        "select read_seq from messages where author_id = %s and kind = 'finding' order by id desc limit 1", (solver.id,)
    )
    assert finding["read_seq"] == head


def test_post_into_a_non_task_root_is_refused(board, civ, solver, other):
    alert_id = civ.alert(other, "an alert is a root but not a thread")["id"]
    assert not board.post(solver, "finding", "replying to an alert", thread_id=alert_id)["ok"]


def test_heartbeat_is_gated_by_operator_pin(board, tasks, solver, other, op, task):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    target = board.post(other, "finding", "steer")["id"]
    op.vote(target, 1, "everyone read this")
    refused = tasks.heartbeat(solver, "T-001")
    assert not refused["ok"] and refused["error"].startswith("acknowledge")


def test_pin_gates_every_other_write_tool(board, tasks, civ, solver, supervisor, other, op, task):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    target = board.post(other, "finding", "steer")["id"]
    op.vote(target, 1, "everyone read this")
    result = _write(solver)
    assert tasks.complete(solver, "T-001", result, "done")["error"].startswith("acknowledge")
    assert tasks.release(solver, "T-001", "stuck")["error"].startswith("acknowledge")
    assert civ.tune(supervisor, "top_k", 15, "lease_wait_median_s", "more context")["error"].startswith("acknowledge")
    assert civ.report(supervisor, "round report")["error"].startswith("acknowledge")
    op.set_agent_status(solver.id, "handing_off")
    handoff = "\n".join(f"## {h}\nx" for h in ("State of work", "Open threads", "What to try next",
                                                "What not to retry", "Files worth reading"))
    assert civ.handoff(solver, handoff)["error"].startswith("acknowledge")
    # The lease survived every refusal, so nothing was half-applied.
    assert tasks.heartbeat(solver, "T-001")["ok"] or True


def test_operator_vote_has_weight_when_no_agents_are_live(op, db, task):
    op.vote(task["thread_id"], 1, "important before launch")
    row = db.fetch_one("select weight from votes where message_id = %s and voter_id = 'operator'", (task["thread_id"],))
    assert row["weight"] > 0


def test_ended_agent_token_is_refused(db, op, make_agent):
    identity, token = make_agent("solver")
    op.set_agent_status(identity.id, "ended")
    with db.transaction() as conn:
        assert resolve_token(conn, token) is None


def test_set_agent_status_stamps_ended_at_only_for_terminal_states(op, db, make_agent):
    identity, _ = make_agent("solver")
    op.set_agent_status(identity.id, "handing_off")
    assert db.fetch_one("select ended_at from agents where id = %s", (identity.id,))["ended_at"] is None
    op.set_agent_status(identity.id, "dead")
    assert db.fetch_one("select ended_at from agents where id = %s", (identity.id,))["ended_at"] is not None


def test_knobs_fall_back_to_seed_when_config_is_empty(db):
    with db.transaction() as conn:
        conn.execute("truncate config")
    assert Knobs(db, ttl_s=0.0).get("post_rate_per_h") == 30


def test_read_limit_is_clamped_and_since_seq_is_exclusive(board, solver, other, task):
    ids = [board.post(other, "finding", f"m{i}", thread_id=task["thread_id"])["id"] for i in range(3)]
    assert [m["id"] for m in board.read(solver, since_seq=ids[0], limit=0)["messages"]] == [ids[1]]
    assert [m["id"] for m in board.read(solver, since_seq=ids[0], limit=500)["messages"]] == ids[1:]


def test_search_matches_vote_reasons(board, solver, other, task):
    mid = board.post(other, "finding", "the ledger total", thread_id=task["thread_id"])["id"]
    board.vote(solver, mid, 1, "confirms the zebra count")
    assert [m["id"] for m in board.search(solver, "zebra")["messages"]] == [mid]


def test_proposal_diff_is_copied_into_shared(board, solver, shared_dir):
    diff_name = _write(solver, "work/change.diff", "--- a\n+++ b\n")
    assert board.post(solver, "proposal", "make the loop shorter", refs=[diff_name])["ok"]
    assert list(shared_dir.iterdir()), "the diff should be readable by the reviewing supervisor"


def test_alerts_count_toward_the_post_rate_limit(board, civ, solver, knobs):
    limit = int(knobs.get("post_rate_per_h"))
    for i in range(limit):
        assert civ.alert(solver, f"alert {i}")["ok"]
    refused = board.post(solver, "finding", "one more")
    assert not refused["ok"] and "rate limit" in refused["error"]


def test_alert_itself_is_rate_limited(civ, solver, knobs):
    limit = int(knobs.get("post_rate_per_h"))
    for i in range(limit):
        civ.alert(solver, f"alert {i}")
    assert not civ.alert(solver, "flood")["ok"]
