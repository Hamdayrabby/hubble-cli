"""Unit tests for modern UI/UX enhancements in Hubble CLI:
- ThinkingAccordion & collapsible chain-of-thought
- Advanced 2-column line numbered diff renderer (render_diff)
- Segmented visual context gauge & USD cost estimation
- Semantic mentions (@git, @diff, @todos, @problems) & fuzzy path resolution
- Subcommand autocompletions & Kitty protocol input ergonomics
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from hubble.agent import Agent
from hubble.repl import (
    Command,
    Repl,
    ReplCompleter,
    _estimate_session_cost,
    _is_subsequence,
    _segmented_gauge,
)
from hubble.tools import ToolContext
from hubble.ui import ReplEvents, ThinkingAccordion, render_diff


def test_thinking_accordion_lifecycle():
    con = Console(record=True, width=100)
    acc = ThinkingAccordion(con, show_live=False, theme_name="terminal")
    assert not acc.collapsed
    assert acc.get_full_text() == ""

    acc.feed("Analyzing code structure...")
    acc.feed(" checking AST nodes...")
    assert "Analyzing code structure... checking AST nodes..." in acc.get_full_text()

    acc.collapse()
    assert acc.collapsed
    output = con.export_text()
    assert "▼ Thought for" in output
    assert "tokens" in output
    assert "/thinking view" in output

    # Re-collapsing should be a no-op
    acc.collapse()


def test_thinking_accordion_empty_no_output():
    con = Console(record=True, width=100)
    acc = ThinkingAccordion(con, show_live=False, theme_name="terminal")
    acc.collapse()
    assert acc.collapsed
    assert con.export_text().strip() == ""


def test_render_diff_dual_line_numbers():
    diff_text = """--- a/calc.py
+++ b/calc.py
@@ -10,3 +10,4 @@ def add(a, b):
     x = a
-    return x - b
+    return x + b
+    # note
"""
    rendered = render_diff(diff_text, max_lines=50)
    plain = rendered.plain
    assert "@@ -10 +10 @@ def add(a, b):" in plain
    # Check old line 10 and new line 10
    assert "10 │   10 │" in plain
    assert "x = a" in plain
    # Check deletion on line 11 (old has 11, new is blank)
    assert "11 │" in plain
    assert "-    return x - b" in plain
    # Check addition on line 11 and 12 (old is blank, new has 11 and 12)
    assert "+    return x + b" in plain
    assert "+    # note" in plain


def test_render_diff_truncation():
    lines = ["--- a/test.py", "+++ b/test.py", "@@ -1,20 +1,20 @@"] + [f"+line {i}" for i in range(50)]
    diff_text = "\n".join(lines)
    rendered = render_diff(diff_text, max_lines=10)
    plain = rendered.plain
    assert "more diff lines" in plain


def test_segmented_gauge():
    assert _segmented_gauge(0.0, blocks=6) == "░░░░░░"
    assert _segmented_gauge(0.5, blocks=6) == "███░░░"
    assert _segmented_gauge(1.0, blocks=6) == "██████"
    assert _segmented_gauge(0.33, blocks=6) == "██░░░░"


def test_estimate_session_cost():
    # Claude 3.5 Sonnet: $3/M prompt, $15/M completion
    cost_sonnet = _estimate_session_cost("claude-3-5-sonnet", 10_000, 2_000)
    # (10000*3 + 2000*15) / 1e6 = (30000 + 30000) / 1e6 = 0.06
    assert pytest.approx(cost_sonnet, 0.001) == 0.06

    # GPT-4o: $2.50/M prompt, $10/M completion
    cost_gpt4o = _estimate_session_cost("gpt-4o", 10_000, 1_000)
    # (10000*2.5 + 1000*10) / 1e6 = 0.035
    assert pytest.approx(cost_gpt4o, 0.001) == 0.035

    # DeepSeek: $0.25/M prompt, $1.00/M completion
    cost_deepseek = _estimate_session_cost("deepseek-coder", 100_000, 10_000)
    # (100000*0.25 + 10000*1.0) / 1e6 = 0.035
    assert pytest.approx(cost_deepseek, 0.001) == 0.035


def test_is_subsequence():
    assert _is_subsequence("win", "tests/test_windows_job.py")
    assert _is_subsequence("trans", "tests/test_transactions_and_syntax.py")
    assert _is_subsequence("diff", "@diff")
    assert not _is_subsequence("xyz", "tests/test_windows_job.py")


def test_semantic_mentions_expansion(tmp_path):
    ctx = ToolContext(root=tmp_path)
    ctx.todos = [{"task": "Modernize CLI UX", "done": True}, {"task": "Write comprehensive tests", "done": False}]
    agent = MagicMock()
    agent.ctx = ctx
    agent.last_error = "SyntaxError on line 42"
    store = MagicMock()
    repl = Repl(agent, store)

    # Test @todos
    expanded_todos = repl.expand_mentions("Please review @todos")
    assert "<todos>" in expanded_todos
    assert "[x] Modernize CLI UX" in expanded_todos
    assert "[ ] Write comprehensive tests" in expanded_todos

    # Test @problems
    expanded_problems = repl.expand_mentions("Check @problems in current turn")
    assert "<problems>" in expanded_problems
    assert "SyntaxError on line 42" in expanded_problems

    # Test @git in a git repo
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(stdout="## main...origin/main\n M hubble/ui.py\n")
        expanded_git = repl.expand_mentions("Check @git status")
        assert "<git-status>" in expanded_git
        assert "## main...origin/main" in expanded_git


def test_fuzzy_file_mention(tmp_path):
    sub = tmp_path / "tests"
    sub.mkdir()
    target_file = sub / "test_modern_ui.py"
    target_file.write_text("print('hello modern ui')\n", encoding="utf-8")

    ctx = ToolContext(root=tmp_path)
    agent = MagicMock()
    agent.ctx = ctx
    store = MagicMock()
    repl = Repl(agent, store)

    expanded = repl.expand_mentions("Inspect @modern_ui")
    assert '<file path="tests/test_modern_ui.py">' in expanded
    assert "hello modern ui" in expanded


def test_repl_completer_subcommands():
    agent = MagicMock()
    agent.ctx.root = Path(".")
    store = MagicMock()
    repl = Repl(agent, store)
    completer = ReplCompleter(repl)

    doc = MagicMock()
    doc.text_before_cursor = "/route "
    completions = list(completer.get_completions(doc, None))
    labels = [c.text for c in completions]
    assert "fast " in labels
    assert "strong " in labels
    assert "auto" in labels

    doc.text_before_cursor = "/thinking "
    completions = list(completer.get_completions(doc, None))
    labels = [c.text for c in completions]
    assert "view" in labels
    assert "on" in labels
    assert "off" in labels

    doc.text_before_cursor = "/multiline "
    completions = list(completer.get_completions(doc, None))
    labels = [c.text for c in completions]
    assert "on" in labels
    assert "off" in labels


def test_repl_completer_semantic_mentions():
    agent = MagicMock()
    agent.ctx.root = Path(".")
    store = MagicMock()
    repl = Repl(agent, store)
    completer = ReplCompleter(repl)

    doc = MagicMock()
    doc.text_before_cursor = "Inspect @"
    completions = list(completer.get_completions(doc, None))
    tags = [c.text for c in completions]
    assert "git" in tags
    assert "diff" in tags
    assert "todos" in tags
    assert "problems" in tags


def test_repl_events_thinking_integration():
    con = Console(record=True)
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx, show_reasoning=False)

    events.turn_start()
    assert events.accordion is not None
    events.reasoning("First step is analyzing the problem...")
    events.reasoning(" Then drafting solution...")

    # Text arriving should collapse the accordion automatically
    events.text("Here is the solution.")
    assert events.accordion.collapsed
    assert "First step is analyzing the problem... Then drafting solution..." in events.last_thinking


def test_cmd_thinking_view_modal():
    agent = MagicMock()
    agent.events.last_thinking = "Step 1: Parse AST\nStep 2: Optimize code"
    store = MagicMock()
    repl = Repl(agent, store)
    # Must run without raising NameError, MissingStyle, or other exceptions
    repl.cmd_thinking("view")


def test_render_diff_eof_newline_handling():
    diff_text = """--- a/file.txt
+++ b/file.txt
@@ -1,2 +1,2 @@
 line1
-line2
\\ No newline at end of file
+line2_modified
\\ No newline at end of file
"""
    rendered = render_diff(diff_text)
    plain = rendered.plain
    assert "No newline at end of file" in plain
    assert "line2_modified" in plain


def test_tool_result_clean_antigravity_style():
    from hubble.tools import Tool
    con = Console(record=True, width=100)
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)
    with patch("hubble.ui.console", con):
        tool = MagicMock(spec=Tool)
        tool.name = "grep"
        tool.kind = "search"
        # Tool start should not print verbose bullets or target
        events.tool_start(tool, {"pattern": "def"})
        out_start = con.export_text()
        assert out_start == ""
        assert events.tool_calls_this_turn == 1

        # Successful tool result should keep terminal output clean and not dump verbose ⎿ output
        events.tool_result(tool, {"pattern": "def"}, "match1\nmatch2\nmatch3\nmatch4", False)
        out_result = con.export_text()
        assert out_result == ""

        # On error, it should print ✘
        events.tool_result(tool, {"pattern": "def"}, "Syntax error: invalid pattern", True)
        out_err = con.export_text()
        assert "✘" in out_err
        assert "Syntax error" in out_err


def test_tool_start_and_result_subagent_events():
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)
    tool_task = MagicMock()
    tool_task.name = "task"
    tool_subagent = MagicMock()
    tool_subagent.name = "invoke_subagent"

    with patch.object(events, "_stop_status") as mock_stop_status, \
         patch.object(events, "_close_board") as mock_close_board:
        
        # tool_start should return early for task and invoke_subagent
        events.tool_start(tool_task, {"prompt": "research"})
        events.tool_start(tool_subagent, {"prompt": "research"})
        assert events.status is None

        # tool_result should close board and force-stop status for both
        events.tool_result(tool_task, {"prompt": "research"}, "Done", False)
        assert mock_close_board.call_count == 1
        mock_stop_status.assert_called_with(force=True)

        events.tool_result(tool_subagent, {"prompt": "research"}, "Done", False)
        assert mock_close_board.call_count == 2
        mock_stop_status.assert_called_with(force=True)


def test_subagent_board_not_wrapped_in_footed():
    from hubble.board import SubagentBoard
    from hubble.spinner import Footed
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)

    with patch("rich.live.Live.start"):
        events.subagent_start("sub1", "Subagent 1")
        assert events.board_live is not None
        assert isinstance(events.board, SubagentBoard)
        # Verify the renderable passed to Live is the board itself, not Footed
        assert not isinstance(events.board_live.renderable, Footed)
        assert events.board_live.renderable is events.board
        events.batch_end()


def test_thinking_accordion_no_snippet_leakage():
    con = Console(record=True, width=100)
    acc = ThinkingAccordion(con, show_live=True, theme_name="terminal")
    acc.feed("Sensitive thought that should not be previewed")
    # Live display should be clean and not leak the snippet
    if acc.live:
        text = str(acc.live.renderable)
        assert "Sensitive thought" not in text
    acc.collapse()
    out = con.export_text()
    assert "Sensitive thought" not in out
    assert "▼ Thought for" in out


def test_shimmer_live_clean_and_idempotent_stop():
    from hubble.spinner import ShimmerLive, Shimmer
    con = Console()
    s = ShimmerLive(Shimmer(), con)
    # Stop multiple times should be safe and idempotent
    s.stop()
    s.stop()
    assert s._stopped


def test_streaming_markdown_does_not_use_footed():
    from hubble.ui import StreamingMarkdown, _BlockPreview
    from hubble.spinner import Footed
    con = Console(record=True, force_terminal=True)
    md = StreamingMarkdown(con)
    with patch("rich.live.Live.start"):
        md.feed("Testing streaming markdown")
        assert md.live is not None
        assert isinstance(md.live.renderable, _BlockPreview)
        assert not isinstance(md.live.renderable, Footed)
        md.flush()


def test_start_shimmer_stops_existing_active_shimmer():
    import hubble.spinner as spinner
    con = Console()
    with patch("rich.live.Live.start"), patch("rich.live.Live.stop"):
        s1 = spinner.start_shimmer(con, label="First")
        assert spinner._ACTIVE_SHIMMER is s1
        assert not s1._stopped

        s2 = spinner.start_shimmer(con, label="Second")
        assert s1._stopped
        assert spinner._ACTIVE_SHIMMER is s2

        s2.stop()
        assert s2._stopped
        assert spinner._ACTIVE_SHIMMER is None


def test_tool_start_subagent_tools_force_stops_status():
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)
    tool_task = MagicMock()
    tool_task.name = "task"
    tool_subagent = MagicMock()
    tool_subagent.name = "invoke_subagent"

    with patch.object(events, "_stop_status") as mock_stop:
        events.tool_start(tool_task, {"prompt": "do task"})
        mock_stop.assert_called_once_with(force=True)

    with patch.object(events, "_stop_status") as mock_stop:
        events.tool_start(tool_subagent, {"prompt": "invoke subagent"})
        mock_stop.assert_called_once_with(force=True)


def test_repl_events_text_force_stops_status():
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)
    with patch.object(events, "_stop_status") as mock_stop, \
         patch("hubble.ui.console.print"):
        events.text("Assistant chunk")
        mock_stop.assert_called_once_with(force=True)


def test_repl_events_reasoning_force_stops_status_when_show_reasoning():
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx, show_reasoning=True)
    with patch.object(events, "_stop_status") as mock_stop:
        events.reasoning("thinking delta")
        mock_stop.assert_called_once_with(force=True)


def test_star_flower_glyphs_replaced_with_telescope():
    from hubble.spinner import GLYPHS
    import hubble.ui as ui
    import hubble.theme as theme

    # No flower asterisks
    for bad in ("✻", "✽", "✳"):
        assert bad not in GLYPHS, f"Found deprecated flower glyph {bad} in GLYPHS"

    # Telescope icon present in GLYPHS
    assert "🔭" in GLYPHS
    assert getattr(ui, "ASSISTANT_ICON", None) == "🔭"
    assert getattr(theme, "ASSISTANT_ICON", None) == "🔭"


def test_no_redundant_hubble_header_before_assistant_response():
    con = Console(record=True, width=100)
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)
    with patch("hubble.ui.console", con):
        events.turn_start()
        events.text("This is direct assistant response.")
        output = con.export_text()
        # Verify no redundant '✻ Hubble' or 'Hubble' header printed
        assert "✻ Hubble" not in output
        assert "✻" not in output


def test_tool_summary_line_single_and_multiple_calls():
    from hubble.tools import Tool
    con = Console(record=True, width=100)
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)

    # 1. Single tool call
    with patch("hubble.ui.console", con):
        events.turn_start()
        tool = MagicMock(spec=Tool)
        tool.name = "read_file"
        tool.kind = "read"
        events.tool_start(tool, {"path": "main.py"})
        events.tool_result(tool, {"path": "main.py"}, "file content", False)
        assert events.tool_calls_this_turn == 1

        # Summary line shown before text
        events.text("Here is the content.")
        out = con.export_text()
        assert "● 1 tool call" in out

        # turn_end should not print duplicate summary
        con.clear()
        from hubble.provider import TurnResult
        events.turn_end(TurnResult(text="Done"))
        out_end = con.export_text()
        assert "● 1 tool call" not in out_end

    # 2. Multiple tool calls
    con2 = Console(record=True, width=100)
    with patch("hubble.ui.console", con2):
        events.turn_start()
        events.tool_start(tool, {"path": "a.py"})
        events.tool_result(tool, {"path": "a.py"}, "a", False)
        events.tool_start(tool, {"path": "b.py"})
        events.tool_result(tool, {"path": "b.py"}, "b", False)
        events.tool_start(tool, {"path": "c.py"})
        events.tool_result(tool, {"path": "c.py"}, "c", False)
        assert events.tool_calls_this_turn == 3

        events.text("Examined 3 files.")
        out2 = con2.export_text()
        assert "● 3 tool calls" in out2


def test_tool_summary_line_shown_on_turn_end_if_no_text():
    from hubble.tools import Tool
    from hubble.provider import TurnResult
    con = Console(record=True, width=100)
    ctx = ToolContext(root=Path("."))
    events = ReplEvents(ctx)

    with patch("hubble.ui.console", con):
        events.turn_start()
        tool = MagicMock(spec=Tool)
        tool.name = "shell"
        tool.kind = "exec"
        events.tool_start(tool, {"command": "ls"})
        events.tool_result(tool, {"command": "ls"}, "file.txt", False)
        assert events.tool_calls_this_turn == 1

        # Turn ends without text() being called
        events.turn_end(TurnResult(text=""))
        out = con.export_text()
        assert "● 1 tool call" in out

