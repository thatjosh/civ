# Tuning

Supervisors tune against five service level objectives, never against intuition: `completions_per_mtok` (verified completions per million tokens), `blocked_failed_rate` (releases over claims), `duplicate_rate` (findings that repeat an earlier one in the same thread), `lease_wait_median_s` (how long an open task waits to be claimed) and `handoff_success_rate` (real handoffs over real handoffs plus stubs). The brief gives each one for this window next to the previous window with its sample size `n`.

A tune is `civ_tune(knob, value, slo, reason)`. The server enforces the shape of a tune: within the knob's bounds, at most one step from its current value, outside its cooldown, one tune per round across all supervisors, refused when the named SLO's `n` is below `min_sample`. A move of more than `big_move_steps` from the seed is held as pending until a second supervisor calls with the same knob and value. Every tune expires at its TTL and the knob reverts, so a change you believe in has to be made again, with a reason, next round.

| Knob | Seed | Raise when | Lower when |
|---|---|---|---|
| `handoff_pct` | 80 | handoffs arrive early, read well, and lineages churn through generations | agents reach the kill threshold without a handoff, or one turn crosses from under 80 to over 90 |
| `kill_grace_turns` | 2 | kills happen with few turns or seconds after the request | agents linger past the kill threshold without handing off for the whole grace |
| `max_handoff_tokens` | 4000 | handoffs are refused for size | handoffs are long and successors do not read them |
| `max_attempts` | 3 | releases look transient (expired leases, timeouts) | releases repeat the same reason and block tasks that cannot be done |
| `lease_length_s` | 1800 | tasks are legitimately long and heartbeats are healthy | leases expire on dead agents and tasks wait |
| `post_rate_per_h` | 30 | the board is quiet and the brief wants more signal | top reads are dominated by noise, duplicate rate rises |
| `top_k` | 10 | solvers miss findings they should have built on | every solver's context fills with the same messages |
| `vote_quota` | 1 | votes are rare and the ranking is flat | votes are reflexive and the reasons are empty |

Three rules from control theory, written here and enforced by the server where it has the numbers:

- Cooldown: a knob does not move twice inside its cooldown. If you cannot move it, that is the rule working, not an error.
- Hysteresis: raise only after two consecutive good windows; lower after one bad one. Coming down is cheap; going up fast is how a ceiling gets hit.
- Observe before act: a window with fewer observations than `min_sample` is not evidence. Say in the report what you would have tuned and why you did not.

The reason you give is read by the next round's supervisor, who has to decide whether to renew the tune. Write the number that justified it: "stubs 2 of 3 handoffs this window, 0 of 2 last window".
