"""The merge gate: apply a proposal's diff in an isolated worktree, run the harness tests there,
and land it on the repository only when they pass.

This is the one path by which the civilisation changes its own code, so it is deliberately a
little slow and entirely mechanical. The board server validates a civ_merge call and queues a
row (M6 contract); this module, running inside the orchestrator, drains the queue. A supervisor
never touches git, and there is no way to merge around the tests, because the tests are what
decides. A red run is recorded with its output for the next round to read; a green run commits
on a short-lived branch, bumps harness/VERSION, and fast-forwards the repository.

    python -m harness.orchestrator.merger --once      # drain the queue once (the drill)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_TEST_COMMAND = "python -m pytest harness/tests -q -x -p no:cacheprovider"
DEFAULT_PROTECTED_PATHS = (
    "harness/orchestrator/spawner.py", "harness/orchestrator/governor.py", "harness/orchestrator/merger.py",
    "harness/board_mcp/ceilings.py", "harness/board_mcp/auth.py", "harness/board_mcp/tools/task.py",
    "harness/board_mcp/merge_rules.py", "control/", "task/", "scripts/",
)
DEFAULT_MAX_PER_ROUND = 2
DEFAULT_TEST_DB_URL = "postgresql://127.0.0.1:5432/civ_merge_test"
TEST_TIMEOUT_S = 600.0
OUTPUT_TAIL_LINES = 40
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)\s*$")

STATUS_MERGED = "merged"
STATUS_FAILED = "failed"

DIRTY_TREE = "working tree has uncommitted changes under harness/"
REPO_MOVED = "repository moved; retry"


@dataclass
class MergeOutcome:
    merge_id: int
    status: str
    detail: str
    version_before: str | None = None
    version_after: str | None = None
    commit_sha: str | None = None
    # The files the diff names, from the diff itself rather than the queued row, because the
    # row's list is whatever the server stored and the merger must report what it merged.
    paths: list[str] | None = None

    @property
    def merged(self) -> bool:
        return self.status == STATUS_MERGED


# --- diff inspection and the merge section of the shape --------------------------------------
# These mirror harness/board_mcp/merge_rules.py (WS-L) and defer to it when it is importable, so
# the server's validation and the merger's defence in depth cannot drift apart.


def paths_in_diff(text: str) -> list[str]:
    try:
        from harness.board_mcp.merge_rules import paths_in_diff as shared
        return shared(text)
    except ImportError:
        pass
    paths: list[str] = []
    for line in text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].split("\t")[0].strip()
            if target == "/dev/null":
                continue
            paths.append(target[2:] if target.startswith("b/") else target)
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                target = parts[3]
                paths.append(target[2:] if target.startswith("b/") else target)
    seen: list[str] = []
    for p in paths:
        if p not in seen:
            seen.append(p)
    return seen


def protected(paths: list[str], protected_paths: list[str] | tuple[str, ...]) -> list[str]:
    try:
        from harness.board_mcp.merge_rules import protected as shared
        return shared(paths, list(protected_paths))
    except ImportError:
        pass
    hits = []
    for p in paths:
        for rule in protected_paths:
            if (rule.endswith("/") and p.startswith(rule)) or p == rule:
                hits.append(p)
                break
    return hits


def outside_harness(paths: list[str]) -> list[str]:
    try:
        from harness.board_mcp.merge_rules import outside_harness as shared
        return shared(paths)
    except ImportError:
        return [p for p in paths if not p.startswith("harness/")]


def merge_settings(shape_json: dict[str, Any] | None) -> dict[str, Any]:
    """The [merge] section of civ.toml as stored in control_state.shape, with the contract's
    defaults for anything missing."""
    try:
        from harness.board_mcp.merge_rules import load_merge_section
        return load_merge_section(shape_json or {})
    except ImportError:
        pass
    section = (shape_json or {}).get("merge") or {}
    return {
        "max_per_round": int(section.get("max_per_round", DEFAULT_MAX_PER_ROUND)),
        "protected_paths": list(section.get("protected_paths", DEFAULT_PROTECTED_PATHS)),
        "test_command": str(section.get("test_command", DEFAULT_TEST_COMMAND)),
    }


def bump_patch(version: str) -> str | None:
    m = VERSION_RE.match(version)
    if not m:
        return None
    major, minor, patch = (int(x) for x in m.groups())
    return f"{major}.{minor}.{patch + 1}"


# --- the merger ------------------------------------------------------------------------------

RunFn = Callable[..., subprocess.CompletedProcess]
SleepFn = Callable[[float], Awaitable[None]]


class Merger:
    """Drains the merge queue: one worktree, one test run and one fast-forward per row."""

    def __init__(
        self,
        operator: Any,
        repo_root: Path | str,
        test_command: str | list[str] = DEFAULT_TEST_COMMAND,
        protected_paths: list[str] | tuple[str, ...] = DEFAULT_PROTECTED_PATHS,
        version_file: str = "harness/VERSION",
        poll_s: float = 5.0,
        run: RunFn = subprocess.run,
        test_db_url: str | None = None,
        test_timeout_s: float = TEST_TIMEOUT_S,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._op = operator
        self._root = Path(repo_root).resolve()
        self._test_command = shlex.split(test_command) if isinstance(test_command, str) else list(test_command)
        self._protected = list(protected_paths)
        self._version_file = version_file
        self._poll_s = poll_s
        self._run = run
        self._test_db_url = test_db_url or os.environ.get("CIV_MERGE_TEST_DATABASE_URL", DEFAULT_TEST_DB_URL)
        self._test_timeout_s = test_timeout_s
        self._sleep = sleep

    # --- the loop ---

    async def run_until(self, done: asyncio.Event) -> list[MergeOutcome]:
        """Drain the queue every poll until `done` is set, then once more so a merge requested in
        the final round is not left queued."""
        outcomes: list[MergeOutcome] = []
        while not done.is_set():
            outcomes.extend(await asyncio.to_thread(self.drain))
            try:
                await asyncio.wait_for(done.wait(), timeout=self._poll_s)
            except asyncio.TimeoutError:
                pass
        outcomes.extend(await asyncio.to_thread(self.drain))
        return outcomes

    def drain(self) -> list[MergeOutcome]:
        outcomes: list[MergeOutcome] = []
        for row in list(self._op.merge_queue() or []):
            # merge_start is the claim: a second merger (or a restarted one) seeing the same row
            # loses the race here rather than merging twice.
            if not self._op.merge_start(int(row["id"])):
                continue
            outcome = self.merge_one(row)
            # merge_finish records the row and logs merge.<status> itself (the server owns the
            # events for its own table), so the merger reports once and does not log again.
            self._op.merge_finish(outcome.merge_id, outcome.status, outcome.detail,
                                  version_before=outcome.version_before, version_after=outcome.version_after,
                                  commit_sha=outcome.commit_sha)
            log.info("merge %s: %s (%s)", outcome.merge_id, outcome.status, outcome.detail.splitlines()[0] if outcome.detail else "")
            outcomes.append(outcome)
        return outcomes

    # --- one merge ---

    def merge_one(self, row: dict[str, Any]) -> MergeOutcome:
        merge_id = int(row["id"])
        diff_path = self._root / str(row["diff_ref"])
        if not diff_path.is_file():
            return MergeOutcome(merge_id, STATUS_FAILED, f"diff file not found: {row['diff_ref']}")
        diff_text = diff_path.read_text(encoding="utf-8")
        paths = paths_in_diff(diff_text)
        if not paths:
            return MergeOutcome(merge_id, STATUS_FAILED, "diff names no files")
        # Defence in depth: the server checked these when it queued the row, and the rules may
        # have changed since, so the merger checks again against what it has been given.
        bad = outside_harness(paths)
        if bad:
            return MergeOutcome(merge_id, STATUS_FAILED, "a proposal may only change files under harness/: " + ", ".join(bad))
        hits = protected(paths, self._protected)
        if hits:
            return MergeOutcome(merge_id, STATUS_FAILED, "protected path: " + ", ".join(hits))
        dirty = self._git("status", "--porcelain", "--", "harness/").stdout.strip()
        if dirty:
            return MergeOutcome(merge_id, STATUS_FAILED, DIRTY_TREE)

        branch = f"civ/merge-{merge_id}"
        worktree = Path(tempfile.mkdtemp(prefix=f"civ-merge-{merge_id}-"))
        try:
            # A stale branch from an interrupted attempt would make the add fail; clear it first.
            self._git("branch", "-D", branch, check=False)
            added = self._git("worktree", "add", "-b", branch, str(worktree), "HEAD", check=False)
            if added.returncode != 0:
                return MergeOutcome(merge_id, STATUS_FAILED, "could not create worktree: " + added.stderr.strip())
            check = self._git("apply", "--check", str(diff_path), cwd=worktree, check=False)
            if check.returncode != 0:
                return MergeOutcome(merge_id, STATUS_FAILED, "diff does not apply: " + check.stderr.strip())
            applied = self._git("apply", str(diff_path), cwd=worktree, check=False)
            if applied.returncode != 0:
                return MergeOutcome(merge_id, STATUS_FAILED, "diff does not apply: " + applied.stderr.strip())

            tests = self._run_tests(worktree)
            if tests is not None:
                return MergeOutcome(merge_id, STATUS_FAILED, tests)

            version_path = worktree / self._version_file
            try:
                before = version_path.read_text(encoding="utf-8").strip()
            except OSError:
                return MergeOutcome(merge_id, STATUS_FAILED, f"version file unreadable: {self._version_file}")
            after = bump_patch(before)
            if after is None:
                return MergeOutcome(merge_id, STATUS_FAILED, f"version file unreadable: {before!r} is not major.minor.patch")
            version_path.write_text(after + "\n", encoding="utf-8")

            author = row.get("requested_by") or "a supervisor"
            message = f"civ merge: proposal #{row.get('proposal_id')} by {author} ({', '.join(paths)})"
            self._git("add", "-A", "--", "harness", cwd=worktree)
            committed = self._git("commit", "-q", "-m", message, cwd=worktree, check=False)
            if committed.returncode != 0:
                return MergeOutcome(merge_id, STATUS_FAILED, "commit failed: " + committed.stderr.strip())
            # The fast-forward is the only write to the real repository. If HEAD moved while the
            # tests ran, it is refused rather than forced, and the row says to try again.
            ff = self._git("merge", "--ff-only", branch, check=False)
            if ff.returncode != 0:
                return MergeOutcome(merge_id, STATUS_FAILED, REPO_MOVED)
            sha = self._git("rev-parse", "HEAD").stdout.strip()
            return MergeOutcome(merge_id, STATUS_MERGED, f"merged as {sha[:12]}: {message}", before, after, sha, paths)
        finally:
            self._git("worktree", "remove", "--force", str(worktree), check=False)
            shutil.rmtree(worktree, ignore_errors=True)
            self._git("branch", "-D", branch, check=False)

    def _run_tests(self, worktree: Path) -> str | None:
        """None on green; the failure detail otherwise."""
        env = {**os.environ, "CIV_TEST_DATABASE_URL": self._test_db_url}
        try:
            proc = self._run(self._test_command, cwd=str(worktree), env=env, capture_output=True, text=True,
                             timeout=self._test_timeout_s)
        except subprocess.TimeoutExpired:
            return f"tests timed out after {self._test_timeout_s:g} s"
        if proc.returncode == 0:
            return None
        output = (proc.stdout or "") + (proc.stderr or "")
        tail = "\n".join(output.strip().splitlines()[-OUTPUT_TAIL_LINES:])
        return f"tests failed (exit {proc.returncode}):\n{tail}"

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        proc = self._run(["git", *args], cwd=str(cwd or self._root), capture_output=True, text=True)
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc


# --- command line: drain once ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="drain the civ merge queue")
    p.add_argument("--once", action="store_true", help="drain the queue once and exit (the only mode for now)")
    p.add_argument("--db-url", default=os.environ.get("CIV_DATABASE_URL", "postgresql://127.0.0.1:5432/civ"))
    p.add_argument("--root", type=Path, default=Path(os.environ.get("CIV_ROOT", Path(__file__).resolve().parents[2])))
    args = p.parse_args(argv)
    logging.basicConfig(level=os.environ.get("CIV_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from harness.board_mcp.db import Db
    from harness.board_mcp.operator import Operator

    operator = Operator(Db(args.db_url))
    settings = merge_settings(operator.control_get("shape"))
    merger = Merger(operator, args.root, settings["test_command"], settings["protected_paths"])
    outcomes = merger.drain()
    for o in outcomes:
        first = o.detail.splitlines()[0] if o.detail else ""
        extra = f" {o.version_before} -> {o.version_after} {o.commit_sha[:12] if o.commit_sha else ''}" if o.merged else ""
        print(f"merge #{o.merge_id}: {o.status}{extra} {first}")
    if not outcomes:
        print("merge queue empty")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
