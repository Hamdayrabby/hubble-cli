"""Workspace tools exposed to the model through native function calling.

Every path is confined to the workspace root (plus configured additional_dirs),
secret files are refused, and file changes are snapshotted for /undo.
"""

import ast
import difflib
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hubble.messaging import MessageBus, default_message_bus
from hubble.process import ProcessManager, default_process_manager

MAX_OUTPUT_CHARS = 30000
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 50000
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
    allow_external_reads: bool = False  # allow read_file/grep/glob to read existing files outside workspace
    shell_argv: List[str] = field(default_factory=lambda: detect_shell())
    shell_timeout: int = 120
    sandbox: str = "off"  # "off" | "auto" | "native" | "docker" (see sandbox.py)
    sandbox_image: str = "python:3.12-slim"
    sandbox_memory: str = "1g"
    sandbox_cpus: str = "2"
    sandbox_network: bool = True
    sandbox_writable: List[Path] = field(default_factory=list)  # extra writable dirs for the native sandbox
    allow_unsandboxed: bool = True  # False on a server: `unsandboxed: true` shell calls are refused
    read_mtimes: Dict[str, float] = field(default_factory=dict)
    todos: List[Dict[str, str]] = field(default_factory=list)
    # One dict per agent turn: absolute path -> original bytes (None if the file did not exist).
    checkpoints: List[Dict[str, Optional[bytes]]] = field(default_factory=list)
    process_manager: ProcessManager = field(default_factory=lambda: default_process_manager)
    agent_id: str = "main"
    message_bus: Any = field(default_factory=lambda: default_message_bus)

    def resolve(self, raw: str, write: bool = False) -> Path:
        if not raw or not str(raw).strip():
            raise ToolError("path is required")
        p = Path(str(raw).strip()).expanduser()
        if not p.is_absolute():
            p = self.root / p
        p = p.resolve()
        for base in [self.root, *self.extra_dirs]:
            if p == base or p.is_relative_to(base):
                return p
        if not write and self.allow_external_reads and p.exists():
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

    def begin_transaction(self) -> "FileTransaction":
        return FileTransaction(self)



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


def validate_code_syntax(path: Path, content: str) -> Optional[str]:
    """Check code syntax for known file types before writing to disk."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        try:
            ast.parse(content)
        except SyntaxError as e:
            return f"Python syntax error at line {e.lineno}: {e.msg}"
    elif suffix == ".json":
        try:
            json.loads(content)
        except ValueError as e:
            return f"JSON syntax error: {e}"
    return None


class FileTransaction:
    """Two-Phase Commit (2PC) transaction for staging and committing multi-file edits."""

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.staged: Dict[Path, str] = {}
        self.backups: Dict[Path, Optional[bytes]] = {}
        self.committed = False

    def stage_write(self, p: Path, content: str, skip_syntax: bool = False):
        if not skip_syntax:
            err = validate_code_syntax(p, content)
            if err:
                existed = p.exists()
                orig_err = validate_code_syntax(p, _read_text_strict(p)) if existed else None
                if not orig_err:
                    raise ToolError(f"Pre-commit syntax validation failed for {self.ctx.rel(p)}: {err}")
        if p not in self.backups:
            self.backups[p] = p.read_bytes() if p.exists() else None
        self.staged[p] = content

    def commit(self) -> List[str]:
        written = []
        try:
            for p, content in self.staged.items():
                self.ctx.snapshot(p)
                _write_text(p, content)
                self.ctx.read_mtimes[str(p)] = p.stat().st_mtime
                written.append(self.ctx.rel(p))
            self.committed = True
            return written
        except Exception as e:
            self.rollback()
            raise ToolError(f"Transaction commit failed: {e}. All staged files restored.") from e

    def rollback(self):
        for p, backup in self.backups.items():
            if backup is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(backup)
        self.staged.clear()


class WriteFile(Tool):
    name = "write_file"
    description = ("Create a new file or completely overwrite an existing one. Prefer edit_file for "
                   "changes to existing files. Existing files must be read first.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace root"},
        "content": {"type": "string", "description": "Full file content"},
        "skip_syntax_check": {"type": "boolean", "description": "Skip pre-commit syntax check (default false)"},
    }, "required": ["path", "content"]}
    kind = "edit"

    def precheck(self, args, ctx):
        p = ctx.resolve(args["path"], write=True)
        ctx.check_secret(p)
        if p.is_dir():
            raise ToolError(f"'{args['path']}' is a directory")
        _require_fresh_read(p, ctx, "overwrite")

    def preview(self, args, ctx):
        p = ctx.resolve(args["path"], write=True)
        old = _read_text(p) if p.is_file() else ""
        return unified_diff(old, args.get("content", ""), ctx.rel(p)) or "(no changes)"

    def run(self, args, ctx):
        p = ctx.resolve(args["path"], write=True)
        ctx.check_secret(p)
        if p.is_dir():
            raise ToolError(f"'{args['path']}' is a directory")
        _require_fresh_read(p, ctx, "overwrite")
        content = args["content"]
        if not args.get("skip_syntax_check"):
            err = validate_code_syntax(p, content)
            if err:
                existed = p.exists()
                orig_err = validate_code_syntax(p, _read_text_strict(p)) if existed else None
                if not orig_err:
                    raise ToolError(f"Pre-commit syntax validation failed for {ctx.rel(p)}: {err}. "
                                    "Write was rejected to prevent corrupting the file.")
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
        "skip_syntax_check": {"type": "boolean", "description": "Skip pre-commit syntax check (default false)"},
    }, "required": ["path", "old_string", "new_string"]}
    kind = "edit"

    def _apply(self, args, ctx):
        p = ctx.resolve(args["path"], write=True)
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
        if count == 0 and any(re.match(r"^\s*\d+\s*\|\s*", line) for line in old.splitlines()):
            # Strip line-number prefixes (e.g. '  14 | ') copied from read_file
            c_old_lines = [re.sub(r"^\s*\d+\s*\|\s*", "", line) for line in old.splitlines()]
            c_old = "\r\n".join(c_old_lines) if crlf else "\n".join(c_old_lines)
            c_new_lines = [re.sub(r"^\s*\d+\s*\|\s*", "", line) for line in new.splitlines()]
            c_new = "\r\n".join(c_new_lines) if crlf else "\n".join(c_new_lines)
            if text.count(c_old) == 1:
                return p, text, text.replace(c_old, c_new, 1), 1
            span = _loose_line_match(text, c_old)
            if span:
                start, end = span
                replacement = c_new.strip("\r\n").replace("\r\n", "\n")
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
        if not args.get("skip_syntax_check"):
            err = validate_code_syntax(p, updated)
            if err:
                orig_err = validate_code_syntax(p, text)
                if not orig_err:
                    raise ToolError(f"Pre-commit syntax validation failed for {ctx.rel(p)}: {err}. "
                                    "Edit was rejected to prevent corrupting the file.")
        ctx.snapshot(p)
        _write_text(p, updated)
        ctx.read_mtimes[str(p)] = p.stat().st_mtime
        n = count if args.get("replace_all") else 1
        return f"Edited {ctx.rel(p)} ({n} replacement{'s' if n != 1 else ''})"



@dataclass
class _DiffHunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: List[str]


_HUNK_RE = re.compile(r"^@@\s*-(\d+)(?:,(\d+))?\s*\+(\d+)(?:,(\d+))?\s*@@(?:.*)?$")


def parse_unified_diff(patch_str: str) -> List[_DiffHunk]:
    """Parse a unified diff into a list of _DiffHunk objects.

    Tolerant to git diff headers (diff --git, index, ---, +++), omitted line counts,
    and blank context lines that omit leading spaces.
    """
    lines = patch_str.splitlines()
    hunks: List[_DiffHunk] = []
    current_hunk: Optional[_DiffHunk] = None

    for raw_line in lines:
        m = _HUNK_RE.match(raw_line)
        if m:
            if current_hunk is not None:
                hunks.append(current_hunk)
            old_s = int(m.group(1))
            old_l = int(m.group(2)) if m.group(2) is not None else (1 if old_s != 0 else 0)
            new_s = int(m.group(3))
            new_l = int(m.group(4)) if m.group(4) is not None else (1 if new_s != 0 else 0)
            current_hunk = _DiffHunk(old_start=old_s, old_lines=old_l,
                                     new_start=new_s, new_lines=new_l, lines=[])
            continue

        if current_hunk is None:
            continue

        if raw_line.startswith((" ", "+", "-", "\\")):
            current_hunk.lines.append(raw_line)
        elif raw_line == "":
            current_hunk.lines.append(" ")
        elif raw_line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        else:
            current_hunk.lines.append(" " + raw_line)

    if current_hunk is not None:
        hunks.append(current_hunk)

    if not hunks:
        raise ToolError("No unified diff hunks found in patch (expected '@@ -start,len +start,len @@').")
    return hunks


def split_patch_into_hunks(patch_str: str) -> List[str]:
    """Split a multi-hunk unified diff patch into individual single-hunk patch strings."""
    hunks = parse_unified_diff(patch_str)
    header_lines = []
    for line in patch_str.splitlines():
        if line.startswith("@@"):
            break
        if line.startswith(("---", "+++", "diff --git", "index ")):
            header_lines.append(line)
    hdr = "\n".join(header_lines) + ("\n" if header_lines else "")

    result = []
    for h in hunks:
        hunk_lines = [f"@@ -{h.old_start},{h.old_lines} +{h.new_start},{h.new_lines} @@"] + h.lines
        result.append(hdr + "\n".join(hunk_lines) + "\n")
    return result


def apply_selected_hunks(
    original_text: str,
    patch_str: str,
    selected_hunk_indices: Optional[List[int]] = None,
    path_for_err: str = ""
) -> Tuple[str, int]:
    """Apply a unified diff patch string to original_text, optionally filtering by hunk indices.

    Returns (updated_text, applied_hunk_count).
    Supports selective hunk staging, fuzzy offset matching (up to +-100 lines),
    stripped whitespace matching, and CRLF/LF line ending preservation.
    """
    hunks = parse_unified_diff(patch_str)
    is_crlf = "\r\n" in original_text
    has_trailing_newline = (not original_text) or original_text.endswith("\n") or original_text.endswith("\r\n")

    if selected_hunk_indices is not None:
        target_indices = {i for i in selected_hunk_indices if 0 <= i < len(hunks)}
    else:
        target_indices = set(range(len(hunks)))

    if not target_indices:
        return original_text, 0

    # Check if the last applied hunk ends with a '\ No newline at end of file' directive
    for hunk_idx in sorted(target_indices, reverse=True):
        last_hunk = hunks[hunk_idx]
        found_nl_directive = False
        for hline in reversed(last_hunk.lines):
            if hline.startswith("\\"):
                has_trailing_newline = False
                found_nl_directive = True
                break
            elif hline.startswith(("+", "-")):
                break
        if found_nl_directive:
            break

    file_lines = original_text.replace("\r\n", "\n").splitlines()
    line_offset = 0
    applied_count = 0

    for i, hunk in enumerate(hunks):
        old_hunk_lines: List[str] = []
        new_hunk_lines: List[str] = []
        for hline in hunk.lines:
            if hline.startswith(" "):
                c = hline[1:]
                old_hunk_lines.append(c)
                new_hunk_lines.append(c)
            elif hline.startswith("-"):
                old_hunk_lines.append(hline[1:])
            elif hline.startswith("+"):
                new_hunk_lines.append(hline[1:])
            elif hline.startswith("\\"):
                continue

        if i not in target_indices:
            continue

        target_idx = max(0, min(hunk.old_start - 1 + line_offset, len(file_lines)))

        if not old_hunk_lines:
            # Pure insertion
            found_idx = target_idx
        else:
            def _matches(idx: int, fuzzy: bool) -> bool:
                if idx < 0 or idx + len(old_hunk_lines) > len(file_lines):
                    return False
                if not fuzzy:
                    return all(file_lines[idx + j] == old_hunk_lines[j] for j in range(len(old_hunk_lines)))
                return all(file_lines[idx + j].rstrip() == old_hunk_lines[j].rstrip() for j in range(len(old_hunk_lines)))

            found_idx = None
            # 1. Exact match at target_idx
            if _matches(target_idx, fuzzy=False):
                found_idx = target_idx
            else:
                # 2. Offset scan near target_idx (exact)
                max_scan = max(100, len(file_lines))
                for d in range(1, max_scan + 1):
                    if target_idx + d <= len(file_lines) and _matches(target_idx + d, fuzzy=False):
                        found_idx = target_idx + d
                        break
                    if target_idx - d >= 0 and _matches(target_idx - d, fuzzy=False):
                        found_idx = target_idx - d
                        break

            # 3. Fuzzy match (ignoring trailing whitespace)
            if found_idx is None:
                if _matches(target_idx, fuzzy=True):
                    found_idx = target_idx
                else:
                    max_scan = max(100, len(file_lines))
                    for d in range(1, max_scan + 1):
                        if target_idx + d <= len(file_lines) and _matches(target_idx + d, fuzzy=True):
                            found_idx = target_idx + d
                            break
                        if target_idx - d >= 0 and _matches(target_idx - d, fuzzy=True):
                            found_idx = target_idx - d
                            break

            # 4. Search entire file if still not found
            if found_idx is None:
                exact_hits = [idx for idx in range(len(file_lines) - len(old_hunk_lines) + 1)
                              if _matches(idx, fuzzy=False)]
                if len(exact_hits) == 1:
                    found_idx = exact_hits[0]
                elif not exact_hits:
                    fuzzy_hits = [idx for idx in range(len(file_lines) - len(old_hunk_lines) + 1)
                                  if _matches(idx, fuzzy=True)]
                    if len(fuzzy_hits) == 1:
                        found_idx = fuzzy_hits[0]

            if found_idx is None:
                preview = "\n".join(f"  {l}" for l in old_hunk_lines[:6])
                more = f"\n  ... ({len(old_hunk_lines) - 6} more lines)" if len(old_hunk_lines) > 6 else ""
                name = f"'{path_for_err}'" if path_for_err else "file"
                raise ToolError(
                    f"Hunk @@ -{hunk.old_start},{hunk.old_lines} +{hunk.new_start},{hunk.new_lines} @@ "
                    f"failed to match context in {name}.\nExpected lines:\n{preview}{more}"
                )

        file_lines[found_idx: found_idx + len(old_hunk_lines)] = new_hunk_lines
        line_offset = found_idx + len(new_hunk_lines) - (hunk.old_start - 1 + len(old_hunk_lines))
        applied_count += 1

    nl = "\r\n" if is_crlf else "\n"
    new_text = nl.join(file_lines)
    if has_trailing_newline and file_lines:
        new_text += nl
    return new_text, applied_count


def apply_unified_patch_with_count(original_text: str, patch_str: str, path_for_err: str = "") -> Tuple[str, int]:
    return apply_selected_hunks(original_text, patch_str, selected_hunk_indices=None, path_for_err=path_for_err)


def apply_unified_patch(original_text: str, patch_str: str, path_for_err: str = "") -> str:
    updated, _ = apply_selected_hunks(original_text, patch_str, selected_hunk_indices=None, path_for_err=path_for_err)
    return updated


class ApplyPatch(Tool):
    name = "apply_patch"
    description = (
        "Apply a unified diff patch to a file. Supports standard unified diff format "
        "(with @@ -start,len +start,len @@ hunk headers). Context lines (' ') verify position. "
        "More robust for multi-site or non-contiguous edits than single edit_file calls."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to the workspace root"},
            "patch": {"type": "string", "description": "Unified diff patch content including hunk headers"},
            "selected_hunks": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Optional list of 0-based hunk indices to selectively apply. Omit to apply all hunks.",
            },
            "skip_syntax_check": {"type": "boolean", "description": "Skip pre-commit syntax check (default false)"},
        },
        "required": ["path", "patch"],
    }
    kind = "edit"

    def _apply(self, args, ctx):
        p = ctx.resolve(args["path"], write=True)
        ctx.check_secret(p)
        if p.is_dir():
            raise ToolError(f"'{args['path']}' is a directory")
        if not p.is_file():
            raise ToolError(f"File not found: {args['path']}. Use write_file to create it.")
        text = _read_text_strict(p)
        patch = args.get("patch", "")
        if not patch or not patch.strip():
            raise ToolError("patch is empty")
        selected = args.get("selected_hunks")
        updated, hunk_count = apply_selected_hunks(text, patch, selected_hunk_indices=selected, path_for_err=ctx.rel(p))
        return p, text, updated, hunk_count

    def precheck(self, args, ctx):
        p, _, _, _ = self._apply(args, ctx)
        _require_fresh_read(p, ctx, "patch")

    def preview(self, args, ctx):
        try:
            p, text, updated, count = self._apply(args, ctx)
        except ToolError as e:
            return f"(patch will fail: {e})"
        diff = unified_diff(text, updated, ctx.rel(p))
        if args.get("selected_hunks") is not None:
            return f"# Staged {count} selected hunk(s)\n" + diff
        return diff

    def run(self, args, ctx):
        p, text, updated, count = self._apply(args, ctx)
        _require_fresh_read(p, ctx, "patch")
        if text == updated:
            return f"No changes applied to {ctx.rel(p)} (no hunks selected or patch resulted in identical content)"
        if not args.get("skip_syntax_check"):
            err = validate_code_syntax(p, updated)
            if err:
                orig_err = validate_code_syntax(p, text)
                if not orig_err:
                    raise ToolError(f"Pre-commit syntax validation failed for {ctx.rel(p)}: {err}. "
                                    "Patch was rejected to prevent corrupting the file.")
        ctx.snapshot(p)
        _write_text(p, updated)
        ctx.read_mtimes[str(p)] = p.stat().st_mtime
        return f"Applied {count} hunk{'s' if count != 1 else ''} to {ctx.rel(p)} ({len(updated.splitlines())} lines)"


class Shell(Tool):
    name = "shell"
    description = ("Run a shell command in the workspace root and return stdout, stderr and exit code. "
                   "Use it for builds, tests, git and package managers. Commands are non-interactive "
                   "(stdin is closed) and time out. Do not use it to read or edit files; use the file tools. "
                   "Set is_background=True for servers, watchers, and ongoing processes.")
    parameters = {"type": "object", "properties": {
        "command": {"type": "string", "description": "Command to run"},
        "timeout": {"type": "integer", "description": "Timeout in seconds (default 120, max 600)"},
        "description": {"type": "string", "description": "5-10 word summary of what the command does"},
        "is_background": {
            "type": "boolean",
            "description": "Run command asynchronously in the background. Use for long-running servers, test watchers, or tasks that need ongoing monitoring or stdin interaction."
        },
        "wait_ms": {
            "type": "integer",
            "description": "Milliseconds to wait after starting the background process before returning initial output (default 1000)."
        },
        "unsandboxed": {"type": "boolean",
                        "description": "Run outside the command sandbox (the user is asked first). Only when the "
                                       "command must write outside the workspace, or failed because the sandbox "
                                       "blocked it."},
    }, "required": ["command"]}
    kind = "exec"

    def preview(self, args, ctx):
        from hubble.sandbox import effective_mode
        cmd = args.get("command", "")
        suffix = "\n(background task: managed via manage_task)" if args.get("is_background") else ""
        if args.get("unsandboxed") and effective_mode(ctx.sandbox) != "off":
            return f"{cmd}{suffix}\n(outside the sandbox: full access to this machine)"
        return f"{cmd}{suffix}" if suffix else cmd

    def run(self, args, ctx):
        from hubble.sandbox import effective_mode
        cmd = args["command"]
        timeout = max(1, min(int(args.get("timeout") or ctx.shell_timeout), 600))
        if args.get("unsandboxed") and not ctx.allow_unsandboxed:
            raise ToolError("Running outside the sandbox is disabled here. Run the command inside the "
                            "sandbox, or do the work without it.")
        is_bg = bool(args.get("is_background"))
        wait_ms = int(args.get("wait_ms") or 1000)
        mode = "off" if args.get("unsandboxed") else effective_mode(ctx.sandbox)
        if is_bg:
            if mode == "docker":
                raise ToolError("Background tasks are not currently supported inside Docker sandbox mode.")
            return self._run_background(cmd, wait_ms, ctx, native=mode == "native")
        if mode == "docker":
            return self._run_docker(cmd, timeout, ctx)
        return self._run_host(cmd, timeout, ctx, native=mode == "native")

    def _run_background(self, cmd: str, wait_ms: int, ctx: ToolContext, native: bool = False) -> str:
        argv = list(ctx.shell_argv)
        if "powershell" in argv[0].lower() or "pwsh" in argv[0].lower():
            stripped = cmd.strip()
            if (stripped.startswith(('"', "'")) and not stripped.startswith("&")
                    and (" " in stripped or "\t" in stripped)):
                cmd = "& " + stripped
            cmd = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd
        if native:
            from hubble.sandbox import SandboxUnavailable, wrap
            try:
                full = wrap(argv, cmd, ctx.root, [*ctx.extra_dirs, *ctx.sandbox_writable], ctx.sandbox_network)
            except SandboxUnavailable as e:
                raise ToolError(f"shell_sandbox is 'native' but no sandbox is available: {e}. "
                                "Set shell_sandbox to \"auto\" or \"off\".")
            argv, cmd = full[:-1], full[-1]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        win_job = None
        if sys.platform == "win32":
            from hubble.sandbox import WindowsJobSandbox, parse_memory_limit_bytes
            mem_bytes = parse_memory_limit_bytes(ctx.sandbox_memory)
            win_job = WindowsJobSandbox(memory_limit_bytes=mem_bytes)

        task = ctx.process_manager.start_task(
            command=cmd,
            argv=argv + [cmd],
            cwd=ctx.root,
            env=env,
            wait_ms=wait_ms,
            win_job=win_job,
        )

        status_info = ctx.process_manager.get_status(task.task_id, tail_lines=20)
        recent_out = status_info.get("recent_output", "").strip()
        lines = [
            f"Background task started: {task.task_id} (PID {task.proc.pid})",
            f"Command: {cmd}",
            f"Status: {task.status}",
            f"Log file: {task.log_file}",
        ]
        if recent_out:
            lines.append("\nInitial output:\n" + recent_out)
        lines.append(f"\nUse manage_task(action='status', task_id='{task.task_id}') to check output or manage_task(action='kill', task_id='{task.task_id}') to stop.")
        return "\n".join(lines)

    def _run_host(self, cmd: str, timeout: int, ctx: ToolContext, native: bool = False) -> str:
        argv = list(ctx.shell_argv)
        if "powershell" in argv[0].lower() or "pwsh" in argv[0].lower():
            stripped = cmd.strip()
            if (stripped.startswith(('"', "'")) and not stripped.startswith("&")
                    and (" " in stripped or "\t" in stripped)):
                cmd = "& " + stripped
            cmd = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd
        if native:
            from hubble.sandbox import SandboxUnavailable, wrap
            try:
                full = wrap(argv, cmd, ctx.root, [*ctx.extra_dirs, *ctx.sandbox_writable], ctx.sandbox_network)
            except SandboxUnavailable as e:
                raise ToolError(f"shell_sandbox is 'native' but no sandbox is available: {e}. "
                                "Set shell_sandbox to \"auto\" or \"off\".")
            argv, cmd = full[:-1], full[-1]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        # Own process group, so timeouts and Ctrl+C kill the whole tree. Otherwise grandchildren
        # (dev servers, watchers) keep the pipes open and the CLI hangs.
        group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                 else {"start_new_session": True})
        win_job = None
        if sys.platform == "win32":
            from hubble.sandbox import WindowsJobSandbox, parse_memory_limit_bytes
            mem_bytes = parse_memory_limit_bytes(ctx.sandbox_memory)
            win_job = WindowsJobSandbox(memory_limit_bytes=mem_bytes)

        try:
            proc = subprocess.Popen(argv + [cmd], cwd=ctx.root, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", env=env, **group)
            if win_job:
                win_job.assign_process(proc)
        except OSError as e:
            if win_job:
                win_job.close()
            raise ToolError(f"Could not start shell: {e}")
        try:
            stdout, stderr = communicate_interruptibly(proc, timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            if win_job:
                win_job.close()
            stdout, stderr = _drain(proc)
            raise ToolError(f"Command timed out after {timeout}s (process tree killed). "
                            "Long-running servers are not supported.\n" + truncate(stdout or "", 5000))
        except KeyboardInterrupt:
            _kill_tree(proc)
            if win_job:
                win_job.close()
            _drain(proc)
            raise
        finally:
            if win_job:
                win_job.close()
        result = _format_result(stdout, stderr, proc.returncode)
        if native and proc.returncode != 0:
            from hubble.sandbox import looks_blocked
            if looks_blocked(stderr or stdout or ""):
                result += ("\n\n[sandbox] This command ran in a sandbox that only allows writes inside the workspace"
                           + ("" if ctx.sandbox_network else " and blocks network access")
                           + ". If it failed because of that, retry it with unsandboxed: true (the user will be "
                           "asked).")
        return result

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
            stdout, stderr = communicate_interruptibly(proc, timeout)
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


class ManageTask(Tool):
    name = "manage_task"
    description = (
        "Manage background tasks and processes started with shell(is_background=True). "
        "Actions: 'list' (list running/recent background tasks), "
        "'status' (check task status, exit code, and recent output log), "
        "'send_input' (send input text to a running task's stdin), "
        "'kill' (terminate a running task and its process tree)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "status", "send_input", "kill"],
                "description": "Action to perform on background tasks",
            },
            "task_id": {
                "type": "string",
                "description": "The task ID (e.g. 'task-1'). Required for 'status', 'send_input', 'kill'.",
            },
            "input": {
                "type": "string",
                "description": "Input text to send to the process's standard input. Required for 'send_input'.",
            },
            "tail_lines": {
                "type": "integer",
                "description": "Number of recent log lines to retrieve (default 50). Optional for 'status'.",
            },
        },
        "required": ["action"],
    }
    kind = "exec"

    def target(self, args: Dict[str, Any]) -> str:
        return f"{args.get('action', '')} {args.get('task_id', '')}".strip()

    def preview(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        act = args.get("action", "")
        tid = args.get("task_id", "")
        if act == "kill":
            return f"Kill background task {tid}"
        if act == "send_input":
            return f"Send input to background task {tid}: {args.get('input', '')[:40]}"
        return f"Manage task {act} {tid}".strip()

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        action = str(args.get("action", "")).lower().strip()
        pm = ctx.process_manager

        if action == "list":
            tasks = pm.list_tasks()
            if not tasks:
                return "No background tasks currently recorded."
            lines = [f"Background tasks ({len(tasks)}):"]
            for t in tasks:
                rt = f"{t['runtime_seconds']}s" if t["runtime_seconds"] is not None else "-"
                ec = f"exit {t['exit_code']}" if t["exit_code"] is not None else "-"
                lines.append(f"  • {t['task_id']}: [{t['status']}] PID {t['pid']} (runtime: {rt}, {ec}) - {t['command'][:80]}")
            return "\n".join(lines)

        task_id = args.get("task_id", "").strip()
        if not task_id:
            raise ToolError(f"task_id is required for action '{action}'")

        if action == "status":
            tail_lines = max(1, min(int(args.get("tail_lines") or 50), 500))
            try:
                info = pm.get_status(task_id, tail_lines=tail_lines)
            except KeyError as e:
                raise ToolError(str(e))
            lines = [
                f"Task ID: {info['task_id']}",
                f"PID: {info['pid']}",
                f"Status: {info['status']}",
                f"Exit Code: {info['exit_code'] if info['exit_code'] is not None else 'running'}",
                f"Log File: {info['log_file']}",
            ]
            if info["recent_output"]:
                lines.append(f"\nRecent Output (last {tail_lines} lines):\n{info['recent_output']}")
            else:
                lines.append("\nRecent Output: (empty)")
            return "\n".join(lines)

        if action == "send_input":
            text = args.get("input")
            if text is None:
                raise ToolError("input is required for send_input")
            try:
                res = pm.send_input(task_id, text)
                return res
            except (KeyError, RuntimeError) as e:
                raise ToolError(str(e))

        if action == "kill":
            try:
                res = pm.kill_task(task_id)
                return res
            except (KeyError, RuntimeError) as e:
                raise ToolError(str(e))

        raise ToolError(f"Unknown action: '{action}'. Must be one of: list, status, send_input, kill.")


class SendMessage(Tool):
    name = "send_message"
    description = (
        "Send a message to another agent or subagent (e.g. 'parent', a subagent role like "
        "'code-reviewer', 'research', a task ID like 'task-1', or 'broadcast' to all active agents). "
        "Enables reactive inter-agent coordination without blocking or busy-polling."
    )
    kind = "read"
    parameters = {
        "type": "object",
        "properties": {
            "recipient": {
                "type": "string",
                "description": "Recipient agent ID (e.g. 'parent', subagent name/task ID, or 'broadcast')",
            },
            "message": {
                "type": "string",
                "description": "The message content to communicate with the recipient",
            },
        },
        "required": ["recipient", "message"],
    }

    def target(self, args: Dict[str, Any]) -> str:
        return args.get("recipient", "")

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        recipient = str(args.get("recipient", "")).strip()
        message = str(args.get("message", "")).strip()
        if not recipient:
            raise ToolError("recipient is required")
        if not message:
            raise ToolError("message cannot be empty")

        bus = getattr(ctx, "message_bus", None) or default_message_bus
        sender = getattr(ctx, "agent_id", "main")
        msg = bus.send(sender=sender, recipient=recipient, content=message)
        return f"Message delivered to '{recipient}' (id: {msg.id})."




def _format_result(stdout: str, stderr: str, returncode: int) -> str:
    parts = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append(f"[stderr]\n{stderr.rstrip()}")
    parts.append(f"[exit code {returncode}]")
    return truncate("\n".join(parts))


def communicate_interruptibly(proc: subprocess.Popen, timeout: float):
    """proc.communicate(timeout=...) in short slices. One long communicate() on Windows waits in
    a thread join that Ctrl+C cannot break into, so the key did nothing until the command ended.
    Raises subprocess.TimeoutExpired once `timeout` seconds have passed in total."""
    deadline = time.time() + timeout
    while True:
        try:
            return proc.communicate(timeout=min(0.25, max(0.01, deadline - time.time())))
        except subprocess.TimeoutExpired:
            if time.time() >= deadline:
                raise


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


class SymbolsTool(Tool):
    name = "symbols"
    description = (
        "Search codebase symbols (classes, functions, methods, types, interfaces) across the "
        "workspace using fast AST/symbol indexing, or outline a specific file. "
        "Use this before broad grepping to locate definitions directly."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Symbol name or substring to search for across the workspace"},
            "path": {"type": "string", "description": "Optional file path to return a symbol outline for that file"},
            "limit": {"type": "integer", "description": "Max symbols to return (default 30)"},
        },
    }
    kind = "read"

    def run(self, args: Dict[str, Any], ctx: ToolContext) -> str:
        from hubble.indexer import SymbolIndex
        idx = SymbolIndex(ctx.root)
        limit = max(1, min(int(args.get("limit") or 30), 100))

        if args.get("path"):
            p = ctx.resolve(args["path"])
            ctx.check_secret(p)
            rel = ctx.rel(p)
            syms = idx.outline(rel)
            if not syms:
                return f"No symbols found in {rel} (or file type not supported for symbol extraction)."
            lines = [f"[{rel} symbols ({len(syms)})]"]
            for s in syms[:limit]:
                lines.append(f"  line {s.line}: [{s.kind}] {s.name}{s.signature or ''}" + (f"  # {s.docstring}" if s.docstring else ""))
            if len(syms) > limit:
                lines.append(f"  ... {len(syms) - limit} more symbols")
            return "\n".join(lines)

        query = args.get("query", "").strip()
        if query:
            syms = idx.search(query, limit=limit)
            if not syms:
                return f"No symbols matching '{query}' found in workspace. Try grep to search raw text."
            lines = [f"[Found {len(syms)} symbol(s) matching '{query}']"]
            for s in syms:
                lines.append("  " + s.format_line())
            return "\n".join(lines)

        return f"[Workspace symbol outline]\n{idx.repo_map(max_symbols=limit)}"


def default_tools() -> List[Tool]:
    return [ReadFile(), WriteFile(), EditFile(), ApplyPatch(), Shell(), ManageTask(), SendMessage(), Grep(), Glob(), ListDir(), SymbolsTool(), TodoWrite()]


READ_ONLY_TOOL_NAMES = {"read_file", "grep", "glob", "list_dir", "symbols", "send_message"}


def run_tool(tool: Tool, args: Dict[str, Any], ctx: ToolContext) -> tuple:
    """Returns (output, is_error). Never raises for tool-level failures."""
    try:
        tool.validate(args)
        return tool.run(args, ctx), False
    except ToolError as e:
        return f"Error: {e}", True
    except OSError as e:
        return f"Error: {e}", True
