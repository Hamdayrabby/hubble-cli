"""Skills: reusable instructions the model can call itself, or the user can run with /<name>.

A skill is `<name>/SKILL.md` (with resources alongside it) or a plain `<name>.md`, under
`~/.hubble/skills/` (user, every project) or `<project>/.hubble/skills/` (project only).
It needs `name` and `description` frontmatter; the description is what tells the model,
and the user, when to reach for it — only the description is loaded into the system prompt,
and the full body loads on demand, so adding skills does not bloat every request.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from hubble.settings import HOME_DIR
from hubble.tools import Tool, ToolContext, ToolError, truncate

NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
FRONTMATTER_RX = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

TEMPLATE = """---
name: {name}
description: One sentence saying what this does and, importantly, WHEN to use it (the model \
and /help match on this). Example: "Run the project's test suite and summarize failures. Use \
when asked to test, check or verify the code works."
---

# {title}

Step-by-step instructions for the model to follow when this skill is invoked. Be concrete:
name the exact commands, files or conventions this project uses.
"""


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    scope: str  # "user" | "project"
    args_hint: str = ""
    _body: Optional[str] = field(default=None, repr=False)

    def body(self) -> str:
        if self._body is None:
            try:
                self._body = _parse(self.path)[1]
            except OSError as e:
                self._body = f"(could not read {self.path}: {e})"
        return self._body


def _parse(path: Path) -> tuple:
    """Returns (frontmatter dict, body). A file with no frontmatter is treated as pure body."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RX.match(text)
    if not m:
        return {}, text
    meta: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, val = line.partition(":")
            meta[key.strip().lower()] = val.strip().strip("'\"")
    return meta, m.group(2)


def _skill_dirs(root: Path) -> List[tuple]:
    return [(HOME_DIR / "skills", "user"), (root / ".hubble" / "skills", "project")]


def discover_skills(root: Path) -> List[Skill]:
    """Project skills come after user skills and win on a name clash (more specific wins)."""
    found: Dict[str, Skill] = {}
    for base, scope in _skill_dirs(root):
        if not base.is_dir():
            continue
        candidates = sorted(base.glob("*/SKILL.md")) + sorted(base.glob("*.md"))
        for path in candidates:
            name = (path.parent.name if path.name == "SKILL.md" else path.stem).lower()
            if not NAME_RX.match(name):
                continue
            try:
                meta, body = _parse(path)
            except OSError:
                continue
            description = meta.get("description") or next(
                (l.strip("# ").strip() for l in body.splitlines() if l.strip()), "") or "(no description)"
            found[name] = Skill(name=meta.get("name", name).lower() or name, description=description,
                                path=path, scope=scope, args_hint=meta.get("argument-hint", ""))
    return sorted(found.values(), key=lambda s: s.name)


def skills_prompt_block(skills: List[Skill]) -> str:
    """Name + description only (progressive disclosure); call the skill tool for the full body."""
    if not skills:
        return ""
    lines = [f'- {s.name}: {s.description}' for s in skills]
    return ("# Skills\nReusable instructions for recurring tasks in this project. If one clearly matches "
           "what the user asked, call the skill tool to load its full instructions before proceeding. "
           "Do not call it speculatively; only when its description matches.\n" + "\n".join(lines))


class SkillTool(Tool):
    name = "skill"
    description = ("Load the full instructions for a named skill (see the Skills section of your system "
                   "prompt for names and descriptions). Call this before following a skill's steps.")
    parameters = {"type": "object", "properties": {
        "name": {"type": "string", "description": "Skill name, exactly as listed"},
    }, "required": ["name"]}
    kind = "read"

    def __init__(self, skills: List[Skill]):
        self.by_name = {s.name: s for s in skills}

    def target(self, args):
        return args.get("name", "")

    def run(self, args, ctx):
        skill = self.by_name.get(str(args["name"]).lower())
        if not skill:
            available = ", ".join(sorted(self.by_name)) or "(none)"
            raise ToolError(f"no skill named '{args['name']}'. Available: {available}")
        return truncate(skill.body(), 40000)


class WriteSkillTool(Tool):
    name = "write_skill"
    description = ("Save a reusable skill so future sessions in this project can call it by name. Use this "
                   "when you notice a multi-step procedure the user is likely to repeat (a release "
                   "checklist, a debugging recipe, project-specific conventions) — not for one-off tasks. "
                   "Ask the user before writing one unless they asked you to save it.")
    parameters = {"type": "object", "properties": {
        "name": {"type": "string", "description": "lowercase-with-hyphens, becomes the /name command"},
        "description": {"type": "string",
                        "description": "One sentence: what it does and when to use it (for matching)"},
        "body": {"type": "string", "description": "Full instructions in Markdown"},
        "scope": {"type": "string", "enum": ["project", "user"],
                  "description": "project (default; this repo only) or user (every project)"},
    }, "required": ["name", "description", "body"]}
    kind = "edit"

    def _path(self, args, ctx: ToolContext) -> Path:
        name = str(args["name"]).lower().strip()
        if not NAME_RX.match(name):
            raise ToolError("name must be lowercase letters, digits, - or _ (max 41 chars)")
        base = HOME_DIR / "skills" if args.get("scope") == "user" else ctx.root / ".hubble" / "skills"
        return base / name / "SKILL.md"

    def _content(self, args) -> str:
        return f"---\nname: {args['name']}\ndescription: {args['description']}\n---\n\n{args['body'].strip()}\n"

    def target(self, args):
        return str(args.get("name", ""))

    def preview(self, args, ctx):
        try:
            path = self._path(args, ctx)
        except ToolError as e:
            return f"(will fail: {e})"
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        new = self._content(args)
        from hubble.tools import unified_diff
        return unified_diff(old, new, str(path)) or "(no changes)"

    def run(self, args, ctx: ToolContext):
        path = self._path(args, ctx)
        existed = path.is_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx.snapshot(path)
        path.write_text(self._content(args), encoding="utf-8")
        where = "every project" if args.get("scope") == "user" else "this project"
        return f"{'Updated' if existed else 'Saved'} skill '{args['name']}' for {where} at {path}. " \
               f"Available immediately as /{args['name']} or the skill tool."
