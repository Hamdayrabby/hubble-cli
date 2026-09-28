import io
import time

from rich.console import Console

from hubble.ui import StreamingMarkdown, _BlockPreview


def term(width=80, height=24):
    return Console(file=io.StringIO(), width=width, height=height, force_terminal=True, color_system=None,
                   legacy_windows=False)


def streamer(con=None):
    """A StreamingMarkdown whose committed (printed-for-good) blocks are collected for checking;
    the live frames it also draws to the test console are ignored."""
    md = StreamingMarkdown(con or term())
    md.committed = []
    md._render = lambda chunk: md.committed.append(chunk.strip("\n")) if chunk.strip() else None
    return md


def caught_up(md, limit=1.5):
    """Wait until the typing has caught up with everything received (fails if it takes > limit s)."""
    t0 = time.time()
    while time.time() - t0 < limit:
        shown, typing = md.visible()
        if not typing:
            return shown, typing
        time.sleep(0.05)
    return md.visible()


def test_text_is_typed_out_word_by_word_and_catches_up():
    md = streamer()
    text = "The scanner probes every model on the gateway and keeps the ones that answer properly"
    md.feed(text)                                   # one big chunk from the model
    time.sleep(0.1)
    partial, typing = md.visible()
    assert typing and 0 < len(partial) < len(text)  # typed out, not dumped at once
    assert text[len(partial) - 1] in " \n"          # whole words only
    shown, typing = caught_up(md)
    assert shown == text and not typing             # never lags far behind the model
    md.flush()
    assert md.live is None and md.committed == [text]


def test_preview_renders_typed_text_with_cursor():
    con = term()
    md = streamer(con)
    md.feed("Hello there wonderful world")
    md._stop_live()                                  # render the preview by hand, no live thread
    md.shown = 12.0
    with con.capture() as cap:
        con.print(_BlockPreview(md))
    out = cap.get()
    assert "Hello there" in out and "▌" in out and "world" not in out


def test_finished_blocks_commit_and_only_the_current_one_stays_live():
    md = streamer()
    md.feed("The scanner probes each model.\n\nNext paragraph")
    caught_up(md)
    md.feed("")                                      # a later delta commits the finished paragraph
    assert md.committed == ["The scanner probes each model."]
    assert md.visible()[0] == "Next paragraph"
    md.flush()
    assert md.committed == ["The scanner probes each model.", "Next paragraph"]


def test_open_code_block_shows_while_streaming():
    md = streamer()
    md.feed("```python\ndef add(a, b):\n    return a")
    assert "def add(a, b):" in caught_up(md)[0]
    md.feed(" + b\n```\n\n")
    md.flush()
    assert any("return a + b" in c for c in md.committed)


def test_tall_block_preview_shows_newest_lines_only():
    con = term(height=10)
    md = streamer(con)
    md.feed("```\n" + "\n".join(f"line {i}" for i in range(50)))
    caught_up(md)
    md._stop_live()
    md.shown = float(len(md.buf))
    with con.capture() as cap:
        con.print(_BlockPreview(md))
    out = cap.get()
    assert "line 49" in out and "line 0\n" not in out


def test_flush_finishes_typing_quickly():
    md = streamer()
    md.feed("word " * 400)
    t0 = time.time()
    md.flush()
    assert time.time() - t0 <= StreamingMarkdown.FINISH_S + 0.25   # never holds up the next step long
    assert md.committed[0].count("word") == 400


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
            ev.text(piece)                               # spinner stops, typing preview live
        ev.turn_end(TurnResult(text="...", tool_calls=[ToolCall("c", "read_file", "{}")]))
        ev.tool_start(tool, {"path": "a.txt"})           # spinner live again
        ev.tool_result(tool, {"path": "a.txt"}, "1\thello", False)
    ev.turn_start()
    ev.text("Done.")
    ev.turn_end(TurnResult(text="Done."))
    out = con.file.getvalue()
    assert "look at the file." in out and "Checking" in out and "Done." in out


def test_non_terminal_output_is_immediate_and_has_no_live_display():
    con = Console(file=io.StringIO(), width=80, force_terminal=False)
    md = StreamingMarkdown(con)
    md.feed("hello\n\nworld")
    assert md.live is None
    md.flush()
    assert "hello" in con.file.getvalue() and "world" in con.file.getvalue()
