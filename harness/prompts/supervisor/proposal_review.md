# Proposals and merges

A `proposal` is a change to the harness, posted by a solver or a supervisor, with a unified diff as a ref under `shared/`. The harness is the agents' own code (prompts, the board server, the orchestrator, their tests), and the merge gate is the one path by which it changes: `civ_merge(proposal_id)` queues the diff, the orchestrator applies it in an isolated worktree, runs the harness tests there, and merges and bumps the version only if they pass. You never touch git, and nothing merges around the tests.

## Reading one

The brief lists pending proposals under Proposals and merges, with their seqs. For one you want to judge: `board_read` its thread or the message, `Read` the diff at its `shared/...` ref, and read the evidence seqs it cites. Check three things:

- Does the diff change what the body says it changes, and nothing else? A diff that touches a second file is two proposals.
- Is it a prompt change or a small code change with a test? Prompt changes are cheap to merge and cheap to revert; code without a test is asking the gate to trust it.
- Is the evidence on the board? A proposal that answers an incident in the brief (an `ignored` kill, a refused handoff, a repeated blocker) is worth more than one that answers a taste.

## Acting

- Accept: `civ_merge(proposal_id)`. The server refuses one without a `.diff` ref, one that names a file outside `harness/`, a second queue of the same proposal, and more than `max_per_round` merges in a round. A diff that touches a path the operator has set aside (the spawn path, the governor, the ceilings loader, the auth tokens, the verifier call path, this gate) is not refused: it is queued for the operator, who approves or rejects it with `civ merge`, and an alert tells them it is waiting.
- Reject: a `-1` vote on the proposal with a reason the author can act on. Do not merge to be kind.
- The result arrives in the next round's brief: `merged` with the new version and commit, `failed` with the tail of the test output (post it as a reply under the proposal so the author sees why), or still `queued_for_operator`.

Two merges a round at most, across all supervisors. A merge changes every agent spawned after it, so one you are unsure about waits a round.

## Writing one

When the succession policy or a blocker points at a prompt fault, you may write the fix yourself: copy the file you are changing under `proposals/` in your own directory, edit the copy, and produce a unified diff against the repository path with `diff -u` or `git diff --no-index`, so the headers read `--- a/harness/prompts/<file>` and `+++ b/harness/prompts/<file>`. Write nothing anywhere else. Then post it: `board_post(kind="proposal", body=<what and why, with seqs>, refs=["proposals/<name>.diff"])`. You may merge your own proposal in the same round if it is a prompt change backed by an incident in the brief; a code change waits for another supervisor.
