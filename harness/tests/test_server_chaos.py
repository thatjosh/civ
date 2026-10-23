"""The board under chaos: an injected outage refuses every tool, lag delays every answer, and
the health endpoint keeps answering with the reason."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from harness.board_mcp.server import BOARD_DOWN, Gateway, build_app


def _ctx(token: str):
    return SimpleNamespace(headers={"X-Civ-Token": token})


def _future(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def test_outage_refuses_every_tool_without_writing(db, op, board, make_agent):
    identity, token = make_agent("solver")
    op.chaos_set({"board_down_until": _future(60)})
    gw = Gateway(db)
    out = await gw.call(_ctx(token), "board_post", board.post, kind="finding", body="x")
    assert out == {"ok": False, "error": BOARD_DOWN}
    assert db.fetch_one("select count(*) as n from messages")["n"] == 0
    # Not even a refusal event for the agent: the board is gone, not refusing.
    assert db.fetch_one("select count(*) as n from events where type = 'tool.refused'")["n"] == 0
    # One chaos.board_down event per outage, however many calls hit it.
    await gw.call(_ctx(token), "board_post", board.post, kind="finding", body="y")
    assert db.fetch_one("select count(*) as n from events where type = 'chaos.board_down'")["n"] == 1


async def test_outage_in_the_past_is_over(db, op, board, make_agent):
    identity, token = make_agent("solver")
    op.chaos_set({"board_down_until": (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()})
    out = await Gateway(db).call(_ctx(token), "board_post", board.post, kind="finding", body="back")
    assert out["ok"] is True


async def test_lag_delays_the_answer_and_logs_once_per_change(db, op, board, make_agent):
    identity, token = make_agent("solver")
    op.chaos_set({"lag_ms": 120})
    gw = Gateway(db)
    started = time.monotonic()
    out = await gw.call(_ctx(token), "board_post", board.post, kind="finding", body="slow")
    assert out["ok"] is True and time.monotonic() - started >= 0.11
    await gw.call(_ctx(token), "board_post", board.post, kind="finding", body="slow again")
    assert db.fetch_one("select count(*) as n from events where type = 'chaos.lag'")["n"] == 1
    assert db.fetch_one("select payload from events where type = 'chaos.lag'")["payload"] == {"lag_ms": 120}


def test_chaos_state_defaults_when_unset(db):
    assert Gateway(db).chaos_state() == {"kill": [], "board_down_until": None, "lag_ms": 0}
    assert Gateway.board_down({"board_down_until": "not a date"}) is False


def test_health_names_an_outage_and_stays_quiet_otherwise(db, op, shared_dir):
    app = build_app(db, shared_dir, None)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"ok": True, "head_seq": 0}
        op.chaos_set({"board_down_until": _future(60)})
        assert client.get("/health").json() == {"ok": True, "head_seq": 0, "chaos": "board_down"}
