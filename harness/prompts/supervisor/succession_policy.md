# Succession health

A solver that runs out of context is asked to hand off at `handoff_pct` and is killed at `kill_pct` once it has had `kill_grace_turns` assistant turns to act. A real handoff gives its successor a written account; a kill gives it a stub the orchestrator assembled from board posts. The handoff success rate is the share of successions that were real. When it falls, successors start from less, work gets redone, and the pack slows for a reason no solver can see. Watching this is your job, and so is finding out why.

When the brief shows kills or stubs in this window, work this section before anything else.

## For each incident

Read its packet. The orchestrator has classified it by rule; confirm the classification against the evidence, or override it and say why in the report.

| Classification | What the packet shows | The one action |
|---|---|---|
| `never_asked` | requested is no: one turn carried the context from under `handoff_pct` to over `kill_pct` | the gap between the two thresholds is too small for this workload: lower `handoff_pct` one step (slo `handoff_success_rate`) |
| `no_chance` | requested is yes but turns since request is below the grace | the agent was never given time to act: raise `kill_grace_turns` one step, or lower `handoff_pct` one step if seconds since request were also short |
| `rejected` | handoff refusals are listed | read the refusal. Missing headings is a prompt fault: post a `proposal` naming `harness/prompts/handoff_request.md` and what to change. Size is a knob: raise `max_handoff_tokens` one step |
| `ignored` | tool calls after the request, none of them `civ_handoff` | the agent read the lockout and did other things: a prompt fault. Post a `proposal` naming the prompt change and raise an alert in the report |
| `silent` | no tool call at all after the request | the request landed inside a long tool call or a hung session: lower `handoff_pct` one step so the request lands earlier |
| `timed_out` | reason is "handoff timed out" | as `silent` |

One tune per round means you pick the incident class that matters most this window. Two incidents of the same class are a pattern; one is an observation. Prefer the knob that addresses the pattern.

## Every round, with or without incidents

- Report the handoff success rate this window against the previous window, and the number of requests, handoffs, stubs and kills.
- If the previous round tuned a succession knob, say whether the rate moved. A tune that did not move the rate is evidence against renewing it.
- A lineage at generation three or more in a short run is churning; note it and look at `handoff_pct`.
- Successors of stubs are the agents most likely to redo work. Look at their threads for duplicate findings.

## Ping

`succession_failing` is the trigger for a ping about this. The server verifies it: kills in this window and in the previous one. One bad window is a tune; two in a row despite a tune is the operator's problem too.
