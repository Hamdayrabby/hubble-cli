import pytest

import hubble.keystore as keystore


@pytest.fixture(autouse=True)
def _no_real_credential_store(monkeypatch):
    """Never read or write the developer's real OS keychain from a test. Tests that exercise
    the keychain path patch keystore._backend with an in-memory fake themselves."""
    monkeypatch.setattr(keystore, "_disabled", True)
