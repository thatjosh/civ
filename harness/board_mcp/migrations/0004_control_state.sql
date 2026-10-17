-- 0004_control_state: M5. The operator's state and the epoch, copied verbatim from the block after
-- the "migration 0004" marker in schema.sql (a test checks the copy).
--
-- control_state is the handful of values the civ command line and the orchestrator share through
-- the database rather than a file: whether spawning is paused, the epoch, the shape the run uses,
-- how far the operator has read the inbox, and the last restore. The epoch is exposed through a
-- SQL function so that every table that stamps it gets the value as a column default, and no
-- writer (server, orchestrator, CLI) has to know the epoch exists.
create table if not exists control_state (
  key text primary key,            -- epoch | paused | shape | inbox_last_seq | last_restore
  value jsonb not null,
  updated_at timestamptz not null default now()
);
insert into control_state (key, value) values ('epoch', '1'), ('paused', 'false'), ('inbox_last_seq', '0')
on conflict (key) do nothing;

-- The epoch is read by a function so every writer stamps it without knowing about it. The table is
-- schema-qualified because pg_dump restores run with an empty search_path, and the default on
-- events fires during their COPY.
create or replace function public.current_epoch() returns int language sql stable as
  $$ select coalesce((select (value #>> '{}')::int from public.control_state where key = 'epoch'), 1) $$;
alter table agents add column if not exists epoch int not null default public.current_epoch();
alter table events add column if not exists epoch int not null default public.current_epoch();
create index if not exists events_epoch_ts_idx on events (epoch, ts);
