"""Custom sub-agent definitions: named specialists the model can delegate to with the task tool.

An agent is a Markdown file `<name>.md` under `~/.hubble/agents/` (user, every project) or
`<project>/.hubble/agents/` (project only; wins on a name clash). Frontmatter:

    ---
    name: test-writer
    description: Writes focused pytest tests for a module. Use after adding a feature.
    tools: read_file, grep, glob, write_file, edit_file, shell   # optional; default: capability's set
    model: codestral-latest                                       # optional; default: caller's model
    capability: edit                                              # optional; read_only | edit
    color: yellow                                                 # optional; badge color
    fork_context: false                                           # optional; inherit parent history
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
    color: str = ""                  # e.g. 'cyan', 'magenta', 'green', 'yellow', 'blue', 'red', 'purple', 'orange'
    fork_context: bool = False       # whether this agent forks parent conversation history
    extra: dict = field(default_factory=dict)


BUILTIN_AGENTS: dict = {
    "research": AgentDef(
        name="research",
        description="Fast, read-only specialist for broad codebase exploration, documentation lookups, and web search.",
        prompt=(
            "You are a research specialist sub-agent. Investigate the codebase, find all relevant files, "
            "trace execution paths, look up documentation, and synthesize clear, factual findings. "
            "Finish with a structured report citing exact file paths and line numbers."
        ),
        path=Path("builtin/research.md"),
        scope="builtin",
        tools=["read_file", "grep", "glob", "list_dir", "symbols", "web_search", "web_fetch"],
        capability="read_only",
        color="cyan",
    ),
    "self": AgentDef(
        name="self",
        description="Clones parent capabilities and tools to offload a complex, focused task into a clean conversation context.",
        prompt=(
            "You are an autonomous execution sub-agent with full parent capabilities. "
            "Implement the assigned task thoroughly in the workspace, verify your changes by running tests or builds, "
            "and provide a concise report of what was changed and verified."
        ),
        path=Path("builtin/self.md"),
        scope="builtin",
        capability="edit",
        color="magenta",
    ),
    "code-reviewer": AgentDef(
        name="code-reviewer",
        description="Analyzes code changes, git diffs, security vulnerabilities, edge cases, and code quality.",
        prompt=(
            "You are a code review specialist sub-agent. Inspect the targeted code or diffs for correctness, "
            "security flaws, edge cases, performance bottlenecks, and style conformance. "
            "Provide actionable feedback citing exact file paths and lines."
        ),
        path=Path("builtin/code-reviewer.md"),
        scope="builtin",
        tools=["read_file", "grep", "glob", "list_dir", "symbols"],
        capability="read_only",
        color="green",
    ),
    "test-writer": AgentDef(
        name="test-writer",
        description="Writes focused unit tests, edge-case tests, and test suites for newly implemented features or bug fixes.",
        prompt=(
            "You are a test-writing specialist sub-agent. Analyze the code, identify edge cases, write comprehensive "
            "unit tests using pytest/unittest, run the tests via shell, and ensure all tests pass before completing your report."
        ),
        path=Path("builtin/test-writer.md"),
        scope="builtin",
        tools=["read_file", "write_file", "edit_file", "apply_patch", "shell", "grep", "glob", "list_dir"],
        capability="edit",
        color="yellow",
    ),
}


def agent_dirs(root: Path) -> List[tuple]:
    from hubble.plugins import plugin_dirs
    return ([(HOME_DIR / "agents", "user")] + [(d / "agents", f"plugin:{n}") for n, d in plugin_dirs(root)]
            + [(root / ".hubble" / "agents", "project")])


def save_agent_definition(root: Path, name: str, description: str, prompt: str,
                          capability: str = "read_only", tools: Optional[List[str]] = None,
                          model: str = "", scope: str = "project",
                          color: str = "", fork_context: bool = False) -> AgentDef:
    """Save an agent markdown definition to <project>/.hubble/agents/ or ~/.hubble/agents/."""
    target_dir = (HOME_DIR / "agents") if scope == "user" else (root / ".hubble" / "agents")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{name}.md"

    frontmatter = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"capability: {capability}",
    ]
    if color:
        frontmatter.append(f"color: {color}")
    if fork_context:
        frontmatter.append("fork_context: true")
    if tools:
        frontmatter.append(f"tools: {', '.join(tools)}")
    if model:
        frontmatter.append(f"model: {model}")
    frontmatter.append("---")
    frontmatter.append("")
    frontmatter.append(prompt.strip())
    frontmatter.append("")

    target_path.write_text("\n".join(frontmatter), encoding="utf-8")
    return AgentDef(
        name=name,
        description=description,
        prompt=prompt.strip(),
        path=target_path,
        scope=scope,
        tools=tools,
        model=model,
        capability=capability,
        color=color,
        fork_context=fork_context,
    )


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
            color = meta.get("color", "")
            raw_fork = meta.get("fork_context", False)
            if isinstance(raw_fork, str):
                fork_ctx = raw_fork.lower() in ("true", "1", "yes", "y")
            else:
                fork_ctx = bool(raw_fork)
            found[name] = AgentDef(name=name, description=meta.get("description") or "(no description)",
                                   prompt=body.strip(), path=path, scope=scope, tools=tools,
                                   model=meta.get("model", ""), capability=cap,
                                   color=color, fork_context=fork_ctx)
    return sorted(found.values(), key=lambda a: a.name)


TEMPLATE = """---
name: {name}
description: What this agent is for and WHEN to use it (the main agent picks agents by this line).
# tools: read_file, grep, glob, list_dir      # optional: restrict its tools
# model: some-model-id                        # optional: run it on a different model
# color: cyan                                 # optional: cyan, magenta, green, yellow, blue, red, purple, orange
# fork_context: false                         # optional: set true to fork parent conversation history
capability: read_only                         # read_only or edit
---

You are a specialist sub-agent. Describe its role, the rules it must follow, and exactly what
its final report should contain.
"""
