"""Review tests for the task tools: path safety, quota timing, lease expiry, and blocked tasks."""

from __future__ import annotations

from pathlib import Path

import pytest


def _write(identity, name="work/result.txt", text="answer\n") -> str:
    path = Path(identity.cwd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return name


def test_result_path_escaping_the_directory_is_refused(tasks, solver, task, tmp_path):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    outside = tmp_path / "outside.txt"
    outside.write_text("leak")
    relative = tasks.complete(solver, "T-001", "../../outside.txt", "s")
    absolute = tasks.complete(solver, "T-001", str(outside), "s")
    assert "inside your own directory" in relative["error"]
    assert "inside your own directory" in absolute["error"]


def test_symlink_inside_the_directory_pointing_outside_is_refused(tasks, solver, task, tmp_path):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    link = Path(solver.cwd) / "work" / "link.txt"
    link.symlink_to(secret)
    assert "inside your own directory" in tasks.complete(solver, "T-001", "work/link.txt", "s")["error"]


def test_absolute_path_inside_the_directory_is_accepted(tasks, solver, task):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    name = _write(solver)
    assert tasks.complete(solver, "T-001", str(Path(solver.cwd) / name), "done")["ok"]


def test_votes_cast_before_the_first_claim_do_not_satisfy_the_quota(tasks, board, solver, other, op):
    # The quota is "between claims", so an early vote must not be banked against a later claim.
    op.create_task("T-001", "a", "a")
    op.create_task("T-002", "b", "b")
    board.vote(solver, board.post(other, "finding", "early")["id"], 1, "banked early")
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    assert tasks.complete(solver, "T-001", _write(solver), "done")["ok"]
    assert "cast 1 more vote" in tasks.claim(solver)["error"]


def test_empty_queue_claim_does_not_start_the_quota_clock(tasks, solver, op):
    assert tasks.claim(solver)["task"] is None
    op.create_task("T-001", "a", "a")
    assert tasks.claim(solver)["task"]["id"] == "T-001"


def test_old_owner_cannot_complete_after_its_expired_lease_was_swept(tasks, solver, other, task, db):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    db.execute("update tasks set lease_expires = now() - interval '1 second'")
    assert tasks.claim(other)["task"]["id"] == "T-001"
    late = tasks.complete(solver, "T-001", _write(solver), "too late")
    assert "do not hold" in late["error"]


def test_expired_lease_is_dead_even_before_a_sweep(tasks, solver, task, db):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    db.execute("update tasks set lease_expires = now() - interval '1 second'")
    assert not tasks.heartbeat(solver, "T-001")["ok"]


def test_blocked_task_is_never_claimed_even_after_its_backoff(tasks, solver, other, task, db):
    db.execute("update config set value = 1 where knob = 'max_attempts'")
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    assert tasks.release(solver, "T-001", "cannot do it")["status"] == "blocked"
    db.execute("update tasks set next_attempt_at = now() - interval '1 hour'")
    assert tasks.claim(other)["task"] is None


def test_release_then_reclaim_keeps_the_attempt_count(tasks, board, solver, other, task):
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    assert tasks.release(solver, "T-001", "first try failed")["attempts"] == 1
    # Backoff keeps it out of the queue briefly, so pull next_attempt_at forward for the test.
    tasks.db.execute("update tasks set next_attempt_at = now()")
    claimed = tasks.claim(other)["task"]
    assert claimed["id"] == "T-001" and claimed["attempts"] == 1


def test_two_agents_submitting_identical_bytes_share_one_result_file(tasks, board, solver, other, op, shared_dir):
    op.create_task("T-001", "a", "a")
    op.create_task("T-002", "b", "b")
    assert tasks.claim(solver)["task"]["id"] == "T-001"
    assert tasks.claim(other)["task"]["id"] == "T-002"
    first = tasks.complete(solver, "T-001", _write(solver, text="same bytes\n"), "x")["result_ref"]
    second = tasks.complete(other, "T-002", _write(other, text="same bytes\n"), "y")["result_ref"]
    assert first == second and len(list(shared_dir.iterdir())) == 1
