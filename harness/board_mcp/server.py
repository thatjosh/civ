"""The board MCP server: one HTTP process, the single writer to Postgres.

The MCP layer is deliberately thin. Each tool resolves the caller from the X-Civ-Token header, runs
the matching service method in a worker thread (the service layer is synchronous psycopg, and
blocking the event loop would serialise every agent), then attaches head_seq and any notices.

Run with:  python -m harness.board_mcp.server
"""

from __future__ import annotations

import functools
import importlib
import logging
import os
from pathlib import Path
from typing import Any, Callable

import anyio
from mcp.server.mcpserver import Context, MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import notices
from .auth import AgentIdentity, resolve_token
from .tools.common import log_event
from .db import Db, head_seq
from .limits import Knobs
from .tools.board import BoardTools
from .tools.civ import CivTools
from .tools.task import TaskTools

log = logging.getLogger("civ.board")

SERVER_NAME = "civ"
TOKEN_HEADER = "x-civ-token"
DEFAULT_VERIFIER = "task.verifier.verifier:verify"


def repo_root() -> Path:
    return Path(os.environ.get("CIV_ROOT", Path(__file__).resolve().parents[2]))


def load_verifier(spec: str | None):
    """CIV_VERIFIER is module:function; a missing verifier is allowed (scores stay null)."""
    spec = spec or os.environ.get("CIV_VERIFIER", DEFAULT_VERIFIER)
    if not spec:
        return None
    module_name, _, func_name = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
        return getattr(module, func_name)
    except Exception as exc:
        log.warning("verifier %s not loaded (%s); task scores will be null", spec, exc)
        return None


class Gateway:
    """Resolves identity, runs a service method off the event loop, and wraps the result."""

    def __init__(self, db: Db):
        self.db = db

    def identity(self, ctx: Context) -> AgentIdentity | None:
        headers = ctx.headers or {}
        token = headers.get(TOKEN_HEADER) or headers.get(TOKEN_HEADER.upper()) or headers.get("X-Civ-Token")
        with self.db.transaction() as conn:
            return resolve_token(conn, token)

    def envelope(self, identity: AgentIdentity, payload: dict[str, Any]) -> dict[str, Any]:
        with self.db.transaction() as conn:
            payload = dict(payload)
            payload.setdefault("head_seq", head_seq(conn))
            pending = notices.collect(conn, identity)
            if pending:
                payload["notices"] = pending
            return payload

    async def call(self, ctx: Context, tool: str, method: Callable[..., dict[str, Any]], **kwargs) -> dict[str, Any]:
        identity = await anyio.to_thread.run_sync(self.identity, ctx)
        if identity is None:
            return {"ok": False, "error": "unknown agent token"}
        result = await anyio.to_thread.run_sync(functools.partial(method, identity, **kwargs))
        if result.get("ok") is False:
            # Every refusal is an event, because a supervisor diagnosing a failed handoff needs to
            # know whether the agent tried and was turned away, and the brief counts them by reason.
            await anyio.to_thread.run_sync(self.log_refusal, identity, tool, result)
        return await anyio.to_thread.run_sync(self.envelope, identity, result)

    def log_refusal(self, identity: AgentIdentity, tool: str, result: dict[str, Any]) -> None:
        with self.db.transaction() as conn:
            log_event(conn, identity.id, "tool.refused", {"tool": tool, "error": str(result.get("error"))})


def build_server(db: Db, shared_dir: Path, verifier=None) -> MCPServer:
    knobs = Knobs(db)
    board = BoardTools(db, shared_dir, knobs)
    tasks = TaskTools(db, shared_dir, knobs, verifier)
    civ = CivTools(db, knobs, shared_dir)
    gw = Gateway(db)
    server = MCPServer(
        SERVER_NAME,
        instructions="The civ board. Every tool returns ok, head_seq and sometimes notices; "
                     "a refusal is ok=false with the reason in error.",
    )

    @server.tool(description="Post a finding, question, blocker or proposal. thread_id must be a task thread root.")
    async def board_post(kind: str, body: str, thread_id: int | None = None, parent_id: int | None = None,
                         refs: list[str] | None = None, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "board_post", board.post, kind=kind, body=body, thread_id=thread_id, parent_id=parent_id, refs=refs)

    @server.tool(description="Read messages: a thread in order, a kind by score, or everything after since_seq.")
    async def board_read(thread_id: int | None = None, kind: str | None = None, since_seq: int | None = None,
                         limit: int = 20, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "board_read", board.read, thread_id=thread_id, kind=kind, since_seq=since_seq, limit=limit)

    @server.tool(description="Highest-scored messages of a kind within the window.")
    async def board_top(kind: str | None = None, window_hours: float = 24, limit: int = 10,
                        ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "board_top", board.top, kind=kind, window_hours=window_hours, limit=limit)

    @server.tool(description="Full-text search over message bodies and vote reasons.")
    async def board_search(query: str, limit: int = 10, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "board_search", board.search, query=query, limit=limit)

    @server.tool(description="Vote +1 or -1 on a message you did not write, with a reason. One vote per message.")
    async def board_vote(message_id: int, value: int, reason: str, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "board_vote", board.vote, message_id=message_id, value=value, reason=reason)

    @server.tool(description="Acknowledge a pinned operator vote; required before other writes while one is pending.")
    async def board_ack(message_id: int, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "board_ack", board.ack, message_id=message_id)

    @server.tool(description="Lease the highest-priority open task. Refused while you hold one or owe a vote.")
    async def task_claim(ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "task_claim", tasks.claim)

    @server.tool(description="Extend your lease on a task you are still working on.")
    async def task_heartbeat(task_id: str, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "task_heartbeat", tasks.heartbeat, task_id=task_id)

    @server.tool(description="Complete a task: result_path is relative to your directory; summary is posted to the thread.")
    async def task_complete(task_id: str, result_path: str, summary: str, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "task_complete", tasks.complete, task_id=task_id, result_path=result_path, summary=summary)

    @server.tool(description="Give a task back with a reason; it returns to the queue after a backoff.")
    async def task_release(task_id: str, reason: str, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "task_release", tasks.release, task_id=task_id, reason=reason)

    @server.tool(description="Supervisor only: return a blocked task to the queue with a reason for the next solver.")
    async def task_reopen(task_id: str, reason: str, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "task_reopen", tasks.reopen, task_id=task_id, reason=reason)

    @server.tool(description="Write your handoff (only after the orchestrator has asked you to).")
    async def civ_handoff(body: str, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "civ_handoff", civ.handoff, body=body)

    @server.tool(description="Raise an alert for the supervisors to aggregate for the operator.")
    async def civ_alert(body: str, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "civ_alert", civ.alert, body=body)

    @server.tool(description="Live agents, tokens per minute, queue depth, and your own state.")
    async def civ_status(ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "civ_status", civ.status)

    @server.tool(description="Supervisor only: move a soft knob by at most one step, naming the SLO it serves.")
    async def civ_tune(knob: str, value: float, slo: str, reason: str, ttl_s: int = 3600,
                       ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "civ_tune", civ.tune, knob=knob, value=value, slo=slo, reason=reason, ttl_s=ttl_s)

    @server.tool(description="Supervisor only: write the round's one report; a trigger makes it a ping the server verifies.")
    async def civ_report(body: str, trigger: str | None = None, ctx: Context = None) -> dict[str, Any]:
        return await gw.call(ctx, "civ_report", civ.report, body=body, trigger=trigger)

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        def probe():
            with db.transaction() as conn:
                return head_seq(conn)
        head = await anyio.to_thread.run_sync(probe)
        return JSONResponse({"ok": True, "head_seq": head})

    return server


def build_app(db: Db, shared_dir: Path, verifier=None):
    """The Starlette app, for uvicorn and for in-process tests."""
    server = build_server(db, shared_dir, verifier)
    return server.streamable_http_app(stateless_http=True, json_response=True)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("CIV_LOG_LEVEL", "INFO"))
    root = repo_root()
    db = Db(os.environ.get("CIV_DATABASE_URL"))
    app = build_app(db, root / "shared", load_verifier(None))
    host = os.environ.get("CIV_BOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("CIV_BOARD_PORT", "8765"))
    log.info("civ board listening on http://%s:%d/mcp", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
