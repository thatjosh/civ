"""Config seeds: the soft knobs and their bounds.

Kept in Python (not only in the migration SQL) because reset scripts and tests need to re-seed a
truncated config table without replaying migrations. A test asserts the migration seeded the
same rows, so the two copies cannot drift silently.
"""

from __future__ import annotations

# knob -> (value, min, max, step). cooldown_s uses the column default of 1200.
CONFIG_SEEDS: dict[str, tuple[float, float, float, float]] = {
    "post_rate_per_h": (30, 5, 120, 5),
    "vote_rate_per_h": (20, 5, 60, 5),
    "vote_quota": (1, 0, 3, 1),
    "lease_length_s": (1800, 300, 7200, 300),
    "max_attempts": (3, 1, 6, 1),
    "backoff_base_s": (60, 10, 600, 10),
    "top_k": (10, 5, 50, 5),
    "long_body_chars": (2000, 500, 8000, 500),
    "decay_offset_h": (2, 1, 12, 1),
    "decay_power": (1.5, 1, 3, 0.25),
    "handoff_pct": (80, 50, 90, 5),
    "kill_pct": (90, 60, 95, 5),
    "max_handoff_tokens": (4000, 1000, 8000, 500),
    # Assistant turns an agent is given after the handoff request before the kill threshold is
    # allowed to act. Two is the minimum that lets it read the lockout and make the call.
    "kill_grace_turns": (2, 0, 10, 1),
    # M4 round rules. min_sample is the window sample below which no tune is accepted (3 for the
    # eight-task demo pack; the design's 10 arrives with civ.toml in M5). big_move_steps is how far
    # from its seed a knob may sit before a second supervisor has to agree.
    "min_sample": (3, 1, 50, 1),
    "big_move_steps": (2, 1, 5, 1),
    # Wall-clock floor on the same grace (M6): three sub-second tool calls are turns too, and an
    # agent cannot write a handoff in under a couple of seconds whatever its turn count says.
    "kill_grace_s": (20, 0, 300, 5),
}

SEED_SQL = """
insert into config (knob, value, min, max, step)
values (%(knob)s, %(value)s, %(min)s, %(max)s, %(step)s)
on conflict (knob) do update
  set value = excluded.value, min = excluded.min, max = excluded.max, step = excluded.step,
      set_by = null, reason = null, set_at = now(), expires_at = null
"""


def seed_config(conn) -> None:
    """Upsert every knob to its seed value on an open psycopg connection."""
    with conn.cursor() as cur:
        for knob, (value, lo, hi, step) in CONFIG_SEEDS.items():
            cur.execute(SEED_SQL, {"knob": knob, "value": value, "min": lo, "max": hi, "step": step})
