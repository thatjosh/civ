from pathlib import Path

import pytest

from harness.orchestrator.prompts import BRIEF_HEADING, SOLVER_FILES, SUPERVISOR_FILES, PromptAssembler, PromptContext

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


def sup_ctx(**overrides) -> PromptContext:
    base = dict(agent_id="sup-001-r2", role="supervisor", generation=2, lineage_id="S-001", harness_version="v0.1")
    base.update(overrides)
    return PromptContext(**base)


def test_supervisor_prompt_orders_policies_then_brief_then_identity(assembler):
    prompt = assembler.supervisor_prompt(sup_ctx(), "## Round 2\n\nthe brief body")
    positions = [prompt.index((PROMPTS_DIR / name).read_text().strip()[:40]) for name in SUPERVISOR_FILES]
    assert positions == sorted(positions), "supervisor files must appear in SUPERVISOR_FILES order"
    brief_at = prompt.index(BRIEF_HEADING)
    assert max(positions) < brief_at < prompt.index("the brief body") < prompt.index("agent_id: sup-001-r2")
    assert "role: supervisor" in prompt and "generation: 2" in prompt and "lineage_id: S-001" in prompt
    assert prompt.rstrip().endswith("Begin.")


def test_supervisor_prompt_puts_broadcasts_before_the_brief(assembler):
    prompt = assembler.supervisor_prompt(sup_ctx(broadcasts=["Prefer breadth"]), "brief")
    assert prompt.index("Operator broadcasts") < prompt.index("- Prefer breadth") < prompt.index(BRIEF_HEADING)


def test_supervisor_prompt_names_its_tools_and_its_limits(assembler):
    prompt = assembler.supervisor_prompt(sup_ctx(), "brief")
    for needle in ("civ_tune", "civ_report", "task_reopen", "board_vote", "round done", "succession_failing",
                   "never_asked", "no_chance", "handoff_success_rate", "## Diagnosis", "## For the operator", "min_sample"):
        assert needle in prompt, needle
    assert "Never call `task_claim`" in prompt


def test_supervisor_files_are_no_longer_placeholders():
    for name in SUPERVISOR_FILES + ("supervisor/human_ping_template.md", "supervisor/proposal_review.md"):
        text = (PROMPTS_DIR / name).read_text()
        assert "placeholder" not in text.lower(), name
