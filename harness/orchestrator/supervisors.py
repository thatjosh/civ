"""Supervisor rounds: short sessions the orchestrator spawns on a timer.

A supervisor is not a persistent agent. Every round the orchestrator opens a round row, asks
the operator for the brief (the window's SLOs, succession incidents, blockers, alerts, the
previous reports), renders it into the prompt, and spawns the supervisor sessions together.
When they are done it closes the round with a summary of what they did. Fresh eyes every
round, and a bounded cost: a session reads one brief, not the whole board.

The cron waits an interval, runs a round, and repeats until the solver population is done,
then runs one final round so the last window is read too. The orchestrator stays the only
process that can spawn; a supervisor only ever writes to the board.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from harness.orchestrator.brief import render_brief
from harness.orchestrator.spawner import SpawnResult, Spawner

log = logging.getLogger(__name__)

PING_PREFIX = "[ping:"


@dataclass
class RoundResult:
    """What one round did, for the run summary and the exit code."""

    number: int
    final: bool
    round_id: int | None = None
    reports: int = 0
    pings: int = 0
    tuned: str | None = None
    tuned_from: float | None = None
    tuned_to: float | None = None
    reopened: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    cost_estimated: bool = False
    turns: int = 0
    sessions: list[SpawnResult] = field(default_factory=list)
    error: str | None = None

    @property
    def failed(self) -> bool:
        return bool(self.error) or any(s.error for s in self.sessions)

    def summary(self) -> dict[str, Any]:
        """The close_round payload: what the next brief and the dashboard read back."""
        return {
            "reports": self.reports, "pings": self.pings, "tuned": self.tuned,
            "reopened": list(self.reopened), "cost_usd": self.cost_usd, "turns": self.turns,
            "sessions": len(self.sessions), "errors": [s.error for s in self.sessions if s.error],
        }


def supervisor_ids(slot: int, number: int) -> tuple[str, str, str]:
    """(agent id, lineage id, directory name) for a supervisor slot in a round.

    The lineage is the slot (S-001) and the generation is the round number, so a
    supervisor's identity block reads like any other agent's and the directory
    agents/sup-001/ carries across rounds without ever carrying context.
    """
    return f"sup-{slot:03d}-r{number}", f"S-{slot:03d}", f"sup-{slot:03d}"


class SupervisorRound:
    """Runs one round: open, brief, spawn the supervisors together, close."""

    def __init__(self, spawner: Spawner, operator: Any, count: int = 1, shape_reader: Any | None = None) -> None:
        # The spawner carries the supervisors' model, turn budget and prompt
        # assembler, because those are session facts and it already owns them.
        self._spawner = spawner
        self._op = operator
        self._count = max(1, int(count))
        # With a shape reader attached the count follows control/civ.toml (M5), so a
        # `civ reshape` changes the next round's size without a restart.
        self._shape = shape_reader

    @property
    def count(self) -> int:
        if self._shape is not None:
            return max(1, int(self._shape.current().supervisors))
        return self._count

    async def run(self, number: int, final: bool = False) -> RoundResult:
        outcome = RoundResult(number=number, final=final)
        try:
            outcome.round_id = int(self._op.open_round(number, final))
            brief = dict(self._op.round_brief(outcome.round_id) or {})
            # The operator may not know how many sessions the orchestrator spawns per round.
            count = self.count
            brief.setdefault("round", {}).setdefault("supervisors", count)
            markdown = render_brief(brief)
            self._op.log_event(None, "round.brief_rendered",
                               {"round_id": outcome.round_id, "number": number, "chars": len(markdown)})
            outcome.sessions = list(await asyncio.gather(*(self._spawn(slot, number, markdown) for slot in range(1, count + 1))))
            self._collect(outcome)
        except Exception as exc:  # noqa: BLE001 - a broken round must not take the cron down with it
            log.exception("supervisor round %d failed", number)
            outcome.error = f"{type(exc).__name__}: {exc}"
        finally:
            if outcome.round_id is not None:
                try:
                    self._op.close_round(outcome.round_id, outcome.summary())
                except Exception as exc:  # noqa: BLE001 - the round still happened; report it rather than crash
                    log.warning("could not close round %d: %s", number, exc)
                    outcome.error = outcome.error or f"close_round: {exc}"
        return outcome

    async def _spawn(self, slot: int, number: int, brief_markdown: str) -> SpawnResult:
        agent_id, lineage_id, dirname = supervisor_ids(slot, number)
        result = await self._spawner.spawn(
            agent_id=agent_id, lineage_id=lineage_id, generation=number, role="supervisor",
            brief=brief_markdown, cwd=self._spawner.agent_dir(dirname),
        )
        result.generation = number
        return result

    def _collect(self, outcome: RoundResult) -> None:
        """Read back what the sessions did from the board, because the orchestrator never
        parses what an agent says; the tune, the reports and the reopens are rows."""
        reports = list(self._op.reports_for_round(outcome.round_id) or [])
        outcome.reports = len(reports)
        outcome.pings = sum(1 for r in reports if r.get("trigger") or str(r.get("body", "")).startswith(PING_PREFIX))
        current = self._op.current_round() or {}
        outcome.tuned = current.get("tuned_knob")
        outcome.tuned_from = current.get("tuned_from")
        outcome.tuned_to = current.get("tuned_to")
        outcome.reopened = list(self._op.reopened_in_round(outcome.round_id) or [])
        costs = [s.cost_usd for s in outcome.sessions if s.cost_usd is not None]
        outcome.cost_usd = sum(costs) if costs else None
        outcome.cost_estimated = any(s.cost_estimated for s in outcome.sessions)
        outcome.turns = sum(s.turns for s in outcome.sessions)


SleepFn = Callable[[float], Awaitable[None]]


class SupervisorCron:
    """Rounds on an interval until the population is done, then one final round."""

    def __init__(self, round: SupervisorRound, interval_s: float, sleep: SleepFn = asyncio.sleep,
                 shape_reader: Any | None = None) -> None:
        self._round = round
        self._interval_s = float(interval_s)
        self._sleep = sleep
        self._shape = shape_reader

    @property
    def interval_s(self) -> float:
        # Read at the start of every wait, so a reshaped cadence applies from the next round.
        if self._shape is not None:
            return float(self._shape.current().supervisor_round_s)
        return self._interval_s

    async def run_until(self, population_done: asyncio.Event) -> list[RoundResult]:
        results: list[RoundResult] = []
        number = 0
        while True:
            await self._wait_interval_or(population_done)
            if population_done.is_set():
                break
            number += 1
            results.append(await self._round.run(number))
        # The last window still holds the end of the run (the final handoffs, the
        # last completions), and a post-mortem is the cheapest report there is.
        results.append(await self._round.run(number + 1, final=True))
        return results

    async def _wait_interval_or(self, event: asyncio.Event) -> None:
        """Sleep one interval, or less if the population finishes first."""
        if event.is_set():
            return
        timer = asyncio.create_task(self._sleep(self.interval_s))
        waiter = asyncio.create_task(event.wait())
        try:
            await asyncio.wait({timer, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (timer, waiter):
                if not task.done():
                    task.cancel()


def format_rounds(rounds: list[RoundResult]) -> str:
    """One line per round for the run summary."""
    lines = []
    for r in rounds:
        tuned = "tuned none"
        if r.tuned:
            tuned = f"tuned {r.tuned}"
            if r.tuned_from is not None and r.tuned_to is not None:
                tuned += f" ({r.tuned_from:g} -> {r.tuned_to:g})"
        cost = "n/a" if r.cost_usd is None else (f"~{r.cost_usd:.4f}" if r.cost_estimated else f"{r.cost_usd:.4f}")
        suffix = " (final)" if r.final else ""
        error = f", error: {r.error}" if r.error else ""
        lines.append(f"round {r.number}{suffix}: reports {r.reports}, pings {r.pings}, {tuned}, "
                     f"reopened {r.reopened}, turns {r.turns}, cost ${cost}{error}")
    return "\n".join(lines)
