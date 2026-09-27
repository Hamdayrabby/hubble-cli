"""Workspace tools exposed to the model through native function calling.

Every path is confined to the workspace root (plus configured additional_dirs),
secret files are refused, and file changes are snapshotted for /undo.
"""

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

MAX_OUTPUT_CHARS = 30000
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000
IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", "dist", "build",
                ".mypy_cache", ".pytest_cache", ".idea", ".tox", ".next", "target"}
SECRET_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", ".netrc", ".pgpass",
                ".npmrc", ".pypirc"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"}
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")


class ToolError(Exception):
    pass


def is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.endswith(".env"):
        return True
    if name.startswith(".env.") and not name.endswith(SAFE_ENV_SUFFIXES):
        return True
    return name in SECRET_NAMES or path.suffix.lower() in SECRET_SUFFIXES


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit * 2 // 3]
    tail = text[-limit // 3:]
    return f"{head}\n\n... [{len(text) - len(head) - len(tail)} chars truncated] ...\n\n{tail}"


def detect_shell(preference: str = "auto") -> List[str]:
    """argv prefix for running a command string."""
    pref = (preference or "auto").lower()
    if sys.platform == "win32":
        if pref in ("bash", "git-bash") and shutil.which("bash"):
            return [shutil.which("bash"), "-lc"]
        if pref == "cmd":
            return ["cmd", "/d", "/s", "/c"]
        exe = shutil.which("pwsh") if pref in ("auto", "pwsh") else None
        exe = exe or shutil.which("powershell") or "powershell"
        return [exe, "-NoProfile", "-NonInteractive", "-Command"]
    return [shutil.which("bash") or "/bin/sh", "-c"] if pref in ("auto", "bash") else [pref, "-c"]


def shell_name(argv: List[str]) -> str:
    exe = Path(argv[0]).stem.lower()
    return {"pwsh": "PowerShell 7", "powershell": "Windows PowerShell 5.1", "cmd": "cmd.exe"}.get(exe, exe)


@dataclass
class ToolContext:
    root: Path
    extra_dirs: List[Path] = field(default_factory=list)
    allow_secrets: bool = False
    shell_argv: List[str] = field(default_factory=lambda: detect_shell())
    shell_timeout: int = 120
    sandbox: str = "off"  # "off" | "docker": run shell commands in an isolated container
    sandbox_image: str = "python:3.12-slim"
    sandbox_memory: str = "1g"
    sandbox_cpus: str = "2"
    sandbox_network: bool = True
    read_mtimes: Dict[str, float] = field(default_factory=dict)
    todos: List[Dict[str, str]] = field(default_factory=list)
    # One dict per agent turn: absolute path -> original bytes (None if the file did not exist).
    checkpoints: List[Dict[str, Optional[bytes]]] = field(default_factory=list)

    def resolve(self, raw: str) -> Path:
        if not raw or not str(raw).strip():
            raise ToolError("path is required")
        p = Path(str(raw).strip()).expanduser()
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        for base in [self.root, *self.extra_dirs]:
            if p == base or p.is_relative_to(base):
                return p
        raise ToolError(f"'{raw}' is outside the workspace ({self.root}). "
                        "Add the directory to additional_dirs in .hubble/settings.json to allow it.")

    def rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.root).as_posix() or "."
        except ValueError:
            return str(p)

    def check_secret(self, p: Path):
        if is_secret_path(p) and not self.allow_secrets:
            raise ToolError(f"Access to '{self.rel(p)}' is blocked: it looks like a secrets file.")

    def begin_turn(self):
        self.checkpoints.append({})

    def reset_state(self):
        self.checkpoints.clear()
        self.read_mtimes.clear()
        self.todos = []

    def snapshot(self, p: Path):
        if not self.checkpoints:
            self.begin_turn()
        key = str(p)
        if key not in self.checkpoints[-1]:
            self.checkpoints[-1][key] = p.read_bytes() if p.exists() else None

    def undo(self) -> List[str]:
        """Revert the most recent turn that changed files. Returns changed relative paths."""
        while self.checkpoints and not self.checkpoints[-1]:
            self.checkpoints.pop()
        if not self.checkpoints:
            return []
        restored = []
        for key, original in self.checkpoints.pop().items():
            p = Path(key)
            if original is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(original)
            self.read_mtimes.pop(key, None)
            restored.append(self.rel(p))
        return restored


def _read_text(p: Path) -> str:
    with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _read_text_strict(p: Path) -> str:
    """For files about to be rewritten: lossy decoding would corrupt non-UTF-8 bytes."""
    try:
        with open(p, "r", encoding="utf-8", newline="") as f:
            return f.read()
    except UnicodeDecodeError:
        raise ToolError(f"'{p.name}' is not valid UTF-8; refusing to rewrite it (it would be corrupted). "
                        "Edit it with a shell command that preserves its encoding, or ask the user.")


def _write_text(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _is_binary(p: Path) -> bool:
    try:
        with open(p, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return False


def unified_diff(old: str, new: str, name: str) -> str:
    diff = difflib.unified_diff(old.replace("\r\n", "\n").splitlines(keepends=True),
                                new.replace("\r\n", "\n").splitlines(keepends=True),
                                fromfile=f"a/{name}", tofile=f"b/{name}", n=3)
    return "".join(diff)


class Tool:
    name = ""
    description = ""
    parameters: Dict[str, Any] = {}
    kind = "read"  # read | edit | exec

    def schema(self) -> Dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    def target(self, args: Dict[str, Any]) -> str:
        """String matched against permission rules, e.g. a path or a command."""
        return str(args.get("path") or args.get("command") or "")

    def preview(self, args: Dict[str, Any], ctx: ToolContext) -> Optional[str]:
        """Diff or description shown in the approval prompt."""
        return None

    def precheck(self, args: Dict[str, Any], ctx: ToolContext):
        """Raise ToolError for calls certain to fail, so the user is not asked to approve them."""

    def validate(self, args: Dict[str, Any]):
        props = self.parameters.get("properties", {})
        for req in self.parameters.get("required", []):
            if req not in args or args[req] is None:
                raise ToolError(f"missing required argument '{req}'")
        for key, val in args.items():
            expected = props.get(key, {}).get("type")
            if expected == "integer" and not isinstance(val, int):
                try:
                    args[key] = int(val)
                except (TypeError, ValueError):
                    raise ToolError(f"argument '{key}' must be an integer")
            elif expected == "boolean" and isinstance(val, str):
                args[key] = val.lower() in ("true", "1", "yes")
            elif expected == "string" and not isinstance(val, str):
                args[key] = str(val)

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        raise NotImplementedError


class ReadFile(Tool):
    name = "read_file"
    description = ("Read a text file from the workspace. Returns lines prefixed with line numbers "
                   "(`N | text`; the prefix is not part of the file). Use offset/limit for large files. "
                   "Always read a file before editing it.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace root"},
        "offset": {"type": "integer", "description": "1-based line to start from (default 1)"},
        "limit": {"type": "integer", "description": f"Max lines to return (default {MAX_READ_LINES})"},
    }, "required": ["path"]}

    def run(self, args, ctx):
        p = ctx.resolve(args["path"])
        ctx.check_secret(p)
        if not p.exists():
            raise ToolError(f"File not found: {args['path']}")
        if p.is_dir():
            raise ToolError(f"'{args['path']}' is a directory; use list_dir")
        if _is_binary(p):
            raise ToolError(f"'{args['path']}' is a binary file")
        lines = _read_text(p).splitlines()
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or MAX_READ_LINES), MAX_READ_LINES))
        chunk = lines[offset - 1: offset - 1 + limit]
        ctx.read_mtimes[str(p)] = p.stat().st_mtime
        if not lines:
            return f"[{ctx.rel(p)} is empty]"
        width = len(str(offset + len(chunk)))
        body = "\n".join(f"{i:>{width}} | {line[:MAX_LINE_CHARS]}"
                         for i, line in enumerate(chunk, offset))
        end = offset + len(chunk) - 1
        header = f"[{ctx.rel(p)} lines {offset}-{end} of {len(lines)}]"
        more = f"\n[... {len(lines) - end} more lines; continue with offset={end + 1}]" if end < len(lines) else ""
        return truncate(f"{header}\n{body}{more}")


def _loose_line_match(text: str, old: str) -> Optional[tuple]:
    """Find old as whole lines, ignoring trailing whitespace and surrounding blank lines.

    Returns (start, end) character offsets of the unique match, excluding the final line break.
    """
    old_lines = [l.rstrip() for l in old.strip("\r\n").replace("\r\n", "\n").split("\n")]
    if not old_lines or not any(old_lines):
        return None
    lines = text.splitlines(keepends=True)
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    hits = [i for i in range(len(lines) - len(old_lines) + 1)
            if all(lines[i + j].rstrip() == old_lines[j] for j in range(len(old_lines)))]
    if len(hits) != 1:
        return None
    i = hits[0]
    last = lines[i + len(old_lines) - 1]
    end = offsets[i + len(old_lines) - 1] + len(last.rstrip("\r\n"))
    return offsets[i], end


def _require_fresh_read(p: Path, ctx: ToolContext, verb: str):
    if not p.exists():
        return
    seen = ctx.read_mtimes.get(str(p))
    if seen is None:
        raise ToolError(f"You must read_file '{ctx.rel(p)}' before you {verb} it.")
    if p.stat().st_mtime > seen + 1e-6:
        raise ToolError(f"'{ctx.rel(p)}' changed since you last read it. Read it again first.")


class WriteFile(Tool):
    name = "write_file"
    description = ("Create a new file or completely overwrite an existing one. Prefer edit_file for "
                   "changes to existing files. Existing files must be read first.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace root"},
        "content": {"type": "string", "description": "Full file content"},
    }, "required": ["path", "content"]}
    kind = "edit"

    def precheck(self, args, ctx):
        p = ctx.resolve(args["path"])
        ctx.check_secret(p)
        if p.is_dir():
            raise ToolError(f"'{args['path']}' is a directory")
        _require_fresh_read(p, ctx, "overwrite")

    def preview(self, args, ctx):
        p = ctx.resolve(args["path"])
        old = _read_text(p) if p.is_file() else ""
        return unified_diff(old, args.get("content", ""), ctx.rel(p)) or "(no changes)"

    def run(self, args, ctx):
        p = ctx.resolve(args["path"])
        ctx.check_secret(p)
        if p.is_dir():
            raise ToolError(f"'{args['path']}' is a directory")
        _require_fresh_read(p, ctx, "overwrite")
        content = args["content"]
        existed = p.exists()
        if existed and "\r\n" in _read_text_strict(p) and "\r\n" not in content:
            content = content.replace("\n", "\r\n")
        ctx.snapshot(p)
        _write_text(p, content)
        ctx.read_mtimes[str(p)] = p.stat().st_mtime
        verb = "Updated" if existed else "Created"
        return f"{verb} {ctx.rel(p)} ({len(content.splitlines())} lines)"


class EditFile(Tool):
    name = "edit_file"
    description = ("Replace an exact string in a file. old_string must match the file exactly "
                   "(including indentation, without line-number prefixes) and be unique unless "
                   "replace_all is true. Include surrounding lines to make it unique.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace root"},
        "old_string": {"type": "string", "description": "Exact text to replace"},
        "new_string": {"type": "string", "description": "Replacement text"},
        "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)"},
    }, "required": ["path", "old_string", "new_string"]}
    kind = "edit"

    def _apply(self, args, ctx):
        p = ctx.resolve(args["path"])
        ctx.check_secret(p)
        if not p.is_file():
            raise ToolError(f"File not found: {args['path']}. Use write_file to create it.")
        text = _read_text_strict(p)
        old, new = args["old_string"], args["new_string"]
        if not old:
            raise ToolError("old_string is empty. Use write_file to create or replace a whole file.")
        if old == new:
            raise ToolError("old_string and new_string are identical")
        crlf = "\r\n" in text
        count = text.count(old)
        if count == 0 and crlf and "\r\n" not in old:
            old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            span = _loose_line_match(text, old)
            if span:
                start, end = span
                replacement = new.strip("\r\n").replace("\r\n", "\n")
                if crlf:
                    replacement = replacement.replace("\n", "\r\n")
                return p, text, text[:start] + replacement + text[end:], 1
        if count == 0:
            hint = ""
            first = old.strip().splitlines()[0].strip() if old.strip() else ""
            if first and first in text:
                line_no = text[: text.index(first)].count("\n") + 1
                hint = f" The first line appears near line {line_no}; check whitespace and re-read the file."
            raise ToolError(f"old_string not found in {ctx.rel(p)}.{hint}")
        if count > 1 and not args.get("replace_all"):
            raise ToolError(f"old_string matches {count} places in {ctx.rel(p)}. "
                            "Add surrounding context to make it unique, or set replace_all=true.")
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        return p, text, updated, count

    def precheck(self, args, ctx):
        p, _, _, _ = self._apply(args, ctx)
        _require_fresh_read(p, ctx, "edit")

    def preview(self, args, ctx):
        try:
            p, text, updated, _ = self._apply(args, ctx)
        except ToolError as e:
            return f"(edit will fail: {e})"
        return unified_diff(text, updated, ctx.rel(p))

    def run(self, args, ctx):
        p, text, updated, count = self._apply(args, ctx)
        _require_fresh_read(p, ctx, "edit")
        ctx.snapshot(p)
        _write_text(p, updated)
        ctx.read_mtimes[str(p)] = p.stat().st_mtime
        n = count if args.get("replace_all") else 1
        return f"Edited {ctx.rel(p)} ({n} replacement{'s' if n != 1 else ''})"


class Shell(Tool):
    name = "shell"
    description = ("Run a shell command in the workspace root and return stdout, stderr and exit code. "
                   "Use it for builds, tests, git and package managers. Commands are non-interactive "
                   "(stdin is closed) and time out. Do not use it to read or edit files; use the file tools.")
    parameters = {"type": "object", "properties": {
        "command": {"type": "string", "description": "Command to run"},
        "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 600)"},
        "description": {"type": "string", "description": "5-10 word summary of what the command does"},
    }, "required": ["command"]}
    kind = "exec"

    def preview(self, args, ctx):
        return args.get("command", "")

    def run(self, args, ctx):
        cmd = args["command"]
        timeout = max(1, min(int(args.get("timeout") or ctx.shell_timeout), 600))
        if ctx.sandbox == "docker":
            return self._run_docker(cmd, timeout, ctx)
        return self._run_host(cmd, timeout, ctx)

    def _run_host(self, cmd: str, timeout: int, ctx: ToolContext) -> str:
        argv = list(ctx.shell_argv)
        if "powershell" in argv[0].lower() or "pwsh" in argv[0].lower():
            cmd = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        # Own process group, so timeouts and Ctrl+C kill the whole tree. Otherwise grandchildren
        # (dev servers, watchers) keep the pipes open and the CLI hangs.
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                 else {"start_new_session": True})
        try:
            proc = subprocess.Popen(argv + [cmd], cwd=ctx.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", env=env, **group)
        except OSError as e:
            raise ToolError(f"Could not start shell: {e}")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            stdout, stderr = _drain(proc)
            raise ToolError(f"Command timed out after {timeout}s (process tree killed). "
                            "Long-running servers are not supported.\n" + truncate(stdout or "", 5000))
        except KeyboardInterrupt:
            _kill_tree(proc)
            _drain(proc)
            raise
        return _format_result(stdout, stderr, proc.returncode)

    def _run_docker(self, cmd: str, timeout: int, ctx: ToolContext) -> str:
        docker = shutil.which("docker")
        if not docker:
            raise ToolError("sandbox is set to 'docker' but the docker CLI was not found on PATH. "
                            "Install Docker Desktop, or set shell_sandbox to \"off\".")
        import uuid
        name = f"hubble-sandbox-{uuid.uuid4().hex[:12]}"
        argv = [docker, "run", "--rm", "--name", name, "-i",
                "--memory", ctx.sandbox_memory, "--cpus", ctx.sandbox_cpus,
                "-v", f"{ctx.root}:/workspace", "-w", "/workspace"]
        if not ctx.sandbox_network:
            argv += ["--network", "none"]
        argv += [ctx.sandbox_image, "sh", "-lc", cmd]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        except OSError as e:
            raise ToolError(f"Could not start docker: {e}")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._docker_kill(docker, name)
            stdout, stderr = _drain(proc)
            raise ToolError(f"Command timed out after {timeout}s (sandbox container killed). "
                            "Long-running servers are not supported.\n" + truncate(stdout or "", 5000))
        except KeyboardInterrupt:
            self._docker_kill(docker, name)
            _drain(proc)
            raise
        if proc.returncode == 125 and "Unable to find image" in (stderr or ""):
            raise ToolError(f"Sandbox image '{ctx.sandbox_image}' could not be pulled.\n{stderr.strip()}")
        if proc.returncode == 125 and "docker daemon" in (stderr or "").lower():
            raise ToolError("Docker is installed but the daemon isn't running. Start Docker Desktop, "
                            f"or set shell_sandbox to \"off\".\n{stderr.strip()}")
        return _format_result(stdout, stderr, proc.returncode)

    @staticmethod
    def _docker_kill(docker: str, name: str):
        # Kill by container name directly: killing the `docker run` client process does not
        # reliably stop the container itself, so the client kill alone is not enough.
        try:
            subprocess.run([docker, "kill", name], capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _format_result(stdout: str, stderr: str, returncode: int) -> str:
    parts = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.rstrip()}")
    parts.append(f"[exit code {returncode}]")
    return truncate("\n".join(parts))


def _kill_tree(proc: subprocess.Popen):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           stdin=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(proc: subprocess.Popen):
    try:
        return proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError):
        return "", ""


def _is_link(p: Path) -> bool:
    try:
        if p.is_symlink():
            return True
        # Path.is_junction() only exists from Python 3.12; fall back to the win32 reparse-point
        # bit on older versions so a Windows junction is still refused pre-3.12, not skipped.
        is_junction = getattr(p, "is_junction", None)
        if is_junction is not None:
            return is_junction()
        if sys.platform == "win32":
            attrs = getattr(p.stat(), "st_file_attributes", 0)
            return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
        return False
    except OSError:
        return True


def _walk_files(base: Path):
    for dirpath, dirs, files in os.walk(base):
        # Links and junctions can point outside the workspace; do not descend into them.
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.endswith(".egg-info")
                         and not _is_link(Path(dirpath) / d))
        for name in sorted(files):
            yield Path(dirpath) / name


class Grep(Tool):
    name = "grep"
    description = ("Search file contents with a regular expression (ripgrep syntax). Returns "
                   "`path:line: text` matches, or only file paths with files_only=true.")
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Regular expression"},
        "path": {"type": "string", "description": "File or directory to search (default workspace root)"},
        "glob": {"type": "string", "description": "Only search files matching this glob, e.g. *.py"},
        "ignore_case": {"type": "boolean", "description": "Case-insensitive search"},
        "files_only": {"type": "boolean", "description": "Return only matching file paths"},
    }, "required": ["pattern"]}
    max_results = 200

    def target(self, args):
        return str(args.get("path") or ".")

    def run(self, args, ctx):
        base = ctx.resolve(args.get("path") or ".")
        if base.is_file():
            ctx.check_secret(base)
        pattern = args["pattern"]
        rg = shutil.which("rg")
        if rg:
            out = self._ripgrep(rg, pattern, base, args, ctx)
        else:
            out = self._python(pattern, base, args, ctx)
        if not out:
            return (f"No matches for /{pattern}/ in file contents. "
                    "(grep searches inside files; use glob to find files by name.)")
        extra = f"\n[... truncated at {self.max_results} results]" if len(out) >= self.max_results else ""
        return truncate("\n".join(out[: self.max_results]) + extra)

    def _ripgrep(self, rg, pattern, base, args, ctx) -> List[str]:
        cmd = [rg, "--no-heading", "--line-number", "--color", "never", "--max-columns", "300",
               "--max-count", "50"]
        if args.get("ignore_case"):
            cmd.append("-i")
        if args.get("files_only"):
            cmd.append("-l")
        if args.get("glob"):
            cmd += ["--glob", args["glob"]]
        if not ctx.allow_secrets:
            # Speeds things up; the is_secret_path filter below is what enforces the policy.
            cmd.append("--glob-case-insensitive")
            for g in (".env", "*.env", ".env.*", "id_*", *SECRET_NAMES, *(f"*{s}" for s in SECRET_SUFFIXES)):
                cmd += ["--glob", f"!{g}"]
        cmd += ["-e", pattern, "--", ctx.rel(base)]
        try:
            res = subprocess.run(cmd, cwd=ctx.root, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=60, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ToolError(f"ripgrep failed: {e}")
        if res.returncode == 2 and not res.stdout:
            raise ToolError(res.stderr.strip()[:500] or "ripgrep error")
        lines = []
        for line in res.stdout.splitlines():
            m = re.match(r"^((?:[A-Za-z]:)?[^:]*)(:.*)?$", line)
            path, rest = (m.group(1), m.group(2) or "") if m else (line, "")
            path = path.replace("\\", "/").removeprefix("./")
            if not ctx.allow_secrets and is_secret_path(Path(path)):
                continue
            lines.append(path + rest)
            if len(lines) >= self.max_results:
                break
        return lines

    def _python(self, pattern, base, args, ctx) -> List[str]:
        try:
            rx = re.compile(pattern, re.IGNORECASE if args.get("ignore_case") else 0)
        except re.error as e:
            raise ToolError(f"Invalid regex: {e}")
        files = [base] if base.is_file() else _walk_files(base)
        out = []
        for f in files:
            if args.get("glob") and not fnmatch.fnmatch(f.name, args["glob"]):
                continue
            if (is_secret_path(f) and not ctx.allow_secrets) or _is_binary(f):
                continue
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            if args.get("files_only"):
                                out.append(ctx.rel(f))
                                break
                            out.append(f"{ctx.rel(f)}:{i}:{line.rstrip()[:300]}")
                            if len(out) >= self.max_results:
                                return out
            except OSError:
                continue
        return out


class Glob(Tool):
    name = "glob"
    description = ("Find files by glob pattern, e.g. **/*.py or src/**/test_*.js. "
                   "Returns paths sorted by most recently modified.")
    parameters = {"type": "object", "properties": {
        "pattern": {"type": "string", "description": "Glob pattern relative to path"},
        "path": {"type": "string", "description": "Directory to search in (default workspace root)"},
    }, "required": ["pattern"]}
    max_results = 200

    def target(self, args):
        return str(args.get("path") or ".")

    def run(self, args, ctx):
        base = ctx.resolve(args.get("path") or ".")
        pattern = args["pattern"].replace("\\", "/")
        if not pattern.startswith("**/") and "/" not in pattern:
            pattern = "**/" + pattern
        # fnmatch's * also matches "/", so "**/" only needs extra variants for zero directories.
        variants = {pattern, pattern.replace("/**/", "/")}
        if pattern.startswith("**/"):
            variants.add(pattern[3:])
        matches = []
        for f in _walk_files(base):
            rel = f.relative_to(base).as_posix()
            if any(fnmatch.fnmatch(rel, v) for v in variants):
                matches.append(f)
        if not matches:
            return f"No files match '{args['pattern']}'"

        def mtime(f: Path) -> float:
            try:
                return f.stat().st_mtime
            except OSError:
                return 0.0
        matches.sort(key=mtime, reverse=True)
        more = f"\n[... {len(matches) - self.max_results} more]" if len(matches) > self.max_results else ""
        return "\n".join(ctx.rel(f) for f in matches[: self.max_results]) + more


class ListDir(Tool):
    name = "list_dir"
    description = "List the files and subdirectories of a directory."
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "Directory (default workspace root)"},
    }}

    def run(self, args, ctx):
        p = ctx.resolve(args.get("path") or ".")
        if not p.is_dir():
            raise ToolError(f"Not a directory: {args.get('path')}")
        entries = sorted((e for e in p.iterdir() if e.name not in IGNORED_DIRS),
                         key=lambda e: (not e.is_dir(), e.name.lower()))
        lines = [f"[{ctx.rel(p)}]"]
        for e in entries[:300]:
            if e.is_dir():
                lines.append(f"  {e.name}/")
            else:
                try:
                    size = e.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"  {e.name}  ({size} bytes)")
        if len(entries) > 300:
            lines.append(f"  ... {len(entries) - 300} more")
        return "\n".join(lines)


class TodoWrite(Tool):
    name = "todo_write"
    description = ("Create or update the task list for multi-step work. Send the full list each time. "
                   "Keep exactly one item in_progress while working; mark items completed as soon as done.")
    parameters = {"type": "object", "properties": {
        "todos": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
        }, "required": ["content", "status"]}},
    }, "required": ["todos"]}

    def target(self, args):
        return ""

    def validate(self, args):
        super().validate(args)
        if not isinstance(args["todos"], list):
            raise ToolError("todos must be an array")

    def run(self, args, ctx):
        todos = []
        for t in args["todos"]:
            if isinstance(t, dict) and t.get("content"):
                status = t.get("status", "pending")
                todos.append({"content": str(t["content"]),
                              "status": status if status in ("pending", "in_progress", "completed") else "pending"})
        ctx.todos = todos
        done = sum(t["status"] == "completed" for t in todos)
        return f"Todo list updated ({done}/{len(todos)} completed)"


def default_tools() -> List[Tool]:
    return [ReadFile(), WriteFile(), EditFile(), Shell(), Grep(), Glob(), ListDir(), TodoWrite()]


READ_ONLY_TOOL_NAMES = {"read_file", "grep", "glob", "list_dir"}


def run_tool(tool: Tool, args: Dict[str, Any], ctx: ToolContext) -> tuple:
    """Returns (output, is_error). Never raises for tool-level failures."""
    try:
        tool.validate(args)
        return tool.run(args, ctx), False
    except ToolError as e:
        return f"Error: {e}", True
    except OSError as e:
        return f"Error: {e}", True
