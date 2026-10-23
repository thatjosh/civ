"""Chaos controls, read by the orchestrator from control_state.

The operator pulls these from the command line (civ chaos ...); the orchestrator only reads.
The shape is {"kill": [agent ids], "board_down_until": iso or null, "lag_ms": int}. The runner
asks should_kill() for the agent it is driving, and a hit becomes the ordinary kill path with
reason "chaos kill", so the successor starts from a stub exactly as after any other kill. The
board outage and lag are the server's to honour; they are here only so the orchestrator can
show them.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

CHAOS_KEY = "chaos"
DEFAULT_CHAOS: dict[str, Any] = {"kill": [], "board_down_until": None, "lag_ms": 0}


def normalise(value: Any) -> dict[str, Any]:
    """The chaos row with every key present and the kill list a list of strings."""
    data = dict(DEFAULT_CHAOS)
    if isinstance(value, dict):
        data.update({k: v for k, v in value.items() if k in DEFAULT_CHAOS})
    data["kill"] = [str(x) for x in (data.get("kill") or [])]
    data["lag_ms"] = int(data.get("lag_ms") or 0)
    return data


class ChaosReader:
    """A cached view of control_state.chaos, re-read at most every poll_s."""

    def __init__(self, operator: Any, poll_s: float = 2.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._op = operator
        self._poll_s = float(poll_s)
        self._clock = clock
        self._cache: dict[str, Any] = dict(DEFAULT_CHAOS)
        self._read_at: float | None = None

    def refresh(self) -> dict[str, Any]:
        self._cache = normalise(self._op.control_get(CHAOS_KEY))
        self._read_at = self._clock()
        return dict(self._cache)

    def current(self) -> dict[str, Any]:
        if self._read_at is None or self._clock() - self._read_at >= self._poll_s:
            self.refresh()
        return dict(self._cache)

    def should_kill(self, agent_id: str) -> bool:
        return agent_id in self.current()["kill"]

    def acknowledge(self, agent_id: str) -> None:
        """Remove the agent from the kill list, in the database and in the cache, so the kill
        happens once and a successor with a new id is not hit by a stale entry."""
        self._op.chaos_ack_kill(agent_id)
        self._cache["kill"] = [x for x in self._cache.get("kill", []) if x != agent_id]
