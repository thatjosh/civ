# civ build contract for M0 to M2

This file is the agreement between the three workstreams. If the design doc (`docs/civ-design.html`) and this file disagree, this file wins for M0 to M2, because it records the concrete decisions made against the real environment. Deviations from the design doc are listed at the end.

## Environment (verified)

- Repo: `~/Desktop/civ`. Python 3.12 venv at `.venv`. Activate with `source .venv/bin/activate`.
- Installed: `claude-agent-sdk` 0.2.167, `mcp` 2.3.0 (note: 2.x renamed FastMCP to `MCPServer`, import `from mcp.server.mcpserver import MCPServer, Context`), `psycopg` 3.3 with pool, `uvicorn`, `httpx`, `pytest`, `pytest-asyncio` (asyncio_mode = auto in `pyproject.toml`).
- Postgres 17 binaries at `/opt/homebrew/opt/postgresql@17/bin` (add to PATH). Server running on `127.0.0.1:5432`, data dir `/opt/homebrew/var/postgresql@17`, started with `pg_ctl -D /opt/homebrew/var/postgresql@17 -l /opt/homebrew/var/postgresql@17/server.log start`. Databases `civ` and `civ_test`. No password, local trust auth.
- Env vars: `CIV_DATABASE_URL` (default `postgresql://127.0.0.1:5432/civ`), `CIV_TEST_DATABASE_URL` (default `postgresql://127.0.0.1:5432/civ_test`), `CIV_ROOT` (default: repo root), `CIV_BOARD_URL` (default `http://127.0.0.1:8765`).
- Run tests: `cd ~/Desktop/civ && source .venv/bin/activate && python -m pytest`.
- Claude Agent SDK auth: works through the Claude Code login on this machine, no API key. Verified with a one-turn `query()`; `ResultMessage.total_cost_usd` and `usage` are populated. Model id `claude-opus-5-5`.
- Rules for every workstream: do not `git commit` (the orchestrator commits). Do not edit files owned by another workstream. No em dashes anywhere, including comments and prompts. Comments explain why ("because X, we do Y"), classes encapsulate state, keep it concise. Tests run against the real `civ_test` database where a database is needed, and must leave it clean (truncate in a fixture).

## Ownership

| Workstream | Owns | Depends on |
|---|---|---|
| WS-A (M0 + M1) | `harness/board_mcp/**`, `harness/tests/test_board_*.py`, `test_task_*.py`, `test_civ_*.py`, `test_schema*.py`, `test_concurrency*.py`, `scripts/db_init.sh`, `scripts/migrate.py` | nothing |
| WS-B (M2 orchestrator) | `harness/orchestrator/**`, `harness/prompts/**`, `harness/tests/test_prompts*.py`, `test_spawner*.py`, `test_monitor*.py`, `test_main*.py` | schema and tool contract below |
| WS-C (task pack + demo) | `task/**`, `scripts/bootstrap.sh`, `scripts/seed_tasks.py`, `scripts/reset_db.py`, `scripts/board_dump.py`, `scripts/demo.sh`, `README.md` build section | schema, `Operator` API below |

## MCP tool names

Claude Code exposes MCP tools as `mcp__<server>__<tool>`, and dots are not safe in tool names, so the dotted names in the design doc become underscored: `board_post`, `board_read`, `board_top`, `board_search`, `board_vote`, `board_ack`, `task_claim`, `task_heartbeat`, `task_complete`, `task_release`, `civ_handoff`, `civ_alert`, `civ_status`, `civ_tune`, `civ_report`. The server name is `civ`, so the agent sees `mcp__civ__board_post` and so on. `civ_merge` is M6 and is not built.

## Schema (WS-A implements exactly; WS-B and WS-C code against it)

`harness/board_mcp/schema.sql` is canonical; `migrations/0001_init.sql` applies it. All timestamps `timestamptz`.

```sql
agents (
  id text primary key,                 -- e.g. 'sol-003'
  role text not null check (role in ('solver','supervisor')),
  generation int not null default 1,
  lineage_id text not null,            -- stable across successions
  harness_version text not null default 'dev',
  status text not null default 'live' check (status in ('live','handing_off','ended','dead')),
  cwd text not null,
  token text not null unique,          -- spawn token, sent as X-Civ-Token
  tokens_in bigint not null default 0,
  tokens_out bigint not null default 0,
  context_pct real not null default 0,
  spawned_at timestamptz not null default now(),
  ended_at timestamptz
)
messages (
  id bigserial primary key,            -- the sequence number; the API returns it as both id and seq
  author_id text not null,             -- an agents.id or the literal 'operator'
  kind text not null check (kind in ('task','finding','question','blocker','proposal','handoff','broadcast','alert','report')),
  thread_id bigint references messages(id),   -- the thread root; null for a root (a task root or a broadcast)
  parent_id bigint references messages(id),
  body text not null,
  refs text[] not null default '{}',
  read_seq bigint,                     -- stamped by the server from session_reads, never supplied by the agent
  created_at timestamptz not null default now()
)
votes (
  message_id bigint not null references messages(id),
  voter_id text not null,              -- agents.id or 'operator'
  value smallint not null check (value in (-1, 1)),
  weight real not null,                -- fixed at vote time
  reason text not null,
  created_at timestamptz not null default now(),
  primary key (message_id, voter_id)
)
tasks (
  id text primary key,                 -- e.g. 'T-001'
  thread_id bigint not null references messages(id),   -- the kind=task root message
  title text not null,
  description text not null,
  status text not null default 'open' check (status in ('open','leased','done','blocked')),
  priority_base real not null default 0,
  lease_owner text references agents(id),
  lease_expires timestamptz,
  attempts int not null default 0,
  next_attempt_at timestamptz not null default now(),
  result_ref text,                     -- path under shared/
  score real,                          -- from the verifier
  notes text,                          -- from the verifier
  completed_by text,
  completed_at timestamptz
)
config (
  knob text primary key,
  value double precision not null,
  min double precision not null, max double precision not null, step double precision not null,
  cooldown_s int not null default 1200, ttl_s int,
  set_by text, reason text, set_at timestamptz not null default now(), expires_at timestamptz
)
events (
  seq bigserial primary key,
  ts timestamptz not null default now(),
  agent_id text,
  type text not null,                  -- e.g. 'tool.board_post', 'agent.spawned', 'agent.turn', 'task.claimed'
  payload jsonb not null default '{}'
)
handoffs (
  id bigserial primary key,
  lineage_id text not null, from_agent text not null, to_agent text,
  generation int not null, body text not null, stub boolean not null default false,
  created_at timestamptz not null default now()
)
-- server bookkeeping (allowed by the design: all state lives in Postgres)
session_reads (agent_id text primary key, last_seq bigint not null default 0, updated_at timestamptz)
broadcast_seen (agent_id text primary key, last_seq bigint not null default 0)
operator_pins (message_id bigint primary key references messages(id), pinned_until timestamptz not null)
operator_acks (message_id bigint, agent_id text, acked_at timestamptz default now(), primary key (message_id, agent_id))
```

Config seeds (inserted by migration 0001): `post_rate_per_h` 30 [5,120,5], `vote_rate_per_h` 20 [5,60,5], `vote_quota` 1 [0,3,1], `lease_length_s` 1800 [300,7200,300], `max_attempts` 3 [1,6,1], `backoff_base_s` 60 [10,600,10], `top_k` 10 [5,50,5], `long_body_chars` 2000 [500,8000,500], `decay_offset_h` 2 [1,12,1], `decay_power` 1.5 [1,3,0.25], `handoff_pct` 80 [50,90,5], `kill_pct` 90 [60,95,5], `max_handoff_tokens` 4000 [1000,8000,500].

## Tool contract

Every tool returns a JSON-serialisable dict. A rule refusal is returned, not raised, as `{"ok": false, "error": "<one sentence the agent can act on>"}`. Success is `{"ok": true, ...}`. Every response also carries `"head_seq": <max messages.id>` and, when there is something unseen, `"notices": [...]` (see Notices). Bugs raise normally.

Identity: the HTTP header `X-Civ-Token` is mapped to an `agents` row (status `live` or `handing_off`). Missing or unknown token: every tool returns `{"ok": false, "error": "unknown agent token"}`. Supervisor-only tools return `{"ok": false, "error": "supervisor only"}` for solvers.

Message shape (used by read, top, search): `{"id", "seq", "author_id", "kind", "thread_id", "parent_id", "body", "refs", "score", "votes_up", "votes_down", "created_at"}`.

- `board_post(kind, body, thread_id=None, parent_id=None, refs=None)` -> `{"ok", "id", "seq"}`. Solvers may post `finding | question | blocker | proposal`; `alert` goes through `civ_alert`, `report` and `handoff` through their tools; `task` and `broadcast` are operator-only (refused). Server stamps `read_seq` from `session_reads`. Rate limited per `post_rate_per_h`. A body longer than `long_body_chars` is written to `shared/msg-<sha256[:16]>.md`, the stored body becomes the first 300 chars plus ` [full text: <ref>]`, and the ref is appended to `refs`. A `proposal` must carry at least one ref (the diff path) or is refused. `thread_id` must reference an existing root (a task root) when given; a post with `parent_id` but no `thread_id` inherits the parent's thread.
- `board_read(thread_id=None, kind=None, since_seq=None, limit=20)` -> `{"ok", "messages": [...], "head_seq"}`. With `thread_id` or `since_seq`: ascending by seq. With only `kind`: descending by score. Pinned operator-vote messages come first in any read. `limit` capped at 50. Updates `session_reads.last_seq = head_seq`.
- `board_top(kind=None, window_hours=24, limit=10)` -> same shape, descending by score within the window. Updates `session_reads`.
- `board_search(query, limit=10)` -> same shape; Postgres full text (`to_tsvector('english', body)` plus vote reasons via a join) with ILIKE fallback for short queries. Updates `session_reads`.
- `board_vote(message_id, value, reason)` -> `{"ok", "weight"}`. Refused: own message, second vote on the same message, empty reason, `value` not in (-1, 1), over `vote_rate_per_h`. Weight: solver 1.0; supervisor `live_solvers / max(live_supervisors, 1)`; operator votes use the supervisor weight.
- `board_ack(message_id)` -> `{"ok"}`. While an operator pin is pending for this agent (pinned, not expired, not acked by this agent), every write tool except `board_ack` returns `{"ok": false, "error": "acknowledge operator vote on message <id> first"}`; reads still work.
- `task_claim()` -> `{"ok", "task": {"id", "title", "description", "thread_id", "lease_expires", "attempts"}}` or `{"ok": true, "task": null, "reason": "no open tasks"}`. Refused with a reason if the caller already holds a lease, or has cast fewer than `vote_quota` votes since its last claim (the first claim is exempt). Selection: `status='open' and next_attempt_at <= now()`, ordered by `priority_base + thread score` desc, `id` asc, using `SELECT ... FOR UPDATE SKIP LOCKED`. Sets `status='leased'`, `lease_owner`, `lease_expires = now() + lease_length_s`. Also sweeps expired leases back to `open` on every claim.
- `task_heartbeat(task_id)` -> `{"ok", "lease_expires"}`. Refused if the caller does not hold the lease.
- `task_complete(task_id, result_path, summary)` -> `{"ok", "result_ref", "score", "notes"}`. `result_path` is relative to the agent's cwd (absolute paths outside it are refused). Copies the file to `shared/<sha256[:16]>-<basename>`, sets `result_ref`, calls the verifier `verify(task_id, result_abs_path) -> (score, notes)` (the server is given the callable at start; a missing verifier gives score null), marks `done`, posts a `finding` with `summary` into the task thread authored by the caller, writes `completed_by` and `completed_at`, releases the lease. Refused if the caller does not hold the lease.
- `task_release(task_id, reason)` -> `{"ok", "attempts", "next_attempt_at", "status"}`. Bumps `attempts`, posts a `blocker` with the reason in the thread, sets `next_attempt_at = now() + backoff_base_s * 2^(attempts-1)`; at `attempts >= max_attempts` sets `status='blocked'`, else `open`.
- `civ_handoff(body)` -> `{"ok", "handoff_id"}`. Accepted only when the caller's status is `handing_off` (set by the orchestrator in M3; in M2 nothing sets it, but the tool and its validation exist). Validates the five headings (`## State of work`, `## Open threads`, `## What to try next`, `## What not to retry`, `## Files worth reading`) and size (`len(body)/4 <= max_handoff_tokens`), stores the handoff, posts kind `handoff`, keeps the lease (M3 transfers it).
- `civ_alert(body)` -> `{"ok", "id"}`. Posts kind `alert` (no thread).
- `civ_status()` -> `{"ok", "live_agents": {"solver": n, "supervisor": n}, "tokens_per_min": x, "tasks": {"open", "leased", "done", "blocked"}, "you": {"id", "generation", "context_pct", "lease": task_id or null, "votes_since_claim": n}}`.
- `civ_tune(knob, value, slo, reason, ttl_s=3600)` [supervisor] -> `{"ok", "knob", "value", "expires_at"}`. Refused: unknown knob, outside [min, max], more than one step from the current value, inside cooldown, empty reason, `slo` not in `completions_per_mtok | blocked_failed_rate | duplicate_rate | lease_wait`. Records an event with the reason. (Round logic and min_sample are M4.)
- `civ_report(body, trigger=None)` [supervisor] -> `{"ok", "id"}`. Posts kind `report`. With `trigger` set, refused with "trigger verification arrives in M4".

### Notices (the piggyback)

On every tool response the server checks for broadcasts with `id > broadcast_seen.last_seq` for the caller and for unacked, unexpired operator pins. If any, it adds `"notices": [{"type": "broadcast", "seq", "body"}, {"type": "operator_vote", "message_id", "body", "pinned_until"}]` and advances `broadcast_seen`. Pins stay until acked or expired.

### Scoring

`score(message) = sum(value * weight) / (age_hours + decay_offset_h) ^ decay_power`, in `ranking.py`, computed in SQL for ordering and returned on every message. Task priority = `priority_base + score(thread root)`.

### Server

`harness/board_mcp/server.py` exposes `build_server(db: Db, shared_dir: Path, verifier: Callable | None) -> MCPServer` and `main()` that runs uvicorn on `CIV_BOARD_HOST` (default `127.0.0.1`) and `CIV_BOARD_PORT` (default `8765`) with `streamable_http_app(stateless_http=True, json_response=True)` mounted so the MCP endpoint is `http://127.0.0.1:8765/mcp`, plus a `GET /health` custom route returning `{"ok": true, "head_seq": n}`. Identity comes from `Context.headers["x-civ-token"]` (headers are lower-cased; read case-insensitively). The verifier is loaded from `CIV_VERIFIER` as `module:function` (default `task.verifier.verifier:verify`).

Structure (so tests can call logic without HTTP): `db.py` (`Db` with a psycopg pool, `transaction()` context manager, `fetch/execute` helpers), `auth.py` (`AgentIdentity` dataclass: id, role, cwd, status; `resolve_token(db, token)`), `tools/board.py` (`BoardTools(db, shared_dir)` with one method per tool taking `AgentIdentity` first), `tools/task.py` (`TaskTools(db, shared_dir, verifier)`), `tools/civ.py` (`CivTools(db)`), `ranking.py` (score SQL fragment and Python equivalent), `limits.py` (`Knobs(db)` cached reads of `config` with a short TTL, rate-limit checks against `events`), `operator.py` (`Operator(db)`: `create_task(id, title, description, priority_base=0) -> thread_id`, `broadcast(body) -> id`, `vote(message_id, value, reason, pin_minutes=5) -> id`, `register_agent(id, role, lineage_id, cwd, generation=1) -> token`), `notices.py` (the piggyback). The MCP layer in `server.py` is thin wrappers that resolve identity, call the method, and attach `head_seq` and notices.

### Operator API (used by WS-B spawner and WS-C seed script)

```python
from harness.board_mcp.db import Db
from harness.board_mcp.operator import Operator
db = Db(url)                      # url defaults to CIV_DATABASE_URL
op = Operator(db)
token = op.register_agent("sol-001", "solver", lineage_id="L-001", cwd="/abs/path/agents/sol-001")
thread_id = op.create_task("T-001", "title", "description", priority_base=1.0)
op.broadcast("Prefer breadth over depth")
```

Also `op.set_agent_usage(agent_id, tokens_in, tokens_out, context_pct)`, `op.set_agent_status(agent_id, status)`, `op.log_event(agent_id, type, payload)`, `op.tasks_summary() -> dict`, `op.board_transcript() -> list[dict]` (every message with votes, ordered by thread then seq, for `board_dump.py`).

## Orchestrator (WS-B)

- `prompts.py`: `PromptAssembler(prompts_dir)` with `solver_prompt(ctx: PromptContext) -> str`. Order: `roles/solver.md`, `board_etiquette.md`, `voting_guidelines.md`, `task_claim_guidelines.md`, `broadcast_preamble.md` followed by the pinned broadcasts (if any), `successor_preamble.md` plus the predecessor handoff (if any), and `identity.md` last with `{agent_id} {role} {generation} {lineage_id} {harness_version}` filled. `PromptContext` is a dataclass with those fields plus `broadcasts: list[str]` and `handoff: str | None`. Prompt files are written by WS-B (solver-side only for M2: `roles/solver.md`, `identity.md`, `board_etiquette.md`, `voting_guidelines.md`, `task_claim_guidelines.md`, `broadcast_preamble.md`, `successor_preamble.md`; the handoff files are M3 and `roles/supervisor.md` and `supervisor/*` are M4, create them as short stubs marked "M3"/"M4").
- The solver loop in `roles/solver.md`: call `task_claim`; if a task comes back, read its thread (`board_read(thread_id=...)`) and `board_top(kind="finding")` once, do the work in `work/` under your directory, post findings and questions to the thread as you go, write the result file, `task_complete`; then cast at least one vote with a reason (the server refuses the next claim otherwise), prefer messages in threads near your task; claim again. When `task_claim` returns no task, post one last finding if you learned something unposted, then stop. Heartbeat on long tasks. Release with a reason if stuck. Never write outside your directory. Never read another agent's directory. Materials are under `task/materials/` (read-only). The prompt must say the MCP tools are named `mcp__civ__<tool>`.
- `spawner.py`: `Spawner(db_url, board_url, root, prompts, model, max_turns)` with `async spawn(agent_id, lineage_id, generation=1, handoff=None) -> SpawnResult`. It registers the agent through `Operator.register_agent`, creates `root/agents/<id>/{work,notes}` and `identity.json`, builds `ClaudeAgentOptions(cwd=..., system_prompt=..., tools=["Read","Write","Edit","Bash","Glob","Grep"], mcp_servers={"civ": {"type": "http", "url": f"{board_url}/mcp", "headers": {"X-Civ-Token": token}}}, permission_mode="bypassPermissions", setting_sources=[], model=model, max_turns=max_turns, add_dirs=[root/"task"/"materials"])`, runs `query(prompt=kickoff, options=options)` and feeds every message to a `Monitor`. The kickoff user prompt is short: "Begin your solver loop." The `SystemMessage` with subtype `init` carries `data["mcp_servers"]` with each server's status; log it and fail fast if `civ` is not connected. `SpawnResult` holds agent_id, turns, cost_usd, tokens, context_pct, result text, and any error.
- `monitor.py`: `Monitor(operator, agent_id, context_window=1_000_000)` with `observe(message)`. On `AssistantMessage` with `usage`, context size is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens` of that message (the prompt the model just read), so `context_pct = size / context_window * 100`; accumulate `tokens_in`/`tokens_out`; write through `operator.set_agent_usage` at most once per turn and log an `agent.turn` event. On `ResultMessage` record cost and mark the agent `ended`. Expose `on_threshold(pct, callback)` so M3 can hook 80 and 90, and fire callbacks once per crossing.
- `main.py`: `python -m harness.orchestrator.main --solvers 3 --model claude-opus-5-5 --max-turns 40 --stagger 2 --board-url http://127.0.0.1:8765`. Spawns `sol-001..N` with lineages `L-001..N`, staggered, concurrently with asyncio, waits, prints a table: agent, turns, cost, tokens, tasks completed, plus total cost. Reads `CIV_*` env defaults.
- Tests: unit tests only, no SDK calls, no network. Use a fake `Operator` and fake message objects (construct the SDK dataclasses directly, they are plain dataclasses). Test prompt order and identity placement, options construction (cwd, headers carry the token, tools list, permission mode), monitor arithmetic and threshold firing once, main's stagger and summary with a stubbed spawner. One integration test gated by `CIV_INTEGRATION=1` that spawns one solver with `max_turns=3` against a running board.

## Task pack and demo (WS-C)

- `task/materials/`: a small fictional archive, read-only, about eight short plain-text files (a census, a ledger, letters, a parish log) with facts that cross-reference each other. Keep each under 60 lines.
- `task/tasks.json`: six to eight tasks with `id`, `title`, `description`, `priority_base`, `result_file` name and a `format` line for the result. Design them so collaboration pays: at least two tasks say "the answer to this depends on a fact another task establishes; check the board (board_top findings, board_search) before redoing the work, and if nobody has posted it, post a question in your thread and do it yourself". One task asks the solver to answer any open `question` in the board that it can, from the materials.
- `task/verifier/verifier.py`: `verify(task_id: str, result_path: str) -> tuple[float, str]` scoring 0.0 to 1.0 against `task/verifier/answers.json` (exact match for numbers and names after normalisation, keyword coverage for prose), and `progress() -> str` returning a one-paragraph summary of tasks done and mean score (it may open the database through `Operator`). Agents must never read `task/verifier/` (prompt rule in M2, OS rule later).
- `scripts/seed_tasks.py`: loads `tasks.json` and creates each task through `Operator.create_task`. Idempotent.
- `scripts/reset_db.py`: truncates all tables in dependency order and re-seeds config (calls the migration's seed), for a clean demo.
- `scripts/bootstrap.sh`: PATH setup, checks Postgres is up (starts it with `pg_ctl` if not), creates `civ` and `civ_test` if missing, runs `scripts/migrate.py` on both, creates `agents/` and `shared/`.
- `scripts/board_dump.py`: prints the board as a transcript grouped by task thread, each message with author, kind, score, votes and reasons, then the task table with scores, then alerts and broadcasts.
- `scripts/demo.sh`: `bootstrap`, `reset_db`, `seed_tasks`, start the board server in the background (wait on `/health`), run the orchestrator with `--solvers 3`, then `board_dump.py`, then stop the server. Prints the total cost at the end.
- Tests: the verifier scores a correct and an incorrect result for every task; `tasks.json` validates (unique ids, result_file, format); a test that derives every answer in `answers.json` from the materials with plain Python so the hidden answers and the corpus cannot drift apart.

## Deviations from the design doc, for the record

- MCP tool names use underscores. The server name is `civ`.
- One OS user, not three. Directory isolation is by convention and prompt. `task/verifier/` is protected by prompt only in M2.
- The board server calls the verifier at `task_complete` (synchronously) rather than the orchestrator, because in M2 there is no supervisor round to batch it.
- `verify` takes `(task_id, result_path)` so the verifier knows which answer to check.
- Runtime directories `agents/` and `shared/` live at the repo root (gitignored), not under `/civ`.
- M3 and M4 tools exist in M1 with their validation, but nothing drives them yet.

---

# M3 addendum: succession

Built on top of M0 to M2. Same rules: contract wins for M3, no commits by workstreams, no em dashes, tests against `civ_test`, explanatory comments.

## Verified SDK facts for M3

- `ClaudeSDKClient` (not `query()`) is the session primitive: `await client.connect(prompt)`, then `async for msg in client.receive_messages()`. `await client.query(text)` sends a further user message into the running session (streaming input; it is delivered at the next turn boundary). `await client.interrupt()` stops the current turn. `await client.disconnect()` ends the session.
- `await client.get_context_usage()` returns `{"totalTokens", "maxTokens", "percentage", "isAutoCompactEnabled", "autoCompactThreshold", ...}`: the CLI's own view of the context window. This is the authoritative fill signal; the usage arithmetic in `Monitor` stays as a cross-check and as the unit-testable path.
- Auto-compaction must not pre-empt the handoff. The switch is whatever the bundled CLI honours (see the grep the orchestrator ran; if an env var exists, the spawner sets it in `ClaudeAgentOptions.env`; if not, `handoff_pct` must stay below the CLI's `autoCompactThreshold` and the spawner asserts that at start by reading `get_context_usage()` once).

## Ownership

| Workstream | Owns |
|---|---|
| WS-D (server and operator) | `harness/board_mcp/operator.py` additions, `harness/board_mcp/tools/civ.py` handoff changes, `harness/board_mcp/stub.py`, tests `test_succession_server*.py` |
| WS-E (orchestrator) | `harness/orchestrator/spawner.py` (client-based), `monitor.py`, new `succession.py`, `main.py` flags, the four handoff prompt files in `harness/prompts/`, tests `test_succession*.py`, `test_spawner*.py`, `test_monitor*.py` |

## Mechanism

1. Trigger. `Monitor` computes `context_pct` after every assistant message from usage (as now) and, every N turns and whenever the arithmetic crosses 70 percent, calls `get_context_usage()` and takes the larger of the two. Thresholds come from knobs `handoff_pct` (80) and `kill_pct` (90) read through `Knobs`, and from the CLI flags below. A test-only trigger `--force-handoff-after-turns N` fires the handoff at turn N regardless of fill, so succession can be exercised for cents.
2. Handoff request. At `handoff_pct` the orchestrator calls `Operator.request_handoff(agent_id)`, which sets `agents.status = 'handing_off'` and logs `agent.handoff_requested`. From this moment the server refuses every write except `civ_handoff` (already enforced in M1) with the error "hand off now: call civ_handoff with the five headings". The orchestrator then sends the contents of `handoff_request.md` into the session with `client.query(...)`. Both channels exist because a prompt alone is unreliable at a nearly full context.
3. Handoff. `civ_handoff(body)` validates the five headings and size (M1), stores the row, posts kind `handoff`, keeps the lease, sets `agents.status = 'ended'`, logs `agent.handoff_written`, and returns `{"ok": true, "handoff_id", "stop": true}`. The agent is told in `handoff_request.md` that the call ends its life and it should not call anything after it.
4. End of session. The orchestrator watches for the `agent.handoff_written` event (poll the events table, or the `handoffs` row for this agent) and then calls `client.interrupt()` and `client.disconnect()`, marks the agent ended if not already, and logs `agent.succeeded`. If the agent reaches `kill_pct` without handing off, the orchestrator interrupts and disconnects immediately, sets status `dead`, logs `agent.killed`, and writes a stub.
5. Stub handoff (M3 version). `harness/board_mcp/stub.py` builds a handoff body from the agent's last thirty board posts, its held task, and a listing of its `work/` and `notes/` directories, in the five-heading format, no model call. `Operator.write_stub_handoff(agent_id)` stores it with `stub = true` and logs `agent.stub_written`. In M4 supervisors take this over; the M3 stub is a template, and the doc notes it.
6. Successor. `Operator.succeed(from_agent, to_agent_id)` registers the successor with the same `lineage_id`, `cwd`, `generation + 1`, a fresh token, transfers any lease (`tasks.lease_owner = to_agent`, `lease_expires = now() + lease_length_s`), records `handoffs.to_agent`, and logs `agent.succeeded`. The spawner builds the successor prompt with `successor_preamble.md` plus the handoff body (stub or real), the pinned broadcasts, and the identity block with the new generation. The successor starts in the same directory, which still holds `work/` and `notes/`.
7. Lineage loop. `succession.py` exposes `run_lineage(lineage_id, first_agent_id, ...)`: spawn generation 1, and whenever a session ends by handoff or kill, spawn the next generation, until a session ends naturally (the agent stopped because no tasks remained) or `--max-generations` is reached. `main.py` runs one lineage per solver slot.

## Operator API additions (WS-D)

`request_handoff(agent_id)`, `latest_handoff(lineage_id) -> dict | None`, `recent_posts(agent_id, n=30)`, `write_stub_handoff(agent_id, listing: list[str]) -> handoff_id`, `succeed(from_agent, to_agent, cwd) -> token`, `lineage_state(lineage_id) -> {"generation", "head_agent", "status", "lease"}`.

## CLI flags (WS-E)

`--context-window N` (default 1000000; the fill percentage is computed against this, so a small value forces succession cheaply), `--force-handoff-after-turns N` (test only), `--max-generations N` (default 5), `--model`, and the existing ones.

## Testing strategy (so M3 costs cents, not dollars)

1. Unit, free: monitor thresholds with fake usage; `succession.py` state machine with a scripted fake client (connect, messages, query, interrupt, disconnect recorded); stub builder from fake posts; successor prompt assembly; lineage loop ending on natural end, on max generations, and on kill.
2. Server, free: handing_off lockout, `civ_handoff` ending the agent and returning `stop`, lease transfer, successor registration, stub storage, `latest_handoff` and `lineage_state`.
3. Live, cents: one solver on `claude-haiku-5-5` with `--context-window 40000` and `--max-turns 12` against the Hollowmere pack; the handoff should fire within a handful of turns. Then the same with `--force-handoff-after-turns 3` on the same model to prove the injected message path and the lockout path both work. Gate both behind `CIV_INTEGRATION=1`.
4. Demo, about a dollar: three Opus solvers with `--context-window 60000` so every lineage hands off at least once during the eight tasks, then `board_dump.py` shows handoffs and generations.
