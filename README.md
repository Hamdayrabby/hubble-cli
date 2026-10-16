# Hubble

[![PyPI version](https://img.shields.io/pypi/v/hubble-cli.svg?style=flat-square&color=007acc)](https://pypi.org/project/hubble-cli/)
[![Python versions](https://img.shields.io/pypi/pyversions/hubble-cli.svg?style=flat-square)](https://pypi.org/project/hubble-cli/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg?style=flat-square)](https://opensource.org/licenses/MIT)

An agentic coding CLI engineered for autonomous, safe repository interaction. Hubble runs directly in your workspace to search, read, edit files, and execute shell commands using native tool calling under strict, user-configurable approval tiers.

It connects natively to Claude via Anthropic's Messages API (with prompt caching and extended thinking), any OpenAI-compatible gateway, or local models running through Ollama and LM Studio.

```
       ██╗  ██╗██╗   ██╗██████╗ ██████╗ ██╗     ███████╗
       ██║  ██║██║   ██║██╔══██╗██╔══██╗██║     ██╔════╝
       ███████║██║   ██║██████╔╝██████╔╝██║     █████╗  
       ██╔══██║██║   ██║██╔══██╗██╔══██╗██║     ██╔══╝  
       ██║  ██║╚██████╔╝██████╔╝██████╔╝███████╗███████╗
       ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝╚══════╝

 > hubble "refactor the auth session handler to use JWT tokens"
 
   • read_file       src/auth/session.py (lines 1-84)
   • grep            "SessionStore" in src/
   • edit_file       src/auth/session.py [diff preview]
                     Allow edit? [y/n/a] y
   • shell           pytest tests/test_auth.py -v
                     Allow command? [y/n/a] a
   ✔ All 14 tests passing. Changes committed to branch.
```

---

## Contents

- [Highlights](#highlights)
- [Installation](#installation)
- [Quickstart](#quickstart)
- [Execution Modes](#execution-modes)
- [Safety & Permissions](#safety--permissions)
- [Sandboxed Shell Execution](#sandboxed-shell-execution)
- [Tools](#tools)
- [Subagents & Model Teams](#subagents--model-teams)
- [Model Context Protocol (MCP)](#model-context-protocol-mcp)
- [Lifecycle Hooks](#lifecycle-hooks)
- [Custom Agents & Plugins](#custom-agents--plugins)
- [Git Worktrees](#git-worktrees)
- [GitHub Actions Integration](#github-actions-integration)
- [Model Routing & Telemetry](#model-routing--telemetry)
- [Configuration](#configuration)
- [CLI Reference](#cli-reference)
- [Interactive REPL Reference](#interactive-repl-reference)
- [Project Architecture](#project-architecture)
- [License](#license)

---

## Highlights

- **Repository-Native Agent Loop**: Reads, searches, and edits source trees with AST-aware line replacements, disk-freshness checks, and automated checkpoint snapshots for one-command rollback (`/undo`).
- **Defense-in-Depth Sandboxing**: OS-level command sandboxing via macOS Seatbelt (`sandbox-exec`), Linux `bubblewrap`, or ephemeral Docker containers with configurable CPU, memory, and network isolation.
- **Provider Agnostic**: Native integration with Anthropic Claude Messages API (prompt caching, thinking mode, native tool calling), any OpenAI-compatible API (Hubble Gateway, OpenAI, Groq, OpenRouter, Mistral, DeepSeek), and local LLMs (Ollama, LM Studio).
- **Intelligent Dynamic Routing**: Directs queries, lookups, and research subtasks to fast, low-cost models, escalating complex edits or failed operations to high-reasoning models automatically.
- **Subagent Delegation & Teams**: Dispatches focused research or implementation sub-agents in parallel across specialized models.
- **Git Worktree Isolation**: Spawns isolated scratch environments in `.hubble/worktrees/` on dedicated branches to keep worktree experiments completely detached from the primary branch.
- **Open Extensibility**: Full support for Model Context Protocol (MCP) servers (stdio, Streamable HTTP, HTTP+SSE, automatic OAuth 2.0 authentication) and deterministic lifecycle hooks.
- **Zero Secret Leakage**: Strict file confinement blocks access to `.env`, credentials, SSH keys, and certificates.

---

## Installation

### Recommended: pipx

Install Hubble in an isolated environment accessible globally:

```bash
pipx install hubble-cli
```

To install directly using `uv`:

```bash
uv tool install hubble-cli
```

### Standard pip

```bash
pip install --user hubble-cli
```

> **PATH Note:** If `hubble` is not immediately found after a `pip install`, add the script directory to your system path automatically:
> ```bash
> python -m hubble --add-to-path
> ```
> Restart your terminal afterward.

### Development Install

To run against a local git clone:

```bash
git clone https://github.com/Hamdayrabby/hubble-cli.git
cd hubble-cli
pip install -e .
```

### Upgrading

```bash
pipx upgrade hubble-cli
# or: pip install -U hubble-cli
```

Hubble inspects PyPI for updates at most once daily. Set `"update_check": false` in your configuration to disable checks.

---

## Quickstart

### 1. Launch in Any Repository

```bash
cd /path/to/your/project
hubble
```

On your initial run, if no API keys are detected, Hubble starts an interactive onboarding assistant to configure your credentials.

### 2. Manual Environment Configuration

Alternatively, supply credentials via `.env` in your project root or `~/.hubble/.env`:

```bash
# OpenAI-compatible gateway (default)
HUBBLE_API_KEY="sk-..."
HUBBLE_BASE_URL="https://api.example.com/v1"

# Or native Anthropic Claude
ANTHROPIC_API_KEY="sk-ant-..."
```

API credentials added through the interactive REPL are stored in your OS credential store (Windows Credential Manager, macOS Keychain, or Linux Secret Service).

---

## Execution Modes

Hubble operates both as an interactive REPL and as a non-interactive, scriptable command-line utility for automation and CI/CD pipelines.

### Interactive REPL

```bash
hubble                                   # Launch interactive session
hubble "explain the authentication flow" # Launch REPL with an initial prompt
hubble -c                                # Continue the latest session in this directory
hubble -r                                # Interactively choose from previous sessions
```

### Non-Interactive / Headless (`-p`)

Execute a task and return directly to the shell:

```bash
# Execute with specific approval policies and automated tool whitelists
hubble -p "fix the failing pytest suite" --permission-mode accept-edits --allow "shell(pytest*)"

# Pipe diffs or log files directly into Hubble
git diff | hubble -p "audit this diff for security regressions" --output-format json

# Stream JSON events for IDE integrations or orchestrators
hubble -p "run static analysis and report issues" --output-format stream-json
```

#### Stream-JSON Event Schema

When invoking `--output-format stream-json`, Hubble emits a newline-delimited JSON stream where each event includes a `type` discriminant:

- `init`: Session metadata, model configuration, and workspace paths.
- `text`: Delta stream of generated assistant response tokens.
- `assistant`: Consolidated model turn including token usage and requested tool invocations.
- `tool_use`: Start of a specific tool invocation with bound arguments.
- `tool_result`: Output payload returned by the executed tool.
- `todos`: Current state of the agent's task checklist.
- `subagent_start` / `subagent_end`: Lifecycle markers for delegated sub-agent tasks.
- `result`: Final turn summary and overall token accounting.

### Multimodal Vision Inputs

Attach screenshots, architecture diagrams, or UI mockups:

```bash
# In headless mode
hubble -p "identify the CSS alignment defect" --image screenshot.png

# In interactive REPL
# 1. Type: "Analyze this layout @screenshot.png"
# 2. Or press Alt+V to paste an image directly from your clipboard
```

---

## Safety & Permissions

Hubble enforces explicit boundaries to prevent unauthorized execution or unintended destructive modifications.

### Permission Modes

Toggle modes dynamically with `Shift+Tab` during a session, or set via `--permission-mode` / `/mode`:

| Mode | File Operations | Shell Execution | Primary Use Case |
|:---|:---|:---|:---|
| `default` | Prompts with unified diff | Prompts with command preview | Standard development |
| `accept-edits` | Auto-approved | Prompts with command preview | Fast iterative refactoring |
| `plan` | Read-only | Read-only | Architecture review and code audits |
| `yolo` | Auto-approved | Auto-approved (except `deny` rules) | Automated CI/CD pipelines |

### Interactive Approval Controls

When a file edit or shell command requires verification:
- `y`: Authorize the action once.
- `a`: Authorize the action for the entire session (all matching edits or commands matching the prefix, e.g., `pytest*`).
- `n`: Deny execution and provide immediate textual feedback to the agent.
- `Ctrl+C`: Abort the current model turn immediately.

### Allow & Deny Rules

Rules can be defined in `.hubble/settings.json` or supplied via CLI flags:

```bash
hubble --allow "shell(pytest*)" --allow "shell(git status*)" --deny "shell(git push*)"
```

- **Compound Command Protection**: Allow rules never bypass shell chains (`&&`, `;`, `|`, or redirects). For example, `shell(git status*)` will reject `git status && rm -rf /`.
- **Precedence**: Deny rules take precedence over allow rules.

### Defensive Boundaries

- **Workspace Confinement**: Operations outside the workspace root are refused unless explicitly whitelisted via `additional_dirs`.
- **Secret File Masking**: Critical credential files (`.env`, `*.pem`, `id_rsa`, `*.key`) cannot be read, matched by grep, or attached to context.
- **Freshness Verification**: Files must be read immediately before an edit or overwrite is permitted; if the on-disk state changes, the write is aborted until re-read.
- **Rollback Snapshotting**: Every file modification is versioned locally. Run `/undo` to revert file modifications made during the preceding turn.

---

## Sandboxed Shell Execution

### OS-Native Sandboxing (macOS & Linux)

On macOS and Linux, shell execution operates under an operating system sandbox by default (`"shell_sandbox": "auto"`):
- **macOS**: Sandboxed via macOS Seatbelt (`sandbox-exec`).
- **Linux**: Sandboxed via `bubblewrap` (`bwrap`).

The sandbox permits system-wide reads while restricting write operations strictly to the workspace root, temp directories, and directories defined in `sandbox_writable`.

If a command requires broader access (such as installing global packages), the model requests an unsandboxed retry. This triggers an explicit user confirmation prompt regardless of permission mode (unless running in `yolo`).

### Containerized Docker Sandbox (Cross-Platform)

For complete OS-agnostic isolation, execute all shell commands inside a dedicated Docker container:

```bash
# In the REPL
/sandbox docker
```

Configure Docker execution parameters in `settings.json`:

```json
{
  "shell_sandbox": "docker",
  "sandbox_image": "python:3.12-slim",
  "sandbox_memory": "1g",
  "sandbox_cpus": "2",
  "sandbox_network": true
}
```

- The workspace directory is mounted to `/workspace`.
- Containers are instantiated per command and discarded immediately upon completion.
- Set `"sandbox_network": false` to isolate the container from outbound network traffic.

---

## Tools

Hubble provides models with a curated set of deterministic tools:

| Tool | Description |
|:---|:---|
| `read_file` | Reads file content with line numbers, supporting `offset` and `limit` for large files. |
| `edit_file` | Applies deterministic string replacements; enforces single-match uniqueness and preserves CRLF formatting. |
| `apply_patch` | Applies unified diff patches (`@@ -start,len +start,len @@`) with fuzzy offset (±100 lines) and whitespace tolerance; ideal for multi-site edits. |
| `write_file` | Writes or overwrites workspace files (requires reading existing files first). |
| `shell` | Runs commands in workspace root (PowerShell on Windows, bash/sh on Unix) with timeout handling. |
| `symbols` | Fast AST & regex symbol indexer for Python, TS/JS, Go, and Rust; search symbol definitions or outline files before grepping. |
| `grep` | Fast regex search powered by `ripgrep` (falls back to Python regex if uninstalled). |
| `glob` | Discovers files using glob patterns, sorted by modification time. |
| `list_dir` | Lists directory trees and entry metadata. |
| `todo_write` | Maintains an interactive, visible task checklist in the console UI. |
| `task` | Dispatches sub-agents for isolated research (`read_only`) or implementation (`edit`). |
| `invoke_subagent` | Dispatches specialized sub-agents with pre-configured roles (`research`, `self`, `code-reviewer`, `test-writer`) and workspace isolation (`inherit`, `branch`, `share`). |
| `define_subagent` | Dynamically defines and registers new specialist subagents on the fly. |
| `web_search` | Performs web queries (DuckDuckGo by default; Brave or Tavily if API keys are set). |
| `web_fetch` | Converts remote web pages to clean readable text with domain-level consent prompts. |
| `mcp__*` | Dynamic tools exported by configured Model Context Protocol servers. |

---

## Subagents & Model Teams

For complex tasks requiring broad exploratory research or isolated code changes, Hubble can delegate work to specialized sub-agents via `invoke_subagent` or `task`.

### Pre-Configured Specialists

Hubble comes with out-of-the-box specialists that require zero configuration:

| Specialist | Role | Capability | Default Tools |
|:---|:---|:---|:---|
| `research` | Fast, read-only specialist for broad codebase exploration, documentation lookups, and web queries. | `read_only` | `read_file`, `grep`, `glob`, `list_dir`, `symbols`, `web_search`, `web_fetch` |
| `self` | Clones parent capabilities to offload a focused, complex task into a clean conversation context. | `edit` | All parent tools (edits, shell, test runs) |
| `code-reviewer` | Deep audit specialist for security vulnerabilities, edge cases, diffs, and code quality. | `read_only` | `read_file`, `grep`, `glob`, `list_dir`, `symbols` |
| `test-writer` | Writes focused unit and integration tests for new features, verifying them via test runners. | `edit` | `read_file`, `write_file`, `edit_file`, `apply_patch`, `shell`, `grep`, `glob`, `list_dir` |

### Triggering Policy & Gating

Uncontrolled subagent spawning wastes tokens and slows down execution. Hubble gives you explicit control over when subagents are permitted to spawn:

| Policy | Behavior | Best For |
|:---|:---|:---|
| `auto` *(default)* | Subagent tools (`task`, `invoke_subagent`) are available to the model, constrained by prompt guidelines instructing it to only delegate for massive multi-module codebases or explicit requests. | Standard development with strong models. |
| `explicit` | **Deterministic Gating:** Subagent tools are completely withheld from the active toolset *unless* the user's prompt explicitly mentions "agent", "subagent", or "delegate". | Everyday coding without unwanted subagent distractions. |
| `off` | Subagent tools are disabled completely across all turns. | Strict single-agent execution and maximum token conservation. |

Configure the policy via:
- **Interactive REPL**: `/subagent [auto|explicit|off]`
- **CLI Flag**: `hubble --subagent-policy explicit`
- **Settings**: `"subagent_policy": "explicit"` in `.hubble/settings.json`

### Dynamic Subagent Creation (`define_subagent`)

The model (or user) can dynamically define and persist new specialists on the fly:

```python
define_subagent(
    name="api-docs-generator",
    description="Analyzes FastAPI/Flask route handlers and generates OpenAPI specs",
    system_prompt="You inspect routes, schemas, and docstrings to produce accurate OpenAPI specifications.",
    capability="read_only",
    scope="project" # or "user"
)
```
Once saved, the specialist is immediately available across all subagent tools and sessions.

### Execution Isolation & Behavior

- **`inherit` / `none`**: Subagent runs directly in the parent workspace directory. Multiple `read_only` sub-agents triggered in a single turn execute in parallel.
- **`branch` / `share` / `worktree`**: Spins up the subagent inside a detached git worktree, committing changes to an isolated branch for review before merging.
- **Token Budgets**: Each subagent run can be constrained with an explicit token budget (e.g. `budget: 500000`) to prevent runaway context consumption.

### Model Teams

Assign specific models to distinct sub-agent roles:

```bash
/team add hubble:ministral-8b-latest fast, for broad code search and discovery
/team add groq:llama-3.3-70b-versatile quick second opinions and diff reviews
/team add anthropic:claude-opus-5 careful, for deep architectural refactoring
```

Hubble evaluates recent model performance telemetry from `/stats` and routes subtasks accordingly (e.g., dispatching search tasks to fast models, and complex refactors to high-reasoning models).

---

## Developer Lens & Design Language (`LENS.md`)

Avoid generic AI "boilerplate" or conflicting coding styles. True to Hubble's optics and deep-space telescope identity, Hubble views your repository through your personal **Developer Lens** (`LENS.md`) — enforcing your individual coding style, UI design language, aesthetic tokens, and architectural conventions across all code generation.

### How the Lens Works

Lens files are discovered and injected with top priority into the system prompt:
- **Global Lens**: `~/.hubble/LENS.md` (or `~/LENS.md`) applies your universal style across every repository you open.
- **Project Lens**: `<workspace>/LENS.md` (or `.hubble/lens.md`, `STYLE.md`) configures repo-specific design languages and conventions.

When both exist, Hubble merges them so the project lens can specialize or override global rules.

### Managing Your Lens (`/lens`)

- **/lens** *(or `/style`)*: Inspect currently active lens files and preferences.
- **/lens init**: Scaffolds a starter `LENS.md` with sections for:
  - 🎨 **UI & Design Language**: Color palettes (e.g. HSL tailored tokens), typography pairings, spacing, component libraries.
  - 💻 **Coding Style & Conventions**: Guard clauses, early returns, typing rules, naming idioms.
  - 🏗️ **Architecture & Boundaries**: Modular file layout, state flow, dependency rules.
  - 🚫 **Anti-Patterns**: Explicit constraints on what the AI must *never* do.
- **/lens add `<preference>`**: Quickly append a micro-decision or new aesthetic rule to `LENS.md`.

---

## Model Context Protocol (MCP)

Hubble natively supports the [Model Context Protocol](https://modelcontextprotocol.io/), exposing tools, resources, and prompt templates from external systems.

### MCP Configuration

Define servers in `.hubble/settings.json` or `~/.hubble/settings.json`:

```json
{
  "mcp_servers": {
    "filesystem": {
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/path/to/share"]
    },
    "git": {
      "command": ["uvx", "mcp-server-git"]
    },
    "linear": {
      "url": "https://mcp.linear.app/mcp"
    },
    "internal-service": {
      "url": "https://mcp.internal.net/sse",
      "transport": "sse",
      "headers": {
        "Authorization": "Bearer ${INTERNAL_MCP_TOKEN}"
      }
    }
  }
}
```

- **Environment Substitution**: Variables written as `${VAR}` in `url`, `headers`, and `env` are expanded from the environment.
- **OAuth 2.0 Integration**: Remote servers returning HTTP `401` are flagged for login. Run `/mcp login <server>` to initiate the browser-based authorization flow; tokens are stored in the OS credential store and refreshed automatically.
- **Prompts as Slash Commands**: Prompts exported by MCP servers are automatically exposed as REPL slash commands: `/mcp__<server>__<prompt>`.

---

## Lifecycle Hooks

Hooks allow you to execute deterministic host commands at defined phases in the agent loop. Use them to enforce compliance, lint edited files, or inject contextual data.

### Configuration

Add hooks to `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "shell", "command": "python .hubble/verify_command.py" }
    ],
    "PostToolUse": [
      { "matcher": "edit_file", "command": "python .hubble/lint_file.py" }
    ],
    "UserPromptSubmit": [
      { "command": "python .hubble/context_injector.py" }
    ],
    "Stop": [
      { "command": "python .hubble/require_tests.py" }
    ]
  }
}
```

### Event Lifecycle

| Hook Event | Trigger Point | Interception Capabilities |
|:---|:---|:---|
| `SessionStart` | Hubble initializes or resumes | Injects `additionalContext` into the session prompt. |
| `UserPromptSubmit` | User enters a prompt | Can block the prompt or append additional context. |
| `PreToolUse` | Immediately before a tool executes | Can block execution (stderr returned as tool error). |
| `PostToolUse` | Immediately after a tool completes | Can append text to the tool's returned output. |
| `Notification` | System enters an approval wait-state | Read-only observation (e.g., desktop notifications). |
| `PreCompact` | Context window reaches compaction threshold | Can cancel or defer compaction. |
| `SubagentStop` | A delegated subagent finishes | Can append text or warnings to the subagent's report. |
| `Stop` | Model signals turn completion | Can reject completion (`"decision": "block"`), forcing continuation. |
| `SessionEnd` | Session terminates | Cleanup and reporting. |

Hooks receive a JSON payload on `stdin` containing event details and communicate outcomes via exit codes or JSON on `stdout`:
- **Exit non-zero**: Blocks the action; `stderr` is passed back as the rejection rationale.
- **Output `{"decision": "block", "reason": "..."}`**: Blocks the action gracefully.
- **Output `{"additionalContext": "..."}`**: Appends data to prompts or tool outputs.

---

## Custom Agents & Plugins

### Custom Agents

Define persistent specialist personas by creating Markdown files in `.hubble/agents/` (project) or `~/.hubble/agents/` (global):

```markdown
---
name: test-engineer
description: Generates concise, thorough pytest suites. Use when implementing or fixing features.
tools: read_file, grep, glob, write_file, edit_file, shell
model: codestral-latest
capability: edit
---
You are an expert test engineer. Focus on boundary conditions, mock external dependencies,
and verify that test suites pass before concluding tasks.
```

Manage agents using the REPL:
- `/agents`: List installed agents and capabilities.
- `/agents new <name>`: Scaffold a new agent definition.

### Plugins

Bundle commands, skills, custom agents, hooks, and MCP servers into redistributable packages:

```
my-team-plugin/
  plugin.json         # Plugin manifest defining hooks and MCP servers
  commands/*.md       # Custom slash commands
  agents/*.md         # Custom agent personas
  skills/*/SKILL.md   # Domain-specific procedural skills
```

Install and manage plugins:
```bash
/plugin install https://github.com/org/hubble-devops-plugin.git
/plugin list
/plugin disable my-team-plugin
```

---

## Git Worktrees

Isolate hazardous refactors or run multiple parallel agent tasks without disturbing your active working tree:

```bash
# Launch Hubble inside a fresh worktree
hubble -w refactor-jwt

# Or manage worktrees interactively inside the REPL
/worktree new refactor-jwt      # Creates .hubble/worktrees/refactor-jwt on branch hubble/refactor-jwt
/worktree list                  # List active worktrees
/worktree switch <name>         # Switch workspace context
/worktree exit                  # Return to the primary repository checkout
/worktree remove refactor-jwt   # Delete worktree and prune metadata
```

Worktree directories are registered automatically in `.git/info/exclude` to prevent accidental inclusion in git commits.

---

## GitHub Actions Integration

Deploy Hubble into your CI/CD workflow to perform automated pull request reviews or address issues:

```bash
/install-github
```

This generates `.github/workflows/hubble.yml`. Once configured with a `HUBBLE_API_KEY` secret:
- **Pull Request Reviews**: Hubble reviews every incoming pull request and provides inline feedback.
- **Issue & PR Mentions**: Mention `@hubble <instruction>` in any issue or PR comment to dispatch an agent. On pull requests, Hubble can apply modifications and push commits directly back to the branch.
- **Security Scoping**: Restricts triggering privileges to repository owners, collaborators, and members.

---

## Model Routing & Telemetry

### Tier-Based Model Routing

Optimize latency and inference expenses using `/route`:

```bash
/route fast hubble:ministral-8b-latest    # Small, rapid model for lookups and search
/route strong anthropic:claude-opus-5    # High-reasoning model for complex refactors
/route auto                              # Automatically selects optimal fast model via /stats
```

- **Automatic Escalation**: If the fast model produces malformed tool calls, fails repeatedly, or encounters empty responses, the task escalates to the strong model mid-turn.

### Telemetry & Diagnostics (`/stats`)

Inspect local latency, reliability, token expenditure, and estimated cost:

```bash
# In the REPL
/stats

# From the shell
hubble --stats 30
```

Hubble records local analytics in `~/.hubble/stats.jsonl`:
- Success rates per model.
- Latency and time-to-first-token (TTFT).
- HTTP failure codes (`429`, `503`).
- Cost estimation (custom rates configurable via `"model_prices"`).

---

## Configuration

Settings are resolved using a layered hierarchy where subsequent layers override previous values:

1. Built-in defaults
2. Global configuration: `~/.hubble/settings.json`
3. Repository configuration: `.hubble/settings.json`
4. Local overrides: `.hubble/settings.local.json`
5. CLI arguments

### Example `settings.json`

```json
{
  "model": "codestral-latest",
  "max_tokens": 8192,
  "context_window": 128000,
  "permission_mode": "default",
  "shell": "auto",
  "shell_sandbox": "auto",
  "additional_dirs": [],
  "permissions": {
    "allow": [
      "shell(pytest*)",
      "shell(git status*)",
      "shell(git diff*)"
    ],
    "deny": [
      "shell(git push*)",
      "edit_file(migrations/*)"
    ]
  },
  "mcp_servers": {},
  "hooks": {}
}
```

### Repository Trust Model

To guard against malicious repositories with hostile `.hubble/settings.json` files:
- Repository settings cannot alter `base_url` or `api_key`.
- Critical parameters (`permission_mode`, `shell_sandbox`, `hooks`, and `mcp_servers`) require repository confirmation before execution.
- Trusted repositories are recorded in `~/.hubble/trusted_folders.json`.

---

## CLI Reference

```
usage: hubble [-h] [-p] [-c] [-r [ID]] [-m MODEL] [--provider PROVIDER]
              [--permission-mode {default,accept-edits,plan,yolo}] [--yolo]
              [--persona {code,debug,review,architect,chat}]
              [--subagent-policy {auto,explicit,off}]
              [--output-format {text,json,stream-json}] [--github]
              [--image FILE] [--max-turns MAX_TURNS] [--cwd CWD]
              [-w NAME] [--base-url BASE_URL] [--api-key API_KEY]
              [--allow RULE] [--deny RULE] [--no-session] [--quiet]
              [--test MODEL] [--stats [DAYS]] [--add-to-path] [-v]
              [prompt]
```

### Options

| Option | Description |
|:---|:---|
| `prompt` | Initial instruction to run. |
| `-p`, `--print` | Headless execution: processes prompt, prints response, and terminates. |
| `-c`, `--continue` | Resumes the most recent session in this directory. |
| `-r`, `--resume [ID]` | Resumes a session by ID (interactive picker if omitted). |
| `-m`, `--model MODEL` | Specifies the active model identifier. |
| `--provider NAME` | Selects a configured provider profile. |
| `--permission-mode MODE` | Sets initial safety mode: `default`, `accept-edits`, `plan`, or `yolo`. |
| `--subagent-policy POLICY`| Sets subagent delegation policy: `auto`, `explicit` (deterministic gating), or `off`. |
| `--yolo` | Shortcut for `--permission-mode yolo`. |
| `--persona PERSONA` | Sets system persona: `code`, `debug`, `review`, `architect`, `chat`. |
| `--output-format FORMAT` | Formats output in `-p` mode: `text`, `json`, or `stream-json`. |
| `--image FILE` | Attaches image to initial prompt (repeatable). |
| `-w`, `--worktree NAME` | Runs inside an isolated git worktree (`.hubble/worktrees/<name>`). |
| `--cwd DIR` | Sets the target workspace directory (defaults to `.`). |
| `--allow RULE` | Adds a tool execution allow rule (e.g., `"shell(pytest*)"`). |
| `--deny RULE` | Adds a tool execution deny rule (e.g., `"shell(git push*)"`). |
| `--stats [DAYS]` | Summarizes usage, latency, and cost telemetry (default: 30 days). |
| `--test MODEL` | Verifies model availability and latency, then exits. |
| `--add-to-path` | Registers Hubble installation directory in user PATH. |
| `-v`, `--version` | Displays version and exits. |

---

## Interactive REPL Reference

### Slash Commands

| Command | Description |
|:---|:---|
| `/help` | Displays available commands, shortcuts, and active modes. |
| `/model [name\|#]` | Switches active model or opens the model selection palette. |
| `/models [filter]` | Lists verified models and latencies. |
| `/provider` | Configures and switches LLM provider gateways. |
| `/mode [mode]` | Changes permission tier (`default`, `accept-edits`, `plan`, `yolo`). |
| `/subagent [policy]` | Configures subagent delegation policy (`auto`, `explicit`, `off`). |
| `/route` | Configures dynamic multi-model routing (`fast`, `strong`, `auto`, `off`). |
| `/team` | Manages subagent model teams. |
| `/persona [name]` | Switches system prompt persona. |
| `/compact [focus]` | Manually compacts conversation history (automatic at 80% context capacity). |
| `/undo` | Reverts file modifications applied during the previous turn. |
| `/diff` | Displays active workspace `git diff`. |
| `/sandbox` | Toggles or displays sandboxing status (`auto`, `native`, `docker`, `off`). |
| `/mcp` | Inspects status and tools of registered Model Context Protocol servers. |
| `/hooks` | Lists active lifecycle hooks. |
| `/worktree` | Manages git worktrees (`new`, `switch`, `list`, `remove`, `exit`). |
| `/init` | Scaffolds standard `HUBBLE.md` instructions in the workspace. |
| `/memory` | Displays project instructions and context files currently loaded. |
| `/lens` | Inspects, initializes (`/lens init`), or appends rules (`/lens add`) to `LENS.md` (alias: `/style`). |
| `/add <file>` / `/drop <file>` | Pins or removes specific files from persistent context. |
| `/stats` | Renders session telemetry, token usage, and cost estimates. |
| `/export [file]` | Exports conversation transcript to file. |
| `/clear` | Resets conversation state for a fresh session. |

### Keyboard Shortcuts

- `Shift+Tab`: Cycle through permission modes.
- `Alt+V`: Paste image from system clipboard.
- `@path`: Autocomplete and attach file content, directory trees, or images.
- `!command`: Execute a shell command directly on the host machine.
- `#note`: Append a persistent development note to `./HUBBLE.md`.
- `Ctrl+C`: Interrupt active model generation, tool execution, or clear the prompt. Press twice to exit.
- `Ctrl+D`: Terminate the REPL session cleanly.

---

## Project Architecture

```
hubble/
├── main.py              CLI entrypoint, flag resolution, headless -p orchestration
├── repl.py              Interactive prompt_toolkit REPL, slash commands, @mentions
├── agent.py             Agent loop, compaction logic, subagent task dispatcher
├── ui.py                Rich console formatting, syntax highlighting, diffs, menus
├── indexer.py           Zero-dependency AST & regex symbol indexer (Python, TS/JS, Go, Rust)
├── provider.py          OpenAI-compatible client, SSE streaming, tool call parser
├── anthropic_provider.py Native Anthropic Messages API client (prompt caching, thinking)
├── tools.py             Workspace tool definitions, validation, snapshotting
├── sandbox.py           OS sandboxing (macOS Seatbelt, Linux bubblewrap, Docker)
├── permissions.py       Permission tiers, approval matching, allow/deny rules
├── mcp.py               Model Context Protocol client (stdio, HTTP, SSE)
├── mcp_oauth.py         OAuth 2.0 discovery, browser login, credential storage
├── hooks.py             Deterministic lifecycle hook runner and evaluator
├── subagents.py         Custom agent definition loader and validator
├── plugins.py           Plugin registry, loading, and isolation
├── worktree.py          Git worktree creation, branch switching, and lifecycle
├── router.py            Dynamic tier-based model routing engine
├── scanner.py           Background model verification and latency scanner
├── keystore.py          OS credential store integration (Keyring/SecretService)
├── settings.py          Layered configuration loader and repository trust manager
├── session.py           JSONL session serialization and persistence
└── prompts.py           System prompt templates, personas, and memory loaders
```

---

## License

This project is licensed under the [MIT License](LICENSE).
