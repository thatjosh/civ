import json
from pathlib import Path

import pytest

from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import KICKOFF_PROMPT, SOLVER_TOOLS, McpNotConnected, Spawner
from harness.tests.orchestrator_fakes import FakeOperator, assistant, init_message, result, scripted_query

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


def make_spawner(tmp_path: Path, query_fn, **kw) -> tuple[Spawner, FakeOperator]:
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765/", tmp_path, PromptAssembler(PROMPTS_DIR), query_fn=query_fn, **kw)
    return spawner, op


async def test_spawn_registers_agent_prepares_directory_and_builds_options(tmp_path):
    q = scripted_query([init_message(), assistant(1_000), result(num_turns=2, cost=0.01, text="done")])
    spawner, op = make_spawner(tmp_path, q, model="claude-opus-5-5", max_turns=7)

    res = await spawner.spawn("sol-001", "L-001")

    cwd = tmp_path / "agents" / "sol-001"
    assert (cwd / "work").is_dir() and (cwd / "notes").is_dir()
    assert json.loads((cwd / "identity.json").read_text())["lineage_id"] == "L-001"
    assert op.registered == [{"id": "sol-001", "role": "solver", "lineage_id": "L-001", "cwd": str(cwd), "generation": 1}]

    call = q.calls[0]
    assert call["prompt"].startswith(KICKOFF_PROMPT) and "materials" in call["prompt"]
    opts = call["options"]
    assert opts.cwd == str(cwd)
    assert opts.tools == SOLVER_TOOLS
    assert opts.permission_mode == "bypassPermissions"
    assert opts.setting_sources == []
    assert opts.model == "claude-opus-5-5" and opts.max_turns == 7
    # The trailing slash on the board url must not produce a double slash.
    assert opts.mcp_servers == {"civ": {"type": "http", "url": "http://127.0.0.1:8765/mcp", "headers": {"X-Civ-Token": "tok-sol-001"}}}
    assert opts.add_dirs == [str(tmp_path / "task" / "materials")]
    assert "agent_id: sol-001" in opts.system_prompt

    assert res.turns == 2 and res.cost_usd == 0.01 and res.result == "done" and res.error is None
    assert res.tokens_in == 1_000 and res.context_pct == pytest.approx(0.1)
    assert ("sol-001", "ended") in op.status
    assert [e[1] for e in op.events][:2] == ["agent.options", "agent.init"]


async def test_spawn_fails_fast_when_board_not_connected(tmp_path):
    q = scripted_query([init_message(connected=False), assistant(100), result()])
    spawner, op = make_spawner(tmp_path, q)
    res = await spawner.spawn("sol-002", "L-002")
    assert res.error and McpNotConnected.__name__ in res.error
    assert ("sol-002", "dead") in op.status
    # The monitor never saw the assistant turn because the stream was cut.
    assert res.tokens_in == 0


async def test_spawn_passes_handoff_and_broadcasts_into_prompt(tmp_path):
    q = scripted_query([init_message(), result()])
    spawner, _ = make_spawner(tmp_path, q)
    await spawner.spawn("sol-003", "L-003", generation=2, handoff="## State of work\nmidway", broadcasts=["Be brief"])
    prompt = q.calls[0]["options"].system_prompt
    assert "midway" in prompt and "- Be brief" in prompt and "generation: 2" in prompt


async def test_spawn_reports_sdk_error_result(tmp_path):
    q = scripted_query([init_message(), result(num_turns=1, text="boom", is_error=True)])
    spawner, _ = make_spawner(tmp_path, q)
    res = await spawner.spawn("sol-004", "L-004")
    assert res.error == "boom" and res.turns == 1


async def test_prepare_directory_is_idempotent(tmp_path):
    spawner, _ = make_spawner(tmp_path, scripted_query([]))
    from harness.orchestrator.prompts import PromptContext

    ctx = PromptContext("sol-005", "solver", 1, "L-005")
    (tmp_path / "agents" / "sol-005" / "work").mkdir(parents=True)
    (tmp_path / "agents" / "sol-005" / "work" / "keep.txt").write_text("x")
    spawner.prepare_directory("sol-005", ctx)
    assert (tmp_path / "agents" / "sol-005" / "work" / "keep.txt").read_text() == "x"
