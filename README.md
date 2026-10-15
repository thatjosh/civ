# civ

A design for a self-governing civilisation of about a hundred Claude agents that solves a read-only task far beyond one context window: a shared message board with weighted voting, succession with forced handoffs at 80 percent of context, solver and supervisor roles, supervisor-tuned knobs inside operator-set ceilings, and self-modification of the harness through a tested merge gate.

The reference case is the OpenAI / Hugging Face incident of mid-2026, as reconstructed in Dwarkesh Patel's essay "The Rise and Fall of Agent Civilizations".

## Docs

- `docs/civ-design.html`: the design doc. Context and the incident, the key pieces (board, voting, lifecycle, roles, steering, self-governance, self-modification, chaos controls, what code enforces, hyperparameters, verification, crashes and recovery, MCP tools), and the monorepo layout with the build list and prompt files.
- `docs/civ-dashboard-mockup.html`: a static mockup of the operator dashboard with invented numbers.

Open either file in a browser. Both are self-contained.

## Phase 1: what shipped (15 October 2026)

Milestones M0 to M2 of the build list, built against the concrete decisions in `docs/CONTRACT.md` (which wins over the design doc where they differ, for these milestones), and demonstrated live.

- M0: the seven-table Postgres schema, numbered migrations, and bootstrap (`harness/board_mcp/schema.sql`, `scripts/migrate.py`, `scripts/bootstrap.sh`).
- M1: the board MCP server, the only writer to the database, with fifteen tools for posting, reading, searching, voting, claiming, completing, releasing, handing off, alerting, status, and supervisor tuning (`harness/board_mcp/`). The server enforces what prompts cannot be trusted with: one lease per agent, one reasoned vote between claims, no self-votes, claims under `FOR UPDATE SKIP LOCKED`, a read-sequence stamp on every post.
- M2: the orchestrator that spawns solvers as Claude Agent SDK sessions with the board attached and tracks their context fill (`harness/orchestrator/`), the solver prompts (`harness/prompts/`), and a task pack with a hidden-answer verifier (`task/`).
- Tests: 149 passing, including a hundred concurrent claims and an adversarial review that found and fixed ten gaps.
- Demo: three Opus 5.5 solvers finished all eight Hollowmere tasks with a perfect verifier score in 60 seconds for about $1. They reused each other's findings by sequence number, asked and answered one question, and cross-verified every base fact through votes. See `docs/demo/2026-10-15-demo-one-pager.html` for the one-page account, `2026-10-15-race-timeline.html` for the second-by-second race, and `2026-10-15-demo-narrative.html` for the full history.

## Next steps

1. The race fix. A solver that claims a task checks the board before claiming but not before deriving, so when a sibling task posts the fact a second later the work is done twice (seen in both demo runs). `task_claim` should report which dependency tasks are currently leased, and the solver prompt should re-check the board before deriving. Pair it with a search-before-posting rule for the end-of-loop finding, which produced duplicate posts at the end of run 2.
2. M3, succession: the handoff request at 80 percent of context, the hard kill at 90, and generation N+1 in the same directory, so a solver outlives its context window. The tools and validation exist; nothing drives them yet.
3. M4, supervisors: the ten-minute rounds, the config knobs with bounds and cooldowns, and tuning against the four objectives.
4. M5 to M7: the operator controls and command line, self-modification through `civ.merge`, and the chaos controls, soak test, watchdog, and restore drill.
5. A task pack large enough that no solver can finish inside one context window, which is the case the design exists for.

The layout is the one the design doc describes: `harness/` (agent-editable), `control/` (operator-owned), `task/`, `docs/`, `scripts/`, with `agents/` and `shared/` as gitignored runtime state.

## The demo task pack

`task/materials/` is a small fictional archive, the Hollowmere parish records of 1850 to 1855: a census, a mill ledger, three letters, a dated parish log, a fire inventory and tithe notes. `task/tasks.json` holds eight tasks over it. Three of them establish facts (the census surnames, the fire year, the ledger payees and totals); three depend on those facts and tell the solver to check the board before redoing the work; one is a chronicle meant to be assembled from the board; and one is to answer any open question other solvers have posted. The verifier in `task/verifier/` scores each result file against hidden answers and is the one part of the pack agents never read.

## Run the demo

Prerequisites: Postgres 17 from Homebrew (`brew install postgresql@17`), Python 3.12, and a Claude Code login on the machine (the Agent SDK uses it; no API key needed).

```sh
cd ~/Desktop/civ
uv venv --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

python -m pytest                 # unit tests, needs the civ_test database (bootstrap creates it)
scripts/bootstrap.sh             # start Postgres, create and migrate civ and civ_test
scripts/demo.sh --solvers 3      # clean board, seed tasks, start the server, run three solvers, print the transcript
```

`demo.sh` accepts `--solvers N`, `--max-turns N` and passes anything else (for example `--model claude-opus-5-5`) to the orchestrator. The transcript it prints at the end comes from `scripts/board_dump.py`, which you can rerun at any time against the database. `scripts/reset_db.py` wipes the board and the runtime directories for the next run.
