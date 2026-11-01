# Hubble (hubble-cli)

Agentic coding CLI. The active code is the `hubble/` package; the PyPI distribution is `hubble-cli` and the console script is `hubble` (`hubble.main:main`).

## Repository shape

- `hubble/main.py` — argparse, provider/settings selection, headless `-p`, and `run_headless`; `python -m hubble` and the root `code_cli.py` wrapper reach the same `main`.
- `hubble/repl.py` — interactive REPL, slash commands, `@file` mentions; `hubble/ui.py` — Rich rendering, diffs, and approval prompts (`ReplEvents`).
- `hubble/agent.py` — provider-neutral turn loop. It intentionally never prints; all progress goes through `Events`, which lets the REPL, headless mode, subagents, and the JSONL worker share one loop.
- `hubble/tools.py` — tool implementations and `ToolContext` (workspace confinement, secret masking, read-before-edit checks, per-turn snapshots/undo, PowerShell vs bash). Add new tools to `default_tools()`; add read-only tools to `READ_ONLY_TOOL_NAMES`; use `ctx.resolve`, `ctx.check_secret`, and `ctx.snapshot` rather than bypassing them.
- `hubble/provider.py` — OpenAI-compatible streaming client; `hubble/anthropic_provider.py` — native Claude client. Both return `TurnResult`/`ToolCall`; `hubble/providers.py` selects by `kind`. Keep both implementations in sync when changing tool calling or streaming.
- `hubble/server_worker.py` — separate JSONL entrypoint (`python -m hubble.server_worker --workspace ...`) for hosted use. It deliberately disables repo-controlled hooks, MCP, plugins, yolo mode, and extra dirs; preserve that server-safe behavior when editing settings or tools.
- `hubble/settings.py` — layered settings and repository trust. Project settings can never set `base_url`/`api_key`; several keys (`permission_mode`, `shell_sandbox`, `hooks`, `mcp_servers`, ...) require trust. Do not weaken this filtering.
- Root `chat_cli.py` is the legacy v3 single-file CLI. Root `config.py`, `test_models.py`, `gateway_search_server.py`, and `gateway_search_worker.js` are standalone utilities, not part of the installed package (`pyproject.toml` packages only `hubble`). Do not add current features there.

## Build, test, and CI

- Install for development with `pip install -e ".[dev]"` (or `uv sync --extra dev`; `uv.lock` is checked in).
- Run the suite with `python -m pytest -q -rs` from the repo root. It is self-contained (~400 tests, ~1–2 min; fake HTTP/MCP providers, no API key or network).
- Single test: `python -m pytest tests/test_core.py::test_rules -q`.
- `pytest` collects only `tests/` (`[tool.pytest.ini_options] testpaths`). Root `test_models.py` is a live-API model scanner, not a test; it hits the configured gateway and writes the gitignored `available_models.json`. Do not run it as part of verification.
- CI (`.github/workflows/test.yml`) runs Windows/macOS/Linux × Python 3.10–3.14. Keep syntax 3.10-compatible and do not assume POSIX. Linux native-sandbox tests need `bubblewrap`; Windows has no native sandbox and those tests skip.
- Known red baseline (v4.3.15): Windows/Python 3.10 fails the three `tests/test_process_manager.py` launch tests (the test passes `env={}` and Python exits with "Python runtime state: preinitialized"); macOS can fail the timing-sensitive `tests/test_streaming_render.py::test_text_is_typed_out_word_by_word_and_catches_up`. Check `git diff`/CI annotations before treating either as your regression.
- `tests/conftest.py` disables the real OS keyring and redirects stats, but `HOME_DIR` is bound at import time in many modules. When a test touches user-level state, patch each relevant module constant (see `tests/test_agents_plugins.py::isolate_home`), not just `hubble.settings.HOME_DIR`.
- Use `tests/fake_anthropic.py`, `tests/fake_mcp_server.py`, and `tests/fake_mcp_http.py` for provider/MCP tests; do not add tests that need real credentials or network.
- There is no lint/typecheck/formatter config (no ruff/mypy). `.cursorrules`, `GEMINI.md`, and `.antigravity/rules.md` are untracked identical boilerplate; their 200-line-per-file cap and ruff/mypy instructions do not match this repo. Verify with pytest.
- If you add a runtime dependency, update `pyproject.toml` and `requirements.txt` (the latter is referenced by `hubble/main.py`'s install hint).

## Runtime and release notes

- Version is `hubble/__init__.py:__version__`. Releases need a matching `vX.Y.Z` tag (`publish.yml` rejects mismatches) and a `CHANGELOG.md` entry.
- `HUBBLE_HOME` overrides `~/.hubble` (settings, providers, sessions, stats). Sessions are append-only JSONL under `~/.hubble/projects/<slug>-<hash>/`.
- `.env` loading is a known docs/code mismatch: `load_settings` reads `HOME_DIR/.env` and `PROJECT_DIR/.env` (`PROJECT_DIR` is the parent of the installed `hubble/` package), not `<workspace>/.env`; the `root` argument controls the workspace `.hubble/settings*.json` and trust, not `.env` lookup. Credentials come from `HUBBLE_*`/`AIHUB_*` (plus `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` and search keys); `.env` is gitignored.
- `hubble/prompts.py` injects this file as project memory (priority after `HUBBLE.md`, before `CLAUDE.md`/`GEMINI.md`), so keep it accurate and short.
