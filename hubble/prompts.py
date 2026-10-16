"""System prompt: persona, tool guidance, environment and project memory files."""

import functools
import platform
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

from hubble.settings import HOME_DIR

MEMORY_FILENAMES = ("HUBBLE.md", "AGENTS.md", "CLAUDE.md", "GEMINI.md")
MAX_MEMORY_CHARS = 20000

PERSONAS = {
    "code": "You are Hubble, an expert software engineer working directly in the user's repository.",
    "debug": ("You are Hubble in debug mode. Reproduce the problem, find the root cause before "
              "changing code, explain it in one or two sentences, apply the minimal fix and verify it."),
    "review": ("You are Hubble in review mode, a strict senior reviewer. Read the relevant code and "
               "report findings grouped as Bug, Security, Performance or Style, each with file:line and a "
               "concrete fix. Do not edit files unless asked."),
    "architect": ("You are Hubble in architect mode. Explore the codebase structure and produce designs, "
                  "trade-offs and phased implementation plans. Do not edit files unless asked."),
    "chat": "You are Hubble, a helpful technical assistant. Use tools only when they help answer.",
}

GUIDELINES = """\
# How to work
- Use the tools to inspect the workspace instead of guessing. Locate definitions with symbols or search text with grep/glob, then read_file the relevant parts.
- Always read_file a file before editing it. Prefer edit_file for targeted changes; use apply_patch with standard unified diffs for multi-hunk edits or patch diffs; use write_file for new files.
- In edit_file, old_string must match the file exactly, without the "N | " line-number prefix from read_file.
- Keep changes minimal and consistent with the surrounding code style. Do not add unrelated refactors.
- After changing code, verify it: run the tests, a build, or at least a syntax check with the shell tool.
- For tasks with three or more steps, track progress with todo_write.
- Do NOT use the task tool for routine questions, single-file edits, targeted fixes, or basic exploration. Always inspect, search, and edit files directly with symbols, grep, glob, read_file, edit_file, or apply_patch. Only use task if the user explicitly asks for delegated assistance or a team, or when the task requires exploring an extensive, unfamiliar multi-module codebase across dozens of files. If in doubt, do the work directly yourself.
- If you notice yourself repeating a multi-step procedure this user is likely to need again, offer to
  save it with write_skill so it is available as a skill next time (ask first; do not save one-off tasks).
- Use web_search for anything that may have changed since your training (library versions, APIs, error
  messages), then web_fetch the best result. Cite the URLs you used.
- If a tool call is denied, do not retry the same call; adjust your approach or ask the user.
- Never print or exfiltrate secrets (.env files, keys, tokens).
- Be concise. Lead with the answer or result. Reference code as path:line.
- When the task is done, reply with a short summary of what changed and how it was verified."""


@functools.lru_cache(maxsize=8)
def _git_summary(root: Path) -> str:
    try:
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, capture_output=True,
                                text=True, timeout=5, stdin=subprocess.DEVNULL)
        if branch.returncode != 0:
            return "not a git repository"
        status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True,
                                timeout=5, stdin=subprocess.DEVNULL)
        changed = [l for l in status.stdout.splitlines() if l.strip()]
        summary = f"branch {branch.stdout.strip()}, {len(changed)} changed file(s)"
        if changed:
            summary += "\n" + "\n".join(f"  {l}" for l in changed[:15])
        return summary
    except (OSError, subprocess.TimeoutExpired):
        return "git unavailable"


@functools.lru_cache(maxsize=8)
def workspace_overview(root: Path, limit: int = 80) -> str:
    """Shallow file tree (depth 2) so the model knows what exists before searching."""
    from hubble.tools import IGNORED_DIRS
    lines: List[str] = []

    def entries(d: Path):
        try:
            return sorted((e for e in d.iterdir() if e.name not in IGNORED_DIRS and not e.name.startswith(".")),
                          key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return []

    for e in entries(root):
        if len(lines) >= limit:
            lines.append("  ...")
            break
        lines.append(f"  {e.name}/" if e.is_dir() else f"  {e.name}")
        if e.is_dir():
            children = entries(e)
            for c in children[:12]:
                lines.append(f"    {c.name}/" if c.is_dir() else f"    {c.name}")
            if len(children) > 12:
                lines.append(f"    ... {len(children) - 12} more")
    return "\n".join(lines[: limit + 1]) or "  (empty)"


LENS_FILENAMES = ("LENS.md", "lens.md", ".hubble/lens.md", "STYLE.md", "style.md", "taste.md", "TASTE.md")
MAX_LENS_CHARS = 15000

LENS_TEMPLATE = """# Developer Lens & Design Language

## 🎨 UI & Design Language
- Styling: Modern, clean, and cohesive design system.
- Colors: Curated, harmonic color palettes (e.g. HSL tailored tokens, polished dark mode). Avoid generic primitive colors.
- Typography: Clean hierarchy using modern typography (e.g. Inter, Outfit, Roboto).
- Components: Accessible, responsive layout with subtle micro-interactions and smooth transitions.

## 💻 Coding Style & Conventions
- Pattern: Prefer concise, expressive code with early-return guard clauses.
- Typing: Strict type safety; avoid loose `any` or ambiguous data structures.
- Naming: Descriptive, consistent naming following ecosystem idioms (e.g. kebab-case files, camelCase variables).
- Errors: Explicit error handling with meaningful context; never swallow exceptions silently.

## 🏗️ Architecture & Boundaries
- Structure: Modular, focused files with single responsibilities.
- State: Unidirectional data flow and clear ownership.
- Dependencies: Prefer lightweight, well-maintained libraries over monolithic bloat.

## 🚫 Anti-Patterns (What NOT to do)
- Do not output generic statistical boilerplate or unstyled placeholder UIs.
- Do not make speculative refactors outside the requested scope.
- Do not bypass type checkers or lint rules with blanket disable comments.
"""

TASTE_TEMPLATE = LENS_TEMPLATE


def find_lens_files(root: Path) -> List[Path]:
    """User-level lens (~/.hubble/LENS.md or ~/LENS.md), then workspace lens (LENS.md / .hubble/lens.md)."""
    found = []
    for p in (HOME_DIR / "LENS.md", HOME_DIR / "lens.md", HOME_DIR / ".hubble" / "lens.md",
              HOME_DIR / "STYLE.md", HOME_DIR / "style.md", HOME_DIR / "taste.md"):
        if p.is_file() and p not in found:
            found.append(p)
            break
    for name in LENS_FILENAMES:
        p = root / name
        if p.is_file() and p not in found:
            found.append(p)
            break
    return found


find_taste_files = find_lens_files


def load_lens(root: Path) -> List[Tuple[Path, str]]:
    out, budget = [], MAX_LENS_CHARS
    for p in find_lens_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text and budget > 0:
            out.append((p, text[:budget]))
            budget -= len(text)
    return out


load_taste = load_lens


def find_memory_files(root: Path) -> List[Path]:
    """User-level memory, then files from the filesystem root down to the workspace."""
    found = [p for p in (HOME_DIR / "HUBBLE.md",) if p.is_file()]
    chain = [root, *root.parents]
    stop = next((i for i, d in enumerate(chain) if (d / ".git").exists()), None)
    dirs = chain[: stop + 1] if stop is not None else [root]
    for d in reversed(dirs):
        for name in MEMORY_FILENAMES:
            p = d / name
            if p.is_file():
                found.append(p)
                break  # one memory file per directory; HUBBLE.md takes priority
    return found


def load_memory(root: Path) -> List[Tuple[Path, str]]:
    out, budget = [], MAX_MEMORY_CHARS
    for p in find_memory_files(root):
        try:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text and budget > 0:
            out.append((p, text[:budget]))
            budget -= len(text)
    return out


def build_system_prompt(root: Path, persona: str, shell: str, model: str,
                        pinned: dict, memory: List[Tuple[Path, str]], mode: str,
                        skills_block: str = "",
                        lens: Optional[List[Tuple[Path, str]]] = None,
                        taste: Optional[List[Tuple[Path, str]]] = None) -> str:
    parts = [PERSONAS.get(persona, PERSONAS["code"]), GUIDELINES]
    parts.append(
        "# Environment\n"
        f"- Workspace root: {root}\n"
        f"- OS: {platform.system()} {platform.release()}\n"
        f"- Shell used by the shell tool: {shell}"
        + (" (use PowerShell syntax; `&&` is not supported in 5.1, use `;`)" if "5.1" in shell else "")
        + f"\n- Date: {time.strftime('%Y-%m-%d')}\n"
        f"- Model: {model}\n"
        f"- Git (at session start): {_git_summary(root)}\n"
        f"- Workspace files (snapshot at session start, depth 2):\n{workspace_overview(root)}"
    )
    if mode == "plan":
        parts.append("# Plan mode\nYou are in read-only plan mode. Explore with read-only tools, then present "
                     "a concise step-by-step implementation plan. Do not try to edit files or run commands.")
    effective_lens = lens or taste
    if effective_lens:
        for path, text in effective_lens:
            parts.append(
                f"# Developer Lens & Design Language from {path}\n"
                "Observe and interpret the codebase through the developer's personal lens below. Strictly adhere to "
                "their custom coding style, aesthetic design language, architectural conventions, and anti-patterns. "
                "Do not revert to generic statistical boilerplate or defaults that conflict with these preferences:\n"
                f"{text}"
            )
    for path, text in memory:
        parts.append(f"# Project instructions from {path}\n{text}")
    if skills_block:
        parts.append(skills_block)
    if pinned:
        files = "\n\n".join(f"--- {name} ---\n{text}" for name, text in pinned.items())
        parts.append(f"# Files pinned by the user\n{files}")
    return "\n\n".join(parts)


INIT_PROMPT = """Analyze this codebase and create an HUBBLE.md file in the workspace root with instructions for future \
coding sessions. Include: a one-paragraph overview, how to install, build, run and test (exact commands), the \
high-level architecture and the role of the key files, and code style or conventions you observe. Keep it under \
80 lines, factual, and skip generic advice. If HUBBLE.md already exists, read it and improve it instead."""

COMPACT_PROMPT = """Summarize the conversation so far so the work can continue in a fresh context. Include:
1. The user's goals and explicit requests, in their words where it matters.
2. Key decisions and findings.
3. Files read or modified, with the important details of each change.
4. Errors hit and how they were fixed.
5. Pending tasks and the exact next step.
Be dense and specific; omit pleasantries."""
