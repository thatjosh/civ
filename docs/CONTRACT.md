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

Config seeds (inserted by migration 0001): `post_rate_per_h` 30 [5,120,5], `vote_rate_per_h` 20 [5,60,5], `vote_quota` 1 [0,3,1], `lease_length_s` 1800 [300,7200,300], `max_attempts` 3 [1,6,1], `backoff_base_s` 60 [10,600,10], `top_k` 10 [5,50,5], `long_body_chars` 2000 [500,8000,500], `decay_offset_h` 2 [1,12,1], `decay_power` 1.5 [1,3,0.25], `handoff_pct` 80 [50,90,5], `kill_pct` 90 [60,95,5], `max_handoff_tokens` 4000 [1000,8000,500]. Migration 0002 adds `kill_grace_turns` 2 [0,10,1].

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
4. End of session. The orchestrator watches for the `agent.handoff_written` event (poll the events table, or the `handoffs` row for this agent) and then calls `client.interrupt()` and `client.disconnect()`, marks the agent ended if not already, and logs `agent.succeeded`. If the agent reaches `kill_pct` without handing off, the orchestrator interrupts and disconnects, sets status `dead`, logs `agent.killed`, and writes a stub. Amended after the 30k-window demo: the kill rule is `kill_pct` **and at least `kill_grace_turns` (knob, default 2) assistant turns since the request**, and an agent whose context jumps past both thresholds in one turn is asked first and killed only after the grace. `agent.handoff_sent` carries `turn`, `context_pct`, `kill_due` and `grace_turns`; `agent.handed_off` and `agent.killed` carry `requested`, `turns_since_request` and `seconds_since_request`, which is what the M4 succession brief is computed from.
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

---

# M4 addendum: supervisors

Built on top of M0 to M3 (including the kill-gap rule, migration 0002). Same rules: the contract wins for M4, no commits by workstreams, no em dashes, tests against `civ_test`, explanatory comments. Where this addendum deviates from the design doc the deviation is marked **deviation** and must be recorded in the README's Phase 3 note.

## What a supervisor is for (the thesis of M4)

A supervisor is a short session, spawned by the orchestrator every round, that reads a brief the orchestrator has already assembled and does a bounded set of things: replies to blockers, reopens a wrongly blocked task, votes, tunes at most one knob per round against a named SLO, and writes one report. New in M4 relative to the design doc: **succession health is a first-class SLO**. The brief carries the window's handoff success rate and a root-cause packet for every kill, the server verifies a `succession_failing` ping against the events, and the supervisor's prompt maps each classified cause to one action. The point is that the thing the orchestrator cannot judge (why an agent failed to hand off, and which knob or prompt is at fault) is judged by an agent with the data in front of it, and every judgement leaves a checkable trace: a tune event naming the SLO, or a report naming a classification.

## Ownership

| Workstream | Owns |
|---|---|
| WS-F (server and operator) | `harness/board_mcp/slo.py` (new), `harness/board_mcp/migrations/0003_supervisor_rounds.sql`, `schema.sql` additions, `seed.py` additions, `operator.py` additions, `tools/civ.py` (tune round logic, report triggers), `tools/task.py` (`task_reopen`), `server.py` (register `task_reopen`, log refusals), tests `test_slo.py`, `test_rounds.py`, `test_supervisor_tools.py` |
| WS-G (orchestrator) | `harness/orchestrator/supervisors.py` (new: `SupervisorCron`, `SupervisorRound`), `harness/orchestrator/brief.py` (new: `render_brief`), `spawner.py` (role-aware tools and prompt), `prompts.py` (`supervisor_prompt`), `main.py` flags, `harness/prompts/roles/supervisor.md` and `harness/prompts/supervisor/*.md`, tests `test_supervisors.py`, `test_brief.py`, prompt tests |
| WS-H (validation) | `scripts/supervisor_replay.py`, `scripts/score_report.py`, `docs/eval/` (rubrics), `scripts/ab_supervisors.sh`, README Phase 3 skeleton, `docs/demo/` records |

WS-F and WS-G meet at the `Operator` methods and the brief dict below; WS-H consumes both. Nobody edits another workstream's files; needs go in the integration notes.

## Schema (migration 0003, mirrored in schema.sql)

```sql
create table if not exists rounds (
  id bigserial primary key,
  number int not null,                      -- 1, 2, 3 within a run; the orchestrator supplies it
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  window_start timestamptz not null,        -- the previous round's started_at, or run start
  final boolean not null default false,
  brief jsonb not null default '{}',        -- the brief as built, so slo_degraded can be verified later
  tuned_knob text,                          -- set by civ_tune; one per round
  tuned_by text,
  summary jsonb not null default '{}'       -- written at close: reports, pings, tunes, cost
);
create table if not exists tune_proposals (  -- the first half of a two-supervisor commit
  id bigserial primary key,
  knob text not null,
  value double precision not null,
  proposed_by text not null,
  round_id bigint not null references rounds(id),
  created_at timestamptz not null default now(),
  committed_by text,
  committed_at timestamptz
);
```

Config seeds added (also in `seed.py`): `min_sample` 3 [1, 50, 1] (**deviation**: the design says 10; 3 is for an eight-task pack, and M5's `civ.toml` seeds it at 10 for production), `big_move_steps` 2 [1, 5, 1]. The message kind enum is unchanged; a ping is a `report` whose body starts with `[ping: <trigger>]`.

## SLOs (`harness/board_mcp/slo.py`, pure functions over a connection and a window)

`compute_slos(conn, window_start, window_end) -> dict` returns, for the window:

- `completions_per_mtok`: verified completions (`task.completed` with score not null) divided by tokens in millions (sum of `agent.turn` tokens_in + tokens_out in the window). `n` = completions.
- `blocked_failed_rate`: (`task.released` events) / (`task.claimed` events), 0 when no claims. `n` = claims.
- `duplicate_rate`: among `finding` messages in the window, the fraction that have an earlier finding in the same thread by a different author with word-set Jaccard >= 0.6 (bodies of at least 8 words; completion findings starting `[T-` are excluded). `n` = findings considered.
- `lease_wait_median_s`: median seconds from a task becoming claimable (its `task.created`, or its latest `task.released` with status open, or `knob.reverted`-independent) to its next `task.claimed`. `n` = claims matched.
- `handoff_success_rate`: real handoffs / (real handoffs + stubs) in the window, from `agent.handed_off` and `agent.killed` events; `None` when neither happened. `n` = real + stubs.

`compute_succession(conn, window_start, window_end) -> dict` returns `{"requests", "handoffs", "stubs", "kills", "success_rate", "median_turns_to_handoff", "median_seconds_to_handoff", "refusals": {reason: count}, "generations": {lineage_id: head generation}, "incidents": [packet, ...]}`. One packet per `agent.killed` in the window:

```
{"agent_id", "lineage_id", "generation", "reason", "requested": bool, "turns_since_request", "seconds_since_request",
 "grace_turns", "context_pct_at_request", "tool_calls_after_request": [event type, ...],
 "handoff_refusals": [error, ...], "task": task_id or null, "classification": one of
 "never_asked" | "no_chance" | "rejected" | "ignored" | "silent" | "timed_out"}
```

Classification rules, in order: `never_asked` when `requested` is false; `timed_out` when reason is "handoff timed out"; `rejected` when any `tool.refused` for `civ_handoff` exists after the request; `no_chance` when `turns_since_request` < `grace_turns` (legacy data, or grace 0); `ignored` when there is at least one `tool.*` event after the request and none is `civ_handoff`; otherwise `silent` (no tool call at all after the request: a long tool call or a hung session). The classification is a heuristic the supervisor is told to confirm or override.

Refusal logging (WS-F, `server.py` Gateway): every `{"ok": false}` result is logged as `tool.refused {"tool": <tool name>, "error": <text>}` before the envelope is added. This is what `rejected` and the refusal table in the brief are computed from.

## Operator API additions (WS-F)

- `open_round(number: int, final: bool = False) -> int`: inserts the round with `window_start` = previous round's `started_at` (or the earliest event ts when there is none), logs `round.opened`, returns id.
- `round_brief(round_id) -> dict`: builds and stores the brief (below) on the round row, returns it.
- `close_round(round_id, summary: dict) -> None`: sets `ended_at` and `summary`, logs `round.closed` with the summary.
- `current_round() -> dict | None`: the open round (ended_at null), with id, number, started_at, window_start, tuned_knob, final.
- `set_knob(knob, value, reason) -> None`: an operator write that respects bounds only (no step, no cooldown, no TTL), sets `set_by = 'operator'`, logs `knob.preset`. For fault injection and launch presets.
- `register_agent(...)` unchanged; supervisors are registered with role `supervisor`, lineage `S-<slot>` (`S-001`), id `sup-<slot>-r<round number>` (`sup-001-r3`), generation = round number.
- `reports_for_round(round_id) -> list[dict]` and `pings_since(ts) -> list[dict]`, for the brief and for M5's inbox.

## The brief (dict stored on the round, rendered to markdown by WS-G)

```
{"round": {"id", "number", "final", "window_start", "window_end", "supervisors": n},
 "status": <civ_status without "you">,
 "slo": {"current": compute_slos(window), "previous": compute_slos(previous window) or null,
         "min_sample": knob, "history": [{"round", <slo name>: value...}, ...] last 5 rounds},
 "succession": compute_succession(window),
 "blockers": [{"id", "thread_id", "task_id", "author_id", "body", "score", "created_at"}, ...] top 10 by score in window,
 "blocked_tasks": [{"id", "title", "attempts", "last_release_reason"}, ...],
 "alerts": [{"id", "author_id", "body", "created_at"}, ...] since window_start,
 "previous_reports": [{"id", "author_id", "body"}, ...] from the previous round,
 "tunes": {"active": [{"knob", "value", "seed", "set_by", "reason", "expires_at"}, ...],
           "pending": [{"knob", "value", "proposed_by", "round_number"}, ...],
           "round_tuned": bool},
 "verifier": {"done", "total", "mean_score", "lowest": [{"id", "score", "notes"}, ...]}}
```

## Tool changes (WS-F)

- `civ_tune(knob, value, slo, reason, ttl_s=3600)`, in addition to the M1 rules, in this order: refuse "no supervisor round is open" when `current_round()` is None; refuse "round already tuned" when the round has `tuned_knob`; `slo` must be one of the five above (`handoff_success_rate` added); refuse "below min_sample: <slo> has n=<n> in this window, needs <min_sample>" using the stored brief's `slo.current[slo].n` (for `handoff_success_rate` the sample is `succession.handoffs + succession.stubs`); a **big move** (the new value more than `big_move_steps` steps from the seed) by a supervisor with no matching pending proposal from a different supervisor in this or the previous round records a proposal and returns `{"ok": true, "pending": true, "knob", "value", "needs": "a second supervisor"}` without changing the knob or using the round's tune; a matching second call commits it (sets `committed_by`), applies the change, and uses the round's tune. The committed change logs `tool.civ_tune` with `slo`, `n`, `reason`, `big_move`, `round_id`.
- `civ_report(body, trigger=None)`: refuse "you already reported this round"; refuse "no supervisor round is open"; with `trigger`: must be one of `succession_failing | slo_degraded | big_move_disputed | ceiling_pinned`. Verification: `succession_failing` requires `succession.kills >= 1` in the current brief **and** in the previous round's brief (two consecutive windows); `slo_degraded` requires the body to name an SLO in the form `slo=<name>` and that SLO to have worsened over the last three stored briefs (completions and success rate down, rates and wait up); `big_move_disputed` requires a pending proposal older than two rounds with a different proposal on the same knob; `ceiling_pinned` is refused with "no ceilings until M5". An unverified trigger is refused with "trigger <t> is not in the data: <what was checked>". A verified ping stores the body as `[ping: <trigger>] <body>` and logs `report.ping`.
- `task_reopen(task_id, reason)` [supervisor]: a blocked task returns to open with `attempts = 0`, `next_attempt_at = now()`, posts a `blocker`-kind reply in the thread saying `[T reopened] <reason>`, logs `task.reopened`. Refused when the task is not blocked. **Deviation**: the design's "split a stuck task" is not built; reopening with guidance is the M4 unblock.
- Supervisors may `board_post` findings and proposals and `board_vote` (weighted) as before. `board_post` with `parent_id` on a blocker is how a supervisor replies to it.

## Orchestrator (WS-G)

- `Spawner.prepare(..., role="solver")`: tools by role, `SOLVER_TOOLS` as now, `SUPERVISOR_TOOLS = ["Read", "Grep"]` (they read `shared/` refs and nothing else; no Bash, no Write), `add_dirs` for a supervisor is `shared/` not `task/materials`. Prompt by role via `PromptAssembler.supervisor_prompt(ctx, brief_markdown)`. Order: `roles/supervisor.md`, `supervisor/cron.md`, `supervisor/tune_policy.md`, `supervisor/succession_policy.md`, `supervisor/alert_policy.md`, `supervisor/report_template.md`, the broadcasts (if any), the rendered brief under a `# This round's brief` heading, then `identity.md`. Kickoff: "Begin your round."
- `harness/orchestrator/brief.py`: `render_brief(brief: dict) -> str`, deterministic markdown, tables for SLOs (current vs previous with n), one subsection per incident packet with its classification, blockers with their seqs, active and pending tunes, and a final line "This round has already tuned a knob" when `round_tuned`. Must stay under about 6,000 tokens for a 10-incident window: truncate lists, never bodies below 200 chars.
- `harness/orchestrator/supervisors.py`: `SupervisorRound(spawner, operator, prompts, count, model, max_turns)` with `async run(number, final=False) -> RoundResult` (open round, build brief, spawn `count` supervisor sessions concurrently as plain `Spawner.spawn` sessions with the brief, wait, close round with `{"reports": n, "pings": n, "tuned": knob or null, "reopened": [...], "cost_usd", "turns"}`). `SupervisorCron(round, interval_s, sleep=asyncio.sleep)` with `async run_until(population_done: asyncio.Event)`: round 1 after the first interval (the design's "first round before the population is full" is **not** built; **deviation**), then every `interval_s`; when the event is set, run one final round with `final=True` and return the list of `RoundResult`.
- `main.py` flags: `--supervisors N` (default 0: no rounds, so existing demos are unchanged), `--supervisor-interval S` (default 600), `--supervisor-model` (default the solver model), `--supervisor-max-turns` (default 15), `--preset knob=value` (repeatable; applied through `Operator.set_knob` before any spawn). `amain` runs the solver population and the cron concurrently and prints the round summaries after the solver table: one line per round, `round N: reports r, pings p, tuned <knob> (from -> to), reopened [...], cost $`.
- Supervisors are ordinary sessions (no succession, no monitor thresholds; the monitor still records usage and cost). `max_turns` is the budget.

## Prompt files (WS-G, replacing the M4 placeholders)

`roles/supervisor.md` (what you are, the round, what you may do, end with "round done"), `supervisor/cron.md` (the order of a round), `supervisor/tune_policy.md` (the knob table from the design for the knobs that exist: `handoff_pct`, `kill_grace_turns`, `max_attempts`, `lease_length_s`, `post_rate_per_h`, `max_handoff_tokens`, `top_k`, `vote_quota`; the three rules: cooldown, hysteresis, observe before act; a tune names the SLO it serves and the number that justified it), `supervisor/succession_policy.md` (new: read the succession section first when kills or stubs are non-zero; for each incident confirm the classification against the packet; cause to action: `never_asked` or `no_chance` means the gap is too small, tune `kill_grace_turns` up one step when seconds_since_request was short, or `handoff_pct` down one step when one turn crossed both thresholds; `rejected` means read the refusal, missing headings is a prompt fault (post a `proposal` naming `handoff_request.md`), size is `max_handoff_tokens` up one step; `ignored` is a prompt fault, propose and alert; `silent` or `timed_out` means the request landed inside a long tool call, tune `handoff_pct` down one step; report the success rate this window against last and whether the previous round's tune moved it), `supervisor/alert_policy.md` (the four triggers and that the server verifies them; one ping per round; everything else goes in the report), `supervisor/report_template.md` (headings: `## Window`, `## Succession`, `## Queue and SLOs`, `## Actions taken`, `## Diagnosis`, `## For the operator`), `supervisor/human_ping_template.md`, `supervisor/proposal_review.md` (read pending proposals and vote; merging arrives in M6).

## Validation at small scale (WS-H)

Three measurements, cheapest first, each with a script and a saved record under `docs/demo/`:

1. **Replay eval, diagnosis accuracy on recorded incidents.** `scripts/supervisor_replay.py --fixture docs/demo/2026-10-15-m3-fixture.sql --model <m> [--trials 3] [--without-succession-section]` loads the fixture into a scratch database `civ_replay` (fresh migrate, then the data), starts a board server on a spare port against it, opens a round whose window covers the whole fixture, builds the brief, runs one supervisor session, and writes the report, the tune events and the cost to `docs/demo/<date>-replay-<model>-<trial>.json`. Ground truth from the fixture: sol-002 is `never_asked`, sol-003 is `no_chance` (2.7 s, 0 turns), both stubs landed on T-008 and T-007, the lineages recovered at generation 2 and 3. `scripts/score_report.py <json> --rubric docs/eval/m3-kills.rubric.json` scores: named both agents (2), named each classification or its plain-language equivalent (2), chose a succession knob in the right direction or explained why not (1), did not ping (1, one window only), did not tune a non-succession knob on this evidence (1), report has the six headings (1). Out of 8. The `--without-succession-section` control removes the succession block from the brief; the difference between the two scores is what the orchestrator's packet buys over the supervisor reading the board itself.
2. **Machinery demo, live.** `scripts/ab_supervisors.sh` runs the Hollowmere pack twice with three Haiku solvers at `--context-window 30000 --preset kill_grace_turns=0` (the fault: the old kill rule): arm A `--supervisors 0`, arm B `--supervisors 1 --supervisor-interval 15 --supervisor-model <m>`. Records per arm: tasks done, mean score, handoffs, stubs, cost, supervisor cost share, and for arm B the round summaries. The run is too short for a closed loop to show in the SLOs; what it proves is the round machinery end to end (round rows, brief, a tune accepted under the round rules, a report, a refused second tune) and what supervision costs as a share of the bill.
3. **Closed loop, deferred.** The A/B that would show a tune moving an SLO needs the larger task pack (README next steps item 4). The protocol is written in the README: same fault, runs of at least six rounds, compare `handoff_success_rate` per round between arms, three seeds each.

## Deviations summary (record in README)

One tune per round is enforced across all supervisors (as designed); the first round runs after the first interval, not before the population is full; `min_sample` seeds at 3; no task splitting (reopen instead); `ceiling_pinned` is unverifiable until M5; supervisors do not write stubs (the orchestrator's instant stub from M3 stays, supervisors diagnose it instead); cron interval is a flag, not a knob, until M5's `civ.toml`.

---

# M5 addendum: the operator's controls

Built on top of M0 to M4. Same rules: the contract wins for M5, no commits by workstreams, no em dashes, tests against `civ_test`, explanatory comments. Deviations from the design doc are marked **deviation** and go in the README's Phase 4 note.

## The thesis of M5

Everything built so far is inside the box. M5 is the box and the operator's hands on it: two operator-owned files that no agent writes (`control/ceilings.toml` caps what the system may ever do; `control/civ.toml` gives it its launch-time shape), a governor in the orchestrator that refuses or delays spawns against the ceilings and a pause flag, an epoch counter stamped on every agent and event, and a `civ` command line that works even when the harness is broken: say, vote, inbox, status, pause, resume, reshape, restore. Restore is a succession for the whole population at once, so it reuses M3: it marks the old epoch dead, writes a stub for every lineage caught mid-flight, and the orchestrator restarted with `--resume` spawns generation N+1 for each.

## Ownership

| Workstream | Owns |
|---|---|
| WS-I (control and CLI) | `control/ceilings.toml`, `control/civ.toml`, `control/__init__.py`, `control/cli/__init__.py`, `control/cli/civ.py`, `harness/board_mcp/migrations/0004_control_state.sql`, the matching block in `schema.sql`, `pyproject.toml` (packages and the `civ` entry point), tests `harness/tests/test_cli.py`, `test_control_state.py` |
| WS-J (orchestrator) | `harness/orchestrator/governor.py` (new), `harness/orchestrator/shape.py` (new), `harness/orchestrator/main.py` (civ.toml defaults, `--control-dir`, `--resume`, the population manager), `spawner.py` (governor gate), `succession.py` (`resume_from` and the slot check), `supervisors.py` (shape-driven count and interval), fakes and tests `test_governor.py`, `test_shape.py`, `test_resume.py` |
| WS-K (server, operator reads, drill) | `harness/board_mcp/ceilings.py` (new loader), `tools/civ.py` (tune respects the ceiling), `operator.py` additions, tests `test_ceilings.py`, `test_operator_reads.py`; `scripts/restore_drill.sh`, `scripts/demo.sh` (passes `--control-dir`), README Phase 4 skeleton |

## Schema (migration 0004, mirrored in `schema.sql` after a `-- ---- migration 0004 ----` marker)

```sql
create table if not exists control_state (
  key text primary key,            -- epoch | paused | shape | inbox_last_seq | last_restore
  value jsonb not null,
  updated_at timestamptz not null default now()
);
insert into control_state (key, value) values ('epoch', '1'), ('paused', 'false'), ('inbox_last_seq', '0')
on conflict (key) do nothing;

-- The epoch is read by a function so every writer stamps it without knowing about it.
create or replace function current_epoch() returns int language sql stable as
  $$ select coalesce((select (value #>> '{}')::int from control_state where key = 'epoch'), 1) $$;
alter table agents add column if not exists epoch int not null default current_epoch();
alter table events add column if not exists epoch int not null default current_epoch();
create index if not exists events_epoch_ts_idx on events (epoch, ts);
```

`paused` is `true` or `false`, or `{"on": true, "reason": "..."}`; readers accept both. `shape` is the parsed `civ.toml` as JSON plus `"written_at"`. `last_restore` is `{"at", "epoch", "lineages_stubbed", "leases_released", "minutes_lost"}`.

## The two files (WS-I writes them; the repo copies are the small-scale demo values)

`control/ceilings.toml`:

```toml
# Hard ceilings. The orchestrator reads this file before every spawn and the board server when
# validating a tune, so nothing decided inside the system can exceed them. Operator-owned.
max_live_agents = 8
max_tokens_per_minute = 3000000
max_spend_per_day_usd = 5.0
max_handoff_tokens = 8000
model = "claude-opus-5-5"      # the default model; --model on the command line still wins
```

`control/civ.toml` (keys that are also knobs are seeded into the config table at launch through `Operator.set_knob` with reason `civ.toml`, so they carry `set_by = 'operator'`):

```toml
[population]
solvers = 3
supervisors = 1
[cadence]
supervisor_round_s = 600
[lifecycle]
max_generations = 5
handoff_pct = 80          # knob
kill_pct = 90             # knob
kill_grace_turns = 2      # knob
[voting]
decay_offset_h = 2        # knob
decay_power = 1.5         # knob
operator_pin_s = 300
[governance]
vote_quota = 1            # knob
min_sample = 3            # knob; the design says 10 for a 95-solver run, 3 fits an eight-task pack
big_move_steps = 2        # knob
ttl_default_s = 3600
```

Precedence for the orchestrator: command-line flag, then `civ.toml`, then the dataclass default. `--solvers`, `--supervisors`, `--supervisor-interval`, `--max-generations` and `--model` therefore become optional overrides. `operator_pin_s` and `ttl_default_s` are read by the CLI and the server respectively (**deviation**: `ttl_default_s` is read by `civ_tune` as the default `ttl_s`; `cooldown_s` stays the column default).

## Governor (WS-J, `harness/orchestrator/governor.py`)

`Governor(operator, ceilings_path, poll_s=2.0, clock=time.monotonic, sleep=asyncio.sleep)` with `async acquire(role: str, replaces_slot: bool) -> None`, called by `Spawner.spawn` before every session when a governor is attached (`replaces_slot` is true for a successor, which takes over its predecessor's slot). It loops until clear, in this order, logging one `governor.wait {reason, value, limit}` event per wait and `governor.resumed` when it clears:

1. `paused` in `control_state` is on: wait.
2. Live agents (status `live` or `handing_off`, any role) >= `max_live_agents` and not `replaces_slot`: wait.
3. Tokens in the last minute (`Operator.tokens_per_minute()`) > `max_tokens_per_minute`: wait.
4. Spend in the last 24 hours (`Operator.spend_usd(hours=24)`, from `agent.ended` cost plus the price-table estimate for live sessions) >= `max_spend_per_day_usd`: raise `SpendCeilingReached`. A lineage that meets it ends with `ended_by = "halted"` and the cron stops after the current round; `main` prints the halt and exits 2.

The ceilings file is re-read when its mtime changes, so an operator edit takes effect at the next spawn without a restart. A missing file is an error at start, not a default.

## Shape and the population (WS-J)

`shape.py`: `Shape` dataclass with the fields above, `load_shape(path) -> Shape`, `Shape.knob_seeds() -> dict[str, float]` (the keys marked knob), `Shape.to_json()`. At start `main` loads `civ.toml`, writes it to `control_state.shape` (**deviation**: the design has only `civ reshape` writing it; writing at start too means `civ status` can show the shape the run is using), and seeds the knobs. `ShapeReader(operator, poll_s)` polls `control_state.shape` and `epoch` and exposes `current()`; the population and the cron read it.

`Population` (in `main.py` or a new `population.py`): owns the lineage slots. At start it runs `shape.solvers` lineages (or, with `--resume`, the lineages the database says to continue, then new ones up to the target). When the shape's `solvers` grows it starts new lineages (`sol-004`, `L-004`, ...) at the stagger; when it shrinks, lineages with index above the target are not given a successor at their next succession (`LineageRunner` asks `population.slot_open(index)` before `succeed`), which is the design's "applied lineage by lineage". The cron reads `supervisors` and `supervisor_round_s` from the shape at the start of every round.

`--resume`: `Operator.lineages_to_resume() -> [{"lineage_id", "from_agent", "generation", "handoff_id", "stub"}]`, every lineage whose latest handoff has `to_agent is null`. `LineageRunner.resume(lineage_id, from_agent, handoff_row)` spawns generation N+1 exactly as the loop does after a kill. Lineages whose last session ended naturally are not resumed.

## Server and operator reads (WS-K)

- `harness/board_mcp/ceilings.py`: `Ceilings` loader (path from `CIV_CONTROL_DIR` or `<CIV_ROOT>/control`, mtime cached) with `.get(name)`. `civ_tune` refuses `max_handoff_tokens` above `Ceilings.max_handoff_tokens` with "above the ceiling in control/ceilings.toml (<n>)", and uses `ttl_default_s` from the shape when `ttl_s` is not given.
- `Operator` additions: `tokens_per_minute() -> int`, `spend_usd(hours: float) -> float` (ended sessions by `agent.ended.cost_usd`, live ones estimated from `agents.tokens_in/tokens_out` with the monitor's price table for the ceilings' model), `live_agents() -> {"solver": n, "supervisor": n}`, `epoch() -> int`, `control_get(key) -> Any | None`, `control_set(key, value) -> None`, `lineages_to_resume()`, `agents_without_ack(message_id) -> list[str]` (agents live during the pin window with no `operator_acks` row), `inbox(since_seq, limit=50) -> list[dict]` (reports, pings and alerts with seq > since_seq, newest first, each with `kind`, `author_id`, `body`, `trigger`, `created_at`).
- `scripts/restore_drill.sh`: the recovery runbook at small scale. Clean board, three Haiku solvers at a 30k window with one supervisor on a fifteen-second round, SIGKILL the orchestrator process about 25 seconds in (and `pkill -f "claude --output-format stream-json"` for the sessions it leaves), `civ restore --yes`, restart with `--resume`, then print: epochs seen in events, lineages resumed with their generation and stub flag, tasks done and mean score, the restore broadcast, and the wall time of each half. Saves `docs/demo/<date>-restore-drill-{events,board-transcript,summary}.txt`.

## The `civ` command line (WS-I, `control/cli/civ.py`)

Self-contained: imports only the standard library, `psycopg` and `tomllib`. A test asserts no `harness` import, because this is the one channel that must work when the harness is broken. Installed as `civ` through `[project.scripts]` and runnable as `python -m control.cli.civ`. Common options: `--db-url` (env `CIV_DATABASE_URL`, default `postgresql://127.0.0.1:5432/civ`), `--control-dir` (env `CIV_CONTROL_DIR`, default `<repo>/control`). Exit 0 on success, 1 on a refused or failed action, 2 on bad usage. Every command logs an `operator.<command>` event.

- `civ say "<text>"`: inserts a `broadcast` message as `operator` and prints `broadcast #<seq>`. (The next-tool-result push already exists in the server.)
- `civ vote <message_id> <+1|-1> --reason "<text>"`: the pinned, must-acknowledge vote at supervisor weight (`live solvers / max(live supervisors, 1)`, floored at 1), pinned for `operator_pin_s` from `civ.toml`; prints `vote on #<id> pinned until <time>`. `civ vote --report <message_id>` prints the agents that were live during the pin and never acknowledged, one per line, or `all acknowledged`.
- `civ inbox [--all] [--limit N]`: reports, pings and alerts newer than `inbox_last_seq`, newest first, each as `#<seq> <time> <kind> <author>` and the body indented; pings first. Then expired pins of the last 24 hours with the agents that never acknowledged. Marks read by advancing `inbox_last_seq` unless `--all`.
- `civ status`: epoch, paused (and reason), live agents by role against `max_live_agents`, tasks by status with mean score, tokens per minute against the ceiling, spend in 24 hours against the ceiling, the shape in one line, then the knob table (`knob value seed set_by expires_at`).
- `civ pause [--reason "<text>"]` and `civ resume`: write `control_state.paused`; print the new state.
- `civ reshape`: re-reads `civ.toml`, writes it to `control_state.shape`, bumps the epoch, prints each changed key as `solvers 3 -> 5`; with no change it says so and does not bump.
- `civ restore --yes` (refuses without `--yes`), in one transaction: bump the epoch; for every lineage whose head agent is `live` or `handing_off`, write a stub handoff (five headings; `State of work` names the held task and the agent's last ten post seqs, `Files worth reading` is empty because the CLI never touches `agents/`), with `stub = true`, and set the agent `dead`; set every `leased` task to `open` with the lease cleared, attempts unchanged, `next_attempt_at = now()`; reset the knobs that `civ.toml` seeds to its values and clear `set_by`, `ttl_s`, `expires_at` on every knob; write `last_restore`; insert a broadcast `Restored at <time> into epoch <n>: <k> lineages queued for stubs, <l> leases released, about <m> minutes of activity after <last event time> are lost`; print the same line. **Deviation**: the design's "reset target concurrency" is the shape's `solvers`, which the restore leaves as written in `civ.toml`.

## Live checks (integrator, cents)

1. `civ status` and `civ inbox` on the database after an M4 run.
2. A Haiku demo with `civ pause` issued after ten seconds: no new spawns while paused (`governor.wait` events), successions resume after `civ resume`.
3. `civ say` and `civ vote` during a run: the broadcast appears in the next tool result of every live session, the vote is acknowledged, `civ vote --report` lists who missed it.
4. `scripts/restore_drill.sh`: two epochs in the events, every mid-flight lineage resumed at generation N+1 from a stub, eight of eight done.

## Deviations summary (record in README)

Shape is written to `control_state` at start as well as on reshape; `ttl_default_s` is applied by `civ_tune`; the restore does not reset concurrency separately from the shape; per-agent lifetime budgets and per-generation caps are not built (the spend ceiling is the budget); the CLI's restore stub lists seqs rather than the M3 builder's excerpts, because the CLI imports no harness code; `max_live_agents` counts supervisors.
