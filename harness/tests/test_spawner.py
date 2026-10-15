import json
from pathlib import Path

import pytest

from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import KICKOFF_PROMPT, SESSION_ENV, SOLVER_TOOLS, McpNotConnected, SessionControl, Spawner
from harness.tests.orchestrator_fakes import FakeClient, FakeOperator, assistant, client_factory, init_message, result, scripted_query

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
    # The CLI's own compaction stays off: the handoff is the civilisation's compaction.
    assert opts.env == SESSION_ENV == {"DISABLE_AUTO_COMPACT": "1"}
    assert "agent_id: sol-001" in opts.system_prompt

    assert res.turns == 2 and res.cost_usd == 0.01 and res.result == "done" and res.error is None
    assert res.tokens_in == 1_000 and res.context_pct == pytest.approx(0.1)
    assert res.generation == 1 and not res.stopped_by_runner
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
    # The successor framing comes right before the handoff, and the identity block stays last.
    assert prompt.index("You are a successor") < prompt.index("midway") < prompt.index("agent_id: sol-003")


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


# --- the client-based session, which succession relies on ---------------------


async def test_run_session_connects_with_kickoff_and_always_disconnects(tmp_path):
    client = FakeClient([init_message(), assistant(100), result(num_turns=1)])
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=client_factory([client]))
    res = await spawner.spawn("sol-010", "L-010")
    assert client.connected_with.startswith(KICKOFF_PROMPT)
    assert client.disconnected and not client.interrupted
    assert res.turns == 1 and res.error is None


async def test_hook_can_stop_a_running_session(tmp_path):
    client = FakeClient([init_message(), assistant(100), assistant(200), assistant(300), result()])
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=client_factory([client]))
    seen: list[str] = []

    async def hook(message, control: SessionControl) -> None:
        seen.append(type(message).__name__)
        if len(seen) == 3:
            await control.send("stop please")
            control.request_stop("test")

    res = await spawner.spawn("sol-011", "L-011", on_message=hook)
    # Stopped after the third message: interrupted, disconnected, and the rest of the script never ran.
    assert seen == ["SystemMessage", "AssistantMessage", "AssistantMessage"]
    assert client.sent == ["stop please"] and client.interrupted and client.disconnected
    assert res.stopped_by_runner and res.result == "stopped by runner: test"
    assert res.turns == 2


async def test_spawn_with_token_and_cwd_skips_registration(tmp_path):
    client = FakeClient([init_message(), result()])
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=client_factory([client]))
    cwd = tmp_path / "agents" / "sol-001"
    await spawner.spawn("sol-001-g2", "L-001", generation=2, handoff="## State of work\nx", token="tok-given", cwd=cwd)
    # A successor is registered by Operator.succeed, so the spawner must not register it again.
    assert op.registered == []
    assert client.options.mcp_servers["civ"]["headers"] == {"X-Civ-Token": "tok-given"}
    assert client.options.cwd == str(cwd.resolve())
    assert json.loads((cwd / "identity.json").read_text())["generation"] == 2


async def test_probe_is_asked_every_five_turns(tmp_path):
    usage = {"totalTokens": 5, "maxTokens": 1_000_000, "percentage": 0.0, "isAutoCompactEnabled": False}
    client = FakeClient([init_message()] + [assistant(100)] * 10 + [result()], usage=usage)
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=client_factory([client]))
    await spawner.spawn("sol-012", "L-012")
    assert client.probes == 2
