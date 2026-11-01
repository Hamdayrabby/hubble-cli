"""REA connector tests: parsing and config shape are offline; live is opt-in."""

import json
import shutil

import pytest

from hubble.repo_intel import rea as rea_mod


def test_mcp_entry_matches_hubble_config_shape():
    from hubble.mcp import MCPServerConfig
    entry = {"name": "rea", **{"command": ["rea", "--mcp"], "env": {}}}
    cfg = MCPServerConfig(**entry)
    assert cfg.command[-1] == "--mcp"


def test_mcp_server_entry_needs_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(rea_mod, "find_rea", lambda *a, **k: "")
    with pytest.raises(FileNotFoundError):
        rea_mod.mcp_server_entry()


def test_to_evidence_labels_rea_source():
    ev = rea_mod.to_evidence({"path": "app.exe", "line": 10, "symbol": "main",
                              "note": "import table"})
    assert ev["path"] == "app.exe" and ev["start_line"] == 10
    assert ev["note"].startswith("rea:") and ev["confidence"] == "low"


def test_capabilities_parses_json(monkeypatch):
    class Proc:
        returncode = 0
        stdout = json.dumps({"providers": ["ghidra"]})
        stderr = ""
    monkeypatch.setattr(rea_mod.subprocess, "run", lambda *a, **k: Proc())
    assert rea_mod.capabilities(executable="rea") == {"providers": ["ghidra"]}


@pytest.mark.skipif(shutil.which("rea") is None and shutil.which("rea-agents") is None,
                    reason="rea not installed")
def test_live_rea_detect_reports_version():
    det = rea_mod.detect()
    assert det.available is True and det.executable
