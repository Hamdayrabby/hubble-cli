# Changelog

## 4.2.0

- **Claude provider:** native Anthropic Messages API support via the official `anthropic` SDK —
  streaming, tool use, images, thinking blocks echoed back verbatim, system-prompt caching, the real
  per-model context window from `/v1/models`, and server-side refusal fallbacks on Claude Opus 5 /
  Fable 5.1. `ANTHROPIC_API_KEY` is detected automatically.
- The built-in provider (the AIHub gateway) is now named `aihub` instead of `hubble`. Saved
  settings, sessions and `/fallback` choices that say `hubble` keep working.
- More `/provider add` presets: DeepSeek, xAI, Together, Fireworks, Cerebras, NVIDIA, Moonshot,
  LM Studio, and a custom Anthropic-compatible URL.
- Home screen: a retro arcade scene with the HUBBLE starship; logo back to block letters.
- `python -m hubble --add-to-path` fixes "hubble is not recognized" after a pip install whose
  Scripts folder is not on PATH (user PATH on Windows, shell profile elsewhere); Hubble also shows
  a one-line tip when started that way. Tested on Python 3.13 and 3.14 too.
- Cleaner bottom bar: no white reverse-video background, a thin rule above it, dim labels with
  bright values, no emoji; status items wrap whole onto extra lines, token stats keep their own line.

## 4.1.2

- The big HUBBLE logo now stays big in shorter terminals (e.g. a 20-row VS Code panel): the
  telescope scene, tips and status lines give way first, and the logo shrinks only as a last resort.

## 4.1.1

- Fix constant screen flicker in the REPL: when PyPI reported a version that was not newer than the
  installed one, the new-version check scheduled a redraw on every redraw (hundreds per second).

## 4.1.0

**Safety**
- Shell commands run in an OS sandbox by default on macOS (Seatbelt) and Linux (bubblewrap): they can
  read everything but only write inside the workspace and temp dirs. Leaving the sandbox
  (`unsandboxed: true`) always asks. `/sandbox auto|native|docker|off`.
- Provider API keys are stored in the OS credential store instead of plain text; `/provider secure`
  migrates existing ones.

**Extensibility**
- MCP: remote servers over Streamable HTTP and HTTP+SSE, OAuth login (`/mcp login`), `${VAR}` in
  config, server resources as tools and server prompts as slash commands.
- Hooks: new `SessionStart`, `SessionEnd`, `Notification`, `PreCompact` and `SubagentStop` events.
- Custom agents in `.hubble/agents/*.md` with their own prompt, tools, model and capability.
- Plugins bundling commands, skills, agents, hooks and MCP servers (`/plugin install <folder|git URL>`).
- Edit-capable sub-agents (`task` with `capability: edit`) and a per-task model.

**Workflow**
- Image input: `@image.png`, Alt+V clipboard paste, `--image` for `-p`.
- Git worktrees: `hubble -w NAME`, `/worktree`, and `task` with `isolation: worktree`.
- GitHub integration: `/install-github` sets up PR reviews and `@hubble` mentions in Actions
  (`hubble -p --github`).
- `--output-format stream-json` for headless runs; `HUBBLE_MODEL` environment variable.
- New-version notice: the REPL tells you when a newer hubble-cli is on PyPI (checked at most once a
  day; `"update_check": false` or `HUBBLE_NO_UPDATE_CHECK=1` turns it off).
- Requests identify themselves as `User-Agent: hubble-cli/<version>`.
- Context window is taken from the provider when it publishes one (e.g. OpenRouter).

**Fixes**
- Model availability check no longer marks working models unavailable: rate-limited and timed-out
  models are retried, then shown as "unknown"; reasoning-only replies count as working.
- The "/" command menu kept working only until the first message.
- Model-check progress in the bottom bar and home screen updates live; startup checks are silent.
- Larger logo on the home screen.

## 4.0.0 — Initial public release

Agentic coding CLI: native tool calling, permission modes with allow/deny rules, diff preview
before edits, JSONL sessions with resume, context compaction, multi-provider support with
automatic fallback, web search/fetch, parallel read-only sub-agents, and Skills (reusable,
model-invocable procedures saved per project or per user).
