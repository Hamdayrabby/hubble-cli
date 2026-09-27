"""Permission modes and allow/deny rules.

Rule syntax: `tool` or `tool(pattern)`, e.g. `shell(git status*)`, `edit_file(src/*)`,
`read_file(secrets/*)`. Patterns use fnmatch against the tool's target (a workspace-relative
path, or a command). Deny rules win, then allow rules, then the mode default.
"""

import fnmatch
import re
from typing import List, Optional, Tuple

MODES = ["default", "accept-edits", "plan", "yolo"]
CYCLE_MODES = ["default", "accept-edits", "plan"]  # yolo only by explicit choice
MODE_HELP = {
    "default": "ask before edits and shell commands",
    "accept-edits": "auto-approve file edits, ask before shell commands",
    "plan": "read-only: explore and propose a plan, no edits or commands",
    "yolo": "auto-approve everything except deny rules",
}
ALIASES = {"bash": "shell", "run": "shell", "edit": "edit_file", "write": "write_file",
           "read": "read_file", "ls": "list_dir", "find": "glob", "todo": "todo_write"}
# A command may ride on a pattern allow rule only if it is one plain program invocation:
# no separators, subexpressions, redirects, variables or quotes in any shell (cmd, PowerShell, bash).
SAFE_COMMAND = re.compile(r"^[\w ./:=+,@\-]+$")
# Split points used to apply deny rules to every part of a chained command.
SEPARATORS = re.compile(r"&&|\|\||[;|&\r\n()`{}]|\$\(")
MULTI_WORD_LAUNCHERS = {"python", "python3", "py", "node", "npx", "uv", "uvx", "poetry", "pipx", "dotnet", "go",
                        "cargo", "npm", "pnpm", "yarn", "git", "docker", "kubectl", "pip", "make"}

RULE_RX = re.compile(r"^\s*([\w-]+)\s*(?:\((.*)\))?\s*$")


def parse_rule(rule: str) -> Optional[Tuple[str, Optional[str]]]:
    m = RULE_RX.match(rule)
    if not m:
        return None
    name = m.group(1).lower()
    return ALIASES.get(name, name), m.group(2)


def rule_matches(rule: str, tool: str, target: str) -> bool:
    parsed = parse_rule(rule)
    if not parsed:
        return False
    name, pattern = parsed
    if name not in (tool, "*"):
        return False
    if pattern is None or pattern == "*":
        return True
    norm = target.replace("\\", "/").strip()
    return fnmatch.fnmatchcase(norm, pattern) or fnmatch.fnmatchcase(norm.lower(), pattern.lower())


def command_prefix(command: str) -> str:
    """`python -m pytest -q tests` -> `python -m pytest`; `git status -s` -> `git status`; `pytest -q` -> `pytest`."""
    words = command.strip().split()
    if not words:
        return ""
    prefix = [words[0]]
    exe = words[0].lower().removesuffix(".exe")
    if exe in MULTI_WORD_LAUNCHERS:
        rest = words[1:]
        if rest[:1] == ["-m"] and len(rest) > 1:
            prefix += rest[:2]
        elif rest and re.match(r"^[a-z][\w:-]*$", rest[0]):
            prefix.append(rest[0])
            if exe in ("npm", "pnpm", "yarn") and rest[0] == "run" and len(rest) > 1:
                prefix.append(rest[1])
    return " ".join(prefix)


class Permissions:
    def __init__(self, mode: str = "default", allow: Optional[List[str]] = None,
                 deny: Optional[List[str]] = None):
        self.mode = mode if mode in MODES else "default"
        self.allow = list(allow or [])
        self.deny = list(deny or [])
        self.session_allow: List[str] = []

    def cycle_mode(self) -> str:
        idx = CYCLE_MODES.index(self.mode) if self.mode in CYCLE_MODES else -1
        self.mode = CYCLE_MODES[(idx + 1) % len(CYCLE_MODES)]
        return self.mode

    def _denied(self, tool: str, kind: str, target: str) -> Optional[str]:
        targets = [target]
        if kind == "exec":
            targets += [part.strip() for part in SEPARATORS.split(target) if part.strip()]
        for rule in self.deny:
            if any(rule_matches(rule, tool, t) for t in targets):
                return rule
        return None

    def check(self, tool: str, kind: str, target: str) -> Tuple[str, str]:
        """Returns (decision, reason) where decision is allow | ask | deny."""
        rule = self._denied(tool, kind, target)
        if rule:
            return "deny", f"blocked by deny rule {rule}"
        if kind == "read":
            return "allow", ""
        if self.mode == "plan" and kind != "web":  # fetching docs is fine while planning
            return "deny", ("plan mode is read-only. Finish exploring, then present your plan; "
                            "the user will switch modes to implement it")
        plain = kind != "exec" or bool(SAFE_COMMAND.match(target.strip()))
        for rule in self.allow + self.session_allow:
            if rule_matches(rule, tool, target):
                parsed = parse_rule(rule)
                if not plain and parsed and parsed[1] not in (None, "*"):
                    continue
                return "allow", f"allowed by rule {rule}"
        if self.mode == "yolo":
            return "allow", "yolo mode"
        if kind == "edit" and self.mode == "accept-edits":
            return "allow", "accept-edits mode"
        return "ask", ""

    def _add(self, rule: str):
        if rule not in self.session_allow:
            self.session_allow.append(rule)

    def always_rule(self, tool: str, kind: str, target: str) -> str:
        """Session rule(s) added when the user answers 'always'. Returns a description."""
        if kind == "exec":
            prefix = command_prefix(target)
            if not prefix or not SAFE_COMMAND.match(target.strip()):
                self._add(f"{tool}({target.strip()})")
                return f"{tool}({target.strip()})"
            self._add(f"{tool}({prefix})")
            self._add(f"{tool}({prefix} *)")
            return f"{tool}({prefix} *)"
        if kind == "web":
            self._add(f"{tool}({target})")
            return f"{tool}({target})"
        if kind == "edit":
            self._add("edit_file")
            self._add("write_file")
            return "edit_file, write_file"
        self._add(tool)
        return tool
