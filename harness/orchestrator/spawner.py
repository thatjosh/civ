"""Spawn one agent as a Claude Agent SDK session attached to the board.

The spawner is the only place that knows how an agent is wired: its
directory, its spawn token, the MCP server it talks to, and the SDK options.
Everything else in the orchestrator deals in agent ids and results.

Sessions run through ClaudeSDKClient rather than query(), because succession
has to reach into a running session twice: once to send the handoff request
and once to interrupt it when the handoff has landed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultError,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from harness.orchestrator.monitor import KnobReader, Monitor
from harness.orchestrator.prompts import PromptAssembler, PromptContext

log = logging.getLogger(__name__)

# The built-in tools a solver gets. Bash is included because the task pack is
# files and the solver will want grep and wc; the sandbox is out of scope.
SOLVER_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
# A supervisor reads the board and the shared/ files it references, nothing
# else: no shell, no writes, so its whole output is board actions.
# Write and Bash so a supervisor can produce a diff under proposals/ in its own directory and
# post it as a proposal (M6); the prompt confines the writes, the sandbox is the boundary.
SUPERVISOR_TOOLS = ["Read", "Grep", "Write", "Bash"]
VERSION_FILE = "harness/VERSION"
DEFAULT_VERSION = "dev"
KICKOFF_PROMPT = "Begin your solver loop."
SUPERVISOR_KICKOFF = "Begin your round."
MCP_SERVER_NAME = "civ"
TOKEN_HEADER = "X-Civ-Token"
# The bundled CLI honours this switch; the handoff is our compaction, so the CLI's must stay off.
SESSION_ENV = {"DISABLE_AUTO_COMPACT": "1"}

# The CLI's result subtype when the session hits max_turns; a normal end for us, not a failure.
MAX_TURNS_SUBTYPE = "error_max_turns"
# After an interrupt the CLI normally answers with a result that carries the real
# cost and turn count; we wait this long for it before falling back to an estimate.
DRAIN_AFTER_INTERRUPT_S = 10.0


@dataclass(frozen=True)
class RoleProfile:
    """What differs between the two roles at spawn: tools, the extra directory the
    session may read, and the first message. Everything else is shared."""

    name: str
    tools: tuple[str, ...]
    # Relative to the repo root: the materials for a solver, shared/ for a supervisor.
    extra_dir: str
    kickoff: str

    def kickoff_prompt(self, cwd: Path, extra: Path, root: Path | None = None) -> str:
        if self.name == "supervisor":
            harness = f" The harness source you may propose changes to is under {root}/harness (read it, never edit it in place)." if root else ""
            return f"{self.kickoff} Your directory is {cwd}. Files the board references as shared/... are under {extra}.{harness}"
        return (
            f"{self.kickoff} Your directory is {cwd} (write only there, under work/). "
            f"The read-only materials are at {extra}."
        )


ROLES: dict[str, RoleProfile] = {
    "solver": RoleProfile("solver", tuple(SOLVER_TOOLS), "task/materials", KICKOFF_PROMPT),
    "supervisor": RoleProfile("supervisor", tuple(SUPERVISOR_TOOLS), "shared", SUPERVISOR_KICKOFF),
}


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
    generation: int = 1
    # Set by the runner when it cut the session short on purpose.
    stopped_by_runner: bool = False
    # True when cost_usd was priced from token counts because no result arrived.
    cost_estimated: bool = False


class ClientLike(Protocol):
    """The slice of ClaudeSDKClient the spawner uses, so tests can script one."""

    async def connect(self, prompt: str | None = None) -> None: ...

    def receive_messages(self) -> AsyncIterator[Any]: ...

    async def query(self, prompt: str, session_id: str = "default") -> None: ...

    async def interrupt(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_context_usage(self) -> Any: ...


ClientFactory = Callable[[ClaudeAgentOptions], ClientLike]
QueryFn = Callable[..., AsyncIterator[Any]]


class QueryAdapterClient:
    """Wraps a query()-style function as a client, for tests written against the M2 spawner."""

    def __init__(self, query_fn: QueryFn, options: ClaudeAgentOptions) -> None:
        self._query_fn = query_fn
        self._options = options
        self._stream: AsyncIterator[Any] | None = None
        self.sent: list[str] = []
        self.interrupted = False
        self.disconnected = False

    async def connect(self, prompt: str | None = None) -> None:
        self._stream = self._query_fn(prompt=prompt or "", options=self._options)

    async def receive_messages(self) -> AsyncIterator[Any]:
        assert self._stream is not None
        async for message in self._stream:
            yield message

    async def query(self, prompt: str, session_id: str = "default") -> None:
        self.sent.append(prompt)

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def get_context_usage(self) -> Any:
        return None


class SessionControl:
    """What a running session lets the succession runner do to it."""

    def __init__(self, client: ClientLike, agent_id: str) -> None:
        self._client = client
        self.agent_id = agent_id
        self.stop_requested = False
        self.stop_reason: str | None = None

    async def send(self, text: str) -> None:
        """Deliver a further user message into the running session."""
        await self._client.query(text)

    async def context_usage(self) -> dict[str, Any] | None:
        try:
            usage = await self._client.get_context_usage()
        except Exception as exc:  # noqa: BLE001 - a failed probe must not end the session
            log.warning("context usage probe failed for %s: %s", self.agent_id, exc)
            return None
        return dict(usage) if usage else None

    def request_stop(self, reason: str) -> None:
        """Ask the session loop to interrupt and disconnect after the current message."""
        self.stop_requested = True
        self.stop_reason = reason

    async def interrupt(self) -> None:
        """Cut the current turn now, for a session that is producing no messages to stop on."""
        try:
            await self._client.interrupt()
        except Exception as exc:  # noqa: BLE001 - the session loop still ends on stop_requested
            log.warning("interrupt failed for %s: %s", self.agent_id, exc)


MessageHook = Callable[[Any, SessionControl], Awaitable[None]]


@dataclass
class Prepared:
    """Everything decided before a session starts."""

    agent_id: str
    cwd: Path
    token: str
    options: ClaudeAgentOptions
    kickoff: str


class Spawner:
    def __init__(
        self,
        operator: Any,
        board_url: str,
        root: Path | str,
        prompts: PromptAssembler,
        model: str = "claude-opus-5-5",
        max_turns: int = 40,
        harness_version: str | None = None,
        query_fn: QueryFn | None = None,
        client_factory: ClientFactory | None = None,
        knobs: KnobReader | None = None,
        context_window: int = 1_000_000,
        force_handoff_after_turns: int | None = None,
        drain_s: float = DRAIN_AFTER_INTERRUPT_S,
        governor: Any | None = None,
    ) -> None:
        self._op = operator
        self._drain_s = drain_s
        # The governor (M5) gates every spawn against the operator's ceilings and the pause
        # flag; None means ungoverned, which the unit tests and the M2 demo rely on.
        self._governor = governor
        self._board_url = board_url.rstrip("/")
        self._root = Path(root).resolve()
        self._prompts = prompts
        self._model = model
        self._max_turns = max_turns
        # None means "read harness/VERSION at every spawn", so a merge that bumps the file is
        # carried by the next spawn's identity block without a restart; a string pins it (tests).
        self._harness_version = harness_version
        self._knobs = knobs
        self._window = context_window
        self._force_after = force_handoff_after_turns
        # A client factory wins; a query_fn is adapted so the M2 tests keep working.
        if client_factory is not None:
            self._client_factory = client_factory
        elif query_fn is not None:
            self._client_factory = lambda options: QueryAdapterClient(query_fn, options)
        else:
            self._client_factory = ClaudeSDKClient

    # Directory layout per agent. The directory persists across generations
    # of a lineage, so creating it must be idempotent.
    def agent_dir(self, agent_id: str) -> Path:
        # Absolute, because the board server resolves task_complete result paths against it.
        return (self._root / "agents" / agent_id).resolve()

    def prepare_directory(self, agent_id: str, ctx: PromptContext, cwd: Path | None = None) -> Path:
        cwd = (cwd or self.agent_dir(agent_id)).resolve()
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

    def build_options(self, cwd: Path, system_prompt: str, token: str, role: str = "solver") -> ClaudeAgentOptions:
        profile = self._profile(role)
        return ClaudeAgentOptions(
            cwd=str(cwd),
            system_prompt=system_prompt,
            tools=list(profile.tools),
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
            add_dirs=[str(self._root / profile.extra_dir)],
            env=dict(SESSION_ENV),
        )

    @staticmethod
    def _profile(role: str) -> RoleProfile:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; expected one of {sorted(ROLES)}")
        return ROLES[role]

    def prepare(
        self,
        agent_id: str,
        lineage_id: str,
        generation: int = 1,
        handoff: str | None = None,
        handoff_is_stub: bool = False,
        broadcasts: list[str] | None = None,
        token: str | None = None,
        cwd: Path | None = None,
        role: str = "solver",
        brief: str | None = None,
    ) -> Prepared:
        """Register the agent (unless a token says it already is) and build its session options.

        A supervisor is spawned with the round's brief (rendered markdown), which
        becomes part of its prompt; a solver never has one.
        """
        profile = self._profile(role)
        if role == "supervisor" and brief is None:
            raise ValueError("a supervisor session needs the round's brief")
        ctx = PromptContext(
            agent_id=agent_id,
            role=role,
            generation=generation,
            lineage_id=lineage_id,
            harness_version=self.harness_version(),
            broadcasts=list(broadcasts or []),
            handoff=handoff,
            handoff_is_stub=handoff_is_stub,
        )
        cwd = self.prepare_directory(agent_id, ctx, cwd)
        if token is None:
            # The row carries the version too, not only the identity block, because that is what
            # the dashboard and the brief read; the first live merge run showed every agent as "dev".
            token = self._op.register_agent(agent_id, role, lineage_id=lineage_id, cwd=str(cwd), generation=generation,
                                            harness_version=ctx.harness_version)
        system_prompt = (
            self._prompts.supervisor_prompt(ctx, brief or "") if role == "supervisor" else self._prompts.solver_prompt(ctx)
        )
        options = self.build_options(cwd, system_prompt, token, role)
        # register_agent already logged agent.spawned; this event carries what the spawner chose.
        self._op.log_event(agent_id, "agent.options",
                           {"generation": generation, "lineage_id": lineage_id, "model": self._model, "role": role})
        return Prepared(agent_id, cwd, token, options, self.kickoff_prompt(cwd, role))

    async def spawn(
        self,
        agent_id: str,
        lineage_id: str,
        generation: int = 1,
        handoff: str | None = None,
        broadcasts: list[str] | None = None,
        token: str | None = None,
        cwd: Path | None = None,
        handoff_is_stub: bool = False,
        on_message: MessageHook | None = None,
        monitor: Monitor | None = None,
        role: str = "solver",
        brief: str | None = None,
    ) -> SpawnResult:
        if self._governor is not None:
            # Before prepare, so the agent about to register is not counted against the live
            # ceiling. A successor takes over its predecessor's slot, so only a solver past
            # generation one replaces one; a supervisor's round number is its generation, but it
            # holds no slot and must wait like a new agent.
            await self._governor.acquire(role, replaces_slot=(role == "solver" and generation > 1))
        prepared = self.prepare(agent_id, lineage_id, generation, handoff, handoff_is_stub, broadcasts, token, cwd,
                                role=role, brief=brief)
        monitor = monitor or self.monitor_for(agent_id)
        result = await self.run_session(prepared, monitor, on_message)
        result.generation = generation
        return result

    def monitor_for(self, agent_id: str) -> Monitor:
        return Monitor(self._op, agent_id, context_window=self._window, knobs=self._knobs, force_handoff_after_turns=self._force_after)

    async def run_session(self, prepared: Prepared, monitor: Monitor, on_message: MessageHook | None = None) -> SpawnResult:
        """Run one session to its end, feeding every message to the monitor and the hook.

        The hook may ask the session to stop through SessionControl; the loop
        then interrupts and disconnects after the message it is on. Disconnect
        always happens, so a crashed hook cannot leave a CLI process behind.
        """
        agent_id = prepared.agent_id
        result = SpawnResult(agent_id=agent_id)
        client = self._client_factory(prepared.options)
        control = SessionControl(client, agent_id)
        connected = False
        try:
            await client.connect(prepared.kickoff)
            connected = True
            stream = client.receive_messages().__aiter__()
            drain_until: float | None = None
            while True:
                message = await self._next_message(agent_id, stream, drain_until)
                if message is None:
                    break
                self._check_init(agent_id, message)
                monitor.observe(message)
                if monitor.probe_due():
                    monitor.apply_probe(await control.context_usage())
                if on_message is not None:
                    await on_message(message, control)
                if isinstance(message, ResultMessage):
                    self._record_result(result, message)
                    break
                if control.stop_requested and drain_until is None:
                    # Interrupt, then keep reading for a bounded time rather than
                    # breaking out: the CLI answers an interrupt with a result that
                    # carries the real cost and turn count, and that is the only
                    # place they come from.
                    result.stopped_by_runner = True
                    result.result = f"stopped by runner: {control.stop_reason}"
                    await client.interrupt()
                    drain_until = time.monotonic() + self._drain_s
        except ResultError as exc:
            if exc.subtype == MAX_TURNS_SUBTYPE:
                # The CLI exits non-zero after the max-turns result; the loop above already recorded it.
                self._op.set_agent_status(agent_id, "ended")
            else:
                self._record_failure(agent_id, result, exc)
        except Exception as exc:  # noqa: BLE001 - the caller wants the failure in the result, not a crash
            self._record_failure(agent_id, result, exc)
        finally:
            if connected:
                try:
                    await client.disconnect()
                except Exception as exc:  # noqa: BLE001 - nothing useful can be done about a failed disconnect
                    log.warning("disconnect failed for %s: %s", agent_id, exc)

        totals = monitor.totals
        if not totals.ended:
            # No result ever arrived (interrupted with no reply, or a crash):
            # price the session from its token counts so the summary is honest.
            monitor.finish_without_result(
                reason="interrupted" if result.stopped_by_runner else "no_result",
                mark_ended=result.error is None,
            )
        result.cost_usd = totals.cost_usd
        result.cost_estimated = totals.cost_estimated
        result.tokens_in = totals.tokens_in
        result.tokens_out = totals.tokens_out
        result.context_pct = totals.context_pct
        if result.turns == 0:
            result.turns = totals.turns
        return result

    def kickoff_prompt(self, cwd: Path, role: str = "solver") -> str:
        profile = self._profile(role)
        return profile.kickoff_prompt(cwd, (self._root / profile.extra_dir).resolve(), self._root)

    def harness_version(self) -> str:
        """The version new spawns are tagged with: the pinned string, else harness/VERSION as it
        reads right now, else "dev" when there is no file (a bare tmp root in tests)."""
        if self._harness_version is not None:
            return self._harness_version
        try:
            return (self._root / VERSION_FILE).read_text(encoding="utf-8").strip() or DEFAULT_VERSION
        except OSError:
            return DEFAULT_VERSION

    async def _next_message(self, agent_id: str, stream: AsyncIterator[Any], drain_until: float | None) -> Any | None:
        """The next message, or None when the stream ends or the drain deadline passes."""
        try:
            if drain_until is None:
                return await stream.__anext__()
            remaining = drain_until - time.monotonic()
            if remaining <= 0:
                return None
            return await asyncio.wait_for(stream.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return None
        except asyncio.TimeoutError:
            log.warning("no result within %.0fs of interrupting %s; its cost will be estimated", self._drain_s, agent_id)
            return None

    def _record_result(self, result: SpawnResult, message: ResultMessage) -> None:
        result.turns = message.num_turns
        if result.stopped_by_runner:
            # The note already says why it stopped, and an interrupted result's
            # error flag describes the interrupt, not a failure.
            return
        result.result = message.result
        if message.subtype == MAX_TURNS_SUBTYPE:
            # The turn cap is our own budget, so reaching it is a normal end.
            result.result = f"stopped at the {self._max_turns}-turn cap"
        elif message.is_error:
            result.error = message.result or message.subtype

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
