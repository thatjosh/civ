-- M3 follow-up: the kill rule becomes "kill_pct and at least kill_grace_turns assistant turns since
-- the handoff request". Observed in the 30k-window demo: one agent was killed 2.7 seconds after its
-- request and another jumped past both thresholds in one turn and was never asked at all.
-- Must match harness/board_mcp/seed.py (a test checks this).
insert into config (knob, value, min, max, step) values
  ('kill_grace_turns', 2, 0, 10, 1)
on conflict (knob) do nothing;
