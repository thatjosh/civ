"""Fakes shared by the orchestrator unit tests.

The orchestrator talks to the database only through the Operator API, so a
fake that records calls is all the tests need. No Postgres, no SDK.
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock


class FakeOperator:
    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self.usage: list[tuple[str, int, int, float]] = []
        self.status: list[tuple[str, str]] = []
        self.events: list[tuple[str, str, dict[str, Any]]] = []
        self.summary: dict[str, int] = {"open": 1, "leased": 0, "done": 2, "blocked": 0}

    def register_agent(self, agent_id: str, role: str, lineage_id: str, cwd: str, generation: int = 1) -> str:
        self.registered.append({"id": agent_id, "role": role, "lineage_id": lineage_id, "cwd": cwd, "generation": generation})
        return f"tok-{agent_id}"

    def set_agent_usage(self, agent_id: str, tokens_in: int, tokens_out: int, context_pct: float) -> None:
        self.usage.append((agent_id, tokens_in, tokens_out, context_pct))

    def set_agent_status(self, agent_id: str, status: str) -> None:
        self.status.append((agent_id, status))

    def log_event(self, agent_id: str, type: str, payload: dict[str, Any]) -> None:
        self.events.append((agent_id, type, payload))

    def tasks_summary(self) -> dict[str, int]:
        return dict(self.summary)


def init_message(connected: bool = True) -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={"model": "claude-opus-5-5", "mcp_servers": [{"name": "civ", "status": "connected" if connected else "failed"}]},
    )


def assistant(context_tokens: int, output_tokens: int = 10, text: str = "working") -> AssistantMessage:
    # Spread the context across the three input fields so a test proves the
    # monitor sums all of them rather than reading input_tokens alone.
    cached = context_tokens // 2
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-opus-5-5",
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
