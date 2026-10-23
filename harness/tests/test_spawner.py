import json
from pathlib import Path

import pytest

from harness.orchestrator.prompts import PromptAssembler
from harness.orchestrator.spawner import KICKOFF_PROMPT, SESSION_ENV, SOLVER_TOOLS, SUPERVISOR_KICKOFF, SUPERVISOR_TOOLS, McpNotConnected, SessionControl, Spawner
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
    assert op.registered == [{"id": "sol-001", "role": "solver", "lineage_id": "L-001", "cwd": str(cwd), "generation": 1, "harness_version": "dev"}]

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
    # Stopped after the third message: interrupted, then the interrupt's own result was
    # drained (and seen by the hook) so cost and turns are real; the rest of the script never ran.
    assert seen == ["SystemMessage", "AssistantMessage", "AssistantMessage", "ResultMessage"]
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


# --- what a runner-stopped session reports ------------------------------------


async def test_runner_stop_drains_the_result_so_cost_and_turns_are_real(tmp_path):
    client = FakeClient([init_message(), assistant(100), assistant(200), assistant(300), result()])
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=client_factory([client]))

    async def hook(message, control: SessionControl) -> None:
        if isinstance(message, type(assistant(1))) and control.stop_reason is None:
            control.request_stop("handoff written")

    res = await spawner.spawn("sol-020", "L-020", on_message=hook)
    # The fake answers the interrupt with a result carrying cost 0.05 and the turns run so far.
    assert client.interrupted and res.stopped_by_runner
    assert res.cost_usd == 0.05 and not res.cost_estimated and res.turns == 1
    assert res.result == "stopped by runner: handoff written" and res.error is None
    ends = [e for e in op.events if e[1] == "agent.ended"]
    assert len(ends) == 1 and ends[0][2]["estimated"] is False and ends[0][2]["cost_usd"] == 0.05
    assert ("sol-020", "ended") in op.status


async def test_runner_stop_without_a_result_estimates_the_cost(tmp_path):
    client = FakeClient(
        [init_message(), assistant(100, model="claude-haiku-5-5"), assistant(200, model="claude-haiku-5-5"), assistant(300)],
        after_interrupt="end",
    )
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=client_factory([client]))

    async def hook(message, control: SessionControl) -> None:
        if getattr(message, "usage", None) and message.usage["input_tokens"] == 100:
            control.request_stop("test")

    res = await spawner.spawn("sol-021", "L-021", on_message=hook)
    # Two turns: input 50 + 100, cache write 25 + 50, cache read 25 + 50, output 10 + 10, Haiku prices.
    expected = (150 * 0.10 + 20 * 0.50 + 75 * 0.01 + 75 * 0.125) / 1_000_000
    assert res.cost_estimated and res.cost_usd == pytest.approx(expected)
    assert res.turns == 2 and res.stopped_by_runner and client.disconnected
    ends = [e for e in op.events if e[1] == "agent.ended"]
    assert len(ends) == 1 and ends[0][2] == {"subtype": "interrupted", "num_turns": 2, "cost_usd": pytest.approx(expected), "estimated": True, "is_error": False}


async def test_runner_stop_that_hangs_times_out_and_estimates(tmp_path):
    client = FakeClient([init_message(), assistant(100), assistant(200)], after_interrupt="hang")
    op = FakeOperator()
    spawner = Spawner(op, "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), client_factory=client_factory([client]), drain_s=0.02)

    async def hook(message, control: SessionControl) -> None:
        if getattr(message, "usage", None):
            control.request_stop("test")

    res = await spawner.spawn("sol-022", "L-022", on_message=hook)
    assert res.cost_estimated and res.cost_usd is not None and res.cost_usd > 0
    assert client.interrupted and client.disconnected and res.error is None


async def test_supervisor_role_gets_its_own_tools_directory_prompt_and_kickoff(tmp_path):
    q = scripted_query([init_message(), assistant(200), result(num_turns=3, cost=0.02, text="round done")])
    spawner, op = make_spawner(tmp_path, q, model="claude-opus-5-5", max_turns=15)
    cwd = tmp_path / "agents" / "sup-001"

    res = await spawner.spawn("sup-001-r1", "S-001", generation=1, role="supervisor", brief="## Round 1\n\nBRIEF BODY", cwd=cwd)

    assert op.registered == [{"id": "sup-001-r1", "role": "supervisor", "lineage_id": "S-001", "cwd": str(cwd.resolve()), "generation": 1, "harness_version": "dev"}]
    assert json.loads((cwd / "identity.json").read_text())["role"] == "supervisor"
    call = q.calls[0]
    assert call["prompt"].startswith(SUPERVISOR_KICKOFF) and "shared" in call["prompt"] and "materials" not in call["prompt"]
    opts = call["options"]
    # Write and Bash arrived with M6, so a supervisor can produce a diff under proposals/.
    assert opts.tools == SUPERVISOR_TOOLS == ["Read", "Grep", "Write", "Bash"]
    assert f"{tmp_path.resolve()}/harness" in call["prompt"]
    assert opts.add_dirs == [str(tmp_path / "shared")]
    assert "BRIEF BODY" in opts.system_prompt and "You are a supervisor" in opts.system_prompt
    assert "You are a solver" not in opts.system_prompt
    assert res.turns == 3 and res.cost_usd == 0.02 and res.error is None
    options_event = next(e for e in op.events if e[1] == "agent.options")
    assert options_event[2]["role"] == "supervisor"


async def test_supervisor_without_a_brief_and_unknown_roles_are_refused(tmp_path):
    spawner, _ = make_spawner(tmp_path, scripted_query([]))
    with pytest.raises(ValueError, match="brief"):
        spawner.prepare("sup-001-r1", "S-001", role="supervisor")
    with pytest.raises(ValueError, match="unknown role"):
        spawner.prepare("x", "L", role="governor")


async def test_solver_role_is_the_default_and_unchanged(tmp_path):
    q = scripted_query([init_message(), result()])
    spawner, op = make_spawner(tmp_path, q)
    await spawner.spawn("sol-009", "L-009")
    assert op.registered[0]["role"] == "solver"
    assert q.calls[0]["options"].tools == SOLVER_TOOLS and q.calls[0]["prompt"].startswith(KICKOFF_PROMPT)


async def test_harness_version_is_read_from_the_version_file_at_every_spawn(tmp_path):
    # A merge bumps harness/VERSION; the next spawn must carry the new value in its identity
    # block without a restart, and a root with no file reads as "dev".
    q1 = scripted_query([init_message(), result(num_turns=1)])
    spawner, op = make_spawner(tmp_path, q1)
    await spawner.spawn("sol-001", "L-001")
    assert json.loads((tmp_path / "agents" / "sol-001" / "identity.json").read_text())["harness_version"] == "dev"
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "VERSION").write_text("0.1.3\n")
    q2 = scripted_query([init_message(), result(num_turns=1)])
    spawner2, _ = make_spawner(tmp_path, q2)
    await spawner2.spawn("sol-002", "L-002")
    assert json.loads((tmp_path / "agents" / "sol-002" / "identity.json").read_text())["harness_version"] == "0.1.3"
    assert "harness_version: 0.1.3" in q2.calls[0]["options"].system_prompt
    (tmp_path / "harness" / "VERSION").write_text("0.1.4\n")
    assert spawner2.harness_version() == "0.1.4"


def test_solver_materials_follow_the_pack_when_one_is_set(tmp_path, monkeypatch):
    # CIV_PACK names a generated pack; its materials/ replaces the repo's task/materials for
    # add_dirs and the kickoff, and the supervisor's shared/ is untouched.
    from harness.orchestrator.monitor import StaticKnobs
    from harness.orchestrator.prompts import PromptAssembler
    from harness.orchestrator.spawner import Spawner
    from harness.tests.orchestrator_fakes import FakeOperator

    pack = tmp_path / "packs" / "soak"
    (pack / "materials").mkdir(parents=True)
    monkeypatch.setenv("CIV_PACK", str(pack))
    spawner = Spawner(FakeOperator(), "http://127.0.0.1:8765", tmp_path, PromptAssembler(PROMPTS_DIR), knobs=StaticKnobs())
    prepared = spawner.prepare("sol-001", "L-001")
    assert prepared.options.add_dirs == [str((pack / "materials").resolve())]
    assert str((pack / "materials").resolve()) in prepared.kickoff
    sup = spawner.prepare("sup-001-r1", "S-001", role="supervisor", brief="# This round's brief\nnothing")
    assert sup.options.add_dirs == [str((tmp_path / "shared").resolve())]
    monkeypatch.delenv("CIV_PACK")
    assert spawner.prepare("sol-002", "L-002").options.add_dirs == [str((tmp_path / "task" / "materials").resolve())]
