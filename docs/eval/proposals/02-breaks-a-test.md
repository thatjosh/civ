# 02: a stub-builder change that breaks a harness test

Sorts an agent's posts newest first in `harness/board_mcp/stub.py`, where the successor is meant to read them oldest first. Plausible on its face, and `test_build_stub_has_the_five_headings_and_the_facts` in `harness/tests/test_succession_server.py` fails on its ordering assertion; the other stub test still passes.

What it proves: the red path. The merger applies it in a worktree, the tests fail there, nothing is committed, the version does not move, and the merges row carries the tail of the test output for the next round's supervisor to read.
