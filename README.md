# civ

A design for a self-governing civilisation of about a hundred Claude agents that solves a read-only task far beyond one context window: a shared message board with weighted voting, succession with forced handoffs at 80 percent of context, solver and supervisor roles, supervisor-tuned knobs inside operator-set ceilings, and self-modification of the harness through a tested merge gate.

The reference case is the OpenAI / Hugging Face incident of mid-2026, as reconstructed in Dwarkesh Patel's essay "The Rise and Fall of Agent Civilizations".

## Docs

- `docs/civ-design.html`: the design doc. Context and the incident, the key pieces (board, voting, lifecycle, roles, steering, self-governance, self-modification, chaos controls, what code enforces, hyperparameters, verification, crashes and recovery, MCP tools), and the monorepo layout with the build list and prompt files.
- `docs/civ-dashboard-mockup.html`: a static mockup of the operator dashboard with invented numbers.

Open either file in a browser. Both are self-contained.

## Build status

M0 to M2 of the build list are in progress, against the concrete decisions in `docs/CONTRACT.md` (which wins over the design doc where they differ, for these milestones).

- M0: schema, migrations, bootstrap (`harness/board_mcp/schema.sql`, `scripts/migrate.py`, `scripts/bootstrap.sh`).
- M1: the board MCP server and its tools (`harness/board_mcp/`), tested against a real `civ_test` database, including the hundred-claims-at-once concurrency test.
- M2: the task pack and verifier (`task/`), the solver prompts (`harness/prompts/`), and the orchestrator that spawns solvers with the board attached (`harness/orchestrator/`).

The layout is the one the design doc describes: `harness/` (agent-editable), `control/` (operator-owned), `task/`, `docs/`, `scripts/`, with `agents/` and `shared/` as gitignored runtime state. M3 onwards (succession, supervisors, the operator CLI, self-modification, chaos controls) is not built.

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
