"""Runs one real lineage with a forced handoff against a running board. Gated,
because it costs money (cents, on Haiku) and needs the board server, Postgres,
and the Claude Code login.

    CIV_INTEGRATION=1 python -m pytest harness/tests/test_integration_succession.py -q -s
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("CIV_INTEGRATION") != "1", reason="set CIV_INTEGRATION=1 to run")

ROOT = Path(__file__).resolve().parents[2]


async def test_forced_handoff_produces_a_handoff_row_and_a_second_generation():
    from harness.board_mcp.db import Db
    from harness.board_mcp.limits import Knobs
    from harness.board_mcp.operator import Operator
    from harness.orchestrator.prompts import PromptAssembler
    from harness.orchestrator.spawner import Spawner
    from harness.orchestrator.succession import LineageRunner

    db = Db(os.environ.get("CIV_DATABASE_URL", "postgresql://127.0.0.1:5432/civ"))
    operator = Operator(db)
    spawner = Spawner(
        operator,
        os.environ.get("CIV_BOARD_URL", "http://127.0.0.1:8765"),
        ROOT,
        PromptAssembler(ROOT / "harness" / "prompts"),
        model="claude-haiku-5-5",
        max_turns=10,
        knobs=Knobs(db),
        force_handoff_after_turns=3,
    )
    request = (ROOT / "harness" / "prompts" / "handoff_request.md").read_text(encoding="utf-8")
    runner = LineageRunner(spawner, operator, request, max_generations=2)

    outcome = await runner.run("L-it-succ", "sol-it-succ")

    assert outcome.error is None, outcome.error
    assert outcome.handoffs + outcome.stubs >= 1
    assert outcome.generations == 2
    row = operator.latest_handoff("L-it-succ")
    assert row is not None and row["from_agent"] == "sol-it-succ" and row["to_agent"] == "sol-it-succ-g2"
    head = db.fetch_one("select id, generation, lineage_id from agents where id = 'sol-it-succ-g2'")
    assert head and head["generation"] == 2 and head["lineage_id"] == "L-it-succ"
    # A real handoff beats a stub; print which one happened so a run that fell back is visible.
    print(f"\nhandoffs={outcome.handoffs} stubs={outcome.stubs} cost={outcome.cost_usd}")
