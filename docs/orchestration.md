# Cross-runtime orchestration

Hubble can supervise external coding CLIs (Muse Code, Codex, Antigravity,
OpenCode, Command Code) through one adapter interface. Each task runs in an
isolated worktree pinned to a recorded base commit; results are verified
independently and recorded in an append-only journal.

## Quick start

```powershell
# 1. See what is installed and usable
python -m hubble.orchestrate --list-runtimes

# 2. Dry run with the fake runtime (no keys, no cost)
python -m hubble.orchestrate --repo . --task impl:"add a hello function" --runtime fake

# 3. Real harness (spends model calls)
python -m hubble.orchestrate --repo . --task impl:"add a hello function" --runtime codex
```

Inside the REPL:

```text
/orchestrate runtimes
/orch run impl:add a hello function --runtime codex
```

Or headless through the main CLI:

```powershell
hubble --orchestrate --list-runtimes
hubble --orchestrate --runtime antigravity "impl:add a hello function"
```

## What happens per task

1. The coordinator pins the repo `HEAD` SHA and creates `hubble/orch-<id>-<run>`
   as an isolated worktree (branch of the same name).
2. The selected runtime starts with that worktree as its working directory,
   an allowlisted environment (secrets never cross), and a bounded timeout.
3. Stdout/stderr stream back as versioned events; malformed lines become
   visible `parse_error` events, never silent drops.
4. After the run, acceptance checks execute independently — a harness exit
   code of 0 proves nothing on its own.
5. Status, base SHA, branch, changed files, and an event tail append to
   `<repo>/.hubble/runs.jsonl`.

Nothing merges automatically. Review the worktree diff and merge by hand:

```powershell
git -C .hubble\worktrees\orch-<id>-<run> diff
```

## Retries and resume

Failed tasks retry in a fresh worktree (bounded per-task budget). After an
interruption, `Scheduler.resume_from(store)` restores state from the journal:
succeeded tasks never re-run; ambiguous in-flight runs are flagged for human
inspection before anything re-executes.

## runtimes without a verified CLI

A runtime whose headless mode was never captured refuses to start loudly
(`RuntimeUnavailable` with diagnostics) instead of guessing flags. Verify with
that CLI's `--help`, then implement one adapter file plus contract tests —
the scheduler needs no changes.

## REA (optional)

Reverse Engineer Anything is an optional specialist, not a dependency. When
`rea` is installed, `hubble/repo_intel/rea.py` exposes detection, a stdio
`rea --mcp` server entry for `mcp_servers` settings, and a safe capabilities
probe. Never send proprietary code through it without explicit approval.
