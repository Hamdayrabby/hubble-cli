from pathlib import Path
from unittest.mock import MagicMock, patch
from rich.console import Console

from hubble.agent import Agent, Permissions, ToolContext, TurnResult, _SubagentEvents
from hubble.board import SubagentBoard, _Row


def test_subagent_board_clean_rendering_and_metric_tracking():
    board = SubagentBoard()
    board.add("task-1", "research api", model="claude-3-5-sonnet", context_window=200_000)
    board.tokens("task-1", 1500, prompt_tokens=1200, completion_tokens=300, context_tokens=15000)
    board.step("task-1", "reading auth.py", is_tool=True)

    row = board.rows["task-1"]
    assert row.model == "claude-3-5-sonnet"
    assert row.prompt_tokens == 1200
    assert row.completion_tokens == 300
    assert row.tokens == 1500
    assert row.context_tokens == 15000
    assert row.context_window == 200_000

    con = Console(record=True, width=120)
    con.print(board)
    out = con.export_text()

    # Previous clean design: label, 1 tool, token count, action
    assert "research api" in out
    assert "1 tool" in out
    assert "1.5k tok" in out
    assert "reading auth.py" in out

    board.finish("task-1", "done", "report ready (450 chars)")
    con = Console(record=True, width=120)
    con.print(board)
    out = con.export_text()
    assert "✔" in out
    assert "report ready (450 chars)" in out


def test_subagent_events_forwards_tokens_and_context():
    parent_events = MagicMock()
    sub_events = _SubagentEvents(parent_events, "test subagent", "task-99")

    mock_sub = MagicMock()
    mock_sub.context_tokens = 8400
    sub_events.sub_agent = mock_sub

    mock_turn_res = MagicMock()
    mock_turn_res.usage = {"prompt_tokens": 1050, "completion_tokens": 250, "total_tokens": 1300}
    mock_turn_res.text = ""
    mock_turn_res.reasoning = ""

    sub_events.turn_end(mock_turn_res)

    parent_events.subagent_tokens.assert_called_once_with(
        "task-99",
        1300,
        prompt_tokens=1050,
        completion_tokens=250,
        context_tokens=8400,
    )


def test_repl_cmd_cost_and_context_with_subagents():
    from hubble.repl import Repl

    agent = MagicMock()
    agent.model = "gpt-4o"
    agent.total_prompt_tokens = 5000
    agent.total_completion_tokens = 1500
    agent.context_tokens = 25000
    agent.context_ratio = MagicMock(return_value=0.20)
    agent.messages = [{"role": "user", "content": "hi"}]
    agent.settings = {"context_window": 128000}
    agent.system_prompt = MagicMock(return_value="system prompt instructions")
    agent.tools = []
    agent.memory = []
    agent.pinned = {}

    # Simulate two completed subagents
    agent.subagent_history = [
        {
            "key": "task-1",
            "label": "fast search",
            "model": "mistral-small",
            "provider": "hubble",
            "prompt_tokens": 2000,
            "completion_tokens": 400,
            "total_tokens": 2400,
            "context_tokens": 12000,
            "context_window": 128000,
            "status": "done",
            "tool_calls": 4,
            "duration": 3.2,
        },
        {
            "key": "task-2",
            "label": "deep review",
            "model": "claude-opus-5",
            "provider": "anthropic",
            "prompt_tokens": 1500,
            "completion_tokens": 600,
            "total_tokens": 2100,
            "context_tokens": 20000,
            "context_window": 200000,
            "status": "done",
            "tool_calls": 2,
            "duration": 5.8,
        },
    ]

    repl = MagicMock()
    repl.agent = agent

    con = Console(record=True, width=120)
    import hubble.repl as repl_mod
    orig_console = repl_mod.console
    try:
        repl_mod.console = con
        # Call cmd_cost logic
        repl_mod.Repl.cmd_cost(repl, "")
        out = con.export_text()

        # Check Main Agent
        assert "Main Agent" in out
        assert "model          gpt-4o" in out
        # Main agent prompt tokens = 5000 - (2000 + 1500) = 1500
        assert "input tokens   1,500" in out
        # Main agent completion tokens = 1500 - (400 + 600) = 500
        assert "output tokens  500" in out

        # Check Subagents
        assert "Subagents (2)" in out
        assert "fast search" in out
        assert "mistral-small" in out
        assert "2,000" in out
        assert "400" in out
        assert "deep review" in out
        assert "claude-opus-5" in out
        assert "1,500" in out
        assert "600" in out

        # Check Session Total
        assert "Session Total (Main + Subagents)" in out
        assert "total input    5,000" in out
        assert "total output   1,500" in out
        assert "combined       6,500" in out

        # Call cmd_context logic
        con = Console(record=True, width=120)
        repl_mod.console = con
        repl_mod.Repl.cmd_context(repl, "")
        ctx_out = con.export_text()
        assert "Subagent Contexts (2 executed)" in out or "Subagent Contexts (2 executed)" in ctx_out
        assert "fast search" in ctx_out
        assert "mistral-small" in ctx_out
        assert "12,000" in ctx_out
    finally:
        repl_mod.console = orig_console


def test_subagent_single_line_claude_spinner_and_no_step_spam():
    from hubble.board import SubagentBoard, CLAUDE_GLYPHS
    from hubble.ui import ReplEvents
    from unittest.mock import patch

    board = SubagentBoard()
    board.add("sub-1", "research: Research Hubble CLI")
    board.step("sub-1", "searching the web for \"hubble cli\"")

    con = Console(record=True, width=120)
    con.print(board)
    lines = [l for l in con.export_text().splitlines() if l.strip()]

    # Must be rendered on JUST ONE LINE
    assert len(lines) == 1
    # Must contain Claude spinner glyph
    assert any(g in lines[0] for g in CLAUDE_GLYPHS)
    assert "research: Research Hubble CLI" in lines[0]
    assert "searching the web for \"hubble cli\"" in lines[0]

    # Finish and verify single line with checkmark
    board.finish("sub-1", "done", "Report ready")
    con = Console(record=True, width=120)
    con.print(board)
    fin_lines = [l for l in con.export_text().splitlines() if l.strip()]
    assert len(fin_lines) == 1
    assert "✔" in fin_lines[0]
    assert "Report ready" in fin_lines[0]

    # Verify ReplEvents does not spam console.print on each step
    events = ReplEvents(ToolContext(root=Path(".")))
    with patch("rich.live.Live.start"), patch("hubble.ui.console.print") as mock_print:
        events.subagent_start("sub-1", "research: Research Hubble CLI")
        for action in ["thinking", "searching web", "reading file", "thinking again"]:
            events.subagent_step("sub-1", action)
        events.subagent_end("sub-1", "done", "Report ready")
        # console.print must NOT be spammed on every subagent step!
        assert mock_print.call_count == 0

        # Only _close_board prints the board once
        events._close_board()
        assert mock_print.call_count == 1


def test_subagent_claude_preloader_and_prompt_proxy_no_live():
    from hubble.board import SubagentBoard, CLAUDE_GLYPHS
    from hubble.ui import ReplEvents

    board = SubagentBoard()
    board.add("task-1", "Research hubble cli projects")
    board.step("task-1", "searching the web for \"hubble cli\"", is_tool=True)
    board.tokens("task-1", 1900)

    # 1. to_formatted_text produces Claude-style braille spinner and formatted tuples
    frags = board.to_formatted_text()
    assert len(frags) > 0
    styles = [f[0] for f in frags]
    texts = [f[1] for f in frags]
    combined_text = "".join(texts)

    # Check for Claude glyph
    assert any(g in texts[0] for g in CLAUDE_GLYPHS)
    assert "fg:#00d7ff bold" in styles[0]
    assert "1 subagent running" in combined_text
    assert "Research hubble cli projects" in combined_text
    assert "1 tool" in combined_text
    assert "1.9k tok" in combined_text
    assert "searching the web for \"hubble cli\"" in combined_text

    # 2. When subagent finishes, to_formatted_text returns empty list
    board.finish("task-1", "done", "Report ready")
    assert board.to_formatted_text() == []

    # 3. ReplEvents when prompt proxy is active does NOT start rich.live.Live
    events = ReplEvents(ToolContext(root=Path(".")))
    with patch("hubble.ui.is_prompt_proxy_active", return_value=True), \
         patch("rich.live.Live") as mock_live:
        events.subagent_start("task-1", "Research hubble cli projects")
        assert events.board_live is None
        assert mock_live.call_count == 0
        events._close_board()

    # 4. ReplEvents when prompt proxy is NOT active starts rich.live.Live (backwards compatibility)
    events2 = ReplEvents(ToolContext(root=Path(".")))
    with patch("hubble.ui.is_prompt_proxy_active", return_value=False), \
         patch("rich.live.Live.start"):
        events2.subagent_start("task-1", "Research hubble cli projects")
        assert events2.board_live is not None
        events2._close_board()

    # 5. Multi-subagent orchestration header test
    multi_board = SubagentBoard()
    multi_board.add("sub-1", "Research task")
    multi_board.add("sub-2", "Review task")
    multi_board.step("sub-1", "searching web")
    multi_board.step("sub-2", "reading files")
    multi_frags = multi_board.to_formatted_text()
    multi_combined = "".join(f[1] for f in multi_frags)
    assert "Orchestrating 2 subagents (2 running · 0 done" in multi_combined
    assert "Research task" in multi_combined
    assert "Review task" in multi_combined


