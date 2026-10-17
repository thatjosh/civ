"""The merge gate against a real, tiny git repository built in tmp_path. No board, no network."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.orchestrator.merger import DIRTY_TREE, REPO_MOVED, Merger, bump_patch, merge_settings, paths_in_diff, protected
from harness.tests.orchestrator_fakes import FakeOperator

PROMPT = "# Hand off now\nStop and write the handoff.\n"
TEST_OK = "from pathlib import Path\n\ndef test_prompt_mentions_handoff():\n    text = Path(__file__).resolve().parents[1].joinpath('prompts', 'x.md').read_text()\n    assert 'handoff' in text\n"


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository with harness/VERSION, one prompt and one test that reads the prompt."""
    root = tmp_path / "repo"
    (root / "harness" / "prompts").mkdir(parents=True)
    (root / "harness" / "tests").mkdir(parents=True)
    (root / "shared").mkdir()
    (root / "harness" / "VERSION").write_text("0.1.0\n")
    (root / "harness" / "prompts" / "x.md").write_text(PROMPT)
    (root / "harness" / "tests" / "test_x.py").write_text(TEST_OK)
    (root / ".gitignore").write_text("shared/\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "civ@example.test")
    git(root, "config", "user.name", "civ test")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return root


def diff_for(repo: Path, rel: str, new_text: str, name: str) -> str:
    """A unified diff that changes `rel` to `new_text`, written under shared/ like the server does."""
    original = repo / rel
    scratch = repo / "shared" / (name + ".new")
    scratch.write_text(new_text)
    proc = subprocess.run(["git", "diff", "--no-index", "--", str(original), str(scratch)], cwd=repo, capture_output=True, text=True)
    text = proc.stdout
    # Relabel the two sides the way an agent is told to: a/<path> and b/<path>.
    lines = []
    for line in text.splitlines():
        if line.startswith("--- "):
            lines.append(f"--- a/{rel}")
        elif line.startswith("+++ "):
            lines.append(f"+++ b/{rel}")
        elif line.startswith("diff --git "):
            lines.append(f"diff --git a/{rel} b/{rel}")
        else:
            lines.append(line)
    (repo / "shared" / f"{name}.diff").write_text("\n".join(lines) + "\n")
    scratch.unlink()
    return f"shared/{name}.diff"


def op_root(op: FakeOperator) -> Path | None:
    return getattr(op, "repo_root", None)


def make(repo: Path, **kw) -> tuple[Merger, FakeOperator]:
    op = FakeOperator()
    op.repo_root = repo  # so row() can compute paths the way the server would
    merger = Merger(op, repo, test_command=["python", "-m", "pytest", "harness/tests", "-q", "-p", "no:cacheprovider"],
                    protected_paths=["harness/orchestrator/spawner.py", "control/"], **kw)
    return merger, op


def row(op: FakeOperator, diff_ref: str, merge_id: int = 1, proposal_id: int = 7) -> dict:
    # paths as the server stores them when it queues the row (from the diff's headers).
    r = {"id": merge_id, "proposal_id": proposal_id, "requested_by": "sup-001-r2", "round_id": 2,
         "diff_ref": diff_ref, "paths": paths_in_diff((op_root(op) / diff_ref).read_text()) if op_root(op) else [],
         "status": "queued"}
    op.merges.append(r)
    return r


def test_green_merges_bumps_the_version_and_commits(repo):
    merger, op = make(repo)
    ref = diff_for(repo, "harness/prompts/x.md", PROMPT + "Your first call must be the handoff.\n", "ok")
    out = merger.merge_one(row(op, ref))
    assert out.merged, out.detail
    assert out.version_before == "0.1.0" and out.version_after == "0.1.1"
    assert (repo / "harness" / "VERSION").read_text() == "0.1.1\n"
    assert "first call must be the handoff" in (repo / "harness" / "prompts" / "x.md").read_text()
    assert git(repo, "rev-parse", "HEAD") == out.commit_sha
    assert git(repo, "log", "-1", "--pretty=%s") == "civ merge: proposal #7 by sup-001-r2 (harness/prompts/x.md)"
    # Branch and worktree are gone, and the tree is clean.
    assert "civ/merge-1" not in git(repo, "branch", "--list")
    assert git(repo, "status", "--porcelain") == ""


def test_red_records_the_failure_and_leaves_the_tree_untouched(repo):
    merger, op = make(repo)
    head = git(repo, "rev-parse", "HEAD")
    ref = diff_for(repo, "harness/prompts/x.md", "# Stop\nNothing about the thing the test looks for.\n", "red")
    out = merger.merge_one(row(op, ref))
    assert out.status == "failed" and out.detail.startswith("tests failed (exit 1)")
    assert "test_prompt_mentions_handoff" in out.detail
    assert git(repo, "rev-parse", "HEAD") == head
    assert (repo / "harness" / "VERSION").read_text() == "0.1.0\n"
    assert (repo / "harness" / "prompts" / "x.md").read_text() == PROMPT


def test_a_diff_that_does_not_apply_fails(repo):
    merger, op = make(repo)
    (repo / "shared" / "stale.diff").write_text(
        "--- a/harness/prompts/x.md\n+++ b/harness/prompts/x.md\n@@ -1,2 +1,2 @@\n-# Something else\n+# Changed\n Stop and write the handoff.\n")
    out = merger.merge_one(row(op, "shared/stale.diff"))
    assert out.status == "failed" and out.detail.startswith("diff does not apply")


def test_dirty_harness_tree_is_refused(repo):
    merger, op = make(repo)
    ref = diff_for(repo, "harness/prompts/x.md", PROMPT + "more\n", "ok")
    (repo / "harness" / "prompts" / "x.md").write_text(PROMPT + "a developer's half-edit\n")
    out = merger.merge_one(row(op, ref))
    assert out.status == "failed" and out.detail == DIRTY_TREE


def test_protected_and_outside_harness_paths_fail(repo):
    merger, op = make(repo)
    (repo / "shared" / "spawner.diff").write_text(
        "--- a/harness/orchestrator/spawner.py\n+++ b/harness/orchestrator/spawner.py\n@@ -1 +1 @@\n-x\n+y\n")
    out = merger.merge_one(row(op, "shared/spawner.diff"))
    assert out.status == "failed" and out.detail == "protected path: harness/orchestrator/spawner.py"
    (repo / "shared" / "ceiling.diff").write_text(
        "--- a/control/ceilings.toml\n+++ b/control/ceilings.toml\n@@ -1 +1 @@\n-x\n+y\n")
    out = merger.merge_one(row(op, "shared/ceiling.diff", merge_id=2))
    assert out.status == "failed" and out.detail.startswith("a proposal may only change files under harness/")


def test_repository_that_moved_during_the_tests_is_not_merged(repo):
    # A run function that lets the main repository move while the tests are "running".
    real_run = subprocess.run

    def run(cmd, **kw):
        if cmd[:3] == ["python", "-m", "pytest"]:
            (repo / "harness" / "prompts" / "y.md").write_text("new file\n")
            git(repo, "add", "-A")
            git(repo, "commit", "-q", "-m", "someone else")
        return real_run(cmd, **kw)

    merger, op = make(repo, run=run)
    ref = diff_for(repo, "harness/prompts/x.md", PROMPT + "more\n", "ok")
    out = merger.merge_one(row(op, ref))
    assert out.status == "failed" and out.detail == REPO_MOVED
    assert (repo / "harness" / "VERSION").read_text() == "0.1.0\n"
    assert git(repo, "status", "--porcelain") == ""


def test_drain_claims_rows_finishes_them_and_logs(repo):
    merger, op = make(repo)
    ref = diff_for(repo, "harness/prompts/x.md", PROMPT + "more\n", "ok")
    row(op, ref)
    outcomes = merger.drain()
    assert [o.status for o in outcomes] == ["merged"]
    assert op.merges[0]["status"] == "merged" and op.merges[0]["version_after"] == "0.1.1"
    # The event is logged by merge_finish (the operator's), once, with the row's paths.
    assert [e[1] for e in op.events] == ["merge.merged"]
    assert op.events[0][2]["paths"] == ["harness/prompts/x.md"] and op.events[0][2]["version_after"] == "0.1.1"
    # A second drain finds nothing queued.
    assert merger.drain() == []


async def test_run_until_drains_once_more_after_done(repo):
    import asyncio

    merger, op = make(repo, poll_s=0.01)
    done = asyncio.Event()
    done.set()
    ref = diff_for(repo, "harness/prompts/x.md", PROMPT + "more\n", "ok")
    row(op, ref)
    outcomes = await merger.run_until(done)
    assert [o.status for o in outcomes] == ["merged"]


def test_helpers():
    text = "diff --git a/harness/a.md b/harness/a.md\n--- a/harness/a.md\n+++ b/harness/a.md\n@@\n--- a/control/x\n+++ /dev/null\n"
    assert paths_in_diff(text) == ["harness/a.md"]
    assert protected(["harness/orchestrator/spawner.py", "control/ceilings.toml", "harness/prompts/x.md"],
                     ["harness/orchestrator/spawner.py", "control/"]) == ["harness/orchestrator/spawner.py", "control/ceilings.toml"]
    assert bump_patch("0.1.9") == "0.1.10" and bump_patch("dev") is None
    settings = merge_settings({"merge": {"max_per_round": 1}})
    assert settings["max_per_round"] == 1 and "harness/orchestrator/spawner.py" in settings["protected_paths"]
    assert merge_settings(None)["test_command"].startswith("python -m pytest")
