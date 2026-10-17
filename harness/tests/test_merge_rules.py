"""M6: the merge gate's path rules, shared by the server and the merger."""

from __future__ import annotations

from harness.board_mcp.merge_rules import (DEFAULT_PROTECTED_PATHS, DEFAULT_TEST_COMMAND, load_merge_section,
                                           outside_harness, paths_in_diff, protected)

GIT_STYLE = """diff --git a/harness/prompts/handoff_request.md b/harness/prompts/handoff_request.md
index 1111111..2222222 100644
--- a/harness/prompts/handoff_request.md
+++ b/harness/prompts/handoff_request.md
@@ -1,2 +1,2 @@
-old
+new
diff --git a/harness/board_mcp/stub.py b/harness/board_mcp/stub.py
--- a/harness/board_mcp/stub.py
+++ b/harness/board_mcp/stub.py
@@ -1 +1 @@
-x
+y
"""

PLAIN_STYLE = """--- harness/prompts/x.md\t2026-10-17
+++ harness/prompts/x.md\t2026-10-17
@@ -1 +1 @@
-a
+b
"""

DELETION = """--- a/harness/prompts/gone.md
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""


def test_paths_in_diff_reads_git_and_plain_headers_once_each():
    assert paths_in_diff(GIT_STYLE) == ["harness/prompts/handoff_request.md", "harness/board_mcp/stub.py"]
    assert paths_in_diff(PLAIN_STYLE) == ["harness/prompts/x.md"]


def test_paths_in_diff_handles_a_deletion_and_an_empty_diff():
    assert paths_in_diff(DELETION) == ["harness/prompts/gone.md"]
    assert paths_in_diff("") == [] and paths_in_diff("not a diff at all") == []


def test_protected_matches_exact_entries_and_directory_prefixes():
    rules = ("harness/orchestrator/spawner.py", "control/")
    assert protected(["harness/orchestrator/spawner.py"], rules) == ["harness/orchestrator/spawner.py"]
    assert protected(["control/civ.toml"], rules) == ["control/civ.toml"]
    # A prefix without the slash is not a directory rule, and a sibling file is not the file.
    assert protected(["harness/orchestrator/spawner_test.py", "controls/x"], rules) == []


def test_outside_harness_names_everything_not_under_harness():
    assert outside_harness(["harness/prompts/a.md", "control/civ.toml", "task/x", "harnessy/y"]) == ["control/civ.toml", "task/x", "harnessy/y"]


def test_load_merge_section_defaults_and_overrides():
    defaults = load_merge_section(None)
    assert defaults == {"max_per_round": 2, "protected_paths": list(DEFAULT_PROTECTED_PATHS), "test_command": DEFAULT_TEST_COMMAND}
    assert load_merge_section({"population": {"solvers": 3}}) == defaults
    custom = load_merge_section({"merge": {"max_per_round": 1, "protected_paths": ["harness/x.py"], "test_command": "true"}})
    assert custom == {"max_per_round": 1, "protected_paths": ["harness/x.py"], "test_command": "true"}


def test_cli_approval_marker_matches_merge_rules():
    # The CLI is self-contained and repeats the literal; the merger reads it through merge_rules.
    from pathlib import Path

    from harness.board_mcp.merge_rules import APPROVED_MARKER, operator_approved

    cli = (Path(__file__).resolve().parents[2] / "control" / "cli" / "civ.py").read_text()
    assert APPROVED_MARKER in cli
    assert operator_approved({"detail": f"protected paths: x {APPROVED_MARKER}"})
    assert not operator_approved({"detail": "protected paths: x"}) and not operator_approved({})
