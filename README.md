# Hubble

An agentic coding CLI in the style of Claude Code, Antigravity/Gemini CLI and Codex CLI. It works directly
in your repository: it searches, reads and edits files and runs commands through native function calling,
and you approve each action. It talks to any OpenAI-compatible API — the AIHub gateway by default, or
OpenAI, Groq, OpenRouter, Mistral, a local Ollama server, or others via `/provider add`.

## Install

```bash
pipx install hubble-cli
```
(or `pip install --user hubble-cli`; the latest unreleased code: `pipx install git+https://github.com/Hamdayrabby/hubble-cli.git`)

Then just run it from any project:

```bash
cd /path/to/your/project
hubble
```

With no API key configured anywhere, the first run walks you through adding one — no manual `.env` editing
required. To set one up yourself instead, put it in `.env` in your project or in `~/.hubble/.env`:
```
HUBBLE_API_KEY=your_key_here
HUBBLE_BASE_URL=https://aihub.071129.xyz/v1   # or any other OpenAI-compatible base URL
```

**From a local clone**, for development: `pip install -e .` from the repo root installs the `hubble`
command (`aihub` also works, kept as an alias) against your working copy — edits take effect immediately.
Without installing at all: `python code_cli.py [args]` from the repo root (add `--cwd <project>` to work
elsewhere). The original single-file prototype is still there as `python chat_cli.py`.

## Usage

```bash
hubble                                   # interactive REPL
hubble "explain the architecture"        # REPL with a first prompt
hubble -c                                # continue the latest session in this folder
hubble -r                                # choose a session to resume
hubble -p "fix the failing test" --permission-mode accept-edits --allow "shell(pytest*)"
git diff | hubble -p "review this diff" --output-format json
hubble -p "run the tests" --output-format stream-json   # one JSON event per line, as it happens
hubble -p "what is wrong in this UI?" --image screenshot.png
hubble -w refactor-auth                  # work in an isolated git worktree on branch hubble/refactor-auth
hubble -m nvidia/nemotron-3-super-120b-a12b --persona architect
hubble --test codestral-latest           # check that a model responds
```

`stream-json` lines have a `type`: `init`, `text` (streamed delta), `assistant` (one model call, with its
tool calls and usage), `tool_use`, `tool_result`, `todos`, `notice`, `subagent_start`, `subagent_end`, and
always last `result` (the same fields as `--output-format json`).

## Tools the model can use

| Tool | What it does |
|---|---|
| `read_file` | Read with line numbers, `offset`/`limit` for large files |
| `edit_file` | Exact string replace; must be unique unless `replace_all`; keeps CRLF |
| `write_file` | Create or overwrite a file (existing files must be read first) |
| `shell` | Run a command in the workspace root (PowerShell on Windows), with timeout and closed stdin |
| `grep` | Regex search (ripgrep if installed, otherwise Python) |
| `glob` | Find files by pattern, newest first |
| `list_dir` | List a directory |
| `todo_write` | Task list for multi-step work, shown in the terminal |
| `task` | Sub-agent for one self-contained piece of work: `read_only` (default) for research, returns a report; `edit` for a delegated implementation task, with its own file/shell tools — its edits and commands still ask for approval the same way yours would. Several `read_only` tasks in one turn run in parallel; an `edit` task always runs on its own. Can target a different model per task, a [custom agent](#custom-agents) (`agent`), and `isolation: "worktree"` to do its edits in a fresh git worktree on its own branch. |
| `web_search` | Web search. DuckDuckGo by default (no key); set `BRAVE_API_KEY` or `TAVILY_API_KEY` to use those instead |
| `web_fetch` | Fetch a URL as readable text, page by page (`offset`). Asks once per domain; refuses local and private addresses |
| `mcp__<server>__<tool>` | Tools from any MCP server you've configured, plus `list_resources`/`read_resource` for servers that offer resources (see [MCP servers](#mcp-servers) below) |

Safety:
- **Workspace confinement:** paths outside the workspace are refused. Add others with `additional_dirs`.
- **Secret files are blocked:** `.env`, `*.pem`, `id_rsa` and similar are never read, searched or attached (`allow_secret_files` turns this off).
- **Edits need a fresh read:** a file must be read before it is edited or overwritten, and read again if it changed on disk since.
- **Undo:** every change is snapshotted, so `/undo` can revert it.

### Sandboxed shell execution

On **macOS and Linux, shell commands run in an OS sandbox by default** (`"shell_sandbox": "auto"`), like
Codex: a command can read anything but can only write inside the workspace and temp directories (and any
`sandbox_writable` paths you add). macOS uses the built-in `sandbox-exec` (Seatbelt); Linux uses
[bubblewrap](https://github.com/containers/bubblewrap) (`sudo apt install bubblewrap`). If a command
genuinely needs to write elsewhere — a global install, a config file in your home folder — the model
retries it with `unsandboxed: true`, and **you are always asked first**, even in accept-edits mode or with a
matching allow rule (only `yolo` skips that).

**On Windows there is no built-in equivalent**, so commands run directly on your machine with your own
permissions, and approval prompts are the protection — unless you use the Docker sandbox below.

`/sandbox` shows the current state; `/sandbox auto|native|docker|off` changes it.

```json
{
  "shell_sandbox": "auto",
  "sandbox_network": true,
  "sandbox_writable": ["~/.cache/pip", "~/.npm"]
}
```

**Docker sandbox** (any OS, needs [Docker Desktop](https://www.docker.com/products/docker-desktop/)): shell
commands run inside an isolated, disposable container instead of on your machine:

```
/sandbox docker
```
or in `~/.hubble/settings.json` / `.hubble/settings.json`:
```json
{
  "shell_sandbox": "docker",
  "sandbox_image": "python:3.12-slim",
  "sandbox_memory": "1g",
  "sandbox_cpus": "2",
  "sandbox_network": true
}
```
Only the project folder is mounted in (as `/workspace`); nothing else on your machine is reachable from
inside it. Memory and CPU are capped, and the container is removed after every command. Set
`sandbox_image` to whatever your project needs (e.g. `node:20` for a JS project); set `sandbox_network` to
`false` to also block network access from inside the sandbox, if your workflow doesn't need `pip`/`npm`
install-style commands. Like `permission_mode`, `shell_sandbox`, `sandbox_network` and `sandbox_writable` only take effect from
a project's own `.hubble/settings.json` once you've trusted that folder — an untrusted, freshly cloned
project can't quietly turn sandboxing off or re-enable network access on your behalf.

### MCP servers

Connect any [MCP](https://modelcontextprotocol.io/) server — local (stdio) or remote (Streamable HTTP, or
the older HTTP+SSE transport) — and its tools become available to the model, namespaced as
`mcp__<server>__<tool>`:

```json
{
  "mcp_servers": {
    "filesystem": {"command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/path/to/share"]},
    "git": {"command": ["uvx", "mcp-server-git"]},
    "linear": {"url": "https://mcp.linear.app/mcp"},
    "internal": {"url": "https://mcp.example.com/sse", "transport": "sse",
                 "headers": {"Authorization": "Bearer ${INTERNAL_MCP_TOKEN}"}}
  }
}
```
- `${VAR}` in `url`, `headers` and `env` is read from the environment, so tokens stay out of the file.
- `transport` is `auto` by default: Streamable HTTP, falling back to HTTP+SSE for older servers.
- **OAuth:** a remote server that answers 401 shows as "needs login". Run `/mcp login <server>`: Hubble
  discovers the authorization server, registers itself (or uses `"oauth": {"client_id": "..."}`), opens
  your browser, and keeps the token in your OS credential store, refreshing it automatically.
  `/mcp logout <server>` forgets it.
- **Resources** a server offers are readable by the model through `list_resources`/`read_resource`.
- **Prompts** a server offers become slash commands: `/mcp__<server>__<prompt> key=value ...`.

`/mcp` lists servers, their tools and prompts, and which need a login. A server that fails to start or
handshake is skipped with a warning, not a crash. Like the sandbox settings, `mcp_servers` only takes effect
from a project's own `.hubble/settings.json` once you've trusted that folder — a cloned repo can't have you
unknowingly launch arbitrary processes.

### Hooks

Hooks are shell commands that run at fixed points in the agent loop, independent of the model's own
choices — for enforcing project policy, logging, or linting. Configure them in `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [{"matcher": "shell", "command": "python .hubble/check_command.py"}],
    "PostToolUse": [{"matcher": "edit_file", "command": "python .hubble/lint_changed_file.py"}],
    "UserPromptSubmit": [{"command": "python .hubble/inject_context.py"}],
    "Stop": [{"command": "python .hubble/require_tests_ran.py"}]
  }
}
```
Each hook receives a JSON payload on stdin — `{"event": "PreToolUse", "tool": "edit_file", "args": {...}}`
for tool events, `{"event": "UserPromptSubmit", "prompt": "..."}`, or `{"event": "Stop", "final_text": "..."}`
— and reads whatever it needs (e.g. `args["path"]`) from there, not from shell variables. It controls what
happens next through its exit code and stdout:
- **Exit non-zero** → blocks the action; stderr becomes the reason shown to the model.
- **Print `{"decision": "block", "reason": "..."}`** → same, from an exit-0 process.
- **Print `{"additionalContext": "..."}`** → the action proceeds, and the text is appended (to the prompt
  for `UserPromptSubmit`, to the tool's result for `PostToolUse`).
- **`Stop` hooks** can refuse to let the turn end (`"decision": "block"`) — the reason is fed back as if it
  were a new instruction, so the model keeps going (e.g. "you haven't run the tests yet").
- Anything else on stdout is just logged, not acted on.

`matcher` filters by tool name (glob, e.g. `"shell"` or `"mcp__*"`); omit it to run on every event of that
type. Hooks run on the host, not inside the sandbox, and — like `mcp_servers` — only take effect once
you've trusted the project.

All events:

| Event | When | Can |
|---|---|---|
| `SessionStart` | Hubble starts or resumes (`source`: startup / resume) | `additionalContext` is added to the system prompt for the session |
| `UserPromptSubmit` | You send a message | block it, or add context |
| `PreToolUse` | Before a tool runs | block it |
| `PostToolUse` | After a tool runs | add context to its result |
| `Notification` | Hubble is waiting for your approval | observe (e.g. desktop notification) |
| `PreCompact` | Before history is compacted (`trigger`: auto / manual) | block compaction |
| `SubagentStop` | A `task` sub-agent finished | add context to its report |
| `Stop` | The turn is about to end | block = keep going with `reason` as the next instruction |
| `SessionEnd` | Hubble exits | observe |

## Custom agents

Named specialists the model can delegate to with the `task` tool. One Markdown file each, in
`.hubble/agents/` (project) or `~/.hubble/agents/` (user):

```markdown
---
name: test-writer
description: Writes focused pytest tests for a module. Use after adding or changing a feature.
tools: read_file, grep, glob, write_file, edit_file, shell   # optional: narrows its toolset
model: codestral-latest                                       # optional: its own model
capability: edit                                              # read_only (default) or edit
---
You write small, fast pytest tests. Cover the edge cases first. Run the tests before reporting.
```

Only the name and description are shown to the main model, so many agents cost little. A `read_only`
agent never gets edit tools, whatever its `tools` line says. `/agents` lists them; `/agents new <name>`
creates a template.

## Plugins

A plugin is one folder that bundles commands, skills, agents, hooks and MCP servers, so a team can share a
whole setup:

```
my-plugin/
  plugin.json     {"name": "my-plugin", "version": "1.0.0", "description": "...",
                   "hooks": {...}, "mcp_servers": {...}}      # same shape as in settings.json
  commands/*.md   agents/*.md   skills/<name>/SKILL.md
```
`${PLUGIN_DIR}` in a hook or MCP command points at the plugin's folder, so it can ship its own scripts.

`/plugin install <folder or git URL> [--project]`, `/plugin list`, `/plugin disable|enable <name>`,
`/plugin remove <name>`. Hooks and MCP servers from a *project* plugin only load once you trust the folder.

## Images

Attach screenshots or diagrams for a vision-capable model: mention `@shot.png` in a message, press
**Alt+V** to paste an image from the clipboard (shows as `[Image #1]`), or pass `--image file.png` with
`-p`. PNG, JPEG, GIF, WebP and BMP up to 8 MB. Models without vision support usually answer with an error.

## Git worktrees

Work on something risky, or several things at once, without touching your main checkout:

- `hubble -w <name>` or `/worktree new <name>` creates `.hubble/worktrees/<name>` on branch `hubble/<name>`
  and moves every tool there. `/worktree exit` goes back; `/worktree switch <name>`, `/worktree list`,
  `/worktree remove <name> [--force] [--delete-branch]`.
- The folder is added to `.git/info/exclude`, so it never shows up in `git status`.
- A `task` with `isolation: "worktree"` makes an edit sub-agent work in a fresh worktree. Its file edits
  there need no approval (they can't touch your checkout); its shell commands still ask. When it finishes,
  its changes are committed on their own branch and the main model is told how to review and
  cherry-pick them.

## GitHub integration

`/install-github` writes `.github/workflows/hubble.yml`. After you add a `HUBBLE_API_KEY` repository
secret (and optionally `HUBBLE_MODEL` / `HUBBLE_BASE_URL` variables):
- every pull request from the same repository gets a review comment;
- `@hubble <request>` in an issue, or a PR comment, gets an answer. On a pull request Hubble can make the
  requested change, and the workflow pushes it to the PR branch.

Only the repository's owners, members and collaborators can trigger it, since a mention runs an agent with
your API key and push access; comment text is never pasted into a shell script. Under the hood this is
`hubble -p --github`, which reads the Actions event and posts the reply with `GITHUB_TOKEN`.

## Permission modes

Cycle with **Shift+Tab** or set with `/mode` or `--permission-mode`:

| Mode | Behaviour |
|---|---|
| `default` | Ask before every edit and shell command. The prompt shows a diff or the command. |
| `accept-edits` | Edits are auto-approved. Shell commands still ask. |
| `plan` | Read-only. The model explores and proposes a plan. |
| `yolo` | Everything is auto-approved except deny rules. |

At an approval prompt:
- `y` allows the action once.
- `a` allows it for the rest of the session: all edits, or commands with the same prefix, e.g. `pytest*`.
- `n` denies it. You can add feedback, which is sent to the model.
- Ctrl+C stops the turn.

Allow rules never apply to chained commands (`&&`, `;`, `|`, redirects), so `shell(git status*)` does not approve `git status && rm -rf x`.

## In-session commands

| Input | Action |
|---|---|
| `/help` | All commands and shortcuts |
| `/model [name\|#]`, `/models [filter]` | Switch or list models (latencies from `python test_models.py`) |
| `/mode [mode]` | Permission mode |
| `/persona [code\|debug\|review\|architect\|chat]` | System persona |
| `/clear` | New conversation |
| `/compact [focus]` | Summarize history to free context (automatic at 80%) |
| `/resume [#\|id]` | Resume a saved session |
| `/undo` | Revert files changed in the last turn that edited files |
| `/diff` | Show `git diff` |
| `/init` | Generate `HUBBLE.md` project instructions |
| `/memory` | Show loaded memory files |
| `/add <file>`, `/drop <file>`, `/files` | Pin files into the system prompt |
| `/todos`, `/cost`, `/context`, `/config`, `/test [model]`, `/temp [t]`, `/export [file]` | Info and utilities |
| `/sandbox`, `/mcp`, `/hooks` | Sandbox mode, MCP servers (`login`/`logout`), configured hooks |
| `/agents`, `/plugin`, `/skills` | Custom agents, plugins, skills |
| `/worktree` | Git worktrees: `new`, `switch`, `exit`, `remove`, `list` |
| `/install-github` | Set up the GitHub Action |
| `@path` | Attach a file, directory listing or image to your message; Tab completes paths |
| Alt+V | Paste an image from the clipboard |
| `!cmd` | Run a shell command yourself |
| `#note` | Append a note to `./HUBBLE.md` |
| Esc+Enter / Ctrl+J | New line |
| Ctrl+C / Ctrl+D | Cancel turn / quit |

**Custom commands:** `.hubble/commands/<name>.md` (project) or `~/.hubble/commands/<name>.md` (user) becomes
`/<name>`. `$ARGUMENTS` is replaced by the text after the command.

## Providers (extra base URLs and API keys)

Any OpenAI-compatible API can be added next to the built-in AIHub gateway: OpenRouter, Groq, OpenAI, Mistral,
Gemini's OpenAI endpoint, a local Ollama server, and others.

1. Type `/provider add`, or open `/provider` and choose **+ Add a provider**.
2. Pick a known provider or **Custom URL...**, then paste the API key. The key is hidden as you type.
3. The CLI checks the URL and key by listing the provider's models. If the check fails, it tells you why: wrong key, wrong URL, or can't connect.
4. Optionally, the CLI checks which models actually respond, in the background. Progress shows in the bottom bar.
5. Choose whether to switch to the new provider now. If you do, a model picker for that provider opens.

After that:
- `/model` lists models from every provider and switches provider automatically. The choice is saved as your default.
- `/provider` switches provider, `/provider list` shows all of them, and `/provider remove <name>` deletes one.
- `hubble --provider <name>` picks a provider for one run.
- On first start with no API key at all, the same setup runs instead of an error.

Extra providers are stored in `~/.hubble/providers.json`. Their API keys go to your OS credential store
(Windows Credential Manager, macOS Keychain, or Secret Service on Linux); only where no credential store
exists (headless Linux, containers) are they written into that file in plain text. `/provider secure` moves
keys saved by older versions into the credential store.
Their model lists are stored in `~/.hubble/models/<name>.json`. When a model is rate limited or down, Hubble retries that request with a fallback that the current provider actually has, in this order: the fallback you picked with `/fallback`, then `fallback_model` if the provider has it, then the provider's fastest verified model, then `fallback_model` on the built-in hubble provider. Sub-agents use the same route. `/fallback off` disables it for a provider.

## Project memory

On startup the CLI loads `~/.hubble/HUBBLE.md`, then one of `HUBBLE.md`, `AGENTS.md`, `CLAUDE.md` or `GEMINI.md`
from each directory between the git root and the workspace, into the system prompt.

## Configuration

Settings merge in order (later wins):
1. Built-in defaults
2. `~/.hubble/settings.json`
3. `.hubble/settings.json`
4. `.hubble/settings.local.json`
5. CLI flags

```json
{
  "model": "codestral-latest",
  "max_tokens": 8192,
  "context_window": 128000,
  "permission_mode": "default",
  "shell": "auto",
  "additional_dirs": [],
  "permissions": {
    "allow": ["shell(pytest*)", "shell(git status*)", "shell(git diff*)"],
    "deny": ["shell(git push*)", "edit_file(migrations/*)"]
  }
}
```

- Rule syntax is `tool` or `tool(glob)`. The glob is matched against the path or the command.
- `bash`, `edit`, `write` and `read` work as aliases for the tool names.
- Deny rules win over allow rules.
- `shell` can be `auto` (pwsh, then Windows PowerShell), `cmd` or `bash`.

Project settings files (`.hubble/*.json`) come from the repository, so they are treated as untrusted:
- They can never set `base_url` or `api_key`.
- `permission_mode`, `allow_secret_files`, `additional_dirs`, `shell` and allow rules apply only after you trust the folder. The CLI asks once and remembers the answer in `~/.hubble/trusted_folders.json`.
- Deny rules always apply.

Credentials come from `HUBBLE_API_KEY` / `HUBBLE_BASE_URL` (and optionally `HUBBLE_MODEL`) in the
environment, `.env` in your project, or `~/.hubble/.env`. Only `HUBBLE_*` keys (and the older `AIHUB_*`
names) are read from those files; nothing else in a `.env` is touched.

Sessions are saved as JSONL in `~/.hubble/projects/<project>/`. Input history is in `~/.hubble/history`.

## Layout

```
hubble/
  main.py         CLI flags, headless -p mode, resume
  repl.py         prompt_toolkit REPL, slash commands, @mentions
  ui.py           rich rendering: streamed markdown, diffs, approval prompts
  agent.py        agent loop, compaction, task sub-agent
  provider.py     OpenAI-compatible SSE client, retries, tool-call assembly
  tools.py        workspace tools, checkpoints
  sandbox.py      OS-native command sandbox (Seatbelt / bubblewrap)
  mcp.py          MCP client: stdio, Streamable HTTP, SSE; mcp_oauth.py: OAuth login
  hooks.py        hook runner; subagents.py: custom agents; plugins.py: plugins
  worktree.py     git worktrees; github.py: GitHub Actions integration
  images.py       image attachments and clipboard paste; keystore.py: OS credential store
  scanner.py      background model availability checks
  permissions.py  modes and allow/deny rules
  session.py      JSONL transcripts
  prompts.py      system prompt, personas, memory files
  settings.py     layered settings and .env loading
  models.py       model registry (shared with config.py)
tests/            pytest suite (python -m pytest -q)
```

## Models on AIHub

Hubble checks which models actually respond in the background: automatically every time it starts (for
every provider you have configured), and on demand with `/models refresh`. `/models` shows the results and
how long ago they were checked. Set `"model_refresh_hours"` in `~/.hubble/settings.json` to a positive number
to only recheck once the list is that many hours old instead of on every start, or `null` to turn the
automatic check off entirely (`/models refresh` still works). The check is one tiny request per model, so it
costs a little on paid APIs.

Running `python test_models.py` does the same scan for the built-in provider from the command line.
The last scan found 50 working models out of 295. Models that were only rate limited or timed out during a
check show as "unknown", not "unavailable", and one that worked last time stays listed.

| Category | Models |
|---|---|
| Coding (default) | `codestral-latest`, `codestral-2508`, `mistral-code-latest`, `mistral-code-fim-latest` |
| Reasoning | `nvidia/nemotron-3-super-120b-a12b`, `intern-s2-preview`, `intern-s1-mini`, `intern-s1` |
| Fast chat | `ministral-14b-latest`, `open-mistral-nemo`, `ministral-8b-latest`, `ministral-3b-latest` |
| Vision | `meta/llama-3.2-11b-vision-instruct`, `internvl3.5-latest`, `internvl-latest` |

Native tool calling was verified on `codestral-latest`, `mistral-code-latest`, `ministral-14b-latest` and
`nvidia/nemotron-3-super-120b-a12b`.
