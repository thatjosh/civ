"""Spawns one real solver against a running board. Gated, because it costs
money and needs the board server, Postgres, and the Claude Code login.

    CIV_INTEGRATION=1 python -m pytest harness/tests/test_integration_spawn.py -q
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CIV_INTEGRATION") != "1", reason="set CIV_INTEGRATION=1 to run")

ROOT = Path(__file__).resolve().parents[2]


async def test_one_solver_connects_to_board_and_ends():
    from harness.board_mcp.db import Db
    from harness.board_mcp.operator import Operator
    from harness.orchestrator.prompts import PromptAssembler
    from harness.orchestrator.spawner import Spawner

    operator = Operator(Db(os.environ.get("CIV_DATABASE_URL", "postgresql://127.0.0.1:5432/civ")))
    spawner = Spawner(
        operator,
        os.environ.get("CIV_BOARD_URL", "http://127.0.0.1:8765"),
        ROOT,
        PromptAssembler(ROOT / "harness" / "prompts"),
        max_turns=3,
    )
    res = await spawner.spawn("sol-it-001", "L-it-001")
    assert res.error is None, res.error
    assert res.turns >= 1
    assert res.cost_usd is not None and res.cost_usd > 0
