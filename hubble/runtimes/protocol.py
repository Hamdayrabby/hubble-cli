"""RuntimeAdapter contract: launch and supervise an external coding harness.

A runtime is a complete CLI application (Muse Code, Codex, Antigravity, ...)
with its own loop, tools, permissions and sessions. It is NOT a model provider:
provider-backed subagents keep living in hubble/agent.py and must not be
reimplemented here. Adapters build argv arrays (never shell strings) and spawn
with an explicit environment allowlist.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Protocol, runtime_checkable

# Only these variables may cross into a child harness by default. Adapters may
# add harness-specific names (for example its HOME/config dir) explicitly.
DEFAULT_ENV_ALLOWLIST = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "WINDIR",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "TERM", "SHELL", "USER",
)


class RuntimeAdapterError(Exception):
    pass


class RuntimeUnavailable(RuntimeAdapterError):
    """Executable missing, unusable, or authentication not configured."""


class UnsupportedCapability(RuntimeAdapterError):
    """The runtime genuinely cannot do this; callers must not fake it."""


@dataclass
class RuntimeCapabilities:
    one_shot: bool = True
    interactive: bool = False
    json_stream: bool = False
    resume: bool = False
    cancel: bool = True
    worktree_flag: bool = False
    model_override: bool = False
    effort_override: bool = False
    approval_controls: bool = False
    sandbox_modes: List[str] = field(default_factory=list)

    def supports(self, requirement: str) -> bool:
        return bool(getattr(self, requirement, False))


@dataclass
class DetectResult:
    runtime_id: str
    available: bool
    executable: str = ""
    version: str = ""
    diagnostics: List[str] = field(default_factory=list)


@dataclass
class RunSpec:
    prompt: str
    cwd: Path
    task_id: str = ""
    run_id: str = ""
    model: str = ""
    effort: str = ""
    timeout_s: float = 600.0
    permission_profile: str = ""
    env_allowlist: List[str] = field(default_factory=lambda: list(DEFAULT_ENV_ALLOWLIST))
    env_extra: Dict[str, str] = field(default_factory=dict)
    extra_args: List[str] = field(default_factory=list)

    def child_env(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        src = dict(os.environ if base is None else base)
        env = {k: src[k] for k in self.env_allowlist if k in src}
        env.update(self.env_extra)
        return env


@dataclass
class RunHandle:
    runtime_id: str
    run_id: str
    task_id: str = ""
    pid: int = 0
    session_ref: str = ""
    proc: Any = None
    cancelled: bool = False


@runtime_checkable
class RuntimeAdapter(Protocol):
    runtime_id: str

    def detect(self) -> DetectResult: ...
    def capabilities(self) -> RuntimeCapabilities: ...
    def start(self, spec: RunSpec) -> RunHandle: ...
    def stream(self, handle: RunHandle) -> Iterator[Any]: ...
    def send(self, handle: RunHandle, message: str) -> None: ...
    def cancel(self, handle: RunHandle) -> str: ...
    def resume(self, session_ref: str, spec: RunSpec) -> RunHandle: ...
    def close(self, handle: RunHandle) -> None: ...
