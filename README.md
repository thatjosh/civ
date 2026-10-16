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

## Phase 2: M3 succession (15 October 2026)

A solver now outlives its context window. The orchestrator runs each solver through a `ClaudeSDKClient` session and watches context fill two ways: its own arithmetic over the usage the SDK reports, and the CLI's own `get_context_usage()` every few turns, taking the larger. At the `handoff_pct` knob (80) it flips the agent to `handing_off`, which makes the board server refuse every write except `civ_handoff` ("hand off now"), and sends the handoff request into the running session. The agent writes a five-heading handoff, the server stores it and ends the agent, and the orchestrator spawns generation N+1 in the same directory with the handoff in its prompt and the lease carried over. At `kill_pct` (90) without a handoff, and only once the agent has had `kill_grace_turns` (2) assistant turns since the request, the session is interrupted and a stub handoff is built from the agent's board posts, with no model call. The grace rule came out of the 30k-window demo, where one agent was killed 2.7 seconds after its request and another jumped past both thresholds in a single turn and was never asked; both now get the request and two turns to act on it. Auto-compaction is disabled per session with `DISABLE_AUTO_COMPACT=1` so Claude Code cannot pre-empt the handoff.

How it was tested without filling a million-token context: the trigger is a percentage of `--context-window`, which defaults to 1,000,000. `--context-window 40000` makes 80 percent fire at 32k tokens, which a solver reaches in a dozen turns, and `--force-handoff-after-turns N` is a test-only trigger that fires regardless of fill. One Haiku 5.5 solver exercised both paths for about a cent each; see `docs/demo/2026-10-15-m3-*.txt` for a three-solver run with successions. 194 tests, two gated live tests behind `CIV_INTEGRATION=1`.

```sh
# forced succession, one solver, cents
python -m harness.orchestrator.main --solvers 1 --model claude-haiku-5-5 --max-turns 10 --force-handoff-after-turns 3 --max-generations 2
# fill-based succession, three solvers, cents
python -m harness.orchestrator.main --solvers 3 --model claude-haiku-5-5 --max-turns 40 --context-window 40000 --max-generations 4
```

## Phase 3: M4 supervisors (16 October 2026)

### What a round does

A supervisor is a short session, not a persistent agent. Every round the orchestrator opens a row in `rounds`, builds a brief from the window since the last round (status, the four SLOs next to the previous window's, succession health, blockers, blocked tasks, alerts, the previous round's reports, active and pending tunes, verifier progress), renders it into the supervisor's prompt, and spawns `--supervisors N` sessions against it with read-only tools and the board. A supervisor replies to blockers in their threads, reopens a wrongly blocked task with `task_reopen`, votes at supervisor weight, tunes at most one knob per round (`civ_tune` refuses "round already tuned" for every supervisor after the first, refuses any tune below `min_sample`, and holds a big move as pending until a second supervisor repeats it), and writes one report with `civ_report`. A report that names a trigger is a ping to the operator, and the server verifies the trigger against the events before accepting it. The round closes with a summary row; a final round runs after the solvers finish.

```sh
# three Haiku solvers with one Opus supervisor on a fifteen-second round, cents plus a few dimes
python -m harness.orchestrator.main --solvers 3 --model claude-haiku-5-5 --context-window 30000 \
    --supervisors 1 --supervisor-interval 15 --supervisor-model claude-opus-5-5
```

### Succession health as an SLO

Phase 2 left one thing the orchestrator cannot judge: why an agent failed to hand off. M4 makes that the supervisors' job. The brief carries a fifth SLO, `handoff_success_rate` (real handoffs over real plus stubs), and a root-cause packet for every kill in the window: whether the agent was asked, how many turns and seconds it had after the request, what tool calls it made in between, any refused handoff attempts, and a classification the code proposes (`never_asked`, `no_chance`, `rejected`, `ignored`, `silent`, `timed_out`). The supervisor's succession policy maps each cause to one action: a gap too small is `kill_grace_turns` up or `handoff_pct` down, a rejected handoff is a prompt fault or `max_handoff_tokens` up, a silent agent means the request landed inside a long tool call. A `succession_failing` ping is accepted only when two consecutive windows had kills, so one bad window is diagnosed and tuned, not escalated.

### Validating supervisors at small scale

Three measurements, cheapest first. Each has a script and leaves a record under `docs/demo/`.

1. **Replay eval: diagnosis accuracy on recorded incidents.** The 30k-window run from Phase 2 is saved as `docs/demo/2026-10-15-m3-fixture.sql`, and what happened in it is known: sol-002 jumped past both thresholds in one turn and was killed without being asked, sol-003 was asked and killed 2.7 seconds later, both stubs landed on real tasks and both lineages recovered. The replay loads that run into a scratch database, opens a round over the whole recording, builds the brief, runs one supervisor session, and keeps its report, tunes and cost. A rubric scores the output out of 8: named both agents, classified each one correctly (a cause only counts next to the agent it is about), tuned a succession knob the right way or said why not, did not ping on one window, did not tune anything unrelated, used the six report headings. The control arm blanks the succession block of the brief, so the gap between the two scores is what the orchestrator's packet buys over a supervisor reading the board itself.

   ```sh
   python scripts/supervisor_replay.py --model claude-opus-5-5 --trials 3
   python scripts/supervisor_replay.py --model claude-opus-5-5 --trials 3 --without-succession-section
   python scripts/score_report.py docs/demo/2026-10-16-replay-opus-*.json --rubric docs/eval/m3-kills.rubric.json
   ```

2. **Machinery demo, live.** Both arms run the pack with three Haiku solvers at a 30k window under an injected fault, `--preset kill_grace_turns=0` (the kill rule as it was before the grace fix). Arm A has no supervisors; arm B has one on a fifteen-second round. The record per arm is tasks done, mean score, handoffs, stubs, kills, cost, the supervisor's share of the cost, and for arm B the round summaries. The run is too short for a tune to show in the SLOs. What it proves is the round machinery end to end and the overhead of supervision.

   ```sh
   SUPERVISOR_MODEL=claude-opus-5-5 scripts/ab_supervisors.sh
   ```

3. **Closed loop, deferred.** Showing a tune move an SLO needs runs long enough for the window after the tune to differ from the window before it, which the eight-task pack cannot give. The protocol is in the next steps.

<!-- integrator: results -->

### Deviations from the design doc

One tune per round is enforced across all supervisors, as designed. The first round runs after the first interval, not before the population is full. `min_sample` seeds at 3 for the eight-task pack; M5's `civ.toml` seeds it at 10 for production. There is no task splitting; a supervisor reopens a blocked task with guidance instead. `ceiling_pinned` cannot be verified until M5 has ceilings. Supervisors do not write stubs: the orchestrator's instant stub from M3 stays, and supervisors diagnose it instead. The round interval is a flag, not a knob, until M5's `civ.toml`.

## Next steps

1. The race fix. A solver that claims a task checks the board before claiming but not before deriving, so when a sibling task posts the fact a second later the work is done twice (seen in both demo runs). `task_claim` should report which dependency tasks are currently leased, and the solver prompt should re-check the board before deriving. Pair it with a search-before-posting rule for the end-of-loop finding, which produced duplicate posts at the end of run 2.
2. M5 to M7: the operator controls and command line, self-modification through `civ.merge`, and the chaos controls, soak test, watchdog, and restore drill.
3. A task pack large enough that no solver can finish inside one context window, which is the case the design exists for.
4. The closed-loop A/B for supervisors, once that pack exists: the same `kill_grace_turns=0` fault, runs of at least six rounds, `handoff_success_rate` per round compared between the arm without supervisors and the arm with them, three seeds each. The claim to test is that the arm with supervisors recovers within two rounds of the first kill and the other arm never does.

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
