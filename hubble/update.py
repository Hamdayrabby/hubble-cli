"""New-version notice: check PyPI in the background at most once a day and tell the user.

Never blocks startup and never fails loudly: no network, a slow PyPI or a broken cache just
means no notice. Off with `"update_check": false` in settings or HUBBLE_NO_UPDATE_CHECK=1.
"""

import json
import os
import re
import sys
import threading
import time
from typing import Optional, Tuple

import httpx

from hubble import __version__
from hubble.settings import HOME_DIR

PYPI_URL = "https://pypi.org/pypi/hubble-cli/json"
CACHE_FILE = HOME_DIR / "update_check.json"
CHECK_EVERY = 24 * 3600


def parse_version(v: str) -> Tuple[int, ...]:
    """'4.10.2' -> (4, 10, 2). Pre-releases ('4.2.0rc1') sort below their release."""
    m = re.match(r"^\s*(\d+(?:\.\d+)*)(.*)$", v or "")
    if not m:
        return (0,)
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums += (0,) * (3 - len(nums))
    return nums + ((-1,) if m.group(2).strip() else (0,))


def is_newer(latest: str, current: Optional[str] = None) -> bool:
    return parse_version(latest) > parse_version(current or __version__)


def upgrade_command() -> str:
    exe = sys.executable.replace("\\", "/").lower()
    if "/pipx/" in exe:
        return "pipx upgrade hubble-cli"
    return f'"{sys.executable}" -m pip install -U hubble-cli' if " " in sys.executable else \
        f"{sys.executable} -m pip install -U hubble-cli"


def _read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _fetch_latest(timeout: float = 4.0) -> Optional[str]:
    try:
        resp = httpx.get(PYPI_URL, timeout=timeout, headers={"User-Agent": f"hubble-cli/{__version__}"})
        if resp.status_code != 200:
            return None
        return str(resp.json()["info"]["version"])
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


class UpdateChecker:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and os.environ.get("HUBBLE_NO_UPDATE_CHECK", "") in ("", "0")
        self.latest: Optional[str] = None
        self.announced = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not self.enabled:
            return
        cache = _read_cache()
        if cache.get("latest") and time.time() - float(cache.get("checked", 0)) < CHECK_EVERY:
            self.latest = cache["latest"]  # fresh enough: no network at all
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="hubble-update-check")
        self._thread.start()

    def _run(self):
        latest = _fetch_latest()
        if latest is None:
            return
        self.latest = latest
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps({"latest": latest, "checked": time.time()}), encoding="utf-8")
        except OSError:
            pass

    def pending_notice(self) -> Optional[str]:
        """The notice text once, when a newer version is known and not yet shown."""
        if self.announced or not self.latest or not is_newer(self.latest):
            return None
        self.announced = True
        return (f"Update available: hubble {__version__} → {self.latest}. "
                f"Run: {upgrade_command()}")
