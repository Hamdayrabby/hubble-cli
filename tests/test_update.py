import json
import time

import hubble.update as update


def test_version_compare():
    assert update.is_newer("4.1.0", "4.0.0")
    assert update.is_newer("4.10.0", "4.9.9")
    assert not update.is_newer("4.1.0", "4.1.0")
    assert not update.is_newer("4.0.9", "4.1.0")
    assert not update.is_newer("4.2.0rc1", "4.2.0")
    assert update.is_newer("4.2.0", "4.2.0rc1")
    assert update.is_newer("5", "4.9")


def test_notice_shown_once_when_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "__version__", "4.0.0")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: "4.1.0")
    monkeypatch.delenv("HUBBLE_NO_UPDATE_CHECK", raising=False)
    c = update.UpdateChecker()
    c.start()
    c._thread.join(5)
    notice = c.pending_notice()
    assert "4.0.0 → 4.1.0" in notice and ("pipx upgrade" in notice or "-U hubble-cli" in notice)
    assert c.pending_notice() is None  # only once
    assert json.loads((tmp_path / "c.json").read_text())["latest"] == "4.1.0"


def test_no_notice_when_current(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: update.__version__)
    c = update.UpdateChecker()
    c.start()
    if c._thread:
        c._thread.join(5)
    assert c.pending_notice() is None


def test_fresh_cache_skips_network(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"latest": "99.0.0", "checked": time.time()}), encoding="utf-8")
    monkeypatch.setattr(update, "CACHE_FILE", cache)

    def boom(timeout=4.0):
        raise AssertionError("should not hit the network with a fresh cache")
    monkeypatch.setattr(update, "_fetch_latest", boom)
    c = update.UpdateChecker()
    c.start()
    assert c._thread is None and "99.0.0" in c.pending_notice()


def test_disabled_by_setting_or_env(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: "99.0.0")
    c = update.UpdateChecker(enabled=False)
    c.start()
    assert c.pending_notice() is None
    monkeypatch.setenv("HUBBLE_NO_UPDATE_CHECK", "1")
    c = update.UpdateChecker()
    c.start()
    assert c.pending_notice() is None


def _idle_redraws(tmp_path, latest, seconds=2.0):
    """Sit at a real prompt for `seconds` with `latest` known; return how many times it redrew."""
    import threading

    from prompt_toolkit.application import create_app_session, get_app
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from hubble.agent import Agent
    from hubble.permissions import Permissions
    from hubble.repl import Repl
    from hubble.session import SessionStore
    from hubble.tools import ToolContext
    from hubble.ui import ReplEvents

    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "m", "max_turns": 2, "max_tokens": 10, "context_window": 1000,
                         "auto_compact_ratio": 0, "persona": "code"}, ctx, Permissions(), ReplEvents(ctx))
    repl = Repl(agent, SessionStore(tmp_path), {"hubble": object()})
    repl.updates.latest = latest
    counts = {}
    with create_pipe_input() as inp:
        with create_app_session(input=inp, output=DummyOutput()):
            session = repl._prompt_session()

            def driver():
                time.sleep(seconds)
                counts["renders"] = session.app.render_counter
                inp.send_text("\r")
            threading.Thread(target=driver, daemon=True).start()
            session.prompt("> ")
    return counts["renders"], repl


def test_no_redraw_loop_when_pypi_is_not_newer(tmp_path, monkeypatch):
    """Regression: a known-but-not-newer version made every redraw schedule another redraw
    (run_in_terminal), so the terminal flickered nonstop."""
    renders, repl = _idle_redraws(tmp_path, latest="0.0.1")
    assert renders < 15, f"{renders} redraws in 2s while idle: redraw loop"
    assert repl.updates.pending_notice() is None


def test_newer_version_announced_once_without_redraw_loop(tmp_path):
    renders, repl = _idle_redraws(tmp_path, latest="999.0.0")
    assert renders < 15, f"{renders} redraws in 2s while idle: redraw loop"
    assert repl.updates.announced


def test_network_failure_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: None)
    c = update.UpdateChecker()
    c.start()
    c._thread.join(5)
    assert c.pending_notice() is None
