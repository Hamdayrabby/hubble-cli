"""Shared subprocess plumbing for real adapters.

Spawns argv arrays (never shell strings), streams merged stdout/stderr lines
with a timeout, and kills the whole process tree on cancel. The kill pattern
mirrors hubble/process.py BackgroundTask.kill so behavior stays consistent.
"""

import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional


class ProcRun:
    def __init__(self, argv: List[str], cwd: Path, env: Dict[str, str]):
        self.argv = argv
        self.proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self.lines: List[str] = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        try:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                with self._lock:
                    self.lines.append(line)
        except (OSError, ValueError):
            pass

    @property
    def pid(self) -> int:
        return self.proc.pid

    def iter_lines(self, timeout_s: float = 600.0) -> Iterator[str]:
        """Yield lines as they arrive; kill the tree when the budget expires."""
        deadline = time.monotonic() + max(1.0, timeout_s)
        seen = 0
        while True:
            with self._lock:
                fresh = self.lines[seen:]
                seen = len(self.lines)
            for line in fresh:
                yield line
            if self.proc.poll() is not None:
                with self._lock:
                    fresh = self.lines[seen:]
                for line in fresh:
                    yield line
                return
            if time.monotonic() > deadline:
                self.kill_tree()
                raise TimeoutError(f"runtime timed out after {timeout_s:.0f}s")
            time.sleep(0.02)

    def wait(self, timeout_s: float = 5.0) -> Optional[int]:
        try:
            return self.proc.wait(timeout=timeout_s)
        except (subprocess.TimeoutExpired, OSError):
            return self.proc.poll()

    def kill_tree(self) -> str:
        if self.proc.poll() is not None:
            return f"process {self.proc.pid} already exited ({self.proc.returncode})"
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               capture_output=True, stdin=subprocess.DEVNULL, timeout=5)
            else:
                import os
                import signal
                os.killpg(self.proc.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            self.proc.kill()
        except OSError:
            pass
        self.wait()
        return f"process {self.proc.pid} killed"


def find_executable(names: List[str], extra_paths: Optional[List[Path]] = None) -> str:
    import shutil
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit
    for base in (extra_paths or []):
        for name in names:
            for suffix in ("", ".exe", ".cmd"):
                cand = base / f"{name}{suffix}"
                if cand.is_file():
                    return str(cand)
    return ""


_NOISE = ("CMD.EXE was started", "UNC paths are not supported", "'\\\\?\\")


def probe_version(executable: str, args: Optional[List[str]] = None) -> str:
    try:
        proc = subprocess.run([executable, *(args or ["--version"])],
                              capture_output=True, text=True, timeout=15,
                              stdin=subprocess.DEVNULL)
        for raw in (proc.stdout, proc.stderr):
            for line in (raw or "").splitlines():
                text = line.strip()
                if text and not any(n in text for n in _NOISE):
                    return text
        return ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
