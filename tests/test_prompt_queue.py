import time
from pathlib import Path
from hubble.agent import Agent
from hubble.permissions import Permissions
from hubble.repl import Repl
from hubble.session import SessionStore
from hubble.tools import ToolContext
from hubble.ui import HeadlessEvents

def test_prompt_queue_during_busy(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), HeadlessEvents())
    repl = Repl(agent, SessionStore(tmp_path), {"test": object()})
    
    # Start the worker thread
    repl._start_worker()
    
    executed = []
    def slow_send(text: str):
        time.sleep(0.3)
        executed.append(text)
    repl.send = slow_send

    # Submit first task
    repl.handle("first task")
    time.sleep(0.1)
    assert repl.is_busy

    # While busy, submit second task
    repl.handle("second task queued")
    assert "second task queued" in repl.message_queue

    # Wait for both tasks to complete through the queue
    for _ in range(20):
        if len(executed) == 2:
            break
        time.sleep(0.1)

    assert executed == ["first task", "second task queued"]
    assert len(repl.message_queue) == 0
    assert not repl.is_busy

    # Clean up worker thread
    repl._shutdown_event.set()
    repl._task_queue.put(None)
    if repl._worker_thread:
        repl._worker_thread.join(timeout=1.0)


def test_prompt_queue_commands(tmp_path: Path):
    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), HeadlessEvents())
    repl = Repl(agent, SessionStore(tmp_path), {"test": object()})
    
    repl.message_queue = ["prompt 1", "prompt 2"]
    assert len(repl.message_queue) == 2

    # Clear queue command
    repl.cmd_clear_queue()
    assert len(repl.message_queue) == 0


def test_shimmer_animation_in_prompt_message(tmp_path: Path):
    import hubble.spinner as spinner
    from rich.console import Console

    ctx = ToolContext(root=tmp_path)
    agent = Agent(None, {"model": "test-model"}, ctx, Permissions(), HeadlessEvents())
    repl = Repl(agent, SessionStore(tmp_path), {"test": object()})
    
    # Ensure spinner.FOOTER is None (no duplicate middle bar)
    spinner.FOOTER = None
    
    # When busy, start a shimmer and verify _prompt_message renders the Hubble shimmer
    repl.is_busy = True
    sh = spinner.start_shimmer(Console(), label="Aligning mirrors")
    try:
        msg = repl._prompt_message()
        msg_text = "".join(text for _, text in msg)
        assert "Aligning mirrors" in msg_text
        assert "❯" in msg_text
    finally:
        sh.stop()
        msg_idle = repl._prompt_message()
        msg_idle_text = "".join(text for _, text in msg_idle)
        assert "Aligning mirrors" not in msg_idle_text
        assert "❯" in msg_idle_text


def test_no_live_during_prompt_proxy():
    from unittest.mock import patch
    import hubble.spinner as spinner
    from rich.console import Console

    con = Console()
    with patch("hubble.spinner.is_prompt_proxy_active", return_value=True):
        sh = spinner.start_shimmer(con, label="Thinking")
        # In prompt proxy mode, live background loop must NOT be instantiated
        assert sh.live is None
        assert not sh._stopped
        sh.stop()
        assert sh._stopped

