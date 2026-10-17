"""Hot reload on a harness version bump.

A merge bumps harness/VERSION. New spawns already pick the new prompts and version up on their
own (the spawner reads both from disk at every prepare), so what is left is the board server,
which has the old code loaded. When the orchestrator owns that process (--serve-board) it is
restarted here; when something else started it, the bump is only logged and the server keeps
the old code until whoever started it restarts it. Agents already running keep the version they
were born with until their succession, which is what lets two populations be compared.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import subprocess
import time
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_VERSION = "dev"
OnBump = Callable[[str, str], Awaitable[None] | None]


class VersionWatcher:
    """Polls harness/VERSION and calls on_bump(old, new) when it changes."""

    def __init__(self, version_file: Path | str, poll_s: float = 2.0) -> None:
        self._path = Path(version_file)
        self._poll_s = poll_s
        self.last = self.read()

    def read(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8").strip() or DEFAULT_VERSION
        except OSError:
            return DEFAULT_VERSION

    def check(self) -> tuple[str, str] | None:
        """One poll: (old, new) when the version changed since the last look, else None."""
        current = self.read()
        if current == self.last:
            return None
        previous, self.last = self.last, current
        return previous, current

    async def run_until(self, done: asyncio.Event, on_bump: OnBump) -> int:
        """Watch until `done` is set; returns the number of bumps seen."""
        bumps = 0
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=self._poll_s)
            except asyncio.TimeoutError:
                pass
            change = self.check()
            if change is None:
                continue
            bumps += 1
            result = on_bump(*change)
            if inspect.isawaitable(result):
                await result
        return bumps


class BoardServer:
    """The board server as a child process the orchestrator can restart."""

    def __init__(
        self,
        cmd: list[str],
        env: dict[str, str],
        health_url: str,
        popen: Callable[..., Any] = subprocess.Popen,
        http_get: Callable[[str], Any] = lambda url: urllib.request.urlopen(url, timeout=1),
        wait_s: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        log_path: Path | str | None = None,
    ) -> None:
        self._cmd = list(cmd)
        self._env = dict(env)
        self._health_url = health_url
        self._popen = popen
        self._http_get = http_get
        self._wait_s = wait_s
        self._sleep = sleep
        self._log_path = Path(log_path) if log_path else None
        self._proc: Any = None
        self._log_handle: Any = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def cmd(self) -> list[str]:
        return list(self._cmd)

    @property
    def env(self) -> dict[str, str]:
        return dict(self._env)

    def start(self) -> None:
        if self.running:
            return
        stdout: Any = subprocess.DEVNULL
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self._log_path, "a", encoding="utf-8")  # noqa: SIM115 - held for the process's life
            stdout = self._log_handle
        self._proc = self._popen(self._cmd, env={**os.environ, **self._env}, stdout=stdout, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + self._wait_s
        while time.monotonic() < deadline:
            if self._healthy():
                log.info("board server up at %s (pid %s)", self._health_url, getattr(self._proc, "pid", "?"))
                return
            if self._proc.poll() is not None:
                break
            self._sleep(0.25)
        self.stop()
        raise RuntimeError(f"board server did not answer at {self._health_url} within {self._wait_s:g} s")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    def restart(self) -> None:
        # Stop first and then start, rather than start-then-stop, because both bind one port.
        self.stop()
        self.start()

    def _healthy(self) -> bool:
        try:
            with self._http_get(self._health_url) as resp:
                return getattr(resp, "status", 200) == 200
        except Exception:  # noqa: BLE001 - not up yet, or gone
            return False
