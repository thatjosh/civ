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

### Results (16 October 2026)

**Replay eval.** Six Opus supervisor sessions over the recorded 30k-window run, three with the succession packet in the brief and three without it. Every trial is an independent round 1 over a fresh load of the fixture (the first batch ran its trials as rounds 2 and 3 of one database and the later supervisors simply agreed with the first report, so that batch was discarded). Scores against `docs/eval/m3-kills.rubric.json`, out of 8; the name and classification checks look only inside the report's Succession and Diagnosis sections.

| arm | trial 1 | trial 2 | trial 3 | cost for three |
|---|---|---|---|---|
| with the packet | 8 | 8 | 8 | $0.58 |
| without the packet (control) | 6 | 6 | 6 | $0.68 |

The two points of difference are the two classification items, which is what the packet exists for. All three packet-arm supervisors confirmed sol-002 as `never_asked`, and all three overrode the heuristic on sol-003 from `no_chance` to the same root cause: the request landed at 87 percent, one turn had carried the context across both thresholds, and no grace could have helped. Two of the three lowered `handoff_pct` by one step naming `handoff_success_rate` with n=5; the third explained that with no solvers live a tune would expire unused and said what it would have tuned. None pinged on a single window. The control-arm supervisors could see `handoff_success_rate` at 0.6 in the SLO table and the generation-3 lineages on the board, said the succession counters disagreed with the SLO and asked for the packets, and left the knobs alone: the right behaviour given what they were shown, and no diagnosis.

Three things the replayed supervisors said about the harness were taken: `completions_per_mtok` no longer counts supervisor tokens (every post-run round had been lowering it), a supervisor vote cast with no solvers live now weighs one instead of zero, and the record of each one's reasoning is kept whole (a 3,600-character report had been cut to a 300-character preview by the long-body rule, and scored 3 of 8 on text that the full report passed). A fourth suggestion is a next step: ask for the handoff early when the last turn's context growth exceeds the remaining gap to `kill_pct`.

The replay records are `docs/demo/2026-10-16-replay-opus-{1,2,3}.json` and the `-control` files beside them, each with the brief as rendered, the report, the tune events, the refusals and the cost.

**Live A/B under the injected fault.** Three Haiku solvers at a 30k window with `kill_grace_turns` preset to 0, once without supervisors and once with one Opus supervisor on a fifteen-second round. Records: `docs/demo/2026-10-16-ab-{A,B}-{run-summary,board-transcript,events}.txt` and `-metrics.json`.

| | A: no supervisors | B: one supervisor |
|---|---|---|
| tasks done, mean score | 8 of 8, 1.000 | 8 of 8, 0.958 |
| handoffs, stubs, kills | 5, 0, 0 | 3, 1, 1 |
| rounds, reports, tunes, pings | | 3, 3, 0, 0 |
| solver cost | $0.05 | $0.04 |
| supervisor cost | | $0.68 (94 percent of the bill) |
| wall time | 86 s | 157 s |

The fault is not deterministic: arm A drew no kill this time (an earlier arm A run, lost to a script bug, had one), arm B drew one. The machinery held end to end. Round 1 read the brief, saw the operator preset in the knobs section, predicted `no_chance` kills, voted three dependency findings up at weight 3 (three live solvers, one supervisor), and declined to tune with n=0. Round 2's window held the kill plus three real handoffs (n=4, above `min_sample`): the supervisor confirmed the packet's `ignored` classification from the evidence (asked at 83 percent, three turns and two `board_read` calls after the request, no `civ_handoff`, killed 1.7 seconds later), replied to a solver's alert about the T-005 convention with a finding in its thread, and chose not to tune because an `ignored` kill is a prompt fault, not a knob, and overriding an operator preset on one incident would be wrong. It tried to post the prompt fix as a proposal and was refused twice, because a proposal must carry a diff file and supervisors cannot write files; it put the change in its report instead, and it has been applied to `handoff_request.md` (the first tool call after the request must be the handoff). It tried `task_reopen` on a done task and was refused. Round 3 wrote the post-mortem and sent no ping, correctly: the trigger needs kills in two consecutive windows.

What this costs: an Opus supervisor round over this brief is 6 to 10 turns and $0.18 to $0.30, so at this scale supervision is the bill. At the design's scale (95 solvers on Opus, five supervisors every ten minutes) the same rounds are a few percent of solver spend. Two gaps for the next milestone came out of arm B: supervisors need a way to carry a proposal (a diff written to their own directory, which M6's merge gate then tests), and a grace measured in turns is thin when the turns are sub-second tool calls (sol-001 had three in 1.7 seconds), so the kill rule should also require a floor in seconds.

### Deviations from the design doc

One tune per round is enforced across all supervisors, as designed. The first round runs after the first interval, not before the population is full. `min_sample` seeds at 3 for the eight-task pack; M5's `civ.toml` seeds it at 10 for production. There is no task splitting; a supervisor reopens a blocked task with guidance instead. `ceiling_pinned` cannot be verified until M5 has ceilings. Supervisors do not write stubs: the orchestrator's instant stub from M3 stays, and supervisors diagnose it instead. The round interval is a flag, not a knob, until M5's `civ.toml`.

## Phase 4: M5, the operator's controls (17 October 2026)

### The box and the hands

Everything built through M4 runs inside a box; M5 is the box and the operator's hands on it. Two operator-owned files sit outside the harness: `control/ceilings.toml` caps what the system may ever do (live agents, tokens per minute, spend per day, handoff size, the default model), and `control/civ.toml` gives it its launch-time shape (population counts, round cadence, lifecycle thresholds, voting decay, governance constants; the keys that are also knobs are seeded into the config table at launch). The orchestrator's governor reads the ceilings before every spawn and waits while the system is paused, at the agent ceiling or over the token ceiling, and halts the run at the spend ceiling; the board server reads the ceilings when validating a tune, so a knob can never be set above one even by two supervisors acting together. An epoch counter in `control_state` is stamped on every agent and event by a database default, so a restore or a reshape draws a visible line. The `civ` command line (`control/cli/civ.py`, runnable as `python -m control.cli.civ`) imports nothing from the harness, because it is the one channel that must keep working when the harness is broken: `say`, `vote` (pinned, must-acknowledge, with `--report` naming who never acknowledged), `inbox`, `status`, `pause`, `resume`, `reshape` and `restore`.

### Restore is a succession for the whole population

A VM loss, from the database's side, is agents still marked live and leases still held. `civ restore --yes` bumps the epoch, writes a stub handoff for every lineage whose head agent was mid-flight and marks it dead, returns every leased task to the queue without charging an attempt, resets the shape-seeded knobs and clears every tune, and posts a broadcast naming the restore time and the minutes of activity lost. The orchestrator restarted with `--resume` then spawns generation N+1 for each of those lineages from its stub, exactly as it does after a kill, and fills any empty slots up to the shape's population. Nothing new had to be invented for this: it is the M3 kill path applied to everyone at once.

### Live checks

1. `civ status` and `civ inbox` against the board after an M4 run.
2. A Haiku demo with `civ pause` issued ten seconds in: no new spawns while paused (`governor.wait` events), successions resume after `civ resume`.
3. `civ say` and `civ vote` during a run: the broadcast arrives in every live session's next tool result, the vote is acknowledged, `civ vote --report <id>` lists who missed it.
4. The restore drill: `KILL_AFTER_S=25 scripts/restore_drill.sh`. Expected: two epochs in the events, every mid-flight lineage resumed at generation N+1 from a stub, eight of eight done.

```sh
python -m control.cli.civ status
python -m control.cli.civ say "Check the 1853 fire year against the parish log before building on it."
python -m control.cli.civ vote 17 +1 --reason "the convention the verifier uses"
python -m control.cli.civ inbox
KILL_AFTER_S=25 scripts/restore_drill.sh
```

### Results (17 October 2026)

All four live checks ran on three Haiku solvers at a 30k window, for cents. Records: `docs/demo/2026-10-17-m5-controls-*.txt` (checks 1 to 3) and `docs/demo/2026-10-17-restore-drill-*.txt` (check 4).

1. **Status and inbox.** `civ status` on the database left by the M4 A/B showed the epoch, the knob table with the operator preset marked, spend against the ceiling and the shape; `civ inbox` printed the three supervisor reports, pings first.
2. **Pause and resume.** `civ pause` ten seconds in: the next successor spawn waited at the governor (`governor.wait`, reason paused) and started 1.6 seconds after `civ resume`. The token-per-minute ceiling also tripped once on its own (3,001,821 against 3,000,000) and held a spawn for six seconds.
3. **Say and vote.** A broadcast reached seven sessions on their next tool result. A pinned vote was acknowledged by eight agents; `civ vote --report` named the two that never did, and the events showed why: both had been asked to hand off in the same second the vote was pinned, and were killed three and six seconds later. That was the live checks' one real finding. `civ_handoff` had honoured the pin gate, so a dying agent was being asked to acknowledge a steer it could never act on; the handoff is now exempt from both gates (the ack stays open for an agent that wants to clear the steer).
4. **Restore drill.** The orchestrator was killed 25 seconds in with three leases held and one agent mid-handoff. `civ restore --yes` took one second: epoch bumped, three solver lineages stubbed and marked dead, the supervisor's round retired, three leases released with attempts unchanged, knobs reset, the restore broadcast posted. The orchestrator restarted with `--resume` spawned generation 2 of every lineage from its restore stub, continued the supervisor rounds at 2, 3 and 4 (the final round tuned `handoff_pct` down one step on the stub-heavy window), and finished 8 of 8 at mean score 1.000 in 101 seconds.

The first drill failed twice, which is what a drill is for: the restore had stubbed the supervisor's round as if it were a lineage and the resumed orchestrator tried to resume it as a solver, and the cron restarted its round numbers at one, so `sup-001-r1` collided with the old epoch's. Restore now stubs solver lineages only, the population skips a lineage id it does not own, and the cron continues from the database's last round number. The demo reset also now restores `control_state`, so a clean board starts at epoch 1.

### Deviations from the design doc

Shape is written to `control_state` at start as well as on reshape; `ttl_default_s` is applied by `civ_tune`; the restore does not reset concurrency separately from the shape; per-agent lifetime budgets and per-generation caps are not built (the spend ceiling is the budget); the CLI's restore stub lists seqs rather than the M3 builder's excerpts, because the CLI imports no harness code; `max_live_agents` counts supervisors.

## Phase 5: M6, self-modification (17 October 2026)

### The merge gate

The harness is the agents' own code, and M6 gives them the one slow, gated path to change it. A proposal already carries a diff the server copied into `shared/` by hash. `civ_merge(proposal_id)`, supervisor only, validates and queues it: the message must be a proposal with a `.diff` ref, the diff may only touch files under `harness/`, a round may queue at most two, and a proposal already merged or in flight is refused. The orchestrator's merger does the rest, in a git worktree rather than the live tree: apply the diff, run the harness tests there, and only on green commit, bump `harness/VERSION`, and fast-forward the repository. A red run is recorded with the tail of the test output for the next round's supervisor to read. On a version bump the orchestrator restarts the board server it owns and every new spawn carries the new version in its identity block; agents already running keep theirs until succession, so a bad change can only reach agents spawned after it.

### What agents cannot reach

A diff that touches the spawn path, the governor, the merger itself, the ceilings loader, the auth tokens, or the verifier call path is not merged by an agent. The server records it as queued for the operator, posts an alert so `civ inbox` carries it, and the operator approves or rejects it with `civ merge --approve` or `civ merge --reject`. The list of protected paths lives in `control/civ.toml` under `[merge]`, beside the per-round cap and the test command, so the operator owns the boundary as they own the ceilings.

### Three follow-ups carried by the first merges

The merge path's first cargo is the three fixes the M4 and M5 live checks asked for. A wall-clock floor on the kill grace: the runner kills only when the agent has had both `kill_grace_turns` turns and `kill_grace_s` seconds since the request, because three sub-second tool calls are not a chance to hand off. A growth-aware handoff trigger: when the last turn's context growth would carry the agent past `kill_pct`, the handoff fires early rather than waiting for `handoff_pct`, which is the case the replayed supervisors diagnosed twice. And supervisors able to write a diff: they get `Write` and `Bash` for a `proposals/` directory of their own, so a prompt fault found in a round can leave as a proposal instead of a paragraph in a report.

### Validation

Two halves, no model in the first.

1. **Merge drill.** Three prepared diffs under `docs/eval/proposals/`: a one-line wording change to the handoff request (should merge), a change to the stub builder that breaks a harness test (must not), and a comment in the spawner (protected, must wait for the operator). The drill seeds them as a solver's proposals, calls `civ_merge` on each as a registered supervisor, runs the merger once, approves the protected one as the operator, runs the merger again, and prints the merges table, the version and the git log before reverting its own commits.

   ```sh
   scripts/merge_drill.sh
   ```

2. **A supervisor merges on its own.** One Opus supervisor round over a brief that lists the wording proposal as pending, to see it open the ref, judge it, and call `civ_merge` itself. The record is its report and the merges row.

### Results (17 October 2026)

**The drill, no model.** Three prepared proposals from `docs/eval/proposals/` went through the gate on a throwaway branch (`scripts/merge_drill.sh`; record in `docs/demo/2026-10-17-merge-drill-*.txt`):

| proposal | server | merger |
|---|---|---|
| one-line wording change to the handoff request | queued | tests green, merged, `0.1.0` to `0.1.1`, one commit |
| a change to the stub builder that breaks one test | queued | tests red, failed with the pytest tail, tree untouched |
| a comment in the spawner (protected path) | queued for the operator, alert in the inbox | after `civ merge --approve`: merged, `0.1.1` to `0.1.2` |

The drill reverted its two commits and the version returned to `0.1.0`. It took four runs to pass, and each failure was a real gap: the round cap of two counted the still-queued second merge, so the protected case had to be queued after the merger's first pass; the merger's own protected-path re-check refused the operator-approved merge, so the approval marker is now defined once in `merge_rules` and honoured by the merger; and the drill, like the merger, refuses a dirty `harness/` tree, which caught uncommitted fixes twice.

**Live, one Opus supervisor over a pending proposal.** One Haiku solver at a 30k window, the supervisor on a 25-second round with the wording proposal in its brief, the merger and version watcher running and the orchestrator owning the board server (`--serve-board`). The supervisor read the proposal, opened the diff under `shared/`, and declined to merge it: the new sentence said a successor reads the handoff and not the board, which contradicts `successor_preamble.md` (read the handoff, then the thread, and prefer the board where they differ). It voted the proposal down at weight 2 and explained the contradiction in its report. No merge happened, which is the right outcome: the "green" diff passes the tests and is still wrong, and that judgement is what the supervisor adds to the gate. Three rounds cost $0.63; records in `docs/demo/2026-10-17-merge-live-*.txt`.

The live run found two more bugs, both fixed: every agent row showed version `dev` because the register call did not pass the version the identity block already carried (successors copied their predecessor's); and the orchestrator dropped the `[merge]` section of `civ.toml` when writing the shape, which is where the server's merge rules read it.

### Deviations from the design doc

The server queues and the orchestrator merges, where the design has the tool do it synchronously. The merge gate refuses when the working tree under `harness/` is dirty. The board server is hot-reloaded only when the orchestrator owns it (`--serve-board`); the orchestrator's own code is not reloaded until restart. Protected paths live in `control/civ.toml` rather than being hard-coded. There is one OS user, so "under the service user" is the same user.

## Phase 6: M7, a crowd, over time (23 October 2026)

### The soak pack

Eight tasks finish in two minutes, which proves the machinery and nothing else. `task/generate_pack.py` writes a deterministic synthetic parish archive in the Hollowmere style, twenty years of ledgers, parish logs, letters and a census every five years, with the facts generated first and the prose written from them, so every answer is exact and the verifier scores the generator's own reference results at 1.0. A pack lives at `task/packs/<name>/` with its own materials, tasks and answers, and `CIV_PACK` points the server, the orchestrator and `seed_tasks.py --pack` at it. At a 100k-token window a solver hands off several times before the pack is done.

### Chaos, heartbeat, watchdog

Three faults the operator can inject mid-run, all rows in `control_state.chaos` so they survive a restart of either process: `civ chaos kill <agent | random-solver>` (the orchestrator carries it out through the M3 kill path, so the successor starts from a stub), `civ chaos board-down <seconds>` (the server answers every tool with "board unavailable" and writes nothing, while `/health` keeps answering and says why), and `civ chaos lag <ms>`. The orchestrator writes a heartbeat row every thirty seconds and `civ status` prints its age first, because a stale heartbeat is the alarm. `scripts/watchdog.sh` is not part of the stack: every interval it checks the heartbeat, the health endpoint and `pg_isready`, logs one line, pages the operator through a desktop notification, and with `--restart` brings a dead orchestrator back the only way SDK sessions allow: `civ restore --yes`, then the orchestrator relaunched from the argument list it wrote at start, with `--resume`.

### Backups and restore

`scripts/backup.sh` takes a base backup: a `pg_dump` of the database, an `rsync` of `shared/`, the control files and a manifest. `scripts/verify_shared.py` checks every shared file against the hash in its name and every reference the database makes to one. `scripts/restore_from_backup.sh <dir>` recreates the database from the dump, puts `shared/` back, verifies it, and runs `civ restore --yes`; the next step is the orchestrator with `--resume`.

### The soak

```sh
python task/generate_pack.py --years 20 --start 1850 --seed 7 --out task/packs/soak-20y
python scripts/soak.py --pack task/packs/soak-20y --minutes 15 --solvers 6 --model claude-haiku-5-5 \
    --supervisors 1 --supervisor-model claude-opus-5-5 --supervisor-interval 180 --context-window 100000 \
    --chaos "kill-solver@120,board-down@300:20,kill-orchestrator@540" --out docs/demo/2026-10-23-soak
```

The soak bootstraps, seeds the pack, starts the watchdog with `--restart` and the orchestrator with `--serve-board`, runs the chaos schedule by the clock (the orchestrator is killed with SIGKILL at nine minutes and the soak waits for a heartbeat from a new pid, recording the gap), stops at the deadline or when the pack is done, and writes a summary, the events, and a dashboard rendered from the database by `scripts/dashboard.py` (one static page, inline SVG, the mockup's warm palette: heartbeat and epoch boundaries, live agents and tasks done over time, handoff success per round with stubs and kills marked, tokens per minute, the knob timeline, chaos markers, the inbox, spend by role), with a copy on the Desktop.

### Results (23 October 2026)

Six soak runs, each on the twenty-year pack (56 tasks, 159k characters of materials) with Haiku solvers at a 100k window and one Opus supervisor, for about half a dollar each. The first four found something and were fixed; the last two are the record (`docs/demo/2026-10-23-soak-*`, dashboard copy at `~/Desktop/civ-soak-dashboard.html`).

| run | what it found |
|---|---|
| 1 | every lineage ended at the demo's 40-turn cap before a handoff, and one solver stopped dead between two server rules: at its hourly vote limit it could not vote, and without a vote it could not claim. The soak now passes a 400-turn budget; the claim quota is waived while the rate limit holds, with an event. |
| 2 | the orchestrator kill and the watchdog restore worked (back in 39 s, epoch 2, every lineage resumed from a restore stub), but the supervisor never ran: the governor's token ceiling (3M a minute, counting every token the model reads per turn, cache hits included) held its spawn for a minute until the kill. The small-scale ceiling is now 30M and the soak summary names every governor wait. |
| 3 | the supervisor ran and tuned `handoff_pct` down a step; the pack finished before the orchestrator kill was due; two sessions asked to hand off as the queue emptied finished on their own and their successors spawned to claim nothing. No successor is spawned into an empty queue now. |
| 4 | all three faults in one run, restore in 41 s; the round open at the loss was never closed and the soak's early stop cut the final round. Restore closes open rounds as lost; the soak drains before stopping. |
| 5 | the drain ended at once because the soak held no handle to the relaunched orchestrator. Fixed. |
| 6 | clean: chaos kill at 61 s, board down 20 s at 122 s, orchestrator killed at 182 s and back in 41 s in epoch 2, 56 of 56 done at mean score 1.000, two supervisor rounds including the post-mortem, no governor waits. |

**What the final run's supervisors did.** The round-1 supervisor read its brief and found a bug in the brief itself: the succession section counted every refused tool call as a handoff refusal, so a claim-quota refusal and a self-vote refusal appeared under that heading. It wrote a one-line SQL diff for `slo.py` into its own directory and posted it as a proposal with the evidence. The final-round supervisor, in epoch 2 after the restore, read the diff, checked the hunk against the file, and queued it with `civ_merge`, voted on a solver's self-correction, declined to tune (no SLO over `min_sample` in a drained window) and declined to ping. The merge gate then refused the diff: a test pinned the old counting. The test was the thing that was wrong, but so was the diff by a little: it dropped the lockout refusals that are the real signal of an agent ignoring its request. The operator landed the combined rule (refused `civ_handoff` attempts plus writes refused under the lockout) with the proposal credited in the commit. That exchange is the design working end to end: an agent found a fault in its own instruments, another reviewed it, code refused it until the tests agreed, and a human resolved the disagreement.

The merger also left that merge queued until the operator ran it, because it drained when the population ended, before the final round that queued it; the merger now runs as the loop released last.

**Deviations to note from the run.** The pack is small for a soak: three Haiku solvers finish it in six minutes, so the chaos schedule is compressed into the first three. A fifteen-minute run with the design's hundred agents needs a pack an order of magnitude larger, which the generator can produce (`--years 200`). The token ceiling counts cache reads, which makes its number large and its meaning "load", not "spend"; the spend ceiling is the cost bound.

### Deviations from the design doc

The watchdog pages through a desktop notification or stderr, not email. Restart goes through `civ restore` and `--resume` because SDK sessions cannot be reattached: the design's "resumes the sessions it finds" becomes "resumes the lineages it finds". Write-ahead log archiving is not configured, so backups are base backups only and the restore drill shows the loss window. Chaos is three controls (kill, outage, lag), not a general fault menu. The dashboard is a generated static page, not the live app of the follow-up document.

## Next steps

1. The race fix. A solver that claims a task checks the board before claiming but not before deriving, so when a sibling task posts the fact a second later the work is done twice (seen in both demo runs). `task_claim` should report which dependency tasks are currently leased, and the solver prompt should re-check the board before deriving. Pair it with a search-before-posting rule for the end-of-loop finding, which produced duplicate posts at the end of run 2.
2. A real verifier for a real task, which the design leaves to the builder: the soak pack's verifier is exact by construction, and the techniques in the design's verification section (delayed scoring, re-verification, rubric rotation) are untouched.
3. Supervisor follow-ups from the first live rounds: a wall-clock floor on the kill grace alongside the turn count; a growth-aware handoff trigger that asks early when the last turn's context growth exceeds the remaining gap to `kill_pct`; a way for a supervisor to carry a proposal without a diff file, or a writable directory of its own for M6.
4. The closed-loop A/B for supervisors, now that a pack exists: the same `kill_grace_turns=0` fault, runs of at least six rounds, `handoff_success_rate` per round compared between the arm without supervisors and the arm with them, three seeds each. The claim to test is that the arm with supervisors recovers within two rounds of the first kill and the other arm never does.

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
