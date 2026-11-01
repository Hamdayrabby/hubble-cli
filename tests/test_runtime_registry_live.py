"""Detection-only tests for the live runtime registry.

Never spawns a real CLI: no start()/stream() calls, no network, no API keys.
Real-adapter detection must degrade gracefully (available=False with
diagnostics) when executables are missing.
"""

import shutil

import pytest

from hubble.runtimes import default_registry
from hubble.runtimes.protocol import DetectResult
from hubble.runtimes.registry import NoRuntimeAvailable


def _real_adapters(reg):
    return [a for rid, a in ((r, reg.get(r)) for r in reg.ids())
            if rid != "fake" and a is not None]


def test_default_registry_with_fake_contains_fake():
    reg = default_registry(include_fake=True)
    assert "fake" in reg.ids()
    assert reg.get("fake") is not None


def test_real_adapter_detect_never_raises_when_cli_missing(monkeypatch):
    reg = default_registry(include_fake=True)
    reals = _real_adapters(reg)
    if not reals:
        pytest.skip("no real adapters registered yet")
    # Simulate every CLI missing: PATH empty and no executable lookup hits.
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    # Adapters bind the helpers by from-import, so patch each adapter
    # module's own namespace (patching _proc alone does not intercept).
    import importlib
    for mod_name in ("muse_code", "codex", "antigravity", "opencode",
                     "command_code", "claude_code"):
        try:
            mod = importlib.import_module(f"hubble.runtimes.adapters.{mod_name}")
        except Exception:
            continue
        if hasattr(mod, "find_executable"):
            monkeypatch.setattr(mod, "find_executable", lambda *a, **k: "")
        if hasattr(mod, "probe_version"):
            monkeypatch.setattr(mod, "probe_version", lambda *a, **k: "")
    for adapter in reals:
        result = adapter.detect()  # must not raise
        assert isinstance(result, DetectResult)
        assert result.runtime_id
        assert result.available is False


def test_require_resume_returns_capable_or_names_reasons():
    reg = default_registry(include_fake=True)
    try:
        adapter = reg.require(["resume"])
    except NoRuntimeAvailable as exc:
        assert "resume" in str(exc)
    else:
        assert adapter.capabilities().supports("resume")
