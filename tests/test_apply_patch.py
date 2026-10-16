from pathlib import Path
import pytest

from hubble.tools import (
    ToolContext,
    ToolError,
    ApplyPatch,
    ReadFile,
    parse_unified_diff,
    apply_unified_patch,
    apply_unified_patch_with_count,
)


def test_parse_unified_diff_single_and_multi_hunk():
    diff_text = """diff --git a/foo.py b/foo.py
index 123..456 100644
--- a/foo.py
+++ b/foo.py
@@ -10,3 +10,4 @@
 def a():
-    return 1
+    # note
+    return 2
@@ -25,2 +26,3 @@
 def b():
+    pass
     return True
"""
    hunks = parse_unified_diff(diff_text)
    assert len(hunks) == 2
    assert hunks[0].old_start == 10
    assert hunks[0].old_lines == 3
    assert hunks[0].new_start == 10
    assert hunks[0].new_lines == 4
    assert hunks[1].old_start == 25
    assert hunks[1].old_lines == 2


def test_parse_unified_diff_missing_hunks():
    with pytest.raises(ToolError, match="No unified diff hunks found"):
        parse_unified_diff("just some random text without headers")


def test_apply_patch_exact_single_hunk():
    original = "line 1\nline 2\nline 3\nline 4\nline 5\n"
    patch = """@@ -2,3 +2,3 @@
 line 2
-line 3
+line 3 modified
 line 4
"""
    result = apply_unified_patch(original, patch)
    expected = "line 1\nline 2\nline 3 modified\nline 4\nline 5\n"
    assert result == expected


def test_apply_patch_multi_hunk_with_offset():
    original = "\n".join(f"line {i}" for i in range(1, 31)) + "\n"
    # Hunk 1 inserts 2 lines at line 5
    # Hunk 2 modifies line 20 (which is now shifted to line 22)
    patch = """@@ -5,2 +5,4 @@
 line 5
+inserted a
+inserted b
 line 6
@@ -20,2 +22,2 @@
-line 20
+line 20 modified
 line 21
"""
    result = apply_unified_patch(original, patch)
    lines = result.splitlines()
    assert lines[4] == "line 5"
    assert lines[5] == "inserted a"
    assert lines[6] == "inserted b"
    assert lines[7] == "line 6"
    assert "line 20 modified" in lines
    assert "line 20" not in lines


def test_apply_patch_fuzzy_offset_search():
    # Hunk says line 10, but the target code is actually at line 14
    original = "\n".join(f"line {i}" for i in range(1, 25)) + "\n"
    patch = """@@ -10,3 +10,3 @@
 line 14
-line 15
+line 15 changed
 line 16
"""
    result = apply_unified_patch(original, patch)
    assert "line 15 changed" in result
    assert "line 15\n" not in result


def test_apply_patch_fuzzy_trailing_whitespace():
    # Original has trailing whitespace on line 3
    original = "def foo():\n    x = 1  \n    return x\n"
    # Patch context does not have trailing whitespace
    patch = """@@ -1,3 +1,3 @@
 def foo():
     x = 1
-    return x
+    return x + 1
"""
    result = apply_unified_patch(original, patch)
    assert "return x + 1" in result


def test_apply_patch_blank_lines_without_leading_space():
    # LLM forgot leading space on blank line between func a and func b
    original = "def a():\n    return 1\n\ndef b():\n    return 2\n"
    patch = "@@ -1,5 +1,5 @@\n def a():\n-    return 1\n+    return 10\n\n def b():\n"
    result = apply_unified_patch(original, patch)
    assert "return 10" in result
    assert "def b():" in result


def test_apply_patch_crlf_preservation():
    original = "line 1\r\nline 2\r\nline 3\r\n"
    patch = """@@ -1,3 +1,3 @@
 line 1
-line 2
+line 2 updated
 line 3
"""
    result = apply_unified_patch(original, patch)
    assert "\r\n" in result
    assert result == "line 1\r\nline 2 updated\r\nline 3\r\n"


def test_apply_patch_context_mismatch_error():
    original = "hello world\nthis is a test\n"
    patch = """@@ -1,2 +1,2 @@
 non_existent_line
-this is a test
+this is an error
"""
    with pytest.raises(ToolError, match="failed to match context"):
        apply_unified_patch(original, patch, path_for_err="test.txt")


def test_apply_patch_tool_run_and_undo(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    file_p = tmp_path / "calc.py"
    file_p.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    tool = ApplyPatch()
    read_tool = ReadFile()

    # Precheck fails if not read first
    with pytest.raises(ToolError, match="You must read_file"):
        tool.run({"path": "calc.py", "patch": "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"}, ctx)

    # Read file to satisfy freshness
    read_tool.run({"path": "calc.py"}, ctx)

    # Preview returns valid unified diff
    preview = tool.preview({"path": "calc.py", "patch": "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"}, ctx)
    assert "-    return a - b" in preview
    assert "+    return a + b" in preview

    # Apply patch
    out = tool.run({"path": "calc.py", "patch": "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"}, ctx)
    assert "Applied 1 hunk" in out
    assert file_p.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"

    # Undo restores original
    restored = ctx.undo()
    assert "calc.py" in restored
    assert file_p.read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
