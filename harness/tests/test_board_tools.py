from pathlib import Path
"""M1: board_post, board_read, board_top, board_search, board_vote, board_ack."""

from harness.board_mcp.tools.board import BoardTools


def test_post_finding_into_task_thread(board, solver, task):
    out = board.post(solver, "finding", "The 1852 ledger totals 412 shillings", thread_id=task["thread_id"])
    assert out["ok"] and out["id"] == out["seq"]
    read = board.read(solver, thread_id=task["thread_id"])
    bodies = [m["body"] for m in read["messages"]]
    assert bodies[0].startswith("T-001:") and bodies[-1].endswith("412 shillings")
    assert read["head_seq"] == out["seq"]


def test_post_refuses_operator_and_routed_kinds(board, solver):
    assert "civ_alert" in board.post(solver, "alert", "help")["error"]
    assert "civ_report" in board.post(solver, "report", "x")["error"]
    assert not board.post(solver, "task", "x")["ok"]
    assert not board.post(solver, "broadcast", "x")["ok"]
    assert "empty" in board.post(solver, "finding", "   ")["error"]


def test_proposal_requires_a_ref(board, solver):
    assert "ref" in board.post(solver, "proposal", "change the prompt")["error"]
    # The ref must name a real file under the agent's directory, because the server copies it into shared/.
    diff = Path(solver.cwd) / "work" / "prompt.diff"
    diff.parent.mkdir(parents=True, exist_ok=True)
    diff.write_text("--- a\n+++ b\n")
    assert board.post(solver, "proposal", "change the prompt", refs=["work/prompt.diff"])["ok"]


def test_post_inherits_thread_from_parent_and_rejects_non_roots(board, solver, other, task):
    first = board.post(solver, "question", "Which year?", thread_id=task["thread_id"])
    reply = board.post(other, "finding", "1852", parent_id=first["id"])
    assert reply["ok"]
    msgs = board.read(other, thread_id=task["thread_id"])["messages"]
    assert msgs[-1]["parent_id"] == first["id"] and msgs[-1]["thread_id"] == task["thread_id"]
    bad = board.post(other, "finding", "x", thread_id=first["id"])
    assert "not a thread root" in bad["error"]
    assert "does not exist" in board.post(other, "finding", "x", thread_id=99999)["error"]


def test_post_rate_limit(board, solver, db):
    db.execute("update config set value = 5 where knob = 'post_rate_per_h'")
    for i in range(5):
        assert board.post(solver, "finding", f"f{i}")["ok"]
    assert "rate limit" in board.post(solver, "finding", "one more")["error"]


def test_long_body_spills_to_shared_file(board, solver, shared_dir, db):
    db.execute("update config set value = 500 where knob = 'long_body_chars'")
    body = "x" * 1200
    out = board.post(solver, "finding", body)
    msg = board.read(solver, since_seq=out["id"] - 1)["messages"][0]
    assert msg["body"].endswith("]") and "[full text: shared/msg-" in msg["body"]
    assert len(msg["refs"]) == 1 and (shared_dir / msg["refs"][0].split("/")[-1]).read_text() == body


def test_read_seq_is_stamped_from_last_read(board, solver, db, task):
    board.post(solver, "finding", "before any read")
    assert db.fetch_one("select read_seq from messages order by id desc limit 1")["read_seq"] is None
    head = board.read(solver, thread_id=task["thread_id"])["head_seq"]
    board.post(solver, "finding", "after a read")
    assert db.fetch_one("select read_seq from messages order by id desc limit 1")["read_seq"] == head


def test_read_by_kind_orders_by_score_and_limit_is_capped(board, solver, other):
    low = board.post(solver, "finding", "meh")["id"]
    high = board.post(solver, "finding", "important")["id"]
    board.vote(other, high, 1, "this matters")
    msgs = board.read(other, kind="finding")["messages"]
    assert [m["id"] for m in msgs] == [high, low]
    assert msgs[0]["votes_up"] == 1 and msgs[0]["score"] > 0
    assert len(board.read(other, kind="finding", limit=500)["messages"]) == 2  # cap applies, no error


def test_top_window_excludes_old_messages(board, solver, db):
    old = board.post(solver, "finding", "ancient")["id"]
    db.execute("update messages set created_at = now() - interval '3 days' where id = %s", (old,))
    fresh = board.post(solver, "finding", "fresh")["id"]
    ids = [m["id"] for m in board.top(solver, kind="finding", window_hours=24)["messages"]]
    assert ids == [fresh]
    assert set(m["id"] for m in board.top(solver, kind="finding", window_hours=24 * 7)["messages"]) == {old, fresh}


def test_search_fulltext_and_substring_fallback(board, solver, other):
    board.post(solver, "finding", "The mill burned in the spring of 1849")
    hit = board.post(solver, "finding", "Census lists forty households")["id"]
    board.vote(other, hit, 1, "verified against ledger T-7")
    assert [m["body"] for m in board.search(other, "mill burning")["messages"]] == ["The mill burned in the spring of 1849"]
    assert board.search(other, "T-7")["messages"][0]["id"] == hit  # reason text is searchable, via fallback
    assert "empty" in board.search(other, "  ")["error"]


def test_vote_rules(board, solver, other, db):
    mine = board.post(solver, "finding", "my own")["id"]
    assert "own message" in board.vote(solver, mine, 1, "nice")["error"]
    assert "reason" in board.vote(other, mine, 1, "")["error"]
    assert "+1 or -1" in board.vote(other, mine, 2, "r")["error"]
    assert "does not exist" in board.vote(other, 424242, 1, "r")["error"]
    assert board.vote(other, mine, -1, "unverified")["weight"] == 1.0
    assert "already voted" in board.vote(other, mine, 1, "changed my mind")["error"]


def test_vote_rate_limit(board, solver, other, db):
    db.execute("update config set value = 5 where knob = 'vote_rate_per_h'")
    ids = [board.post(solver, "finding", f"f{i}")["id"] for i in range(6)]
    for i in ids[:5]:
        assert board.vote(other, i, 1, "ok")["ok"]
    assert "rate limit" in board.vote(other, ids[5], 1, "ok")["error"]


def test_supervisor_weight_equals_solvers_over_supervisors(board, make_agent, supervisor):
    solvers = [make_agent("solver")[0] for _ in range(4)]
    msg = board.post(solvers[0], "finding", "weighty")["id"]
    assert board.vote(supervisor, msg, 1, "confirmed")["weight"] == 4.0
    make_agent("supervisor")
    msg2 = board.post(solvers[1], "finding", "later")["id"]
    assert board.vote(supervisor, msg2, 1, "confirmed")["weight"] == 2.0


def test_operator_pin_blocks_writes_until_ack(board, solver, other, op, task):
    target = board.post(other, "finding", "look at this")["id"]
    op.vote(target, 1, "everyone read this")
    blocked = board.post(solver, "finding", "trying to write")
    assert blocked["error"] == f"acknowledge operator vote on message {target} first"
    assert not board.vote(solver, target, 1, "agree")["ok"]
    assert board.read(solver, thread_id=task["thread_id"])["ok"]  # reads still work
    assert board.read(solver, kind="finding")["messages"][0]["id"] == target  # pinned first
    assert "no operator vote" in board.ack(solver, 99999)["error"]
    assert board.ack(solver, target)["ok"]
    assert board.post(solver, "finding", "now it works")["ok"]
    assert board.vote(other, target, 1, "x")["error"].startswith("acknowledge")  # other still owes an ack


def test_expired_pin_does_not_block(board, solver, other, op, db):
    target = board.post(other, "finding", "old steer")["id"]
    op.vote(target, 1, "read", pin_minutes=5)
    db.execute("update operator_pins set pinned_until = now() - interval '1 minute'")
    assert board.post(solver, "finding", "free to write")["ok"]
