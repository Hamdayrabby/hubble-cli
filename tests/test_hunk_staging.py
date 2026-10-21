from pathlib import Path
import pytest

from hubble.tools import (
    ApplyPatch,
    ReadFile,
    ToolContext,
    apply_selected_hunks,
    split_patch_into_hunks,
)

SAMPLE_FILE = """line 1
line 2
line 3
line 4
line 5
line 6
line 7
line 8
line 9
line 10
"""

SAMPLE_PATCH = """--- a/test.txt
+++ b/test.txt
@@ -2,3 +2,3 @@
 line 2
-line 3
+line 3 MODIFIED
 line 4
@@ -7,3 +7,3 @@
 line 7
-line 8
+line 8 MODIFIED
 line 9
"""


def test_split_patch_into_hunks():
    hunks = split_patch_into_hunks(SAMPLE_PATCH)
    assert len(hunks) == 2
    assert "line 3 MODIFIED" in hunks[0]
    assert "line 8 MODIFIED" not in hunks[0]
    assert "line 8 MODIFIED" in hunks[1]
    assert "line 3 MODIFIED" not in hunks[1]
    assert "--- a/test.txt" in hunks[0]
    assert "--- a/test.txt" in hunks[1]


def test_apply_selected_hunks_all():
    updated, count = apply_selected_hunks(SAMPLE_FILE, SAMPLE_PATCH, selected_hunk_indices=None)
    assert count == 2
    assert "line 3 MODIFIED" in updated
    assert "line 8 MODIFIED" in updated
    assert "line 3\n" not in updated
    assert "line 8\n" not in updated


def test_apply_selected_hunks_first_only():
    updated, count = apply_selected_hunks(SAMPLE_FILE, SAMPLE_PATCH, selected_hunk_indices=[0])
    assert count == 1
    assert "line 3 MODIFIED" in updated
    assert "line 8 MODIFIED" not in updated
    assert "line 8\n" in updated


def test_apply_selected_hunks_second_only():
    updated, count = apply_selected_hunks(SAMPLE_FILE, SAMPLE_PATCH, selected_hunk_indices=[1])
    assert count == 1
    assert "line 3 MODIFIED" not in updated
    assert "line 3\n" in updated
    assert "line 8 MODIFIED" in updated


def test_apply_selected_hunks_none():
    updated, count = apply_selected_hunks(SAMPLE_FILE, SAMPLE_PATCH, selected_hunk_indices=[])
    assert count == 0
    assert updated == SAMPLE_FILE


def test_apply_patch_tool_selected_hunks(tmp_path: Path):
    target = tmp_path / "code.txt"
    target.write_text(SAMPLE_FILE, encoding="utf-8")

    ctx = ToolContext(root=tmp_path)
    read_tool = ReadFile()
    patch_tool = ApplyPatch()

    # Pre-read
    read_tool.run({"path": "code.txt"}, ctx)

    # Preview selective hunk
    prev = patch_tool.preview({
        "path": "code.txt",
        "patch": SAMPLE_PATCH,
        "selected_hunks": [1],
    }, ctx)
    assert "Staged 1 selected hunk(s)" in prev
    assert "+line 8 MODIFIED" in prev

    # Apply only hunk 1
    res = patch_tool.run({
        "path": "code.txt",
        "patch": SAMPLE_PATCH,
        "selected_hunks": [1],
    }, ctx)
    assert "Applied 1 hunk to code.txt" in res

    content = target.read_text(encoding="utf-8")
    assert "line 3\n" in content
    assert "line 8 MODIFIED" in content
