"""Broadcasts and operator votes ride along on tool results; broadcast marks advance, pins persist."""

from harness.board_mcp.server import Gateway


def test_broadcast_piggybacks_once(db, op, board, solver, other):
    gw = Gateway(db)
    first = gw.envelope(solver, board.post(solver, "finding", "hello"))
    assert "notices" not in first and first["head_seq"] == 1
    b1 = op.broadcast("Prefer breadth over depth")
    b2 = op.broadcast("Post questions early")
    env = gw.envelope(solver, board.read(solver, kind="finding"))
    assert [n["seq"] for n in env["notices"]] == [b1, b2]
    assert env["notices"][0] == {"type": "broadcast", "seq": b1, "body": "Prefer breadth over depth"}
    again = gw.envelope(solver, board.read(solver, kind="finding"))
    assert "notices" not in again  # the mark advanced
    assert [n["seq"] for n in gw.envelope(other, {"ok": True})["notices"]] == [b1, b2]  # per agent


def test_operator_vote_notice_persists_until_ack(db, op, board, solver, other):
    gw = Gateway(db)
    target = board.post(other, "finding", "important")["id"]
    op.vote(target, 1, "everyone look")
    env = gw.envelope(solver, {"ok": True})
    notice = env["notices"][0]
    assert notice["type"] == "operator_vote" and notice["message_id"] == target and notice["body"] == "important"
    assert gw.envelope(solver, {"ok": True})["notices"][0]["message_id"] == target  # still pending
    board.ack(solver, target)
    assert "notices" not in gw.envelope(solver, {"ok": True})


def test_envelope_keeps_explicit_head_seq_and_refusals(db, board, solver):
    gw = Gateway(db)
    env = gw.envelope(solver, {"ok": False, "error": "nope"})
    assert env["ok"] is False and env["error"] == "nope" and env["head_seq"] == 0
