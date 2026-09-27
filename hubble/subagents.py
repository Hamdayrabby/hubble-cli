"""Custom sub-agent definitions: named specialists the model can delegate to with the task tool.

An agent is a Markdown file `<name>.md` under `~/.hubble/agents/` (user, every project) or
`<project>/.hubble/agents/` (project only; wins on a name clash). Frontmatter:

    ---
    name: test-writer
    description: Writes focused pytest tests for a module. Use after adding a feature.
    tools: read_file, grep, glob, write_file, edit_file, shell   # optional; default: capability's set
    model: codestral-latest                                       # optional; default: caller's model
    capability: edit                                              # optional; read_only | edit
    ---
    System prompt for the agent: its role, rules, and how to report back.

Only name and description go into the task tool's description, so many agents cost little.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from hubble.settings import HOME_DIR
from hubble.skills import NAME_RX, _parse

EDIT_TOOLS = {"write_file", "edit_file", "shell"}


@dataclass
class AgentDef:
    name: str
    description: str
    prompt: str
    path: Path
    scope: str                       # "user" | "project" | "plugin:<name>"
    tools: Optional[List[str]] = None
    model: str = ""
    capability: str = "read_only"    # read_only | edit
    extra: dict = field(default_factory=dict)


def agent_dirs(root: Path) -> List[tuple]:
    from hubble.plugins import plugin_dirs
    return ([(HOME_DIR / "agents", "user")] + [(d / "agents", f"plugin:{n}") for n, d in plugin_dirs(root)]
            + [(root / ".hubble" / "agents", "project")])


def discover_agents(root: Path) -> List[AgentDef]:
    found = {}
    for base, scope in agent_dirs(root):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.md")):
            try:
                meta, body = _parse(path)
            except OSError:
                continue
            name = (meta.get("name") or path.stem).lower()
            if not NAME_RX.match(name):
                continue
            tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()] or None
            cap = meta.get("capability", "").replace("-", "_").lower()
            if cap not in ("read_only", "edit"):
                cap = "edit" if tools and EDIT_TOOLS & set(tools) else "read_only"
            found[name] = AgentDef(name=name, description=meta.get("description") or "(no description)",
                                   prompt=body.strip(), path=path, scope=scope, tools=tools,
                                   model=meta.get("model", ""), capability=cap)
    return sorted(found.values(), key=lambda a: a.name)


TEMPLATE = """---
name: {name}
description: What this agent is for and WHEN to use it (the main agent picks agents by this line).
# tools: read_file, grep, glob, list_dir      # optional: restrict its tools
# model: some-model-id                        # optional: run it on a different model
capability: read_only                         # read_only or edit
---

You are a specialist sub-agent. Describe its role, the rules it must follow, and exactly what
its final report should contain.
"""
