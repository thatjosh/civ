-- 0005_merges: M6. The merge gate's queue, copied verbatim from the block after the
-- "migration 0005" marker in schema.sql (a test checks the copy).
--
-- A merges row is one proposal's trip through the gate: the server queues it in milliseconds,
-- the orchestrator's merger applies the diff in a worktree, runs the tests and records the result
-- here, and a proposal touching an operator-set path waits in queued_for_operator for
-- `civ merge --approve`. kill_grace_s is the wall-clock floor on the kill grace, the first of the
-- succession follow-ups the merge path exists to carry.
create table if not exists merges (
  id bigserial primary key,
  proposal_id bigint not null references messages(id),
  requested_by text not null,
  round_id bigint references rounds(id),
  diff_ref text not null,                 -- shared/<hash>-<name>.diff
  paths text[] not null default '{}',     -- files the diff touches, from its headers
  status text not null check (status in ('queued', 'queued_for_operator', 'running', 'merged', 'failed', 'rejected')),
  detail text,                            -- test output tail, the refusal, or the commit
  version_before text,
  version_after text,
  commit_sha text,
  requested_at timestamptz not null default now(),
  finished_at timestamptz
);
insert into config (knob, value, min, max, step) values ('kill_grace_s', 20, 0, 300, 5) on conflict (knob) do nothing;
