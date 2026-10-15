"""Watch one agent's SDK message stream and keep its usage in the database.

The orchestrator never reasons about what an agent says. It only reads the
usage numbers the SDK reports after every assistant turn, because those are
what succession and the dashboard need: how full the context is and how many
tokens the agent has spent. Succession (M3) hangs off two thresholds, the
handoff percentage and the kill percentage, which come from the config knobs
so supervisors can tune them later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from claude_agent_sdk import AssistantMessage, ResultMessage

ThresholdCallback = Callable[[str, float], None]

# Seeds for the two succession knobs; the real values come from the config table.
DEFAULT_HANDOFF_PCT = 80.0
DEFAULT_KILL_PCT = 90.0
# The usage probe is asked every this many turns, and once more when the
# arithmetic first crosses this percentage, because the arithmetic can drift
# from the CLI's own count (system prompt, tool schemas) and the handoff
# must not fire late.
PROBE_EVERY_TURNS = 5
PROBE_AT_PCT = 70.0


class UsageSink(Protocol):
    """The slice of the Operator API the monitor writes through."""

    def set_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int, context_pct: float) -> None: ...

    def set_agent_status(self, agent_id: str, status: str) -> None: ...

    def log_event(self, agent_id: str, type: str, payload: dict[str, Any]) -> None: ...


class KnobReader(Protocol):
    """Reads one config knob. The board's Knobs class satisfies it; tests use StaticKnobs."""

    def get(self, knob: str) -> float: ...


class StaticKnobs:
    """A KnobReader over a dict, for tests and for running without a database."""

    def __init__(self, values: dict[str, float] | None = None) -> None:
        self._values = {"handoff_pct": DEFAULT_HANDOFF_PCT, "kill_pct": DEFAULT_KILL_PCT, **(values or {})}

    def get(self, knob: str) -> float:
        return float(self._values[knob])


@dataclass
class UsageTotals:
    tokens_in: int = 0
    tokens_out: int = 0
    context_pct: float = 0.0
    turns: int = 0
    cost_usd: float | None = None
    ended: bool = False
    fired: set[float] = field(default_factory=set)
    # What the CLI itself last reported, kept apart from the arithmetic so a
    # disagreement between the two is visible in the events.
    probe_pct: float | None = None


class Monitor:
    """Tracks context fill and token spend for one agent, and fires the succession thresholds."""

    def __init__(
        self,
        operator: UsageSink,
        agent_id: str,
        context_window: int = 1_000_000,
        knobs: KnobReader | None = None,
        force_handoff_after_turns: int | None = None,
    ) -> None:
        self._op = operator
        self._agent_id = agent_id
        self._window = context_window
        self._knobs = knobs or StaticKnobs()
        self._force_after = force_handoff_after_turns
        self._thresholds: list[tuple[float, ThresholdCallback]] = []
        self._probed_at_pct = False
        # A turn is probed at most once, so a message that is not a turn (the
        # result, say) cannot trigger a second probe for the same count.
        self._last_probe_turn = -1
        self.totals = UsageTotals()

    # The two succession levels are read once per monitor, at spawn, because a
    # knob that moves mid-session would make "fired once per level" ambiguous.
    @property
    def handoff_pct(self) -> float:
        return self._knobs.get("handoff_pct")

    @property
    def kill_pct(self) -> float:
        return self._knobs.get("kill_pct")

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
        self._fire_forced_handoff()

    # --- the CLI's own view of the context, as a cross-check -------------

    def probe_due(self) -> bool:
        """Whether the runner should ask the CLI for its context usage now."""
        t = self.totals
        if t.turns == self._last_probe_turn:
            return False
        if t.turns and t.turns % PROBE_EVERY_TURNS == 0:
            return True
        if t.context_pct >= PROBE_AT_PCT and not self._probed_at_pct:
            return True
        return False

    def apply_probe(self, usage: dict[str, Any] | None) -> None:
        """Fold a get_context_usage() answer into the fill estimate, taking the larger."""
        t = self.totals
        self._last_probe_turn = t.turns
        if t.context_pct >= PROBE_AT_PCT:
            self._probed_at_pct = True
        if not usage:
            return
        total = int(usage.get("totalTokens", 0) or 0)
        # Measured against our window, not the CLI's, so a small --context-window
        # in a test run still makes the handoff fire.
        probe_pct = total / self._window * 100.0 if total else None
        t.probe_pct = probe_pct
        self._op.log_event(
            self._agent_id,
            "agent.context_probe",
            {
                "total_tokens": total,
                "cli_pct": usage.get("percentage"),
                "probe_pct": round(probe_pct, 3) if probe_pct is not None else None,
                "arith_pct": round(t.context_pct, 3),
            },
        )
        if probe_pct is not None and probe_pct > t.context_pct:
            t.context_pct = probe_pct
            self._op.set_agent_usage(self._agent_id, t.tokens_in, t.tokens_out, t.context_pct)
        self._fire_thresholds(t.context_pct)

    def note_compaction_state(self, usage: dict[str, Any] | None) -> bool:
        """Record whether the CLI still has auto-compaction on. Returns True when it is a problem.

        The spawner sets DISABLE_AUTO_COMPACT for every session; this is the
        check that it took, because a compaction that fires before the handoff
        would wipe exactly the context the handoff is meant to summarise.
        """
        if not usage:
            return False
        enabled = bool(usage.get("isAutoCompactEnabled", False))
        threshold = usage.get("autoCompactThreshold")
        max_tokens = usage.get("maxTokens") or 0
        threshold_pct = None
        if threshold:
            # The CLI reports the threshold in tokens; a value at or under 100 is already a percentage.
            threshold_pct = float(threshold) if float(threshold) <= 100 else float(threshold) / max_tokens * 100.0 if max_tokens else None
        problem = enabled and (threshold_pct is None or self.handoff_pct >= threshold_pct)
        self._op.log_event(
            self._agent_id,
            "agent.compaction_state",
            {"auto_compact_enabled": enabled, "threshold_pct": threshold_pct, "handoff_pct": self.handoff_pct, "problem": problem},
        )
        return problem

    # --- thresholds -------------------------------------------------------

    def _fire_thresholds(self, pct: float) -> None:
        # Each threshold fires once per agent lifetime, because the thing
        # that reacts to it (a handoff request) must not be sent twice.
        for level, callback in self._thresholds:
            if pct >= level and level not in self.totals.fired:
                self.totals.fired.add(level)
                callback(self._agent_id, pct)

    def _fire_forced_handoff(self) -> None:
        # A test-only trigger: fire the handoff level at turn N regardless of
        # fill, so succession can be exercised for cents instead of a full window.
        if self._force_after is None or self.totals.turns < self._force_after:
            return
        level = self.handoff_pct
        for lvl, callback in self._thresholds:
            if lvl == level and level not in self.totals.fired:
                self.totals.fired.add(level)
                self._op.log_event(self._agent_id, "agent.handoff_forced", {"turn": self.totals.turns})
                callback(self._agent_id, self.totals.context_pct)

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
