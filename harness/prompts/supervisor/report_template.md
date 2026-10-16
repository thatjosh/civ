# Report template

One report per supervisor per round with `civ_report(body)`. Six headings, in this order, each present even when its content is "nothing this window". One or two short paragraphs or a few bullets under each. Cite board seqs rather than repeating their contents.

```
## Window
## Succession
## Queue and SLOs
## Actions taken
## Diagnosis
## For the operator
```

- Window: round number, the time span, whether this is the final round, how many agents were live.
- Succession: requests, handoffs, stubs, kills, the success rate this window against last, and whether a previous tune moved it.
- Queue and SLOs: tasks open, leased, done, blocked; each SLO against the previous window with its n; what the numbers say.
- Actions taken: every tool call that changed something (replies, reopens, votes, the tune) with the seq or knob and the reason; every refusal you met and what it meant.
- Diagnosis: for each succession incident, the classification you confirmed or overrode and the evidence; for blocked tasks, why they are blocked; for a prompt fault, the proposal you posted.
- For the operator: what a human should know but need not act on now. If you pinged, say so here and name the trigger.

The final round's report is the run's post-mortem: under Diagnosis, what the next run should change in the knobs, the prompts or the pack.
