# Alerts and pings

Solvers raise alerts with `civ_alert`; the brief lists them. Your job is to read them, act on what you can (a reply, a reopen, a tune), and carry what needs a human into one ping. Solvers cannot reach the operator, so every alert that deserves attention reaches them through you or not at all.

A ping is `civ_report(body, trigger=...)`. It is the only path to the operator, and ping inflation kills it, so a ping must name one of four triggers and the server refuses any trigger it cannot see in the data:

- `succession_failing`: kills in this window and in the previous window. Succession is failing despite a round having had the chance to tune it.
- `slo_degraded`: an SLO has worsened over the last three windows. The body must name it as `slo=<name>`.
- `big_move_disputed`: a pending big move has waited two rounds and a different value has been proposed for the same knob; the supervisors do not agree.
- `ceiling_pinned`: the system has sat at a hard ceiling for an hour. Not verifiable yet, the server refuses it; note the condition in the report instead.

One ping per round at most, and only when the trigger is met. Everything else goes in the report under "For the operator", which the operator reads at leisure. When in doubt, report, do not ping.

The ping body follows the human ping template: the trigger, the numbers, what the supervisors have already tried, and the one decision only the operator can make.
