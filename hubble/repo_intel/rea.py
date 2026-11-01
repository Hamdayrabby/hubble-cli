"""Optional REA (Reverse Engineer Anything) connector for Repository Intelligence.

REA is an external specialist reverse-engineering tool, not Hubble's Python
analyzer. Everything here is optional: Hubble starts and works when REA is
absent. Verified against rea 6.7.0: ``rea --mcp`` serves stdio MCP
(207 tools / 6 prompts, ``rea mcp doctor`` healthy); ``rea doctor`` reports
no analysis engines on this machine, so binary targets are out of scope
until the user configures a provider.

NEVER send private/proprietary code to third-party services through REA
without explicit user approval.
"""

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REA_NAMES = ["rea", "rea-agents"]
REA_MCP_ARGV = ["--mcp"]


@dataclass
class READetect:
    available: bool
    executable: str = ""
    version: str = ""
    diagnostics: List[str] = field(default_factory=list)


def find_rea(extra_paths: Optional[List[Path]] = None) -> str:
    from hubble.runtimes.adapters._proc import find_executable
    return find_executable(REA_NAMES, extra_paths=extra_paths)


def detect(executable: str = "") -> READetect:
    exe = executable or find_rea()
    if not exe:
        return READetect(False, diagnostics=["rea not found on PATH"])
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True, text=True,
                              timeout=20, stdin=subprocess.DEVNULL)
        version = ((proc.stdout or proc.stderr) or "").strip().splitlines()
        version = version[0].strip() if version else ""
    except (OSError, subprocess.TimeoutExpired):
        return READetect(False, executable=exe,
                         diagnostics=["rea found but --version did not respond"])
    return READetect(True, executable=exe, version=version,
                     diagnostics=["optional specialist; absent providers limit binary targets "
                                  "(see `rea doctor`)"])


def mcp_server_entry(executable: str = "") -> Dict[str, Any]:
    """Entry for Hubble's ``mcp_servers`` settings: stdio ``rea --mcp``."""
    exe = executable or find_rea()
    if not exe:
        raise FileNotFoundError("rea not found; install rea-agents first")
    return {"command": [exe, *REA_MCP_ARGV], "env": {}}


def capabilities(executable: str = "", timeout_s: float = 60.0) -> Dict[str, Any]:
    """Safe, target-free probe: ``rea capabilities --format json``."""
    exe = executable or find_rea()
    if not exe:
        raise FileNotFoundError("rea not found; install rea-agents first")
    proc = subprocess.run([exe, "capabilities", "--format", "json"],
                          capture_output=True, text=True, timeout=timeout_s,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise RuntimeError(f"rea capabilities failed: {(proc.stderr or proc.stdout)[:300]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"rea capabilities returned non-JSON: {exc}") from exc
    return data if isinstance(data, dict) else {"capabilities": data}


def to_evidence(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Record REA output in RepoIntel evidence shape (source labelled)."""
    from hubble.repo_intel.evidence import Evidence
    return Evidence(path=entry.get("path", ""), start_line=int(entry.get("line", 0) or 0),
                    symbol=str(entry.get("symbol", "")),
                    evidence_type=entry.get("evidence_type", "search"),
                    confidence=entry.get("confidence", "low"),
                    note="rea:" + str(entry.get("note", ""))).to_dict()
