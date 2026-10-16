# Ping template

A ping is a report with `trigger` set. Its body is read by a human who has not been watching, so it is self-contained and short:

```
[trigger name] in one line: what has gone wrong and for how many windows.
Numbers: the SLO or succession figures this window and the previous ones, with n.
Tried: the tunes and replies the supervisors have already made and what they changed.
Decision for you: the one thing only the operator can do (move a ceiling, change the pack, broadcast, stop).
Evidence: board seqs and agent ids.
```

Do not pad it. The operator can drill into the seqs.
