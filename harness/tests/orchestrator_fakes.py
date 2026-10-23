"""Fakes shared by the orchestrator unit tests.

The orchestrator talks to the database only through the Operator API and to
the model only through a client, so a fake of each that records calls is all
the tests need. No Postgres, no SDK process.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock


class FakeOperator:
    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self.usage: list[tuple[str, int, int, float]] = []
        self.status: list[tuple[str, str]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.summary: dict[str, int] = {"open": 1, "leased": 0, "done": 2, "blocked": 0}
        # Succession state, keyed by lineage, mirroring the handoffs table.
        self.handoffs: dict[str, list[dict[str, Any]]] = {}
        self.handoff_requests: list[str] = []
        self.succeeded: list[tuple[str, str, str]] = []
        self.stubs: list[tuple[str, list[str]]] = []
        self.posts: dict[str, list[dict[str, Any]]] = {}
        # M6: the merges table, rows as dicts with id, proposal_id, requested_by, diff_ref, paths, status.
        self.merges: list[dict[str, Any]] = []
        # Supervisor rounds (M4), mirroring the rounds table and the reads the round makes.
        self.rounds: list[dict[str, Any]] = []
        self.brief_template: dict[str, Any] = {}
        self.reports: dict[int, list[dict[str, Any]]] = {}
        self.reopened: dict[int, list[str]] = {}
        self.presets: list[tuple[str, float, str]] = []
        # The operator's controls (M5), mirroring control_state and the governor's reads.
        self.control: dict[str, Any] = {"epoch": 1, "paused": False}
        self.tpm: int = 0
        self.spend: float = 0.0
        self.resume_rows: list[dict[str, Any]] = []

    # --- M2 surface ---------------------------------------------------------

    def register_agent(self, agent_id: str, role: str, lineage_id: str, cwd: str, generation: int = 1,
                       harness_version: str = "dev") -> str:
        self.registered.append({"id": agent_id, "role": role, "lineage_id": lineage_id, "cwd": cwd,
                                "generation": generation, "harness_version": harness_version})
        return f"tok-{agent_id}"

    def set_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int, context_pct: float) -> None:
        self.usage.append((agent_id, tokens_in, tokens_out, context_pct))

    def set_agent_status(self, agent_id: str, status: str) -> None:
        self.status.append((agent_id, status))

    def log_event(self, agent_id: str, type: str, payload: dict[str, Any]) -> None:
        self.events.append((agent_id, type, payload))

    def tasks_summary(self) -> dict[str, int]:
        return dict(self.summary)

    # --- M3 surface ---------------------------------------------------------

    def request_handoff(self, agent_id: str) -> None:
        self.handoff_requests.append(agent_id)
        self.status.append((agent_id, "handing_off"))
        self.events.append((agent_id, "agent.handoff_requested", {}))

    def write_handoff(self, agent_id: str, body: str, stub: bool = False) -> int:
        """What civ_handoff (or the stub writer) does: store a row for the agent's lineage."""
        lineage_id = self._lineage_of(agent_id)
        rows = self.handoffs.setdefault(lineage_id, [])
        row = {"id": len(rows) + 1, "lineage_id": lineage_id, "from_agent": agent_id, "to_agent": None,
               "generation": self._generation_of(agent_id), "body": body, "stub": stub}
        rows.append(row)
        if not stub:
            self.status.append((agent_id, "ended"))
            self.events.append((agent_id, "agent.handoff_written", {"handoff_id": row["id"]}))
        return row["id"]

    def latest_handoff(self, lineage_id: str) -> dict[str, Any] | None:
        rows = self.handoffs.get(lineage_id) or []
        return dict(rows[-1]) if rows else None

    def recent_posts(self, agent_id: str, n: int = 30) -> list[dict[str, Any]]:
        return list(self.posts.get(agent_id, []))[-n:]

    def write_stub_handoff(self, agent_id: str, listing: list[str]) -> int:
        self.stubs.append((agent_id, list(listing)))
        body = "## State of work\nstub\n## Open threads\n## What to try next\n## What not to retry\n## Files worth reading\n" + "\n".join(listing)
        handoff_id = self.write_handoff(agent_id, body, stub=True)
        self.events.append((agent_id, "agent.stub_written", {"handoff_id": handoff_id}))
        return handoff_id

    def succeed(self, from_agent: str, to_agent: str, cwd: str, harness_version: str | None = None) -> str:
        lineage_id = self._lineage_of(from_agent)
        generation = self._generation_of(from_agent) + 1
        self.succeeded.append((from_agent, to_agent, cwd))
        self.succeeded_versions = getattr(self, "succeeded_versions", []) + [harness_version]
        rows = self.handoffs.get(lineage_id) or []
        if rows:
            rows[-1]["to_agent"] = to_agent
        self.events.append((from_agent, "agent.succeeded", {"to": to_agent}))
        return self.register_agent(to_agent, "solver", lineage_id=lineage_id, cwd=cwd, generation=generation,
                                   harness_version=harness_version or "dev")

    def lineage_state(self, lineage_id: str) -> dict[str, Any]:
        agents = [a for a in self.registered if a["lineage_id"] == lineage_id]
        head = agents[-1] if agents else None
        return {"generation": head["generation"] if head else 0, "head_agent": head["id"] if head else None,
                "status": "live" if head else "none", "lease": None}

    # --- M4 surface ---------------------------------------------------------

    def open_round(self, number: int, final: bool = False) -> int:
        round_id = len(self.rounds) + 1
        self.rounds.append({"id": round_id, "number": number, "final": final, "tuned_knob": None,
                            "ended_at": None, "summary": None, "brief": None})
        self.events.append((None, "round.opened", {"round_id": round_id, "number": number}))
        return round_id

    def round_brief(self, round_id: int) -> dict[str, Any]:
        row = self.rounds[round_id - 1]
        brief = {**self.brief_template, "round": {**self.brief_template.get("round", {}),
                                                   "id": round_id, "number": row["number"], "final": row["final"]}}
        row["brief"] = brief
        return brief

    def close_round(self, round_id: int, summary: dict[str, Any]) -> None:
        row = self.rounds[round_id - 1]
        row["ended_at"] = "closed"
        row["summary"] = summary
        self.events.append((None, "round.closed", {"round_id": round_id, **summary}))

    def current_round(self) -> dict[str, Any] | None:
        open_rounds = [r for r in self.rounds if r["ended_at"] is None]
        return dict(open_rounds[-1]) if open_rounds else None

    def set_knob(self, knob: str, value: float, reason: str) -> None:
        self.presets.append((knob, float(value), reason))
        self.events.append((None, "knob.preset", {"knob": knob, "value": value}))

    def reports_for_round(self, round_id: int) -> list[dict[str, Any]]:
        return list(self.reports.get(round_id, []))

    def last_round_number(self) -> int:
        return max((r.get("number", 0) for r in getattr(self, "rounds", [])), default=0)

    def reopened_in_round(self, round_id: int) -> list[str]:
        return list(self.reopened.get(round_id, []))

    # --- M5 surface ---------------------------------------------------------

    def control_get(self, key: str) -> Any | None:
        return self.control.get(key)

    def control_set(self, key: str, value: Any) -> None:
        self.control[key] = value
        self.events.append((None, "control.set", {"key": key}))

    def epoch(self) -> int:
        return int(self.control.get("epoch", 1))

    def live_agents(self) -> dict[str, int]:
        """Registered agents whose latest status is not ended or dead, by role, the way the
        agents table would count them."""
        latest: dict[str, str] = {}
        for agent_id, status in self.status:
            latest[agent_id] = status
        counts = {"solver": 0, "supervisor": 0}
        for a in self.registered:
            if latest.get(a["id"], "live") not in ("ended", "dead"):
                counts[a["role"]] = counts.get(a["role"], 0) + 1
        return counts

    def tokens_per_minute(self) -> int:
        return int(self.tpm)

    # --- M7 surface ---------------------------------------------------------

    def heartbeat_age_s(self) -> float | None:
        row = self.control.get("heartbeat")
        if not isinstance(row, dict) or not row.get("at"):
            return None
        from datetime import datetime, timezone
        at = datetime.fromisoformat(str(row["at"]))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - at).total_seconds())

    def chaos_get(self) -> dict[str, Any]:
        from harness.orchestrator.chaos import normalise
        return normalise(self.control.get("chaos"))

    def chaos_set(self, patch: dict[str, Any]) -> dict[str, Any]:
        from harness.orchestrator.chaos import normalise
        merged = normalise({**self.chaos_get(), **patch})
        self.control_set("chaos", merged)
        return merged

    def chaos_ack_kill(self, agent_id: str) -> None:
        current = self.chaos_get()
        self.control["chaos"] = {**current, "kill": [x for x in current["kill"] if x != agent_id]}
        self.events.append((agent_id, "chaos.acked", {"agent_id": agent_id}))

    def spend_usd(self, hours: float) -> float:
        return float(self.spend)

    # --- M6 surface ---------------------------------------------------------

    def merge_queue(self, statuses=("queued",)) -> list[dict[str, Any]]:
        return [dict(r) for r in getattr(self, "merges", []) if r.get("status") in statuses]

    def merge_start(self, merge_id: int) -> bool:
        for r in getattr(self, "merges", []):
            if r["id"] == merge_id and r["status"] == "queued":
                r["status"] = "running"
                return True
        return False

    def merge_finish(self, merge_id: int, status: str, detail: str, version_before=None, version_after=None,
                     commit_sha=None) -> None:
        # Like the real Operator: records the row and logs merge.<status> itself, so the merger
        # must not log on its own; unknown ids raise.
        for r in getattr(self, "merges", []):
            if r["id"] == merge_id:
                r.update({"status": status, "detail": detail, "version_before": version_before,
                          "version_after": version_after, "commit_sha": commit_sha})
                self.events.append((r.get("requested_by"), f"merge.{status}", {
                    "merge_id": merge_id, "proposal_id": r.get("proposal_id"), "paths": list(r.get("paths") or []),
                    "detail": (detail or "")[:200], "version_before": version_before, "version_after": version_after,
                    "commit_sha": commit_sha}))
                return
        raise ValueError(f"unknown merge {merge_id}")

    def lineages_to_resume(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.resume_rows]

    def _lineage_of(self, agent_id: str) -> str:
        for a in self.registered:
            if a["id"] == agent_id:
                return a["lineage_id"]
        raise KeyError(agent_id)

    def _generation_of(self, agent_id: str) -> int:
        for a in self.registered:
            if a["id"] == agent_id:
                return a["generation"]
        raise KeyError(agent_id)


def init_message(connected: bool = True) -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={"model": "claude-opus-5-5", "mcp_servers": [{"name": "civ", "status": "connected" if connected else "failed"}]},
    )


def assistant(context_tokens: int, output_tokens: int = 10, text: str = "working", model: str = "claude-opus-5-5") -> AssistantMessage:
    # Spread the context across the three input fields so a test proves the
    # monitor sums all of them rather than reading input_tokens alone.
    cached = context_tokens // 2
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model=model,
        usage={
            "input_tokens": context_tokens - cached,
            "cache_creation_input_tokens": cached // 2,
            "cache_read_input_tokens": cached - cached // 2,
            "output_tokens": output_tokens,
        },
    )


def result(num_turns: int = 3, cost: float = 0.05, text: str = "done", is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1000,
        duration_api_ms=900,
        is_error=is_error,
        num_turns=num_turns,
        session_id="sess-1",
        total_cost_usd=cost,
        usage={},
        result=text,
    )


def scripted_query(messages: list[Any]):
    """Return a query_fn that yields the given messages and records the call."""
    calls: list[dict[str, Any]] = []

    async def query_fn(*, prompt: str, options: Any):
        calls.append({"prompt": prompt, "options": options})
        for m in messages:
            yield m

    query_fn.calls = calls  # type: ignore[attr-defined]
    return query_fn


OnQuery = Callable[["FakeClient", str], None]


class FakeClient:
    """A scripted ClaudeSDKClient: yields its messages, records what was done to it.

    after_interrupt says what the stream does once interrupted: "result" answers
    with a result message the way the CLI does, "end" just stops, and "hang"
    never yields again, so the spawner's drain timeout can be tested.
    """

    def __init__(
        self,
        messages: list[Any],
        on_query: OnQuery | None = None,
        usage: dict[str, Any] | None = None,
        after_interrupt: str = "result",
    ) -> None:
        self.messages = list(messages)
        self.on_query = on_query
        self.usage = usage
        self.after_interrupt = after_interrupt
        self.turns_yielded = 0
        self.options: Any = None
        self.connected_with: str | None = None
        self.sent: list[str] = []
        self.interrupted = False
        self.disconnected = False
        self.probes = 0

    async def connect(self, prompt: str | None = None) -> None:
        self.connected_with = prompt

    async def receive_messages(self) -> AsyncIterator[Any]:
        for m in self.messages:
            if self.interrupted:
                if self.after_interrupt == "result":
                    # The real client ends the turn with a result after an interrupt,
                    # and its turn count is what has actually run so far.
                    yield result(num_turns=self.turns_yielded, text="interrupted")
                elif self.after_interrupt == "hang":
                    await asyncio.Event().wait()
                return
            if isinstance(m, AssistantMessage):
                self.turns_yielded += 1
            yield m
            # Give the runner's watcher task a chance to run between messages.
            await asyncio.sleep(0)

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.sent.append(prompt)
        if self.on_query is not None:
            self.on_query(self, prompt)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def get_context_usage(self) -> dict[str, Any] | None:
        self.probes += 1
        return self.usage


def client_factory(clients: list[FakeClient]):
    """A factory that hands out the given clients in order and remembers the options each got."""
    handed: list[FakeClient] = []

    def factory(options: Any) -> FakeClient:
        client = clients[len(handed)]
        client.options = options
        handed.append(client)
        return client

    factory.handed = handed  # type: ignore[attr-defined]
    return factory
