"""Succession: keep one lineage alive across context windows.

A lineage is one slot in the population. Its first agent runs until its
context nears the handoff threshold; the runner then asks it to hand off,
waits for the handoff row, cuts the session, and starts generation N+1 in the
same directory with the handoff in its prompt. An agent that reaches the kill
threshold without handing off is cut anyway, once it has had kill_grace_turns
assistant turns since the request to act on it, and its successor gets a stub
built from its board posts. The lineage ends when a session ends on its own
(no tasks left, or an error) or when the generation cap is reached.

The server side is the other half of this: once the orchestrator has asked
for a handoff, the board refuses every write except civ_handoff, so the
request reaches the agent even if the injected message is late.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage

from harness.orchestrator.spawner import SessionControl, SpawnResult, Spawner

log = logging.getLogger(__name__)

PHASE_RUNNING = "running"
PHASE_HANDING_OFF = "handing_off"
PHASE_HANDED_OFF = "handed_off"
PHASE_KILLED = "killed"

# A handoff that takes longer than this is treated as a kill: the agent is
# either stuck or ignoring the request, and either way its successor needs to
# start from a stub rather than wait.
DEFAULT_HANDOFF_WAIT_S = 600.0


@dataclass
class LineageResult:
    lineage_id: str
    agent_id: str
    sessions: list[SpawnResult] = field(default_factory=list)
    handoffs: int = 0
    stubs: int = 0
    ended_by: str = "natural"
    error: str | None = None

    @property
    def generations(self) -> int:
        return len(self.sessions)

    @property
    def turns(self) -> int:
        return sum(s.turns for s in self.sessions)

    @property
    def cost_usd(self) -> float | None:
        costs = [s.cost_usd for s in self.sessions if s.cost_usd is not None]
        return sum(costs) if costs else None

    @property
    def tokens_in(self) -> int:
        return sum(s.tokens_in for s in self.sessions)

    @property
    def tokens_out(self) -> int:
        return sum(s.tokens_out for s in self.sessions)

    @property
    def cost_estimated(self) -> bool:
        return any(s.cost_estimated for s in self.sessions)

    @property
    def context_pct(self) -> float:
        return self.sessions[-1].context_pct if self.sessions else 0.0

    @property
    def result(self) -> str | None:
        return self.sessions[-1].result if self.sessions else None


@dataclass
class _SessionState:
    """What the runner knows about the session it is currently driving."""

    agent_id: str
    generation: int
    cwd: Path
    phase: str = PHASE_RUNNING
    handoff_due: bool = False
    kill_due: bool = False
    compaction_checked: bool = False
    requested_at: float | None = None
    # The monitor's turn count when the request went out, so the grace rule can count from it.
    turns_at_request: int | None = None
    watcher: asyncio.Task[None] | None = None
    stop_reason: str | None = None
    # The session's monitor, kept here so the watcher task can read the turn count too.
    monitor: Any = None
    # How long the agent had between the request and its end, for the events and the supervisors.
    end_detail: dict[str, Any] = field(default_factory=dict)


class LineageRunner:
    """Drives one lineage: session, handoff or kill, successor, repeat."""

    def __init__(
        self,
        spawner: Spawner,
        operator: Any,
        handoff_request: str,
        max_generations: int = 5,
        poll_s: float = 2.0,
        handoff_wait_s: float = DEFAULT_HANDOFF_WAIT_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._spawner = spawner
        self._op = operator
        self._handoff_request = handoff_request
        self._max_generations = max_generations
        self._poll_s = poll_s
        self._wait_s = handoff_wait_s
        self._sleep = sleep
        self._clock = clock

    async def run(self, lineage_id: str, agent_id: str) -> LineageResult:
        """Run the lineage `lineage_id` starting from the agent `agent_id` (for example L-001 and sol-001).

        Successors are named after the agent, not the lineage: sol-001-g2, sol-001-g3.
        """
        first_agent_id = agent_id
        outcome = LineageResult(lineage_id=lineage_id, agent_id=first_agent_id)
        generation = 1
        cwd = self._spawner.agent_dir(first_agent_id)
        token: str | None = None
        handoff: str | None = None
        handoff_is_stub = False

        while True:
            state = _SessionState(agent_id=agent_id, generation=generation, cwd=cwd)
            session = await self._run_one(state, lineage_id, token, handoff, handoff_is_stub)
            outcome.sessions.append(session)

            if state.phase == PHASE_HANDING_OFF and self._latest_handoff_from(lineage_id, agent_id):
                # The agent handed off and then its session ended on its own
                # before the watcher noticed; the handoff is still good.
                state.phase = PHASE_HANDED_OFF
            if state.phase == PHASE_HANDING_OFF:
                # Asked to hand off, never did, and the session ended anyway.
                state.phase = PHASE_KILLED
                state.stop_reason = "session ended without a handoff"

            if session.error and state.phase not in (PHASE_HANDED_OFF, PHASE_KILLED):
                outcome.ended_by = "error"
                outcome.error = session.error
                self._op.log_event(agent_id, "lineage.ended", {"lineage_id": lineage_id, "by": "error", "generation": generation})
                return outcome
            if state.phase == PHASE_RUNNING:
                outcome.ended_by = "natural"
                self._op.log_event(agent_id, "lineage.ended", {"lineage_id": lineage_id, "by": "natural", "generation": generation})
                return outcome

            row = self._conclude(state, lineage_id, outcome)
            if row is None:
                outcome.ended_by = "error"
                outcome.error = f"no handoff row for {agent_id} after {state.phase}"
                return outcome
            if generation >= self._max_generations:
                outcome.ended_by = "max_generations"
                self._op.log_event(agent_id, "lineage.ended", {"lineage_id": lineage_id, "by": "max_generations", "generation": generation})
                return outcome

            next_id = f"{first_agent_id}-g{generation + 1}"
            token = self._op.succeed(agent_id, next_id, str(cwd))
            handoff = row["body"]
            handoff_is_stub = bool(row.get("stub", False))
            agent_id = next_id
            generation += 1

    # --- one session ----------------------------------------------------------

    async def _run_one(self, state: _SessionState, lineage_id: str, token: str | None, handoff: str | None, stub: bool) -> SpawnResult:
        monitor = self._spawner.monitor_for(state.agent_id)
        state.monitor = monitor
        # The thresholds only raise flags; the hook below does the work, because
        # the monitor fires synchronously and the work needs the session.
        monitor.on_threshold(monitor.handoff_pct, lambda _agent, _pct: setattr(state, "handoff_due", True))
        monitor.on_threshold(monitor.kill_pct, lambda _agent, _pct: setattr(state, "kill_due", True))

        async def hook(message: Any, control: SessionControl) -> None:
            await self._on_message(state, lineage_id, message, control, monitor)

        try:
            return await self._spawner.spawn(
                state.agent_id,
                lineage_id,
                generation=state.generation,
                handoff=handoff,
                handoff_is_stub=stub,
                token=token,
                cwd=state.cwd,
                on_message=hook,
                monitor=monitor,
            )
        finally:
            if state.watcher is not None:
                state.watcher.cancel()

    async def _on_message(self, state: _SessionState, lineage_id: str, message: Any, control: SessionControl, monitor: Any) -> None:
        if isinstance(message, AssistantMessage) and not state.compaction_checked:
            state.compaction_checked = True
            if monitor.note_compaction_state(await control.context_usage()):
                log.warning("auto-compaction is still on for %s; the handoff may be pre-empted", state.agent_id)

        if (state.handoff_due or state.kill_due) and state.phase == PHASE_RUNNING:
            # A context that jumps past both thresholds in one turn (one large file read
            # does it) is still asked first: a kill with no request behind it buys a stub
            # for nothing. The grace rule below is what bounds the wait.
            await self._request_handoff(state, lineage_id, control, monitor)

        if state.phase == PHASE_HANDING_OFF and self._latest_handoff_from(lineage_id, state.agent_id):
            # Checked before the kill, so a handoff that lands in the same turn the kill
            # threshold fires is honoured rather than thrown away for a stub.
            self._handed_off(state, control)
            return

        if state.kill_due and state.phase == PHASE_HANDING_OFF:
            # The kill rule is kill_pct and at least kill_grace_turns since the request. The
            # request reaches the agent through its next tool result and the injected message,
            # and acting on it takes a turn of its own, so fewer turns than that is no chance.
            since = monitor.totals.turns - (state.turns_at_request or 0)
            if since >= monitor.kill_grace_turns:
                self._kill(state, control, "kill threshold reached")
                return

        if isinstance(message, ResultMessage) and state.watcher is not None:
            state.watcher.cancel()

    async def _request_handoff(self, state: _SessionState, lineage_id: str, control: SessionControl, monitor: Any) -> None:
        state.phase = PHASE_HANDING_OFF
        state.requested_at = self._clock()
        state.turns_at_request = monitor.totals.turns
        # Two channels on purpose: the status flip makes the board refuse other writes,
        # the message tells the agent why. A nearly full context may miss one of them.
        self._op.request_handoff(state.agent_id)
        await control.send(self._handoff_request)
        self._op.log_event(
            state.agent_id,
            "agent.handoff_sent",
            {
                "generation": state.generation,
                "turn": state.turns_at_request,
                "context_pct": round(float(monitor.totals.context_pct), 3),
                # True when both thresholds fired in the same turn: the request is already late.
                "kill_due": state.kill_due,
                "grace_turns": monitor.kill_grace_turns,
            },
        )
        state.watcher = asyncio.create_task(self._watch_handoff(state, lineage_id, control))

    async def _watch_handoff(self, state: _SessionState, lineage_id: str, control: SessionControl) -> None:
        """Poll for the handoff row, because the agent may be inside a long tool call
        and produce no message for the hook to react to."""
        while state.phase == PHASE_HANDING_OFF:
            await self._sleep(self._poll_s)
            if state.phase != PHASE_HANDING_OFF:
                return
            if self._latest_handoff_from(lineage_id, state.agent_id):
                self._handed_off(state, control)
                await control.interrupt()
                return
            if state.requested_at is not None and self._clock() - state.requested_at > self._wait_s:
                self._kill(state, control, "handoff timed out")
                await control.interrupt()
                return

    def _since_request(self, state: _SessionState) -> dict[str, Any]:
        """How long the agent had since the handoff request, in turns and seconds. The supervisors'
        succession brief reads these off the events, so they are stamped on every end."""
        monitor = state.monitor
        turns = None
        if monitor is not None and state.turns_at_request is not None:
            turns = int(monitor.totals.turns) - int(state.turns_at_request)
        seconds = round(self._clock() - state.requested_at, 3) if state.requested_at is not None else None
        grace = int(monitor.kill_grace_turns) if monitor is not None else None
        return {"requested": state.requested_at is not None, "turns_since_request": turns,
                "seconds_since_request": seconds, "grace_turns": grace}

    def _handed_off(self, state: _SessionState, control: SessionControl) -> None:
        state.phase = PHASE_HANDED_OFF
        state.stop_reason = "handoff written"
        state.end_detail = self._since_request(state)
        control.request_stop(state.stop_reason)

    def _kill(self, state: _SessionState, control: SessionControl, reason: str) -> None:
        state.phase = PHASE_KILLED
        state.stop_reason = reason
        state.end_detail = self._since_request(state)
        control.request_stop(reason)

    # --- after the session -----------------------------------------------------

    def _conclude(self, state: _SessionState, lineage_id: str, outcome: LineageResult) -> dict[str, Any] | None:
        """Record how the session ended and return the handoff row the successor starts from."""
        if state.phase == PHASE_HANDED_OFF:
            outcome.handoffs += 1
            self._op.log_event(state.agent_id, "agent.handed_off", {"generation": state.generation, **state.end_detail})
            return self._latest_handoff_from(lineage_id, state.agent_id)
        # Killed: no usable handoff, so build a stub from what the board and the directory hold.
        self._op.set_agent_status(state.agent_id, "dead")
        self._op.log_event(state.agent_id, "agent.killed",
                           {"generation": state.generation, "reason": state.stop_reason, **state.end_detail})
        self._op.write_stub_handoff(state.agent_id, self._listing(state.cwd))
        outcome.stubs += 1
        return self._latest_handoff_from(lineage_id, state.agent_id)

    def _latest_handoff_from(self, lineage_id: str, agent_id: str) -> dict[str, Any] | None:
        row = self._op.latest_handoff(lineage_id)
        if row and row.get("from_agent") == agent_id:
            return row
        return None

    @staticmethod
    def _listing(cwd: Path) -> list[str]:
        names: list[str] = []
        for sub in ("work", "notes"):
            folder = cwd / sub
            if folder.is_dir():
                names.extend(f"{sub}/{p.name}" for p in sorted(folder.iterdir()) if p.is_file())
        return names
