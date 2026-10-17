# 01: a one-line wording change to the handoff request

Adds a clause to the last line of `harness/prompts/handoff_request.md`: the successor reads the handoff, not the board. Touches a prompt only, changes no code, and breaks no test.

What it proves: the green path. `civ_merge` queues it, the merger applies it in a worktree, the harness tests pass, the commit lands, `harness/VERSION` bumps its patch number, and the next spawn's identity block carries the new version.
