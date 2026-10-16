-- 0003_supervisor_rounds: M4. The two supervisor tables, copied verbatim from the block after
-- the "migration 0003" marker in schema.sql (a test checks the copy), then the two knobs the
-- round rules read.
--
-- A round is one supervisor cron tick: opened by the orchestrator before the supervisor sessions
-- spawn, closed when they have all ended. The brief is stored as built so that a later round (or
-- an operator) can verify a "degraded for three windows" claim against what the supervisors saw.
-- tune_proposals is the first half of a two-supervisor commit: a big move is recorded there and
-- applied only when a different supervisor repeats it in the same round or the next.

create table if not exists rounds (
  id bigserial primary key,
  number int not null,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  window_start timestamptz not null,
  final boolean not null default false,
  brief jsonb not null default '{}',
  tuned_knob text,
  tuned_by text,
  summary jsonb not null default '{}'
);

create table if not exists tune_proposals (
  id bigserial primary key,
  knob text not null,
  value double precision not null,
  proposed_by text not null,
  round_id bigint not null references rounds(id),
  created_at timestamptz not null default now(),
  committed_by text,
  committed_at timestamptz
);

-- Must match harness/board_mcp/seed.py (a test checks this). min_sample seeds at 3 rather than
-- the design's 10 because the demo pack has eight tasks; M5's civ.toml raises it for production.
insert into config (knob, value, min, max, step) values
  ('min_sample', 3, 1, 50, 1),
  ('big_move_steps', 2, 1, 5, 1)
on conflict (knob) do nothing;
