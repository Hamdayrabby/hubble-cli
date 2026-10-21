from pathlib import Path
import pytest

from hubble.tools import EditFile, FileTransaction, ToolContext, ToolError, WriteFile, run_tool


def test_pre_commit_syntax_validation_rejects_broken_python(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    py_file = tmp_path / "valid.py"
    py_file.write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    ctx.read_mtimes[str(py_file)] = py_file.stat().st_mtime

    # Edit that introduces broken syntax
    edit_tool = EditFile()
    out, is_err = run_tool(edit_tool, {
        "path": "valid.py",
        "old_string": "return 'world'",
        "new_string": "return (unclosed parenthesis",
    }, ctx)

    assert is_err
    assert "Pre-commit syntax validation failed" in out
    # Verify file was not corrupted on disk!
    assert "return 'world'" in py_file.read_text(encoding="utf-8")


def test_pre_commit_syntax_validation_can_be_skipped(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    py_file = tmp_path / "snippet.py"
    py_file.write_text("def foo():\n    pass\n", encoding="utf-8")
    ctx.read_mtimes[str(py_file)] = py_file.stat().st_mtime

    edit_tool = EditFile()
    out, is_err = run_tool(edit_tool, {
        "path": "snippet.py",
        "old_string": "pass",
        "new_string": "incomplete (",
        "skip_syntax_check": True,
    }, ctx)

    assert not is_err
    assert "incomplete (" in py_file.read_text(encoding="utf-8")


def test_file_transaction_commit_and_rollback(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    f1 = tmp_path / "a.py"
    f2 = tmp_path / "b.py"
    f1.write_text("val_a = 1\n", encoding="utf-8")
    f2.write_text("val_b = 2\n", encoding="utf-8")

    tx = ctx.begin_transaction()
    tx.stage_write(f1, "val_a = 100\n")
    tx.stage_write(f2, "val_b = 200\n")

    # Files should not be modified before commit
    assert f1.read_text(encoding="utf-8") == "val_a = 1\n"
    assert f2.read_text(encoding="utf-8") == "val_b = 2\n"

    # Commit
    written = tx.commit()
    assert "a.py" in written and "b.py" in written
    assert f1.read_text(encoding="utf-8") == "val_a = 100\n"
    assert f2.read_text(encoding="utf-8") == "val_b = 200\n"


def test_file_transaction_rollback_on_syntax_error(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    f1 = tmp_path / "a.py"
    f1.write_text("x = 10\n", encoding="utf-8")

    tx = ctx.begin_transaction()
    with pytest.raises(ToolError, match="Pre-commit syntax validation failed"):
        tx.stage_write(f1, "x = (broken syntax")

    # File remains untouched
    assert f1.read_text(encoding="utf-8") == "x = 10\n"
