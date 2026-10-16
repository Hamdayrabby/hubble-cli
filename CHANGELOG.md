# Changelog

## 4.3.3

- **Complete Deprecation & Removal of `aihub`:**
  - Removed `aihub` console script alias from `pyproject.toml`, preventing the creation of `aihub.exe`.
  - Deleted legacy `aihub.exe` executables and uninstalled obsolete `aihub-code` package.
  - Renamed `DEFAULT_PROVIDER` from `aihub` to `hubble`, aligning the default provider name with Hubble CLI.
  - Maintained smooth legacy backwards compatibility so existing session histories and settings referencing `aihub` seamlessly map to `hubble`.
  - Updated all internal references, documentation, examples, and tests to consistently use `hubble`.

## 4.3.2

- **Compact Modern Block Logo:**
  - Scaled down the main ASCII banner logo from 6 rows to 3 rows of sleek, balanced block letters.
  - Reduced vertical footprint by 50%, preserving screen real estate on smaller terminal windows and panels while keeping visual punch and gradient shimmer.
- **Buttery-Smooth Animation & Zero Typing Lag:**
  - Optimized the background starfield animation rate to 15 fps (~66ms per frame) to prevent terminal I/O buffer saturation and eliminated dropped frames on Windows Terminal and PowerShell.
  - Added fast-path hex color conversion in `_pt_style` to eliminate expensive regex-based `Style.parse` calls during animation frames.
  - Implemented typing-aware animation freezing: the instant the user presses a key, the background animation tick freezes and reuses the cached frame, delivering 0ms input latency and butter-smooth typing responsiveness.
  - Reduced `StreamingMarkdown` live refresh frequency to 15 fps, removing stutter while streaming model responses.

## 4.3.1

- **Sleek Dark Autocompletion Menu:**
  - Overhauled `prompt_toolkit` completion dropdown styles, completely eliminating the stark default white/grey background and unstyled scrollbars when pressing `/` or `@`.
  - Autocompletion menu is now rendered as a sleek, modern, theme-aware floating card with high-contrast accent highlights on selection, muted metadata text, and custom styled scrollbars.
  - Dynamically bound menu styles to Hubble's color schemes in `hubble.theme` (`terminal`, `dark`, `tokyo night`, `solarized dark`, etc.) with instant live restyling upon switching themes with `/theme`.
- **Refined Terminal Response Rhythm & Aesthetics:**
  - Cleaned up tool execution lines: replaced noisy function-call brackets `Tool(cyan_args)` with minimalist badges `● Tool target`.
  - Unified tool result trees with indented bullet points (`•`) and clean branch connectors (`⎿`).
  - Added vertical breathing room before the assistant response (`✻ Hubble`) to cleanly delineate tool calls from final answers.
  - Fixed markdown streaming double-blank-line glitches and bound syntax highlighting dynamically to the active palette's `code_theme`.
  - Replaced harsh full-width yellow panels in permission approval prompts with modern compact `box.ROUNDED` dialogs.
  - Replaced retro `"reverse bold"` full-block inverting in interactive pickers (`/model`, `/resume`) with modern highlighted cursor rows.

## 4.3.0

- **Antigravity-Compatible Subagent Architecture (`invoke_subagent`, `define_subagent`):**
  - Added `invoke_subagent` tool for parallel execution, customizable token budgets, and granular workspace isolation (`inherit`, `branch`, `share`, `worktree`).
  - Added 4 pre-configured out-of-the-box specialists with zero configuration required:
    - `research`: Fast, read-only specialist for broad codebase exploration, symbol lookups, and web search.
    - `self`: Clones parent capabilities to tackle complex, multi-file implementation tasks in a clean context.
    - `code-reviewer`: Deep audit specialist for security vulnerabilities, diff inspection, and code quality.
    - `test-writer`: Automated unit and integration test creation and test-suite verification.
  - Added `define_subagent` tool enabling the AI or user to dynamically specify, configure, and persist new specialist subagents (`project` or `user` scope) on the fly.
- **Developer Lens & Design Language Engine (`LENS.md`, `/lens`):**
  - First-class support for `LENS.md` (project scope) and `~/.hubble/LENS.md` (user global scope), fitting Hubble's optics and telescope identity. Also seamlessly discovers `STYLE.md` and `style.md`.
  - Automatically loads and injects developer coding style, UI design language, color/token systems, and anti-pattern constraints into the model's system prompt with top priority, preventing generic statistical boilerplate ("LLM slop").
  - New interactive `/lens` command (`/lens`, `/lens init`, `/lens add <rule>`, with `/style` as alias) for inspecting and configuring lens preferences directly in the REPL.
- **Subagent Policy & Triggering Controls (`subagent_policy`):** Toggle subagent behavior via `subagent_policy` (`"auto" | "explicit" | "off"`) in `settings.json`, `--subagent-policy`, or the interactive `/subagent` command. In `explicit` mode, both `task` and `invoke_subagent` are gated and only provided to the active tool list if "agent", "subagent", or "delegate" is present in the prompt, preventing unintended subagent spawns on simple tasks. `off` disables subagent delegation entirely.
- **Unified Diff Patch Engine (`apply_patch`):** Native unified diff patch tool supporting standard hunk syntax (`@@ -start,len +start,len @@`), ±100 line fuzzy offset scanning, whitespace tolerance, CRLF/LF preservation, and snapshotting for `/undo`.
- **AST & Code Graph Symbol Indexer (`symbols`):** Fast zero-dependency AST and regex symbol indexer for Python, TypeScript/JavaScript, Go, and Rust. Features disk-cached `mtime` tracking for near-instant (<15ms) symbol lookups, single-file symbol outlines, and condensed repo mapping.

- **`/stats` and `hubble --stats`:** per model, the share of your prompts it finished, call failures by
  status, speed, time to first token, tokens and estimated cost, from a local log
  (`~/.hubble/stats.jsonl`).
- **`/route`:** cost/quality routing. Easy prompts go to a fast model, hard ones to a strong model,
  research sub-agents to fast, and a fast task that struggles escalates to strong. `/route auto`
  picks the fast model from your own stats.
- **Sub-agent model team:** `/team add <provider:model> [what it is for]` builds a list the main model
  picks from per sub-agent, with each model's recent record from `/stats`. Sub-agents (and custom
  agent files) can now run on any configured provider (`provider:model`), several at once in
  parallel, each labelled with its model. When the main model does not choose, a team member is
  picked by job (a fast/search one for research, a careful/review one for edits); a sub-agent whose
  model fails is retried on the next team model (research tasks, or edits that have not acted yet).
- **Ctrl+C works right away:** on Windows it used to wait until a slow or stalled model sent its
  next chunk, or a long shell command ended (a 20 s stall took 20 s to interrupt; now ~0.1 s).
  Ctrl+C with text typed clears the line; twice on an empty prompt exits.
- A spurious HTTP 401 from a gateway is retried once.
- **No small step cap; runs stop when they stop making progress.** A run ends when the model is done,
  when it repeats the same tool call 3 times, or goes 6 steps with nothing new (edits and shell
  commands reset this, so read → fix → re-test is fine), or, for a sub-agent, when it has used
  `subagent_token_budget` tokens (default 2M). `max_turns` (100) and `subagent_max_turns` (150) are
  only backstops. Long sub-agent runs now compact their history like the main agent.
- **Status stays at the bottom while the agent works:** model, mode, context %, tokens this turn
  and for the session, and tool calls now show under the spinner, the sub-agent board and streaming
  text (before, the bar only existed while you typed).
- **Answers type out word by word**, like a chat app, with a cursor: text is revealed at a steady pace (at least ~18 words/s, faster when a big chunk arrives, never more than about half a second behind the model), instead of appearing a paragraph or a burst of lines at a time.
- **Fallback never picks a model that can't use tools:** the model check now tests tool calling;
  chat-only deployments (e.g. a vLLM server without `--enable-auto-tool-choice`) and non-chat models
  (parse, embed, rerank, guard, …) are skipped; a model that fails with "tool choice requires …"
  mid-run is remembered and the next fallback is tried. The picker marks such models "no tools".
- **Approval prompts are a menu**, like other agent CLIs: 1 Yes / 2 Yes, and don't ask again for X this session / 3 No, and tell the model what to do instead. Arrow keys + Enter, or press the number; Esc = no, Ctrl+C stops the turn. The old y/a/n keys still work, and piped input keeps the typed prompt.
- **Sub-agents always report:** a sub-agent that has to stop is asked for a final report from what it
  found instead of returning nothing (or, if the model still won't write one, the list of what it
  read). Such runs show as ◐ partial, not ✔.

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
