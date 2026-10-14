-- civ board schema. Canonical definition; migrations/0001_init.sql applies it verbatim
-- and adds the config seeds. All timestamps are timestamptz.

create table if not exists agents (
  id text primary key,
  role text not null check (role in ('solver', 'supervisor')),
  generation int not null default 1,
  lineage_id text not null,
  harness_version text not null default 'dev',
  status text not null default 'live' check (status in ('live', 'handing_off', 'ended', 'dead')),
  cwd text not null,
  token text not null unique,
  tokens_in bigint not null default 0,
  tokens_out bigint not null default 0,
  context_pct real not null default 0,
  spawned_at timestamptz not null default now(),
  ended_at timestamptz
);

-- messages.id is the board's sequence number; the API returns it as both id and seq.
create table if not exists messages (
  id bigserial primary key,
  author_id text not null,
  kind text not null check (kind in ('task', 'finding', 'question', 'blocker', 'proposal',
                                     'handoff', 'broadcast', 'alert', 'report')),
  thread_id bigint references messages(id),
  parent_id bigint references messages(id),
  body text not null,
  refs text[] not null default '{}',
  read_seq bigint,
  created_at timestamptz not null default now()
);
create index if not exists messages_thread_idx on messages (thread_id, id);
create index if not exists messages_kind_created_idx on messages (kind, created_at);
create index if not exists messages_author_created_idx on messages (author_id, created_at);

create table if not exists votes (
  message_id bigint not null references messages(id),
  voter_id text not null,
  value smallint not null check (value in (-1, 1)),
  weight real not null,
  reason text not null,
  created_at timestamptz not null default now(),
  primary key (message_id, voter_id)
);
create index if not exists votes_voter_created_idx on votes (voter_id, created_at);

create table if not exists tasks (
  id text primary key,
  thread_id bigint not null references messages(id),
  title text not null,
  description text not null,
  status text not null default 'open' check (status in ('open', 'leased', 'done', 'blocked')),
  priority_base real not null default 0,
  lease_owner text references agents(id),
  lease_expires timestamptz,
  attempts int not null default 0,
  next_attempt_at timestamptz not null default now(),
  result_ref text,
  score real,
  notes text,
  completed_by text,
  completed_at timestamptz
);
create index if not exists tasks_status_idx on tasks (status, next_attempt_at);

create table if not exists config (
  knob text primary key,
  value double precision not null,
  min double precision not null,
  max double precision not null,
  step double precision not null,
  cooldown_s int not null default 1200,
  ttl_s int,
  set_by text,
  reason text,
  set_at timestamptz not null default now(),
  expires_at timestamptz
);

create table if not exists events (
  seq bigserial primary key,
  ts timestamptz not null default now(),
  agent_id text,
  type text not null,
  payload jsonb not null default '{}'
);
create index if not exists events_agent_type_ts_idx on events (agent_id, type, ts);
create index if not exists events_type_ts_idx on events (type, ts);

create table if not exists handoffs (
  id bigserial primary key,
  lineage_id text not null,
  from_agent text not null,
  to_agent text,
  generation int not null,
  body text not null,
  stub boolean not null default false,
  created_at timestamptz not null default now()
);

-- Server bookkeeping. These live in Postgres so the board server stays stateless and restartable.
create table if not exists session_reads (
  agent_id text primary key,
  last_seq bigint not null default 0,
  updated_at timestamptz
);

create table if not exists broadcast_seen (
  agent_id text primary key,
  last_seq bigint not null default 0
);

create table if not exists operator_pins (
  message_id bigint primary key references messages(id),
  pinned_until timestamptz not null
);

create table if not exists operator_acks (
  message_id bigint not null,
  agent_id text not null,
  acked_at timestamptz default now(),
  primary key (message_id, agent_id)
);
