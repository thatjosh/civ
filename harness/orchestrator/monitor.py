"""Watch one agent's SDK message stream and keep its usage in the database.

The orchestrator never reasons about what an agent says. It only reads the
usage numbers the SDK reports after every assistant turn, because those are
what succession (M3) and the dashboard need: how full the context is and how
many tokens the agent has spent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from claude_agent_sdk import AssistantMessage, ResultMessage

ThresholdCallback = Callable[[str, float], None]


class UsageSink(Protocol):
    """The slice of the Operator API the monitor writes through."""

    def set_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int, context_pct: float) -> None: ...

    def set_agent_status(self, agent_id: str, status: str) -> None: ...

    def log_event(self, agent_id: str, type: str, payload: dict[str, Any]) -> None: ...


@dataclass
class UsageTotals:
    tokens_in: int = 0
    tokens_out: int = 0
    context_pct: float = 0.0
    turns: int = 0
    cost_usd: float | None = None
    ended: bool = False
    fired: set[float] = field(default_factory=set)


class Monitor:
    """Tracks context fill and token spend for one agent."""

    def __init__(self, operator: UsageSink, agent_id: str, context_window: int = 1_000_000) -> None:
        self._op = operator
        self._agent_id = agent_id
        self._window = context_window
        self._thresholds: list[tuple[float, ThresholdCallback]] = []
        self.totals = UsageTotals()

    def on_threshold(self, pct: float, callback: ThresholdCallback) -> None:
        """Register a callback for when context fill first crosses pct."""
        self._thresholds.append((pct, callback))

    def observe(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            self._observe_turn(message)
        elif isinstance(message, ResultMessage):
            self._observe_result(message)

    def _observe_turn(self, message: AssistantMessage) -> None:
        usage = message.usage or {}
        if not usage:
            return
        # The input side of one assistant message is the whole prompt the
        # model just read, cache hits included, so it is the current context
        # size. Output is what it added.
        context_size = (
            int(usage.get("input_tokens", 0))
            + int(usage.get("cache_creation_input_tokens", 0))
            + int(usage.get("cache_read_input_tokens", 0))
        )
        out = int(usage.get("output_tokens", 0))

        t = self.totals
        t.turns += 1
        t.tokens_in += context_size
        t.tokens_out += out
        t.context_pct = context_size / self._window * 100.0

        # One write per turn is enough for the dashboard and keeps the
        # orchestrator from hammering Postgres on long runs.
        self._op.set_agent_usage(self._agent_id, t.tokens_in, t.tokens_out, t.context_pct)
        self._op.log_event(
            self._agent_id,
            "agent.turn",
            {
                "turn": t.turns,
                # tokens_in/tokens_out are the per-turn deltas civ_status sums for tokens_per_min.
                "tokens_in": context_size,
                "tokens_out": out,
                "context_tokens": context_size,
                "output_tokens": out,
                "context_pct": round(t.context_pct, 3),
            },
        )
        self._fire_thresholds(t.context_pct)

    def _fire_thresholds(self, pct: float) -> None:
        # Each threshold fires once per agent lifetime, because the thing
        # that reacts to it (a handoff request) must not be sent twice.
        for level, callback in self._thresholds:
            if pct >= level and level not in self.totals.fired:
                self.totals.fired.add(level)
                callback(self._agent_id, pct)

    def _observe_result(self, message: ResultMessage) -> None:
        t = self.totals
        t.cost_usd = message.total_cost_usd
        t.ended = True
        self._op.log_event(
            self._agent_id,
            "agent.ended",
            {
                "subtype": message.subtype,
                "num_turns": message.num_turns,
                "cost_usd": message.total_cost_usd,
                "is_error": message.is_error,
            },
        )
        self._op.set_agent_status(self._agent_id, "ended")
