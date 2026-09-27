# Changelog

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
