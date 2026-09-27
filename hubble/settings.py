"""Layered settings.

Precedence (later wins): built-in defaults, ~/.hubble/settings.json,
<project>/.hubble/settings.json, <project>/.hubble/settings.local.json, CLI flags.
Credentials come from HUBBLE_* environment variables or .env files (the older AIHUB_* names,
from before this CLI was renamed, are still read as a fallback).
"""

import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from hubble.models import DEFAULT_MODEL, PROJECT_DIR

HOME_DIR = Path(os.environ.get("HUBBLE_HOME", Path.home() / ".hubble"))

DEFAULTS: Dict[str, Any] = {
    "model": DEFAULT_MODEL,
    "base_url": "https://aihub.071129.xyz/v1",
    "temperature": 0.3,
    "max_tokens": 8192,
    "context_window": 128000,
    "auto_compact_ratio": 0.8,
    "max_turns": 40,
    "permission_mode": "default",
    "persona": "code",
    "shell": "auto",
    "shell_timeout": 120,
    "additional_dirs": [],
    "allow_secret_files": False,
    "model_refresh_hours": 0,  # 0 = always check on startup; set hours to only recheck when stale
    "fallback_model": "codestral-latest",
    "show_reasoning": False,
    "web_tools": True,
    "web_search": {"engine": "auto"},
    "home_animation": True,
    "permissions": {"allow": [], "deny": []},
}


class ConfigError(Exception):
    pass


_ENV_PREFIXES = ("HUBBLE_", "AIHUB_")  # AIHUB_ is the pre-rename name, kept as a fallback


def _read_env_file(path: Path) -> Dict[str, str]:
    """Only HUBBLE_*/AIHUB_* keys are read, so other secrets in a .env never leak into os.environ."""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                if key.startswith(_ENV_PREFIXES):
                    out[key] = val.strip().strip("'\"")
    except OSError:
        pass
    return out


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f"Invalid settings file {path}: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"Settings file {path} must contain a JSON object")
    return data


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    for key, val in override.items():
        if key == "permissions" and isinstance(val, dict):
            perms = base.setdefault("permissions", {"allow": [], "deny": []})
            for kind in ("allow", "deny"):
                perms[kind] = list(perms.get(kind, [])) + list(val.get(kind, []))
        elif isinstance(val, dict) and isinstance(base.get(key), dict):
            base[key] = _merge(dict(base[key]), val)
        else:
            base[key] = val
    return base


# A cloned repository must not be able to loosen security or redirect the API key.
NEVER_FROM_PROJECT = {"base_url", "api_key"}
TRUSTED_ONLY = {"permission_mode", "allow_secret_files", "additional_dirs", "shell"}
TRUST_FILE = HOME_DIR / "trusted_folders.json"


def _trusted_folders() -> list:
    try:
        data = json.loads(TRUST_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def is_trusted(root: Path) -> bool:
    key = str(root.resolve()).lower()
    return any(key == str(t).lower() or key.startswith(str(t).lower().rstrip("\\/") + os.sep)
               for t in _trusted_folders())


def trust_folder(root: Path):
    folders = _trusted_folders()
    if str(root.resolve()) not in folders:
        folders.append(str(root.resolve()))
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    TRUST_FILE.write_text(json.dumps(folders, indent=2), encoding="utf-8")


def _filter_project(data: Dict[str, Any], trusted: bool, ignored: list) -> Dict[str, Any]:
    out = {}
    for key, val in data.items():
        if key in NEVER_FROM_PROJECT or (key in TRUSTED_ONLY and not trusted):
            ignored.append(key)
            continue
        if key == "permissions" and isinstance(val, dict) and not trusted and val.get("allow"):
            ignored.append("permissions.allow")
            val = {"deny": val.get("deny", [])}  # deny rules only tighten, so they always apply
        out[key] = val
    return out


def load_settings(root: Path, overrides: Optional[Dict[str, Any]] = None,
                  trusted: Optional[bool] = None) -> Dict[str, Any]:
    if trusted is None:
        trusted = is_trusted(root)
    settings = copy.deepcopy(DEFAULTS)
    settings = _merge(settings, _read_json(HOME_DIR / "settings.json"))
    ignored: list = []
    for path in (root / ".hubble" / "settings.json", root / ".hubble" / "settings.local.json"):
        settings = _merge(settings, _filter_project(_read_json(path), trusted, ignored))
    settings["_ignored_project_keys"] = sorted(set(ignored))
    settings = _merge(settings, {k: v for k, v in (overrides or {}).items() if v is not None})

    env: Dict[str, str] = {}
    for env_file in (HOME_DIR / ".env", PROJECT_DIR / ".env"):
        for k, v in _read_env_file(env_file).items():
            env.setdefault(k, v)
    env.update({k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES)})

    if not settings.get("api_key"):
        settings["api_key"] = env.get("HUBBLE_API_KEY") or env.get("AIHUB_API_KEY", "")
    if overrides is None or overrides.get("base_url") is None:
        settings["base_url"] = env.get("HUBBLE_BASE_URL") or env.get("AIHUB_BASE_URL") or settings["base_url"]
    return settings


def require_api_key(settings: Dict[str, Any]) -> str:
    key = settings.get("api_key") or ""
    if not key:
        raise ConfigError(
            "Missing HUBBLE_API_KEY. Set it in one of:\n"
            f"  {PROJECT_DIR / '.env'}\n"
            f"  {HOME_DIR / '.env'}\n"
            "  or the environment: $env:HUBBLE_API_KEY=\"...\" (PowerShell) / export HUBBLE_API_KEY=... (bash)"
        )
    return key


def save_user_setting(key: str, value: Any):
    """Persist one value in ~/.hubble/settings.json, keeping everything else in the file."""
    path = HOME_DIR / "settings.json"
    data = _read_json(path)
    data[key] = value
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
