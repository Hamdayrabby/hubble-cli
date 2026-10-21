"""OS-native sandbox for shell commands, like Codex's: the command can read the whole machine
but can only write inside the workspace (plus temp dirs and any `sandbox_writable` paths), and
optionally has no network.

  macOS  sandbox-exec with a Seatbelt profile (built in).
  Linux  bubblewrap (`bwrap`): read-only bind of /, read-write binds of the writable paths.
         Needs the bubblewrap package and unprivileged user namespaces.
  Windows  no built-in equivalent; use shell_sandbox "docker" there.

shell_sandbox: "auto" (default) = native where available, else off; "native" = native or fail;
"docker"; "off".
"""

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional


class SandboxUnavailable(Exception):
    pass


@functools.lru_cache(maxsize=1)
def native_kind() -> Optional[str]:
    """'seatbelt', 'bwrap', or None when this machine has no usable native sandbox."""
    if sys.platform == "darwin" and shutil.which("sandbox-exec"):
        return "seatbelt"
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        try:
            ok = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "true"],
                                capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        return "bwrap" if ok else None
    return None


def why_unavailable() -> str:
    if sys.platform == "win32":
        return "Windows has no built-in command sandbox; use /sandbox docker (needs Docker Desktop)"
    if sys.platform.startswith("linux"):
        if not shutil.which("bwrap"):
            return "install bubblewrap (e.g. `sudo apt install bubblewrap`), or use /sandbox docker"
        return ("bubblewrap is installed but cannot create a sandbox here (unprivileged user namespaces "
                "are disabled, e.g. by AppArmor or inside a container); use /sandbox docker")
    return "sandbox-exec was not found"


def effective_mode(mode: str) -> str:
    """Resolve a shell_sandbox setting to what will actually run: off | native | docker."""
    mode = (mode or "off").lower()
    if mode == "auto":
        return "native" if native_kind() else "off"
    return mode if mode in ("native", "docker") else "off"


def _git_dirs(root: Path) -> List[Path]:
    """In a git worktree, .git is a file pointing into the main repo's .git; git writes there."""
    dot = root / ".git"
    if dot.is_file():
        try:
            text = dot.read_text(encoding="utf-8").strip()
        except OSError:
            return []
        if text.startswith("gitdir:"):
            gitdir = Path(text[7:].strip())
            gitdir = (root / gitdir).resolve() if not gitdir.is_absolute() else gitdir.resolve()
            common = gitdir.parent.parent if gitdir.parent.name == "worktrees" else gitdir
            return [gitdir, common]
    return []


def writable_paths(root: Path, extra: List[Path]) -> List[str]:
    paths = [root, *extra, *_git_dirs(root), Path(tempfile.gettempdir())]
    if sys.platform != "win32":
        paths += [Path("/tmp"), Path("/var/tmp")]
    if sys.platform == "darwin":
        paths += [Path("/private/var/folders")]
    out = []
    for p in paths:
        try:
            real = os.path.realpath(str(p))
        except OSError:
            continue
        if os.path.isdir(real) and real not in out:
            out.append(real)
    return out


def _seatbelt_profile(writable: List[str], network: bool) -> str:
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    rules = ["(version 1)", "(allow default)", "(deny file-write*)",
             "(allow file-write* " + " ".join(f"(subpath {q(p)})" for p in writable)
             + ' (literal "/dev/null") (literal "/dev/zero") (literal "/dev/dtracehelper")'
             + ' (regex #"^/dev/tty") (regex #"^/dev/fd/"))']
    if not network:
        rules += ['(deny network-outbound (remote ip "*:*"))', '(deny network-inbound (local ip "*:*"))']
    return "\n".join(rules)


def wrap(shell_argv: List[str], cmd: str, root: Path, extra_writable: List[Path], network: bool) -> List[str]:
    """argv that runs `cmd` in the native sandbox."""
    kind = native_kind()
    if kind is None:
        raise SandboxUnavailable(why_unavailable())
    writable = writable_paths(root, extra_writable)
    if kind == "seatbelt":
        return ["sandbox-exec", "-p", _seatbelt_profile(writable, network), *shell_argv, cmd]
    argv = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
    for p in writable:
        argv += ["--bind", p, p]
    if not network:
        argv += ["--unshare-net"]
    argv += ["--die-with-parent", "--chdir", str(root), "--", *shell_argv, cmd]
    return argv


DENIED_MARKERS = ("operation not permitted", "read-only file system", "permission denied", "sandbox")


def looks_blocked(output: str) -> bool:
    low = (output or "").lower()
    return any(m in low for m in DENIED_MARKERS)


def has_windows_job_object() -> bool:
    """Check if native Windows Job Object containment is supported on this machine."""
    return sys.platform == "win32"


def parse_memory_limit_bytes(mem_str: Optional[str]) -> Optional[int]:
    """Parse memory strings like '1g', '512m', '2gb' to integer bytes."""
    if not mem_str:
        return None
    s = str(mem_str).strip().lower()
    try:
        if s.endswith("gb") or s.endswith("g"):
            return int(float(s.rstrip("gb")) * 1024 * 1024 * 1024)
        if s.endswith("mb") or s.endswith("m"):
            return int(float(s.rstrip("mb")) * 1024 * 1024)
        if s.endswith("kb") or s.endswith("k"):
            return int(float(s.rstrip("kb")) * 1024)
        return int(s)
    except (ValueError, TypeError):
        return None


class WindowsJobSandbox:
    """Kernel-level process isolation and resource limits for Windows using Win32 Job Objects.

    Guarantees:
    - JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: When the job handle closes (or parent terminates),
      all child, grandchild, and detached processes in the job are immediately killed.
    - Memory cap enforcement: Limits total committed virtual memory across all processes in the job.
    - Process limit: Prevents fork bombs.
    """

    def __init__(self, memory_limit_bytes: Optional[int] = None, max_processes: int = 64):
        self.handle = None
        self.memory_limit = memory_limit_bytes
        self.max_processes = max_processes
        if sys.platform == "win32":
            self._init_job()

    def _init_job(self):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return

            flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            if self.memory_limit:
                flags |= 0x0200  # JOB_OBJECT_LIMIT_JOB_MEMORY
                info.JobMemoryLimit = self.memory_limit
            if self.max_processes:
                flags |= 0x0008  # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                info.BasicLimitInformation.ActiveProcessLimit = self.max_processes

            info.BasicLimitInformation.LimitFlags = flags
            ok = kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(info), ctypes.sizeof(info)
            )
            if ok:
                self.handle = handle
            else:
                kernel32.CloseHandle(handle)
                self.handle = None
        except Exception:
            self.handle = None

    def assign_process(self, proc: subprocess.Popen) -> bool:
        """Assign a spawned subprocess to this Job Object."""
        if not self.handle or sys.platform != "win32":
            return False
        import ctypes
        kernel32 = ctypes.windll.kernel32
        raw_handle = getattr(proc, "_handle", None)
        if raw_handle:
            try:
                return bool(kernel32.AssignProcessToJobObject(self.handle, int(raw_handle)))
            except Exception:
                return False
        return False

    def close(self):
        """Close the job handle, terminating all child processes if still active."""
        if self.handle and sys.platform == "win32":
            import ctypes
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

