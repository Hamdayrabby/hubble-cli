import json
import time

import hubble.update as update


def test_version_compare():
    assert update.is_newer("4.1.0", "4.0.0")
    assert update.is_newer("4.10.0", "4.9.9")
    assert not update.is_newer("4.1.0", "4.1.0")
    assert not update.is_newer("4.0.9", "4.1.0")
    assert not update.is_newer("4.2.0rc1", "4.2.0")
    assert update.is_newer("4.2.0", "4.2.0rc1")
    assert update.is_newer("5", "4.9")


def test_notice_shown_once_when_newer(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "__version__", "4.0.0")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: "4.1.0")
    monkeypatch.delenv("HUBBLE_NO_UPDATE_CHECK", raising=False)
    c = update.UpdateChecker()
    c.start()
    c._thread.join(5)
    notice = c.pending_notice()
    assert "4.0.0 → 4.1.0" in notice and ("pipx upgrade" in notice or "-U hubble-cli" in notice)
    assert c.pending_notice() is None  # only once
    assert json.loads((tmp_path / "c.json").read_text())["latest"] == "4.1.0"


def test_no_notice_when_current(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: update.__version__)
    c = update.UpdateChecker()
    c.start()
    if c._thread:
        c._thread.join(5)
    assert c.pending_notice() is None


def test_fresh_cache_skips_network(monkeypatch, tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"latest": "99.0.0", "checked": time.time()}), encoding="utf-8")
    monkeypatch.setattr(update, "CACHE_FILE", cache)

    def boom(timeout=4.0):
        raise AssertionError("should not hit the network with a fresh cache")
    monkeypatch.setattr(update, "_fetch_latest", boom)
    c = update.UpdateChecker()
    c.start()
    assert c._thread is None and "99.0.0" in c.pending_notice()


def test_disabled_by_setting_or_env(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: "99.0.0")
    c = update.UpdateChecker(enabled=False)
    c.start()
    assert c.pending_notice() is None
    monkeypatch.setenv("HUBBLE_NO_UPDATE_CHECK", "1")
    c = update.UpdateChecker()
    c.start()
    assert c.pending_notice() is None


def test_network_failure_is_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "CACHE_FILE", tmp_path / "c.json")
    monkeypatch.setattr(update, "_fetch_latest", lambda timeout=4.0: None)
    c = update.UpdateChecker()
    c.start()
    c._thread.join(5)
    assert c.pending_notice() is None
