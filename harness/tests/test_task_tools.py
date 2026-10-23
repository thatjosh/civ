"""M1: task_claim, task_heartbeat, task_complete, task_release."""

import hashlib
from pathlib import Path


def write_result(identity, name="result.txt", text="412\n") -> str:
    path = Path(identity.cwd) / "work" / name
    path.write_text(text)
    return f"work/{name}"


def test_claim_and_empty_queue(tasks, solver, other, task):
    out = tasks.claim(solver)
    assert out["ok"] and out["task"]["id"] == "T-001" and out["task"]["thread_id"] == task["thread_id"]
    empty = tasks.claim(other)
    assert empty["ok"] and empty["task"] is None and empty["reason"] == "no open tasks"


def test_second_lease_refused(tasks, solver, op):
    op.create_task("T-001", "a", "a")
    op.create_task("T-002", "b", "b")
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    assert "already hold task T-001" in tasks.claim(solver)["error"]


def test_vote_quota_between_claims(tasks, board, solver, other, op):
    op.create_task("T-001", "a", "a")
    op.create_task("T-002", "b", "b")
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    assert tasks.complete(solver, "T-001", write_result(solver), "done")["ok"]
    refused = tasks.claim(solver)
    assert "cast 1 more vote" in refused["error"]
    msg = board.post(other, "finding", "something to vote on")["id"]
    board.vote(solver, msg, 1, "useful")
    assert tasks.claim(solver)["task"]["id"] == "T-002"


def test_priority_follows_base_and_thread_votes(tasks, board, solver, other, op):
    op.create_task("T-001", "a", "a", priority_base=0)
    t2 = op.create_task("T-002", "b", "b", priority_base=0)
    board.vote(other, t2, 1, "this one matters more")
    assert tasks.claim(solver)["task"]["id"] == "T-002"
    op.create_task("T-003", "c", "c", priority_base=50)
    assert tasks.claim(other)["task"]["id"] == "T-003"


def test_heartbeat_extends_and_requires_lease(tasks, solver, other, task, db):
    tasks.claim(solver)
    db.execute("update tasks set lease_expires = now() + interval '1 minute'")
    out = tasks.heartbeat(solver, "T-001")
    assert out["ok"]
    remaining = db.fetch_one("select extract(epoch from lease_expires - now()) as s from tasks")["s"]
    assert remaining > 1000
    assert "do not hold" in tasks.heartbeat(other, "T-001")["error"]


def test_expired_lease_is_swept_back_to_open(tasks, solver, other, task, db):
    tasks.claim(solver)
    db.execute("update tasks set lease_expires = now() - interval '1 second'")
    out = tasks.claim(other)
    assert out["task"]["id"] == "T-001"
    assert db.fetch_one("select lease_owner from tasks")["lease_owner"] == other.id


def test_complete_copies_by_hash_verifies_and_posts(tasks, board, solver, task, shared_dir, fake_verifier, db):
    tasks.claim(solver)
    rel = write_result(solver, text="total 412\n")
    out = tasks.complete(solver, "T-001", rel, "Ledger totals 412")
    digest = hashlib.sha256(b"total 412\n").hexdigest()[:16]
    assert out == {"ok": True, "result_ref": f"shared/{digest}-result.txt", "score": 0.75, "notes": "checked T-001"}
    assert (shared_dir / f"{digest}-result.txt").read_text() == "total 412\n"
    assert fake_verifier.calls == [("T-001", str(shared_dir / f"{digest}-result.txt"))]
    row = db.fetch_one("select status, score, completed_by, lease_owner from tasks")
    assert (row["status"], row["score"], row["completed_by"], row["lease_owner"]) == ("done", 0.75, solver.id, None)
    last = board.read(solver, thread_id=task["thread_id"])["messages"][-1]
    assert last["kind"] == "finding" and last["body"] == "[T-001 completed] Ledger totals 412" and last["refs"] == [out["result_ref"]]


def test_complete_refusals(tasks, solver, other, task, tmp_path):
    tasks.claim(solver)
    assert "not found" in tasks.complete(solver, "T-001", "work/missing.txt", "s")["error"]
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("x")
    assert "inside your own directory" in tasks.complete(solver, "T-001", str(outside), "s")["error"]
    assert "inside your own directory" in tasks.complete(solver, "T-001", "../../elsewhere.txt", "s")["error"]
    assert "empty" in tasks.complete(solver, "T-001", write_result(solver), " ")["error"]
    assert "do not hold" in tasks.complete(other, "T-001", write_result(other), "s")["error"]


def test_complete_survives_broken_verifier(db, shared_dir, knobs, solver, task):
    from harness.board_mcp.tools.task import TaskTools

    def broken(task_id, path):
        raise RuntimeError("boom")

    t = TaskTools(db, shared_dir, knobs, broken)
    t.claim(solver)
    out = t.complete(solver, "T-001", write_result(solver), "done anyway")
    assert out["ok"] and out["score"] is None and "verifier error: boom" in out["notes"]


def test_release_backoff_then_blocked(tasks, board, solver, other, task, db):
    db.execute("update config set value = 2 where knob = 'max_attempts'")
    db.execute("update config set value = 60 where knob = 'backoff_base_s'")
    tasks.claim(solver)
    first = tasks.release(solver, "T-001", "ledger page is missing")
    assert first["ok"] and first["attempts"] == 1 and first["status"] == "open"
    assert tasks.claim(other)["task"] is None  # backoff keeps it out of the queue
    db.execute("update tasks set next_attempt_at = now()")
    assert tasks.claim(other)["task"]["id"] == "T-001"
    second = tasks.release(other, "T-001", "still missing")
    assert second["attempts"] == 2 and second["status"] == "blocked"
    kinds = [m["kind"] for m in board.read(solver, thread_id=task["thread_id"])["messages"]]
    assert kinds.count("blocker") == 2
    assert "say why" in tasks.release(solver, "T-001", "")["error"]


def test_backoff_doubles(tasks, solver, other, task, db):
    db.execute("update config set value = 6 where knob = 'max_attempts'")
    db.execute("update config set value = 100 where knob = 'backoff_base_s'")
    tasks.claim(solver)
    tasks.release(solver, "T-001", "r1")
    db.execute("update tasks set next_attempt_at = now()")
    tasks.claim(other)
    tasks.release(other, "T-001", "r2")
    secs = db.fetch_one("select extract(epoch from next_attempt_at - now()) as s from tasks")["s"]
    assert 190 < secs <= 200


def test_claim_quota_is_waived_while_the_vote_rate_limit_holds(tasks, board, solver, other, op, db):
    # A solver at its hourly vote limit cannot vote; the claim rule must not then lock it out.
    db.execute("update config set value = 2 where knob = 'vote_rate_per_h'")
    for i in range(3):
        op.create_task(f"T-10{i}", f"t{i}", "d")
    posts = [board.post(other, "finding", f"f{i}")["id"] for i in range(3)]
    assert tasks.claim(solver)["task"]["id"] == "T-100"
    assert board.vote(solver, posts[0], 1, "ok")["ok"] and board.vote(solver, posts[1], 1, "ok")["ok"]
    assert "rate limit" in board.vote(solver, posts[2], 1, "ok")["error"]
    tasks.release(solver, "T-100", "moving on")
    # One vote since the last claim satisfies the quota of one; claim again, then the solver is
    # out of votes for the hour and the next claim is waived rather than refused.
    assert tasks.claim(solver)["task"]["id"] == "T-101"
    tasks.release(solver, "T-101", "moving on")
    out = tasks.claim(solver)
    assert out["ok"] and out["task"]["id"] == "T-102", out
    waived = db.fetch_all("select payload from events where type = 'vote_quota.waived' and agent_id = %s", (solver.id,))
    assert len(waived) == 1 and waived[0]["payload"]["quota"] == 1
