"""The version watcher and the owned board server, with no real process."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness.orchestrator.reload import BoardServer, VersionWatcher


def test_watcher_reports_a_change_once_and_only_when_it_changes(tmp_path: Path):
    f = tmp_path / "VERSION"
    f.write_text("0.1.0\n")
    w = VersionWatcher(f)
    assert w.check() is None
    f.write_text("0.1.1\n")
    assert w.check() == ("0.1.0", "0.1.1")
    assert w.check() is None


def test_watcher_reads_dev_without_a_file(tmp_path: Path):
    w = VersionWatcher(tmp_path / "missing")
    assert w.last == "dev"


async def test_run_until_calls_on_bump_with_old_and_new(tmp_path: Path):
    f = tmp_path / "VERSION"
    f.write_text("0.1.0\n")
    w = VersionWatcher(f, poll_s=0.01)
    seen: list[tuple[str, str]] = []
    done = asyncio.Event()

    async def on_bump(old: str, new: str) -> None:
        seen.append((old, new))
        done.set()

    async def bump_later():
        await asyncio.sleep(0.03)
        f.write_text("0.1.1\n")

    asyncio.create_task(bump_later())
    bumps = await asyncio.wait_for(w.run_until(done, on_bump), timeout=2)
    assert seen == [("0.1.0", "0.1.1")] and bumps == 1


class FakeProc:
    def __init__(self):
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self._returncode = None

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self.killed = True
        self._returncode = -9


class Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_board_server_starts_waits_for_health_and_restarts(tmp_path: Path):
    procs: list[FakeProc] = []
    calls: list[list[str]] = []
    health = {"ready_after": 2, "n": 0}

    def popen(cmd, env, stdout, stderr):
        calls.append(list(cmd))
        procs.append(FakeProc())
        assert env["CIV_BOARD_PORT"] == "8799"
        return procs[-1]

    def http_get(url):
        health["n"] += 1
        if health["n"] >= health["ready_after"]:
            return Resp()
        raise ConnectionError("not yet")

    server = BoardServer(["python", "-m", "harness.board_mcp.server"], {"CIV_BOARD_PORT": "8799"},
                         "http://127.0.0.1:8799/health", popen=popen, http_get=http_get, sleep=lambda s: None)
    server.start()
    assert server.running and len(procs) == 1
    health["ready_after"] = health["n"] + 1
    server.restart()
    assert procs[0].terminated and len(procs) == 2 and server.running
    server.stop()
    assert procs[1].terminated and not server.running


def test_board_server_that_never_answers_is_stopped_and_raises(tmp_path: Path):
    procs: list[FakeProc] = []

    def popen(cmd, env, stdout, stderr):
        procs.append(FakeProc())
        return procs[-1]

    server = BoardServer(["x"], {}, "http://127.0.0.1:1/health", popen=popen,
                         http_get=lambda url: (_ for _ in ()).throw(ConnectionError()), wait_s=0.01, sleep=lambda s: None)
    with pytest.raises(RuntimeError):
        server.start()
    assert procs and procs[0].terminated and not server.running
