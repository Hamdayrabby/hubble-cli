import json
import sys
from pathlib import Path

from hubble.hooks import HookRunner
from hubble.tools import detect_shell

SHELL = detect_shell()


def _script(tmp_path: Path, name: str, code: str) -> str:
    """Write a tiny python script and return the command to run it (avoids shell-quoting
    headaches from inline `python -c "..."` one-liners)."""
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    cmd = f'"{sys.executable}" "{path}"'
    # PowerShell parses two adjacent quoted strings as two statements, not "run this with this
    # arg" -- it needs the call operator to invoke a quoted path. POSIX shells need no such thing.
    if "powershell" in SHELL[0].lower() or "pwsh" in SHELL[0].lower():
        cmd = "& " + cmd
    return cmd


def test_no_hooks_configured_allows(tmp_path):
    runner = HookRunner({}, tmp_path, SHELL)
    result = runner.run("PreToolUse", {"tool": "shell"}, name="shell")
    assert bool(result) and result.decision == "allow"


def test_nonzero_exit_blocks_with_stderr_as_reason(tmp_path):
    cmd = _script(tmp_path, "deny.py", "import sys\nsys.stderr.write('nope, protected file')\nsys.exit(1)\n")
    runner = HookRunner({"PreToolUse": [{"command": cmd}]}, tmp_path, SHELL)
    result = runner.run("PreToolUse", {"tool": "edit_file"}, name="edit_file")
    assert not result and result.decision == "block" and "nope" in result.reason


def test_json_block_decision(tmp_path):
    cmd = _script(tmp_path, "json_block.py",
                 "import json\nprint(json.dumps({'decision': 'block', 'reason': 'secrets dir is off limits'}))\n")
    runner = HookRunner({"PreToolUse": [{"command": cmd}]}, tmp_path, SHELL)
    result = runner.run("PreToolUse", {"tool": "read_file"}, name="read_file")
    assert result.decision == "block" and "secrets dir" in result.reason


def test_additional_context_is_returned(tmp_path):
    cmd = _script(tmp_path, "context.py",
                 "import json\nprint(json.dumps({'additionalContext': 'remember: this repo uses tabs, not spaces'}))\n")
    runner = HookRunner({"UserPromptSubmit": [{"command": cmd}]}, tmp_path, SHELL)
    result = runner.run("UserPromptSubmit", {"prompt": "fix indentation"})
    assert bool(result) and "tabs, not spaces" in result.additional_context


def test_matcher_filters_by_tool_name(tmp_path):
    cmd = _script(tmp_path, "block.py", "import json\nprint(json.dumps({'decision': 'block', 'reason': 'blocked'}))\n")
    runner = HookRunner({"PreToolUse": [{"matcher": "shell", "command": cmd}]}, tmp_path, SHELL)
    assert runner.run("PreToolUse", {}, name="read_file").decision == "allow"
    assert runner.run("PreToolUse", {}, name="shell").decision == "block"


def test_hook_receives_the_payload_on_stdin(tmp_path):
    out_file = tmp_path / "seen.json"
    cmd = _script(tmp_path, "capture.py",
                 f"import sys, json\n"
                 f"data = json.loads(sys.stdin.read())\n"
                 f"json.dump(data, open(r'{out_file}', 'w'))\n")
    runner = HookRunner({"PreToolUse": [{"command": cmd}]}, tmp_path, SHELL)
    runner.run("PreToolUse", {"tool": "shell", "args": {"command": "ls"}}, name="shell")
    seen = json.loads(out_file.read_text())
    assert seen["event"] == "PreToolUse" and seen["args"]["command"] == "ls"


def test_first_block_wins_and_stops_running_later_hooks(tmp_path):
    marker = tmp_path / "ran.txt"
    block_cmd = _script(tmp_path, "first.py", "import json\nprint(json.dumps({'decision': 'block', 'reason': 'first'}))\n")
    second_cmd = _script(tmp_path, "second.py", f"open(r'{marker}', 'w').write('ran')\n")
    runner = HookRunner({"PreToolUse": [{"command": block_cmd}, {"command": second_cmd}]}, tmp_path, SHELL)
    result = runner.run("PreToolUse", {}, name="shell")
    assert result.decision == "block" and result.reason == "first"
    assert not marker.exists()


def test_timeout_is_treated_as_allow_not_a_crash(tmp_path):
    cmd = _script(tmp_path, "slow.py", "import time\ntime.sleep(5)\n")
    runner = HookRunner({"PreToolUse": [{"command": cmd, "timeout": 1}]}, tmp_path, SHELL)
    result = runner.run("PreToolUse", {}, name="shell")
    assert bool(result)  # times out -> allow, does not raise
