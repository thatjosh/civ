"""M6: civ_merge queues a proposal for the gate, in the contract's refusal order, and the Operator's
merge queue methods. Every refusal has a test that fails if its check is removed."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.board_mcp.tools.civ import NO_ROUND

PROMPT_DIFF = """diff --git a/harness/prompts/handoff_request.md b/harness/prompts/handoff_request.md
--- a/harness/prompts/handoff_request.md
+++ b/harness/prompts/handoff_request.md
@@ -1 +1 @@
-# Hand off now
+# Hand off now, please
"""
SPAWNER_DIFF = PROMPT_DIFF.replace("harness/prompts/handoff_request.md", "harness/orchestrator/spawner.py")
OUTSIDE_DIFF = PROMPT_DIFF.replace("harness/prompts/handoff_request.md", "control/civ.toml")
EMPTY_DIFF = "no headers here\n"


@pytest.fixture
def sup(make_agent):
    return make_agent("supervisor", agent_id="sup-001-r1", lineage="S-001")[0]


def propose(board, author, text: str, name: str = "fix.diff", body: str = "change the prompt") -> int:
    """Write a diff into the author's own directory and post it as a proposal, so the server's
    hashing path into shared/ is the one under test."""
    folder = Path(author.cwd) / "proposals"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(text)
    out = board.post(author, "proposal", body, refs=[f"proposals/{name}"])
    assert out["ok"], out
    return out["id"]


# --- refusals, in the contract's order --------------------------------------------------------------


def test_merge_is_supervisor_only(civ, board, solver):
    pid = propose(board, solver, PROMPT_DIFF)
    assert civ.merge(solver, pid)["error"] == "supervisor only"


def test_merge_needs_an_open_round(civ, board, solver, sup):
    pid = propose(board, solver, PROMPT_DIFF)
    assert civ.merge(sup, pid)["error"] == NO_ROUND


def test_merge_refuses_a_message_that_is_not_a_proposal(civ, board, solver, sup, op):
    op.open_round(1)
    finding = board.post(solver, "finding", "a fact")["id"]
    assert civ.merge(sup, finding)["error"] == f"message {finding} is not a proposal"
    assert civ.merge(sup, 99999)["error"] == "message 99999 is not a proposal"


def test_merge_refuses_a_proposal_without_a_diff_ref(civ, board, solver, sup, op):
    op.open_round(1)
    pid = propose(board, solver, PROMPT_DIFF, name="notes.txt")
    assert civ.merge(sup, pid)["error"] == "proposal has no .diff ref"


def test_merge_refuses_a_diff_naming_no_files(civ, board, solver, sup, op):
    op.open_round(1)
    pid = propose(board, solver, EMPTY_DIFF)
    assert civ.merge(sup, pid)["error"] == "diff names no files"


def test_merge_refuses_paths_outside_harness(civ, board, solver, sup, op):
    op.open_round(1)
    pid = propose(board, solver, OUTSIDE_DIFF)
    assert civ.merge(sup, pid)["error"] == "a proposal may only change files under harness/: control/civ.toml"


def test_merge_queues_once_and_refuses_a_second_queue_or_a_merged_proposal(civ, board, solver, sup, op, db):
    op.open_round(1)
    pid = propose(board, solver, PROMPT_DIFF)
    out = civ.merge(sup, pid)
    assert out == {"ok": True, "merge_id": out["merge_id"], "status": "queued"}
    row = db.fetch_one("select * from merges where id = %s", (out["merge_id"],))
    assert row["status"] == "queued" and row["paths"] == ["harness/prompts/handoff_request.md"]
    assert row["requested_by"] == sup.id and row["diff_ref"].startswith("shared/") and row["diff_ref"].endswith("-fix.diff")
    assert civ.merge(sup, pid)["error"] == "already queued"
    db.execute("update merges set status = 'merged' where id = %s", (out["merge_id"],))
    assert civ.merge(sup, pid)["error"] == "already merged"
    types = [e["type"] for e in db.fetch_all("select type from events where agent_id = %s order by seq", (sup.id,))]
    assert "merge.queued" in types


def test_failed_or_rejected_merges_free_the_proposal_for_another_try(civ, board, solver, sup, op, db):
    op.open_round(1)
    pid = propose(board, solver, PROMPT_DIFF)
    first = civ.merge(sup, pid)["merge_id"]
    db.execute("update merges set status = 'failed' where id = %s", (first,))
    assert civ.merge(sup, pid)["ok"]


def test_round_merge_cap(civ, board, solver, other, sup, op, db):
    op.open_round(1)
    a = propose(board, solver, PROMPT_DIFF, name="a.diff")
    b = propose(board, other, PROMPT_DIFF.replace("please", "now"), name="b.diff")
    c = propose(board, solver, PROMPT_DIFF.replace("please", "soon"), name="c.diff")
    assert civ.merge(sup, a)["ok"] and civ.merge(sup, b)["ok"]
    assert civ.merge(sup, c)["error"] == "round merge cap reached (2)"
    # A failed merge does not count against the cap.
    db.execute("update merges set status = 'failed'")
    assert civ.merge(sup, c)["ok"]


def test_cap_comes_from_the_shape(civ, board, solver, other, sup, op):
    op.open_round(1)
    op.control_set("shape", {"merge": {"max_per_round": 1}})
    a = propose(board, solver, PROMPT_DIFF, name="a.diff")
    b = propose(board, other, PROMPT_DIFF.replace("please", "now"), name="b.diff")
    assert civ.merge(sup, a)["ok"]
    assert civ.merge(sup, b)["error"] == "round merge cap reached (1)"


def test_protected_path_is_queued_for_the_operator_with_an_alert(civ, board, solver, sup, op, db):
    op.open_round(1)
    pid = propose(board, solver, SPAWNER_DIFF)
    out = civ.merge(sup, pid)
    assert out["ok"] and out["status"] == "queued_for_operator" and out["paths"] == ["harness/orchestrator/spawner.py"]
    row = db.fetch_one("select status, detail from merges where id = %s", (out["merge_id"],))
    assert row["status"] == "queued_for_operator" and "harness/orchestrator/spawner.py" in row["detail"]
    alert = db.fetch_one("select author_id, body from messages where kind = 'alert' order by id desc limit 1")
    assert alert["author_id"] == sup.id
    assert alert["body"] == f"[merge queued for operator] proposal #{pid} touches harness/orchestrator/spawner.py"
    assert db.fetch_one("select 1 from events where type = 'merge.queued_for_operator'") is not None
    # Queued for the operator still counts as spoken for.
    assert civ.merge(sup, pid)["error"] == "already queued"


def test_protected_paths_come_from_the_shape(civ, board, solver, sup, op):
    op.open_round(1)
    op.control_set("shape", {"merge": {"protected_paths": ["harness/prompts/"]}})
    pid = propose(board, solver, PROMPT_DIFF)
    assert civ.merge(sup, pid)["status"] == "queued_for_operator"


def test_pin_blocks_merge(civ, board, solver, other, sup, op):
    op.open_round(1)
    pid = propose(board, solver, PROMPT_DIFF)
    target = board.post(other, "finding", "steer")["id"]
    op.vote(target, 1, "read this")
    assert civ.merge(sup, pid)["error"].startswith("acknowledge")


def test_missing_diff_file_is_refused(civ, board, solver, sup, op, shared_dir):
    op.open_round(1)
    pid = propose(board, solver, PROMPT_DIFF)
    for f in shared_dir.glob("*-fix.diff"):
        f.unlink()
    assert civ.merge(sup, pid)["error"].startswith("diff file missing: shared/")


def test_civ_merge_is_registered_on_the_server():
    source = (Path(__file__).resolve().parents[1] / "board_mcp" / "server.py").read_text()
    assert "async def civ_merge(proposal_id: int" in source and 'gw.call(ctx, "civ_merge", civ.merge' in source


# --- the Operator's side -------------------------------------------------------------------------


def test_merge_queue_start_finish_and_since(civ, board, solver, sup, op, db):
    op.open_round(1)
    pid = propose(board, solver, PROMPT_DIFF)
    merge_id = civ.merge(sup, pid)["merge_id"]
    queue = op.merge_queue()
    assert [m["id"] for m in queue] == [merge_id] and queue[0]["status"] == "queued"
    assert queue[0]["paths"] == ["harness/prompts/handoff_request.md"] and queue[0]["proposal_id"] == pid
    assert op.merge_start(merge_id) is True
    # Taken once: a second merger must not get it.
    assert op.merge_start(merge_id) is False
    assert op.merge_queue() == []
    op.merge_finish(merge_id, "merged", "committed", version_before="0.1.0", version_after="0.1.1", commit_sha="abc123")
    row = db.fetch_one("select status, detail, version_after, commit_sha, finished_at from merges where id = %s", (merge_id,))
    assert row["status"] == "merged" and row["version_after"] == "0.1.1" and row["commit_sha"] == "abc123" and row["finished_at"]
    event = db.fetch_one("select agent_id, payload from events where type = 'merge.merged'")
    assert event["agent_id"] == sup.id and event["payload"]["merge_id"] == merge_id and event["payload"]["version_after"] == "0.1.1"
    since = op.merges_since(db.fetch_one("select requested_at - interval '1 minute' as t from merges")["t"])
    assert [m["id"] for m in since] == [merge_id]
    with pytest.raises(ValueError):
        op.merge_finish(99999, "failed", "x")


def test_brief_carries_recent_merges_and_pending_proposals(civ, board, solver, other, sup, op, db):
    rid = op.open_round(1)
    waiting = propose(board, solver, PROMPT_DIFF, name="a.diff", body="the one waiting for review")
    queued = propose(board, other, PROMPT_DIFF.replace("please", "now"), name="b.diff")
    board.vote(other, waiting, 1, "worth it")
    merge_id = civ.merge(sup, queued)["merge_id"]
    op.merge_finish(merge_id, "failed", "x" * 500)
    brief = op.round_brief(rid, supervisors=1)
    merges = brief["merges"]
    assert [m["id"] for m in merges["recent"]] == [merge_id]
    assert merges["recent"][0]["status"] == "failed" and len(merges["recent"][0]["detail"]) == 200
    assert set(merges["recent"][0]) == {"id", "proposal_id", "requested_by", "status", "paths", "detail", "version_after", "requested_at"}
    # The failed one is pending again; both proposals wait, the voted one first.
    assert [p["id"] for p in merges["pending_proposals"]] == [waiting, queued]
    first = merges["pending_proposals"][0]
    assert first["author_id"] == solver.id and first["body"] == "the one waiting for review" and first["score"] > 0
    assert first["refs"][0].startswith("shared/") and first["refs"][0].endswith("-a.diff")
