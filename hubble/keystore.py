"""API keys in the OS credential store (Windows Credential Manager, macOS Keychain, Linux Secret
Service) via `keyring`, with plain-text files as the fallback where no store is available
(headless Linux, containers, CI).

providers.json keeps a reference ("keyring:<provider>") instead of the key itself.
"""

from typing import Optional

SERVICE = "hubble-cli"
REF_PREFIX = "keyring:"


def _backend():
    if _disabled:
        return None
    try:
        import keyring
        kr = keyring.get_keyring()
    except Exception:  # ImportError, or a broken backend configuration
        return None
    # keyring's "fail" and "null" backends (no real store on this machine) have priority <= 0.
    if getattr(kr, "priority", 1) <= 0:
        return None
    return keyring


_disabled = False  # tests and HUBBLE_NO_KEYRING=1 force the plain-text fallback


def _init():
    import os
    global _disabled
    _disabled = os.environ.get("HUBBLE_NO_KEYRING", "") not in ("", "0")


_init()


def available() -> bool:
    return _backend() is not None


def store(name: str, secret: str) -> Optional[str]:
    """Save a key; returns the reference to write in place of it, or None if there is no store."""
    kr = _backend()
    if kr is None:
        return None
    try:
        kr.set_password(SERVICE, name, secret)
    except Exception:
        return None
    return REF_PREFIX + name


def resolve(value: str) -> str:
    """A stored value back to the key. Plain keys pass through; a missing reference gives ''."""
    if not isinstance(value, str) or not value.startswith(REF_PREFIX):
        return value
    kr = _backend()
    if kr is None:
        return ""
    try:
        return kr.get_password(SERVICE, value[len(REF_PREFIX):]) or ""
    except Exception:
        return ""


def delete(value: str):
    if not isinstance(value, str) or not value.startswith(REF_PREFIX):
        return
    kr = _backend()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, value[len(REF_PREFIX):])
    except Exception:
        pass


def is_ref(value: str) -> bool:
    return isinstance(value, str) and value.startswith(REF_PREFIX)
