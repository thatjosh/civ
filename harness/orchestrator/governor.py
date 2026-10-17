"""The governor: the gate every spawn passes through, against the operator's ceilings.

control/ceilings.toml is the one file whose numbers nothing inside the system can exceed.
The spawner asks the governor before every session; the governor reads the file (again, if it
changed), reads the live state through the operator, and either returns at once, waits until
the condition clears, or raises when the day's spend is gone. Waiting rather than refusing is
deliberate for the first three ceilings: a paused population or a token spike should slow the
system, not end lineages. Spend is the exception, because a spend ceiling that merely delays
the next spawn would be reached again a minute later.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import tomllib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CEILING_KEYS = ("max_live_agents", "max_tokens_per_minute", "max_spend_per_day_usd", "max_handoff_tokens", "model")


class SpendCeilingReached(RuntimeError):
    """The day's spend ceiling is reached; no further session may start."""


@dataclass(frozen=True)
class Ceilings:
    max_live_agents: int
    max_tokens_per_minute: int
    max_spend_per_day_usd: float
    max_handoff_tokens: int
    model: str

    @classmethod
    def from_mapping(cls, data: dict[str, Any], source: str) -> "Ceilings":
        missing = [k for k in CEILING_KEYS if k not in data]
        if missing:
            raise ValueError(f"{source} is missing {', '.join(missing)}")
        return cls(
            max_live_agents=int(data["max_live_agents"]),
            max_tokens_per_minute=int(data["max_tokens_per_minute"]),
            max_spend_per_day_usd=float(data["max_spend_per_day_usd"]),
            max_handoff_tokens=int(data["max_handoff_tokens"]),
            model=str(data["model"]),
        )


class CeilingsFile:
    """control/ceilings.toml, re-parsed only when its mtime changes.

    The operator edits this file by hand while the system runs, and the edit must bite at the
    next spawn; a stat per spawn is cheap, a parse per spawn is not, hence the mtime cache.
    A missing file is an error, not a default: running without ceilings is the one thing the
    design says never to do.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._mtime: float | None = None
        self._ceilings: Ceilings | None = None

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> Ceilings:
        try:
            mtime = os.stat(self._path).st_mtime_ns
        except FileNotFoundError:
            raise FileNotFoundError(f"ceilings file missing: {self._path}") from None
        if self._ceilings is None or mtime != self._mtime:
            with self._path.open("rb") as handle:
                self._ceilings = Ceilings.from_mapping(tomllib.load(handle), str(self._path))
            if self._mtime is not None:
                log.info("ceilings re-read from %s", self._path)
            self._mtime = mtime
        return self._ceilings


def is_paused(value: Any) -> bool:
    """control_state.paused is `true`/`false` or {"on": true, "reason": ...}; both are accepted."""
    if isinstance(value, dict):
        return bool(value.get("on"))
    return bool(value)


class Governor:
    """Gates spawns against the pause flag and the ceilings, in the contract's order."""

    def __init__(
        self,
        operator: Any,
        ceilings_path: Path | str,
        poll_s: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._op = operator
        self._file = CeilingsFile(ceilings_path)
        self._poll_s = float(poll_s)
        self._clock = clock
        self._sleep = sleep
        # Read once at construction so a missing or malformed file fails at start, not at the first spawn.
        self._file.read()

    @property
    def ceilings(self) -> Ceilings:
        return self._file.read()

    async def acquire(self, role: str, replaces_slot: bool) -> None:
        """Return when a session of `role` may start. A successor passes replaces_slot=True: its
        predecessor is still counted live until it registers, so it must not be held behind
        max_live_agents by the slot it is about to take over."""
        waiting_on: str | None = None
        started = self._clock()
        while True:
            ceilings = self._file.read()
            reason, value, limit = self._blocker(ceilings, replaces_slot)
            if reason == "spend":
                self._op.log_event(None, "governor.halt", {"reason": reason, "value": value, "limit": limit, "role": role})
                raise SpendCeilingReached(f"spend in the last 24 hours is ${value:.2f}, the ceiling is ${limit:.2f}")
            if reason is None:
                if waiting_on is not None:
                    self._op.log_event(None, "governor.resumed",
                                       {"reason": waiting_on, "role": role, "waited_s": round(self._clock() - started, 1)})
                return
            if reason != waiting_on:
                # One event per wait, not per poll: the dashboard wants to know that spawns
                # were held and why, not to be flooded while they are.
                self._op.log_event(None, "governor.wait", {"reason": reason, "value": value, "limit": limit, "role": role})
                log.info("governor holding a %s spawn: %s is %s against %s", role, reason, value, limit)
                waiting_on = reason
            await self._sleep(self._poll_s)

    def _blocker(self, ceilings: Ceilings, replaces_slot: bool) -> tuple[str | None, Any, Any]:
        """(reason, value, limit) for the first condition that blocks, else (None, None, None)."""
        if is_paused(self._op.control_get("paused")):
            return "paused", True, False
        live = self._op.live_agents() or {}
        live_total = int(sum(live.values()))
        if not replaces_slot and live_total >= ceilings.max_live_agents:
            return "live_agents", live_total, ceilings.max_live_agents
        tpm = int(self._op.tokens_per_minute() or 0)
        if tpm > ceilings.max_tokens_per_minute:
            return "tokens_per_minute", tpm, ceilings.max_tokens_per_minute
        spend = float(self._op.spend_usd(hours=24) or 0.0)
        if spend >= ceilings.max_spend_per_day_usd:
            return "spend", spend, ceilings.max_spend_per_day_usd
        return None, None, None
