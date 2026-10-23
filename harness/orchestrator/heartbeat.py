"""The orchestrator's heartbeat: one row the outside world can watch.

The dashboard reads the same database as everything else and goes quiet in exactly the
failures it should show, so the orchestrator writes a heartbeat row on a fixed interval and a
watchdog that is not part of the stack reads its age. The row carries enough to tell what was
running (pid, epoch, live counts, harness version), and the last beat says "stopping" so a
clean exit is not mistaken for a death.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

HEARTBEAT_KEY = "heartbeat"
DEFAULT_INTERVAL_S = 30.0

SleepFn = Callable[[float], Awaitable[None]]


class Heartbeat:
    """Writes control_state.heartbeat every interval_s until told to stop."""

    def __init__(
        self,
        operator: Any,
        interval_s: float = DEFAULT_INTERVAL_S,
        pid: int | None = None,
        sleep: SleepFn = asyncio.sleep,
        harness_version: Callable[[], str] | str = "dev",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._op = operator
        self._interval_s = float(interval_s)
        self._pid = int(pid if pid is not None else os.getpid())
        self._sleep = sleep
        self._version = harness_version
        self._clock = clock
        self.beats = 0

    def beat(self, stopping: bool = False) -> dict[str, Any]:
        """Write one heartbeat row and return it."""
        version = self._version() if callable(self._version) else self._version
        row: dict[str, Any] = {
            "at": self._clock().isoformat(timespec="seconds"),
            "pid": self._pid,
            "epoch": int(self._op.epoch()),
            "live": dict(self._op.live_agents()),
            "harness_version": version,
        }
        if stopping:
            row["stopping"] = True
        self._op.control_set(HEARTBEAT_KEY, row)
        self.beats += 1
        return row

    async def run_until(self, done: asyncio.Event) -> int:
        """Beat now, then every interval until done is set; the last beat says stopping.

        The interval wait is a plain sleep rather than a wait on the event, so the watchdog's
        notion of "two intervals stale" matches what this loop promises; the stopping beat at
        exit is what distinguishes a shutdown from a death.
        """
        self.beat()
        try:
            while not done.is_set():
                await self._sleep(self._interval_s)
                if done.is_set():
                    break
                self.beat()
        finally:
            self.beat(stopping=True)
        return self.beats
