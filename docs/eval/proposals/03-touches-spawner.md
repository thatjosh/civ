# 03: a comment in the spawner, which is a protected path

Adds one comment line above the solver tool list in `harness/orchestrator/spawner.py`. Harmless in itself, applies cleanly, breaks nothing.

What it proves: the operator path. The spawn path is on the protected list in `control/civ.toml`, so `civ_merge` records the merge as queued for the operator and posts an alert instead of queueing it for the merger. `civ merge --list` shows it, `civ merge --approve` releases it, and only then does the merger run it and bump the version.
