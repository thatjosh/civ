"""The SQL score and the Python score agree, and decay does what the design says."""

import pytest

from harness.board_mcp.ranking import score


def test_python_formula():
    assert score(10, 0, 2, 1.5) == pytest.approx(10 / (2 ** 1.5))
    assert score(10, 6, 2, 1.5) == pytest.approx(10 / (8 ** 1.5))
    assert score(-3, 0, 2, 1.5) < 0


def test_sql_matches_python_and_decays(board, solver, other, db):
    fresh = board.post(solver, "finding", "fresh")["id"]
    stale = board.post(solver, "finding", "stale but loved")["id"]
    board.vote(other, fresh, 1, "a")
    board.vote(other, stale, 1, "b")
    db.execute("update messages set created_at = now() - interval '10 hours' where id = %s", (stale,))
    msgs = {m["id"]: m for m in board.read(other, kind="finding")["messages"]}
    assert msgs[fresh]["score"] == pytest.approx(score(1, 0, 2, 1.5), abs=1e-3)
    assert msgs[stale]["score"] == pytest.approx(score(1, 10, 2, 1.5), abs=1e-3)
    assert [m["id"] for m in board.read(other, kind="finding")["messages"]] == [fresh, stale]


def test_decay_knob_changes_ordering(board, solver, other, db):
    old_popular = board.post(solver, "finding", "old, 3 votes")["id"]
    new_single = board.post(solver, "finding", "new, 1 vote")["id"]
    board.vote(other, old_popular, 1, "x")
    db.execute("insert into votes (message_id, voter_id, value, weight, reason) values (%s, 'v2', 1, 1, 'y'), (%s, 'v3', 1, 1, 'z')",
               (old_popular, old_popular))
    board.vote(other, new_single, 1, "x")
    db.execute("update messages set created_at = now() - interval '2 hours' where id = %s", (old_popular,))
    # Gentle decay (power 1): 3/(2+2) = 0.75 beats 1/2 = 0.5, so three older votes still win.
    db.execute("update config set value = 1 where knob = 'decay_power'")
    assert [m["id"] for m in board.read(other, kind="finding")["messages"]][0] == old_popular
    # Steep decay (power 3): 3/64 loses to 1/8, so the board forgets.
    db.execute("update config set value = 3 where knob = 'decay_power'")
    assert [m["id"] for m in board.read(other, kind="finding")["messages"]][0] == new_single
