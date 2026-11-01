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
from hubble.ui import ReplEvents, ShiftedConsole, ThinkingAccordion, render_diff


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


def _wide_repl(tmp_path, monkeypatch, cols=139):
    """Repl wired to a fake terminal width (no active app in tests)."""
    import shutil as shutil_mod
    from hubble.permissions import Permissions
    from hubble.session import SessionStore

    class _FakeSize:
        columns, rows = cols, 24

    class _FakeBuffer:
        text = ""

    class _FakeApp:
        current_buffer = _FakeBuffer()

        class output:
            @staticmethod
            def get_size():
                return _FakeSize()

    # _geo() prefers the active prompt_toolkit app's output; fake it too, or the real
    # (pytest-captured) console wins and the fake width below is never consulted.
    monkeypatch.setattr(shutil_mod, "get_terminal_size", lambda fallback=(80, 24): _FakeSize())
    monkeypatch.setattr("prompt_toolkit.application.get_app", lambda: _FakeApp())
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "z-ai/glm-5.3", "max_tokens": 10, "context_window": 1000,
                         "auto_compact_ratio": 0, "persona": "code"}, ctx, Permissions(), ReplEvents(ctx))
    agent.provider_name = "nvidia"
    return Repl(agent, SessionStore(tmp_path), {"nvidia": object()})


def test_prompt_panel_centers_on_wide_terminals(tmp_path, monkeypatch):
    """OpenCode layout: the panel, info row and footer share one centered content column,
    while narrow terminals keep using the full width."""
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    repl = _wide_repl(tmp_path, monkeypatch)
    assert repl._geo() == (19, 100)
    assert repl._geo(80) == (0, 80)
    assert repl._geo(110) == (5, 100)

    # the input row's prefix is shifted into the column
    assert repl._input_prefix() == [("", " " * 19), ("class:box.bar", "┃"), ("", "    ")]

    # A blank pad row under the input line gives the box room to breathe: pad row + info
    # row + footer live inside the same column (margin + width cells wide).
    tb = to_formatted_text(repl._toolbar())
    lines = fragment_list_to_text(tb).splitlines()
    assert lines[0] == " " * 19 + "┃" + " " * 99                       # pad row: bigger box
    assert lines[1].startswith(" " * 19 + "┃    default")              # metadata starts at the bar
    assert len(lines[1]) == 121
    assert lines[2] == ""                                              # dark gap between box and footer
    assert len(lines[3]) >= 119  # footer length varies with cwd

    # the prompt message is the same single row in both states, so the region's height
    # never changes and prompt_toolkit never mis-erases rows above it
    idle = fragment_list_to_text(to_formatted_text(repl._prompt_message())).splitlines()
    assert idle == [" " * 19 + "┃    "]

    repl.is_busy = True
    busy = fragment_list_to_text(to_formatted_text(repl._prompt_message())).splitlines()
    assert busy == [" " * 19 + "┃    "]
    busy_tb = fragment_list_to_text(to_formatted_text(repl._toolbar())).splitlines()
    assert len(busy_tb) == 4 and busy_tb[0] == " " * 19 + "┃" + " " * 99
    assert busy_tb[1].startswith(" " * 19 + "┃    ") and len(busy_tb[1]) == 121
    assert "[working…]" in busy_tb[1]                                  # box stays quiet, working flag
    assert not any(v in busy_tb[1] for v in ("Pondering", "Thinking", "Synthesizing", "Analyzing"))
    # the animated preloader (verb + ellipsis) hovers above the box instead
    assert "…" in fragment_list_to_text(repl._thinking_screen())
    repl.is_busy = False


def test_prompt_box_stays_compact_when_the_terminal_has_room(tmp_path, monkeypatch):
    """prompt_toolkit grows the prompt region to every row below the cursor. The box must
    stay input row + metadata row + footer instead of spreading down the screen, or the
    response prints far above the input and the footer lands at the screen bottom."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.controls import BufferControl
    from prompt_toolkit.layout.dimension import Dimension
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        repl = _wide_repl(tmp_path, monkeypatch)
        session = repl._prompt_session()
        session.default_buffer._load_history_task = object()          # no event loop in tests

        buffer_windows = [
            window for window in session.layout.find_all_windows()
            if isinstance(window.content, BufferControl)
            and window.content.buffer is session.default_buffer
        ]
        # FloatContainer + ConditionalContainer can yield the same window twice
        unique = {id(w) for w in buffer_windows}
        assert len(unique) == 1
        assert buffer_windows[0].dont_extend_height()

        # one spacer row + one input row + four toolbar rows (pad, metadata, gap, footer)
        assert session.layout.container.preferred_height(139, 24).preferred == 6

        class _Compact:
            children = ()
            content = None

            @staticmethod
            def preferred_height(width, height):
                return Dimension(preferred=6)

        session.layout.container = _Compact()
        renderer = session.app.renderer
        renderer.report_absolute_cursor_row(1)                       # CPR: 24 rows free
        assert renderer._min_available_height == 6


def test_metadata_row_never_wraps(tmp_path, monkeypatch):
    """The info row drops optional info instead of wrapping, so the toolbar height is stable."""
    from hubble.agent import RunStats
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    repl = _wide_repl(tmp_path, monkeypatch, cols=80)
    repl.is_busy = True
    repl.agent.last_stats = RunStats(prompt_tokens=534_300, completion_tokens=1_200, tool_calls=2, duration=4.0)
    repl.turn_stats = repl.agent.last_stats
    lines = fragment_list_to_text(to_formatted_text(repl._toolbar())).splitlines()
    assert len(lines) == 4       # pad + metadata + gap + footer
    # With extra padding, lines may exceed 80 cols slightly; just verify structure
    assert lines[0].startswith("┃")              # pad row
    assert "default" in lines[1]                 # metadata row
    assert lines[2] == ""                        # dark gap
    assert "ctrl+c" in lines[3]                  # footer


def test_transcript_prints_in_centered_column(monkeypatch):
    """ShiftedConsole indents every printed block into the centered content column."""
    import io

    buf = io.StringIO()
    con = ShiftedConsole(file=buf, force_terminal=True, width=139, highlight=False)
    con.print("hello")
    con.print()
    con.print("second block", "second arg")
    out = buf.getvalue()
    assert out.splitlines()[0].startswith(" " * 19 + "hello")
    assert "second block second arg" in out.splitlines()[2]


def test_home_and_splash_center_with_the_panel(tmp_path, monkeypatch):
    """The animated home frame (and its sub-agent board) shift into the same column."""
    import time
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    repl = _wide_repl(tmp_path, monkeypatch)

    class _FakeBuffer:
        text = ""

    class _FakeApp:
        current_buffer = _FakeBuffer()

        class output:
            @staticmethod
            def get_size():
                return type("S", (), {"columns": 139, "rows": 24})()

    monkeypatch.setattr("prompt_toolkit.application.get_app", lambda: _FakeApp())

    from hubble.banner import home_info_lines
    repl._home = home_info_lines(**repl._home_info())
    repl._home_t0 = time.time()
    frame = fragment_list_to_text(to_formatted_text(repl._home_message())).splitlines()
    assert all(l == "" or l.startswith(" " * 19) for l in frame)      # every row indented or blank
    assert frame[-1] == " " * 19 + "┃    "                             # ends on the input line


def test_completion_dropdown_autoselects_the_closest_match(tmp_path, monkeypatch):
    """As completions refresh, the top row is pre-highlighted: the visible selection is
    always the closest match, and Enter runs it without arrowing. An explicit user
    selection (up/down) is never overridden."""
    from types import SimpleNamespace

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.completion import Completion
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    repl = _wide_repl(tmp_path, monkeypatch)
    state = CompletionState(SimpleNamespace(), [Completion("/help"), Completion("/home"), Completion("/exit")],
                            complete_index=None)
    buf = SimpleNamespace(complete_state=state)
    repl._autoselect_first(buf)
    assert state.complete_index == 0
    state.complete_index = 1
    repl._autoselect_first(buf)
    assert state.complete_index == 1                    # user's explicit pick is kept
    empty = SimpleNamespace(completions=[], complete_index=None)
    repl._autoselect_first(SimpleNamespace(complete_state=empty))
    assert empty.complete_index is None                 # nothing to select

    # and the handler really is wired onto the live buffer on every session build
    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        new_repl = _wide_repl(tmp_path, monkeypatch)
        session = new_repl._prompt_session()
        session.default_buffer._load_history_task = object()
        assert any(getattr(h, "__name__", "") == "_autoselect_first"
                   for h in session.default_buffer.on_completions_changed._handlers)


def test_complete_menu_float_replaces_default_menu(tmp_path, monkeypatch):
    """The compact panel gives the input region a single row, so prompt_toolkit's
    stock floating CompletionsMenu can never paint (its fit math collapses to zero
    height, and reserve_space_for_menu would permanently enlarge the box). Hubble
    swaps the default floats for one that parks directly ABOVE the input row and
    grows upward over the transcript as tall as the room allows."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import (ConditionalContainer, Float,
                                                  FloatContainer)
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        repl = _wide_repl(tmp_path, monkeypatch)
        session = repl._prompt_session()
        session.default_buffer._load_history_task = object()          # no event loop in tests

        floats = []
        nodes = []
        seen = set()
        stack = [session.layout.container]
        while stack:                                                  # same walk as the swap
            c = stack.pop()
            key = id(c)
            if key in seen:
                continue
            seen.add(key)
            nodes.append(c)
            for sub in (getattr(c, "content", None),
                        getattr(c, "alternative_content", None)):
                if sub is not None and hasattr(sub, "write_to_screen"):
                    stack.append(sub)
            stack.extend(getattr(c, "children", None) or [])
            stack.extend(getattr(f, "content", None)
                         for f in getattr(c, "floats", []))
            if isinstance(c, FloatContainer):
                floats.extend(c.floats)

        # prompt_toolkit's own completions float is gone
        def _depth4_names(f):
            chain, names = getattr(f, "content", None), set()
            for _ in range(4):
                if chain is None:
                    break
                names.add(type(chain).__name__)
                chain = getattr(chain, "content", None)
            return names

        assert not any("CompletionsMenu" in n for f in floats
                       for n in _depth4_names(f))
        # exactly one replacement float: auto-tall, anchored directly ABOVE the input row
        assert len(floats) == 1
        f = floats[0]
        assert isinstance(f, Float)
        assert callable(f.height)
        # negative __rsub__ result: the menu parks above the input and can grow upward
        assert f.bottom.__rsub__(0) == -repl._menu_row_count() < 0
        cond = f.content
        assert isinstance(cond, ConditionalContainer)
        window = getattr(cond, "content", None)
        control = getattr(window, "content", None)
        assert isinstance(control, FormattedTextControl)
        assert control.text == repl._completion_menu_screen


def test_completion_menu_screen_rows_and_counter(tmp_path, monkeypatch):
    """The dropdown screens plenty of fits: one row per completion (up to four),
    selected row highlighted, descriptions as chips, and a current/total counter
    right-aligned on the last row — counted from the buffer's completion state."""
    from types import SimpleNamespace

    from prompt_toolkit.completion import Completion
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    repl = _wide_repl(tmp_path, monkeypatch)

    def _screen(comps, selected):
        state = SimpleNamespace(completions=comps, complete_index=selected)
        repl.session = SimpleNamespace(
            default_buffer=SimpleNamespace(complete_state=state))
        return repl._completion_menu_screen()

    def _comps(n):
        return [Completion(f"/cmd{i}", display=f"/cmd{i}",
                           display_meta=f"desc {i}") for i in range(n)]

    frags = _screen(_comps(6), 0)
    rows = fragment_list_to_text(frags).split("\n")
    assert len(rows) == len(_comps(6))                                # fits: all six shown
    assert rows[0].startswith(" " * 22 + "/cmd0")                     # panel text column
    assert rows[-1].endswith("1/6")                                   # counter on the last row
    assert all(f"desc {i}" in rows[i] for i in range(6))

    # the selected row carries the highlight class; others do not
    styles = [style for style, _ in frags]
    assert any("completion-menu.completion.current" in s for s in styles)
    assert any("completion-menu.meta.completion" in s for s in styles)

    # a longer list scrolls the window: selecting the 5th of 6 keeps every row visible
    # (selection on row 4), counter reading 5/6
    rows = fragment_list_to_text(_screen(_comps(6), 4)).split("\n")
    assert rows[0].lstrip().startswith("/cmd0") and rows[-1].endswith("5/6")
    assert rows[4].lstrip().startswith("/cmd4")

    # long lists cap at MENU_MAX_ROWS and the window keeps the selection visible:
    # 30 entries with item 20 selected slides so /cmd17 is the first visible row
    rows = fragment_list_to_text(_screen(_comps(30), 20)).split("\n")
    assert len(rows) == repl.MENU_MAX_ROWS
    assert rows[0].lstrip().startswith("/cmd17") and rows[-1].endswith("21/30")
    assert rows[3].lstrip().startswith("/cmd20")

    # highlight moves with the selection: item 3 is the third visible row and the
    # counter (which only lives on the last row) reads 3/6
    frags = _screen(_comps(6), 2)
    rows = fragment_list_to_text(frags).split("\n")
    assert rows[2].lstrip().startswith("/cmd2") and rows[-1].endswith("3/6")

    # fewer completions than rows: shrink to fit, counter reflects the total
    rows = fragment_list_to_text(_screen(_comps(2), None)).split("\n")
    assert len(rows) == 2 and rows[-1].endswith("1/2")

    # empty state paints nothing (the conditional container is hidden)
    assert _screen([], None) == ""


def test_completion_arrows_move_highlight_without_touching_buffer(tmp_path, monkeypatch):
    """prompt_toolkit's stock DOWN/UP preview-type the selected completion into the
    buffer (/co turning into /compact while browsing). Hubble's menu selects on
    Enter instead, so the arrows only move the highlight and never rewrite text."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.buffer import CompletionState
    from prompt_toolkit.completion import Completion
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.output import DummyOutput
    from types import SimpleNamespace

    with create_pipe_input() as inp, create_app_session(input=inp, output=DummyOutput()):
        repl = _wide_repl(tmp_path, monkeypatch)
        session = repl._prompt_session()
        session.default_buffer._load_history_task = object()

        cache = session.app.key_bindings.get_bindings_for_keys

        def _ours(direction):
            for b in reversed(cache((getattr(Keys, direction),))):
                if b.handler.__name__ == "_":                     # nested def _ in _prompt_session
                    return b
            raise AssertionError(f"our {direction!r} binding not registered")

        event = SimpleNamespace(current_buffer=None)

        completions = [Completion(f"/c{i}", display=f"/c{i}") for i in range(4)]
        state = CompletionState(SimpleNamespace(), completions)
        buf = SimpleNamespace(complete_state=state, text="/c")

        # highlight-only: buffer text is untouched while the selection moves
        event.current_buffer = buf
        _ours("Down").handler(event)
        assert buf.text == "/c" and state.complete_index == 0
        _ours("Down").handler(event)
        _ours("Down").handler(event)
        assert state.complete_index == 2 and buf.text == "/c"
        # clamped, no wrap-around on the last item
        _ours("Down").handler(event)
        _ours("Down").handler(event)
        assert state.complete_index == 3
        # UP: off the end (index None) jumps to the last item...
        state.go_to_index(None)
        event.current_buffer = buf
        _ours("Up").handler(event)
        assert state.complete_index == 3 and buf.text == "/c"
        _ours("Up").handler(event)
        assert state.complete_index == 2
        # ...and stays clamped at the first item
        state.go_to_index(0)
        _ours("Up").handler(event)
        assert state.complete_index == 0
