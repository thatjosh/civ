"""Assemble system prompts from the markdown files under harness/prompts.

The prompt files live in the harness so that agents can propose changes to
them. This module only decides the order and fills the placeholders. The
identity block goes last because everything before it is identical for every
solver, which keeps the cacheable prefix stable across the population.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# The solver prompt is these files in this order, then broadcasts, then the
# handoff, then identity. Order matters for caching, so it is a constant, not
# a directory listing.
SOLVER_FILES = (
    "roles/solver.md",
    "board_etiquette.md",
    "voting_guidelines.md",
    "task_claim_guidelines.md",
)
# The supervisor prompt is the role, the round's policies, then the brief the
# orchestrator assembled, then identity. The policies sit before the brief so
# the cacheable prefix is the same for every supervisor in every round.
SUPERVISOR_FILES = (
    "roles/supervisor.md",
    "supervisor/cron.md",
    "supervisor/tune_policy.md",
    "supervisor/succession_policy.md",
    "supervisor/alert_policy.md",
    "supervisor/report_template.md",
)
BRIEF_HEADING = "# This round's brief"
BROADCAST_PREAMBLE = "broadcast_preamble.md"
SUCCESSOR_PREAMBLE = "successor_preamble.md"
STUB_PREAMBLE = "stub_handoff.md"
IDENTITY = "identity.md"

SEPARATOR = "\n\n"


@dataclass
class PromptContext:
    """Everything that varies per agent when a prompt is assembled."""

    agent_id: str
    role: str
    generation: int
    lineage_id: str
    harness_version: str = "dev"
    broadcasts: list[str] = field(default_factory=list)
    handoff: str | None = None
    # A stub was written by the orchestrator from board posts after a kill, so the
    # successor is told to trust it less than a real handoff.
    handoff_is_stub: bool = False


class PromptAssembler:
    """Builds a system prompt from the prompt files in a fixed order."""

    def __init__(self, prompts_dir: Path | str) -> None:
        self._dir = Path(prompts_dir)

    def _read(self, relative: str) -> str:
        path = self._dir / relative
        # Failing loudly beats sending an agent out with half a prompt.
        if not path.exists():
            raise FileNotFoundError(f"prompt file missing: {path}")
        return path.read_text(encoding="utf-8").strip()

    def solver_prompt(self, ctx: PromptContext) -> str:
        parts = [self._read(name) for name in SOLVER_FILES]
        parts.extend(self._broadcast_block(ctx))

        # A successor gets the preamble and the handoff together, so the
        # framing and the content it frames are adjacent.
        if ctx.handoff:
            framing = [self._read(SUCCESSOR_PREAMBLE)]
            if ctx.handoff_is_stub:
                framing.append(self._read(STUB_PREAMBLE))
            parts.append(SEPARATOR.join(framing) + SEPARATOR + ctx.handoff.strip())

        parts.append(self.identity_block(ctx))
        return SEPARATOR.join(parts) + "\n"

    def supervisor_prompt(self, ctx: PromptContext, brief_markdown: str) -> str:
        """The round's prompt: role and policies, broadcasts, the brief, identity.

        The brief is the orchestrator's reading of the board for this window; it
        goes after the policies so a supervisor reads how to act before what to
        act on, and before identity so the identity block stays last.
        """
        parts = [self._read(name) for name in SUPERVISOR_FILES]
        parts.extend(self._broadcast_block(ctx))
        parts.append(BRIEF_HEADING + SEPARATOR + brief_markdown.strip())
        parts.append(self.identity_block(ctx))
        return SEPARATOR.join(parts) + "\n"

    def _broadcast_block(self, ctx: PromptContext) -> list[str]:
        # Broadcasts are only included when there are some, because the
        # preamble on its own would be a heading over nothing.
        if not ctx.broadcasts:
            return []
        lines = [self._read(BROADCAST_PREAMBLE)]
        lines.extend(f"- {text.strip()}" for text in ctx.broadcasts)
        return ["\n".join(lines)]

    def identity_block(self, ctx: PromptContext) -> str:
        return self._read(IDENTITY).format(
            agent_id=ctx.agent_id,
            role=ctx.role,
            generation=ctx.generation,
            lineage_id=ctx.lineage_id,
            harness_version=ctx.harness_version,
        )
