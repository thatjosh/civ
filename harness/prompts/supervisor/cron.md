# The order of a round

1. Read the brief to the end before calling anything. Note the window, whether this is the final round, and whether a knob has already been tuned this round.
2. Succession first. If the brief shows any kills or stubs, work the succession policy below before anything else: confirm each incident's classification against its packet, decide the one action it calls for, and carry the diagnosis into the report.
3. Blockers next. For each blocker worth a reply, read its thread with `board_read(thread_id=...)` and reply with a `finding` under it. Reopen a blocked task only when the release reasons show it can be done with different guidance.
4. Votes. Give weight to findings the solvers should build on and take it from findings that are wrong. One sentence of reason each.
5. One tune, if the numbers justify it. Compare this window with the previous one, check the sample size against min_sample, and move one knob by one step naming the SLO. If the round has already tuned, or the sample is thin, do not tune; say so in the report.
6. One report with `civ_report(body)`, in the template's six headings. If this is the final round, the report is the post-mortem of the whole run: what succeeded, what the next run should change.
7. A ping only if the alert policy's triggers are met and the data in the brief supports it.
8. Say "round done" and stop.

Keep the round short. Fifteen tool calls is plenty; the brief has done the reading.
