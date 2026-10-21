import subprocess
import sys
import time
from pathlib import Path
import pytest

from hubble.process import BackgroundTask, ProcessManager
from hubble.tools import ManageTask, Shell, ToolContext


def test_process_manager_start_and_status(tmp_path: Path):
    pm = ProcessManager(log_dir=tmp_path / "tasks")
    task = pm.start_task(
        command="test-sleep",
        argv=[sys.executable, "-u", "-c", "import time; print('STARTED', flush=True); time.sleep(0.5); print('DONE', flush=True)"],
        cwd=tmp_path,
        env={},
        wait_ms=150,
    )
    assert task.task_id.startswith("task-")
    assert task.status in ("running", "completed")

    status = pm.get_status(task.task_id)
    for _ in range(15):
        if "STARTED" in status["recent_output"]:
            break
        time.sleep(0.1)
        status = pm.get_status(task.task_id)
    assert status["task_id"] == task.task_id
    assert "STARTED" in status["recent_output"]

    # Wait for completion
    task.proc.wait(timeout=5)
    task.poll()
    assert task.status == "completed"
    assert task.exit_code == 0

    time.sleep(0.1)
    status_done = pm.get_status(task.task_id)
    assert status_done["status"] == "completed"
    assert "DONE" in status_done["recent_output"]


def test_process_manager_send_input(tmp_path: Path):
    pm = ProcessManager(log_dir=tmp_path / "tasks")
    script = "import sys; line = sys.stdin.readline().strip(); print(f'RECV:{line}', flush=True)"
    task = pm.start_task(
        command="echo-test",
        argv=[sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env={},
        wait_ms=50,
    )
    assert task.status == "running"

    res = pm.send_input(task.task_id, "hello hubble")
    assert "hello hubble" in res or task.task_id in res

    task.proc.wait(timeout=5)
    task.poll()
    time.sleep(0.1)
    status = pm.get_status(task.task_id)
    assert "RECV:hello hubble" in status["recent_output"]


def test_process_manager_kill(tmp_path: Path):
    pm = ProcessManager(log_dir=tmp_path / "tasks")
    task = pm.start_task(
        command="long-runner",
        argv=[sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env={},
        wait_ms=50,
    )
    assert task.status == "running"
    kill_res = pm.kill_task(task.task_id)
    assert "killed" in kill_res.lower()
    assert task.status == "killed"
    assert task.proc.poll() is not None


def test_shell_tool_background_and_manage_task(tmp_path: Path):
    pm = ProcessManager(log_dir=tmp_path / "tasks")
    ctx = ToolContext(root=tmp_path, process_manager=pm)
    shell_tool = Shell()
    manage_tool = ManageTask()

    bg_script = tmp_path / "bg_runner.py"
    bg_script.write_text("import time\nprint('HELLO_BG', flush=True)\ntime.sleep(10)\n", encoding="utf-8")

    # Launch background task via shell tool
    out = shell_tool.run({
        "command": f'"{sys.executable}" "{bg_script}"',
        "is_background": True,
        "wait_ms": 300,
    }, ctx)
    assert "Background task started: task-" in out
    task_id = out.split("Background task started: ")[1].split(" ")[0].strip()

    # List tasks via manage_task
    list_out = manage_tool.run({"action": "list"}, ctx)
    assert task_id in list_out
    assert "running" in list_out

    # Status check via manage_task (poll briefly for PowerShell/Python process startup)
    status_out = ""
    for _ in range(30):
        status_out = manage_tool.run({"action": "status", "task_id": task_id}, ctx)
        if "HELLO_BG" in status_out:
            break
        time.sleep(0.1)
    assert f"Task ID: {task_id}" in status_out
    assert "HELLO_BG" in status_out

    # Kill task via manage_task
    kill_out = manage_tool.run({"action": "kill", "task_id": task_id}, ctx)
    assert "killed" in kill_out.lower()

    # Check status again
    status_post = manage_tool.run({"action": "status", "task_id": task_id}, ctx)
    assert "killed" in status_post.lower()
