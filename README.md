# civ

A design for a self-governing civilisation of about a hundred Claude agents that solves a read-only task far beyond one context window: a shared message board with weighted voting, succession with forced handoffs at 80 percent of context, solver and supervisor roles, supervisor-tuned knobs inside operator-set ceilings, and self-modification of the harness through a tested merge gate.

The reference case is the OpenAI / Hugging Face incident of mid-2026, as reconstructed in Dwarkesh Patel's essay "The Rise and Fall of Agent Civilizations".

## Docs

- `docs/civ-design.html`: the design doc. Context and the incident, the key pieces (board, voting, lifecycle, roles, steering, self-governance, self-modification, chaos controls, what code enforces, hyperparameters, verification, crashes and recovery, MCP tools), and the monorepo layout with the build list and prompt files.
- `docs/civ-dashboard-mockup.html`: a static mockup of the operator dashboard with invented numbers.

Open either file in a browser. Both are self-contained.

## Status

Design only. Nothing in the build list exists yet. The planned layout is described in the design doc: `harness/` (agent-editable), `control/` (operator-owned), `task/`, `dashboard/`, `docs/`, `scripts/`, with `agents/` and `shared/` as gitignored runtime state.
