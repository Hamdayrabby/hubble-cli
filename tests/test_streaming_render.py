import io

from rich.console import Console

from hubble.ui import StreamingMarkdown, _BlockPreview


def term(width=80, height=24):
    return Console(file=io.StringIO(), width=width, height=height, force_terminal=True, color_system=None,
                   legacy_windows=False)


def plain(renderable, con):
    with con.capture() as cap:
        con.print(renderable)
    return cap.get()


def test_unfinished_paragraph_is_visible_immediately():
    con = term()
    md = StreamingMarkdown(con)
    md.feed("The scanner probes each")
    assert md.live is not None                        # a live preview started on the first words
    assert "The scanner probes each" in plain(_BlockPreview(md), con)
    md.feed(" model.\n\nNext paragraph")
    assert "Next paragraph" in plain(_BlockPreview(md), con)   # only the unfinished block is live
    assert "probes each model." not in plain(_BlockPreview(md), con)
    md.flush()
    assert md.live is None
    out = con.file.getvalue()
    assert "The scanner probes each model." in out and "Next paragraph" in out


def test_open_code_block_shows_while_streaming():
    con = term()
    md = StreamingMarkdown(con)
    md.feed("```python\ndef add(a, b):\n    return a")
    assert "def add(a, b):" in plain(_BlockPreview(md), con)
    md.feed(" + b\n```\n\n")
    assert md.buf == ""                                # closed fence committed for good
    md.flush()
    assert "return a + b" in con.file.getvalue()


def test_tall_block_preview_shows_newest_lines_only():
    con = term(height=10)
    md = StreamingMarkdown(con)
    md.feed("```\n" + "\n".join(f"line {i}" for i in range(50)))
    shown = plain(_BlockPreview(md), con)
    assert "line 49" in shown and "line 0\n" not in shown
    md.flush()


def test_full_turn_cycle_never_overlaps_live_displays(tmp_path, monkeypatch):
    import hubble.ui as ui
    from hubble.provider import ToolCall, TurnResult
    from hubble.tools import ReadFile, ToolContext
    con = term(width=100, height=30)
    monkeypatch.setattr(ui, "console", con)
    ev = ui.ReplEvents(ToolContext(root=tmp_path))
    tool = ReadFile()
    for _ in range(2):
        ev.turn_start()                                  # spinner live
        for piece in ("Let me ", "look at ", "the file.\n\nChecking"):
            ev.text(piece)                               # spinner stops, text preview live
        ev.turn_end(TurnResult(text="...", tool_calls=[ToolCall("c", "read_file", "{}")]))
        ev.tool_start(tool, {"path": "a.txt"})           # spinner live again
        ev.tool_result(tool, {"path": "a.txt"}, "1\thello", False)
    ev.turn_start()
    ev.text("Done.")
    ev.turn_end(TurnResult(text="Done."))
    out = con.file.getvalue()
    assert "look at the file." in out and "Checking" in out and "Done." in out


def test_non_terminal_output_has_no_live_display():
    con = Console(file=io.StringIO(), width=80, force_terminal=False)
    md = StreamingMarkdown(con)
    md.feed("hello")
    assert md.live is None
    md.flush()
    assert "hello" in con.file.getvalue()
