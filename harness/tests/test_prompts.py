from pathlib import Path

import pytest

from harness.orchestrator.prompts import SOLVER_FILES, PromptAssembler, PromptContext

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"


@pytest.fixture
def assembler() -> PromptAssembler:
    return PromptAssembler(PROMPTS_DIR)


def ctx(**overrides) -> PromptContext:
    base = dict(agent_id="sol-007", role="solver", generation=2, lineage_id="L-007", harness_version="v0.1")
    base.update(overrides)
    return PromptContext(**base)


def test_solver_prompt_orders_files_and_puts_identity_last(assembler):
    prompt = assembler.solver_prompt(ctx())
    positions = [prompt.index((PROMPTS_DIR / name).read_text().strip()[:40]) for name in SOLVER_FILES]
    assert positions == sorted(positions), "solver files must appear in SOLVER_FILES order"
    identity_at = prompt.index("agent_id: sol-007")
    assert identity_at > max(positions)
    # Nothing but the identity block may follow the identity heading, because
    # the identity block is what keeps the cacheable prefix stable.
    assert prompt.rstrip().endswith("Begin.")


def test_identity_placeholders_are_filled(assembler):
    prompt = assembler.solver_prompt(ctx())
    for needle in ("agent_id: sol-007", "role: solver", "generation: 2", "lineage_id: L-007", "harness_version: v0.1"):
        assert needle in prompt
    assert "{agent_id}" not in prompt and "{role}" not in prompt


def test_broadcasts_and_handoff_absent_by_default(assembler):
    prompt = assembler.solver_prompt(ctx())
    assert "Operator broadcasts" not in prompt
    assert "You are a successor" not in prompt


def test_broadcasts_listed_after_preamble_in_order(assembler):
    prompt = assembler.solver_prompt(ctx(broadcasts=["Prefer breadth", "Then depth"]))
    pre = prompt.index("Operator broadcasts")
    first = prompt.index("- Prefer breadth")
    second = prompt.index("- Then depth")
    assert pre < first < second < prompt.index("agent_id: sol-007")


def test_handoff_follows_successor_preamble_and_precedes_identity(assembler):
    prompt = assembler.solver_prompt(ctx(handoff="## State of work\nHalf done."))
    pre = prompt.index("You are a successor")
    body = prompt.index("Half done.")
    assert pre < body < prompt.index("agent_id: sol-007")


def test_solver_prompt_names_the_mcp_tools_and_the_rules(assembler):
    prompt = assembler.solver_prompt(ctx())
    assert "mcp__civ__" in prompt
    assert "task_claim" in prompt and "task_complete" in prompt and "board_vote" in prompt
    assert "task/verifier/" in prompt
    assert "materials directory" in prompt


def test_missing_prompt_file_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        PromptAssembler(tmp_path).solver_prompt(ctx())


def test_no_em_dashes_in_any_prompt_file():
    offenders = [p for p in PROMPTS_DIR.rglob("*.md") if chr(0x2014) in p.read_text()]
    assert offenders == []
