# Hubble

An agentic coding CLI in the style of Claude Code, Antigravity/Gemini CLI and Codex CLI. It works directly
in your repository: it searches, reads and edits files and runs commands through native function calling,
and you approve each action. It talks to any OpenAI-compatible API — the AIHub gateway by default, or
OpenAI, Groq, OpenRouter, Mistral, a local Ollama server, or others via `/provider add`.

## Install

```bash
pipx install git+https://github.com/Hamdayrabby/hubble-cli.git
```
(or `pip install --user git+https://github.com/Hamdayrabby/hubble-cli.git` if you don't use pipx)

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
hubble -m nvidia/nemotron-3-super-120b-a12b --persona architect
hubble --test codestral-latest           # check that a model responds
```

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
| `task` | Read-only sub-agent for broad research; returns a report |
| `web_search` | Web search. DuckDuckGo by default (no key); set `BRAVE_API_KEY` or `TAVILY_API_KEY` to use those instead |
| `web_fetch` | Fetch a URL as readable text, page by page (`offset`). Asks once per domain; refuses local and private addresses |

Safety:
- **Workspace confinement:** paths outside the workspace are refused. Add others with `additional_dirs`.
- **Secret files are blocked:** `.env`, `*.pem`, `id_rsa` and similar are never read, searched or attached (`allow_secret_files` turns this off).
- **Edits need a fresh read:** a file must be read before it is edited or overwritten, and read again if it changed on disk since.
- **Undo:** every change is snapshotted, so `/undo` can revert it.

**Shell commands are not sandboxed by default** — they run directly on your machine with your own
permissions, same as anything you'd type yourself. Approval prompts are the only protection unless you
turn on the Docker sandbox below.

### Sandboxed shell execution (optional)

With [Docker Desktop](https://www.docker.com/products/docker-desktop/) installed, shell commands can run
inside an isolated, disposable container instead of directly on your machine:

```
/sandbox on
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
install-style commands. Like `permission_mode`, `shell_sandbox` and `sandbox_network` only take effect from
a project's own `.hubble/settings.json` once you've trusted that folder — an untrusted, freshly cloned
project can't quietly turn sandboxing off or re-enable network access on your behalf.

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
| `@path` | Attach a file (or directory listing) to your message; Tab completes paths |
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

Extra providers are stored in `~/.hubble/providers.json`, with their API keys in plain text, like a `.env` file.
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

Credentials come from `HUBBLE_API_KEY` / `HUBBLE_BASE_URL` in the environment, `.env` in your project, or `~/.hubble/.env`.
Only `AIHUB_*` keys are read from those files.

Sessions are saved as JSONL in `~/.hubble/projects/<project>/`. Input history is in `~/.hubble/history`.

## Layout

```
hubble/
  main.py         CLI flags, headless -p mode, resume
  repl.py         prompt_toolkit REPL, slash commands, @mentions
  ui.py           rich rendering: streamed markdown, diffs, approval prompts
  agent.py        agent loop, compaction, task sub-agent
  provider.py     OpenAI-compatible SSE client, retries, tool-call assembly
  tools.py        workspace tools, sandbox, checkpoints
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
The last scan found 33 working models out of 295.

| Category | Models |
|---|---|
| Coding (default) | `codestral-latest`, `codestral-2508`, `mistral-code-latest`, `mistral-code-fim-latest` |
| Reasoning | `nvidia/nemotron-3-super-120b-a12b`, `intern-s2-preview`, `intern-s1-mini`, `intern-s1` |
| Fast chat | `ministral-14b-latest`, `open-mistral-nemo`, `ministral-8b-latest`, `ministral-3b-latest` |
| Vision | `meta/llama-3.2-11b-vision-instruct`, `internvl3.5-latest`, `internvl-latest` |

Native tool calling was verified on `codestral-latest`, `mistral-code-latest`, `ministral-14b-latest` and
`nvidia/nemotron-3-super-120b-a12b`.
