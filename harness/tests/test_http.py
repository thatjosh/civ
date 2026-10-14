"""M1 over the wire: the Starlette app under uvicorn, an MCP client, and the X-Civ-Token header."""

import json
import socket
import threading
import time

import httpx
import httpx2
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from harness.board_mcp.server import build_app


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def served(db, shared_dir):
    """Run the real app in a background thread for the duration of one test."""
    port = _free_port()
    config = uvicorn.Config(build_app(db, shared_dir, None), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def _payload(result) -> dict:
    # Structured output is the contract; the text block carries the same JSON for clients that lack it.
    if result.structured_content:
        return result.structured_content
    return json.loads(result.content[0].text)


async def _call(base_url: str, token: str | None, name: str, args: dict) -> dict:
    headers = {"X-Civ-Token": token} if token else {}
    async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
        async with streamable_http_client(f"{base_url}/mcp", http_client=http) as (read, write, *_):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return _payload(await session.call_tool(name, args))


def test_health(served):
    body = httpx.get(f"{served}/health").json()
    assert body == {"ok": True, "head_seq": 0}


async def test_post_and_read_over_http(served, make_agent, op):
    _, token = make_agent("solver", agent_id="sol-http")
    thread_id = op.create_task("T-001", "Count", "Count the things")
    posted = await _call(served, token, "board_post", {"kind": "finding", "body": "three things", "thread_id": thread_id})
    assert posted["ok"] and posted["head_seq"] == posted["seq"]
    read = await _call(served, token, "board_read", {"thread_id": thread_id})
    assert [m["body"] for m in read["messages"]][-1] == "three things"
    assert read["messages"][-1]["author_id"] == "sol-http"


async def test_unknown_token_is_refused(served):
    out = await _call(served, "not-a-token", "civ_status", {})
    assert out == {"ok": False, "error": "unknown agent token"}
    out = await _call(served, None, "civ_status", {})
    assert out["error"] == "unknown agent token"


async def test_notices_arrive_over_http(served, make_agent, op):
    _, token = make_agent("solver")
    op.broadcast("Prefer breadth over depth")
    out = await _call(served, token, "civ_status", {})
    assert out["ok"] and out["notices"][0]["type"] == "broadcast"
