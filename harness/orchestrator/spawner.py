"""Spawn one agent as a Claude Agent SDK session attached to the board.

The spawner is the only place that knows how an agent is wired: its
directory, its spawn token, the MCP server it talks to, and the SDK options.
Everything else in the orchestrator deals in agent ids and results.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultError, ResultMessage, SystemMessage, TextBlock, query

from harness.orchestrator.monitor import Monitor
from harness.orchestrator.prompts import PromptAssembler, PromptContext

log = logging.getLogger(__name__)

# The built-in tools a solver gets. Bash is included because the task pack is
# files and the solver will want grep and wc; the sandbox is out of scope.
SOLVER_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
KICKOFF_PROMPT = "Begin your solver loop."
MCP_SERVER_NAME = "civ"
TOKEN_HEADER = "X-Civ-Token"


class McpNotConnected(RuntimeError):
    """Raised when the session starts without the board attached, because a
    solver without the board can only do work nobody will ever see."""


@dataclass
class SpawnResult:
    agent_id: str
    turns: int = 0
    cost_usd: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    context_pct: float = 0.0
    result: str | None = None
    error: str | None = None


# The CLI's result subtype when the session hits max_turns; a normal end for us, not a failure.
MAX_TURNS_SUBTYPE = "error_max_turns"

# query() is injectable so tests can replace the SDK with a scripted stream.
QueryFn = Callable[..., AsyncIterator[Any]]


class Spawner:
    def __init__(
        self,
        operator: Any,
        board_url: str,
        root: Path | str,
        prompts: PromptAssembler,
        model: str = "claude-opus-5-5",
        max_turns: int = 40,
        harness_version: str = "dev",
        query_fn: QueryFn = query,
    ) -> None:
        self._op = operator
        self._board_url = board_url.rstrip("/")
        self._root = Path(root).resolve()
        self._prompts = prompts
        self._model = model
        self._max_turns = max_turns
        self._harness_version = harness_version
        self._query = query_fn

    # Directory layout per agent. The directory persists across generations
    # of a lineage, so creating it must be idempotent.
    def agent_dir(self, agent_id: str) -> Path:
        # Absolute, because the board server resolves task_complete result paths against it.
        return (self._root / "agents" / agent_id).resolve()

    def prepare_directory(self, agent_id: str, ctx: PromptContext) -> Path:
        cwd = self.agent_dir(agent_id)
        (cwd / "work").mkdir(parents=True, exist_ok=True)
        (cwd / "notes").mkdir(parents=True, exist_ok=True)
        identity = {
            "agent_id": ctx.agent_id,
            "role": ctx.role,
            "generation": ctx.generation,
            "lineage_id": ctx.lineage_id,
            "harness_version": ctx.harness_version,
        }
        (cwd / "identity.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
        return cwd

    def build_options(self, cwd: Path, system_prompt: str, token: str) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=str(cwd),
            system_prompt=system_prompt,
            tools=list(SOLVER_TOOLS),
            mcp_servers={
                MCP_SERVER_NAME: {
                    "type": "http",
                    "url": f"{self._board_url}/mcp",
                    "headers": {TOKEN_HEADER: token},
                }
            },
            # Non-interactive by design: there is no human to approve a tool
            # call, and the design puts the safety boundary in the sandbox.
            permission_mode="bypassPermissions",
            # No user or project settings, so the operator's own CLAUDE.md
            # never leaks into a solver's prompt.
            setting_sources=[],
            model=self._model,
            max_turns=self._max_turns,
            add_dirs=[str(self._root / "task" / "materials")],
        )

    async def spawn(
        self,
        agent_id: str,
        lineage_id: str,
        generation: int = 1,
        handoff: str | None = None,
        broadcasts: list[str] | None = None,
    ) -> SpawnResult:
        ctx = PromptContext(
            agent_id=agent_id,
            role="solver",
            generation=generation,
            lineage_id=lineage_id,
            harness_version=self._harness_version,
            broadcasts=list(broadcasts or []),
            handoff=handoff,
        )
        cwd = self.prepare_directory(agent_id, ctx)
        token = self._op.register_agent(agent_id, "solver", lineage_id=lineage_id, cwd=str(cwd), generation=generation)
        options = self.build_options(cwd, self._prompts.solver_prompt(ctx), token)
        # register_agent already logged agent.spawned; this event carries what the spawner chose.
        self._op.log_event(agent_id, "agent.options", {"generation": generation, "lineage_id": lineage_id, "model": self._model})

        monitor = Monitor(self._op, agent_id)
        result = SpawnResult(agent_id=agent_id)
        try:
            async for message in self._query(prompt=self.kickoff_prompt(cwd), options=options):
                self._check_init(agent_id, message)
                monitor.observe(message)
                if isinstance(message, ResultMessage):
                    result.turns = message.num_turns
                    result.result = message.result
                    if message.subtype == MAX_TURNS_SUBTYPE:
                        # The turn cap is our own budget, so reaching it is a normal end.
                        result.result = f"stopped at the {self._max_turns}-turn cap"
                    elif message.is_error:
                        result.error = message.result or message.subtype
        except ResultError as exc:
            if exc.subtype == MAX_TURNS_SUBTYPE:
                # The CLI exits non-zero after the max-turns result; the loop above already recorded it.
                self._op.set_agent_status(agent_id, "ended")
            else:
                self._record_failure(agent_id, result, exc)
        except Exception as exc:  # noqa: BLE001 - the caller wants the failure in the result, not a crash
            self._record_failure(agent_id, result, exc)

        totals = monitor.totals
        result.cost_usd = totals.cost_usd
        result.tokens_in = totals.tokens_in
        result.tokens_out = totals.tokens_out
        result.context_pct = totals.context_pct
        if result.turns == 0:
            result.turns = totals.turns
        return result

    def kickoff_prompt(self, cwd: Path) -> str:
        materials = (self._root / "task" / "materials").resolve()
        return (
            f"{KICKOFF_PROMPT} Your directory is {cwd} (write only there, under work/). "
            f"The read-only materials are at {materials}."
        )

    def _record_failure(self, agent_id: str, result: SpawnResult, exc: BaseException) -> None:
        log.exception("agent %s failed", agent_id)
        result.error = f"{type(exc).__name__}: {exc}"
        self._op.log_event(agent_id, "agent.error", {"error": result.error})
        self._op.set_agent_status(agent_id, "dead")

    def _check_init(self, agent_id: str, message: Any) -> None:
        if not (isinstance(message, SystemMessage) and message.subtype == "init"):
            return
        servers = {s.get("name"): s.get("status") for s in message.data.get("mcp_servers", [])}
        self._op.log_event(agent_id, "agent.init", {"mcp_servers": servers, "model": message.data.get("model")})
        if servers.get(MCP_SERVER_NAME) != "connected":
            raise McpNotConnected(f"board MCP server not connected for {agent_id}: {servers}")


def text_of(message: AssistantMessage) -> str:
    """Join the text blocks of an assistant message, for logs."""
    return "".join(block.text for block in message.content if isinstance(block, TextBlock))
