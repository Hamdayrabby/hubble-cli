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
