import pytest

import hubble.keystore as keystore


@pytest.fixture(autouse=True)
def _no_real_credential_store(monkeypatch):
    """Never read or write the developer's real OS keychain from a test. Tests that exercise
    the keychain path patch keystore._backend with an in-memory fake themselves."""
    monkeypatch.setattr(keystore, "_disabled", True)


@pytest.fixture(autouse=True)
def _private_stats_file(monkeypatch, tmp_path):
    """Usage stats from test runs go to a temp file, never the developer's ~/.hubble/stats.jsonl."""
    import hubble.stats as stats
    monkeypatch.setattr(stats, "STATS_FILE", tmp_path / "stats.jsonl")
