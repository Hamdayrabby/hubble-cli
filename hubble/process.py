"""Asynchronous process supervisor and background task manager for Hubble CLI.

Enables running dev servers, watchers, build monitors, and interactive processes
without blocking the REPL or timing out after 120s.
"""

import collections
import itertools
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from hubble.settings import HOME_DIR


@dataclass
class BackgroundTask:
    task_id: str
    command: str
    proc: subprocess.Popen
    start_time: float
    log_file: Path
    tail_buffer: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=1000))
    status: str = "running"  # "running" | "completed" | "failed" | "killed"
    exit_code: Optional[int] = None
    win_job: Optional[Any] = None

    def poll(self) -> str:
        """Poll the process and update status."""
        if self.proc.poll() is not None:
            self.exit_code = self.proc.returncode
            if self.status != "killed":
                self.status = "completed" if self.exit_code == 0 else "failed"
            if self.win_job:
                try:
                    self.win_job.close()
                except Exception:
                    pass
                self.win_job = None
        return self.status

    def send_input(self, text: str) -> str:
        """Send input string to the process's standard input."""
        self.poll()
        if self.status != "running":
            raise RuntimeError(f"Task '{self.task_id}' is not running (status: {self.status})")
        if not self.proc.stdin:
            raise RuntimeError(f"Task '{self.task_id}' stdin is closed")
        try:
            if not text.endswith("\n"):
                text += "\n"
            self.proc.stdin.write(text)
            self.proc.stdin.flush()
            return f"Sent input to task '{self.task_id}'"
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Failed to write to stdin of task '{self.task_id}': {e}")

    def kill(self) -> str:
        """Terminate the process and its entire process tree."""
        self.poll()
        if self.status != "running":
            return f"Task '{self.task_id}' already terminated (status: {self.status})"
        self.status = "killed"
        if self.win_job:
            try:
                self.win_job.close()
            except Exception:
                pass
            self.win_job = None
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=2)
            else:
                import signal
                os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            self.proc.kill()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            pass
        self.poll()
        self.exit_code = self.proc.returncode if self.proc.returncode is not None else -9
        return f"Task '{self.task_id}' (PID {self.proc.pid}) killed"


class ProcessManager:
    """Singleton/workspace-level supervisor for active background processes."""

    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = log_dir or (HOME_DIR / "tasks")
        self.tasks: Dict[str, BackgroundTask] = {}
        self._id_counter = itertools.count(1)
        self._lock = threading.Lock()

    def start_task(self, command: str, argv: List[str], cwd: Path, env: Dict[str, str],
                   wait_ms: int = 1000, win_job: Optional[Any] = None) -> BackgroundTask:
        """Launch a process asynchronously with real-time log capturing."""
        self.log_dir.mkdir(parents=True, exist_ok=True)
        task_id = f"task-{next(self._id_counter)}"
        log_file = self.log_dir / f"{task_id}.log"
        log_fp = open(log_file, "w", encoding="utf-8", errors="replace", buffering=1)

        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                 else {"start_new_session": True})

        # Ensure unbuffered python and UTF-8
        merged_env = dict(env)
        merged_env.setdefault("PYTHONUNBUFFERED", "1")
        merged_env.setdefault("PYTHONIOENCODING", "utf-8")
        merged_env.setdefault("PYTHONUTF8", "1")

        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=merged_env,
                **group
            )
        except OSError as e:
            log_fp.close()
            if win_job:
                win_job.close()
            raise RuntimeError(f"Could not start process: {e}")

        if win_job:
            win_job.assign_process(proc)

        task = BackgroundTask(
            task_id=task_id,
            command=command,
            proc=proc,
            start_time=time.time(),
            log_file=log_file,
            win_job=win_job,
        )

        # Background stream reader
        def _reader(stream, prefix=""):
            try:
                for line in iter(stream.readline, ""):
                    formatted = f"{prefix}{line}" if prefix else line
                    log_fp.write(formatted)
                    log_fp.flush()
                    task.tail_buffer.append(formatted.rstrip("\r\n"))
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        t_out = threading.Thread(target=_reader, args=(proc.stdout, ""), daemon=True, name=f"{task_id}-out")
        t_err = threading.Thread(target=_reader, args=(proc.stderr, "[stderr] "), daemon=True, name=f"{task_id}-err")
        t_out.start()
        t_err.start()

        def _monitor():
            try:
                proc.wait()
            except Exception:
                pass
            try:
                t_out.join(timeout=1.0)
            except Exception:
                pass
            try:
                t_err.join(timeout=1.0)
            except Exception:
                pass
            task.poll()
            try:
                log_fp.flush()
                log_fp.close()
            except Exception:
                pass

        threading.Thread(target=_monitor, daemon=True, name=f"{task_id}-wait").start()

        with self._lock:
            self.tasks[task_id] = task

        # Wait initial wait_ms
        if wait_ms > 0:
            time.sleep(wait_ms / 1000.0)
        task.poll()
        return task

    def list_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for t in self.tasks.values():
                t.poll()
                runtime = round(time.time() - t.start_time, 1) if t.status == "running" else None
                out.append({
                    "task_id": t.task_id,
                    "command": t.command,
                    "pid": t.proc.pid,
                    "status": t.status,
                    "runtime_seconds": runtime,
                    "exit_code": t.exit_code,
                    "log_file": str(t.log_file),
                })
            return out

    def get_status(self, task_id: str, tail_lines: int = 50) -> Dict[str, Any]:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found. Use manage_task action='list' to view active tasks.")
            task.poll()
            recent_output = list(task.tail_buffer)[-tail_lines:]
            if not recent_output and task.log_file.exists():
                try:
                    with open(task.log_file, "r", encoding="utf-8", errors="replace") as f:
                        lines = [l.rstrip("\r\n") for l in f.readlines()]
                        recent_output = lines[-tail_lines:]
                except Exception:
                    pass
            return {
                "task_id": task.task_id,
                "command": task.command,
                "pid": task.proc.pid,
                "status": task.status,
                "exit_code": task.exit_code,
                "log_file": str(task.log_file),
                "recent_output": "\n".join(recent_output),
            }

    def send_input(self, task_id: str, text: str) -> str:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            return task.send_input(text)

    def kill_task(self, task_id: str) -> str:
        with self._lock:
            task = self.tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            return task.kill()


# Global default instance
default_process_manager = ProcessManager()
