"""The two control files as the M5 contract writes them, for tests that need a control directory."""

from __future__ import annotations

from pathlib import Path

CEILINGS_TOML = """
max_live_agents = 8
max_tokens_per_minute = 3000000
max_spend_per_day_usd = 5.0
max_handoff_tokens = 8000
model = "claude-opus-5-5"
"""

CIV_TOML = """
[population]
solvers = 3
supervisors = 1
[cadence]
supervisor_round_s = 600
[lifecycle]
max_generations = 5
handoff_pct = 80
kill_pct = 90
kill_grace_turns = 2
[voting]
decay_offset_h = 2
decay_power = 1.5
operator_pin_s = 300
[governance]
vote_quota = 1
min_sample = 3
big_move_steps = 2
ttl_default_s = 3600
"""


def write_control_dir(root: Path, ceilings: str = CEILINGS_TOML, civ: str = CIV_TOML) -> Path:
    control = root / "control"
    control.mkdir(parents=True, exist_ok=True)
    (control / "ceilings.toml").write_text(ceilings, encoding="utf-8")
    (control / "civ.toml").write_text(civ, encoding="utf-8")
    return control
