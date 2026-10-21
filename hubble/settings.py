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
    "base_url": "https://api.openai.com/v1",
    "theme": "terminal",
    "temperature": 0.3,
    "max_tokens": 8192,
    "context_window": 128000,   # only a compaction-trigger estimate, not a hard cap the code enforces
    "context_window_auto": True,  # use the model's real size when the provider publishes one (e.g. OpenRouter)
    "auto_compact_ratio": 0.8,
    "max_turns": 100,             # backstop only; runs also stop when they stop making progress
    "subagent_max_turns": 150,    # backstop for sub-agents
    "subagent_token_budget": 400000,   # a sub-agent that has used this many tokens writes its report
    "subagent_policy": "auto",    # "auto" | "explicit" | "off"
    "permission_mode": "default",
    "persona": "code",
    "shell": "auto",
    "shell_timeout": 120,
    "additional_dirs": [],
    "allow_external_reads": True,  # allow read_file/grep/glob to read existing files outside workspace
    # "auto": OS sandbox (macOS Seatbelt / Linux bubblewrap) where available, else off. Commands
    # can read anything but only write in the workspace and temp dirs. "native" | "docker" | "off".
    "shell_sandbox": "auto",
    "sandbox_writable": [],  # extra dirs the native sandbox may write to (e.g. "~/.cache/pip")
    "sandbox_image": "python:3.12-slim",
    "sandbox_memory": "1g",
    "sandbox_cpus": "2",
    "sandbox_network": True,
    "hooks": {},         # {"PreToolUse": [{"matcher": "shell", "command": "...", "timeout": 30}], ...}
    "mcp_servers": {},   # {"name": {"command": ["npx", "-y", "@modelcontextprotocol/server-x"], "env": {}}}
    "allow_secret_files": False,
    "model_refresh_hours": 0,  # 0 = always check on startup; set hours to only recheck when stale
    "fallback_model": "codestral-latest",
    "show_reasoning": False,
    "web_tools": True,
    "web_search": {"engine": "auto"},
    "home_animation": True,
    "update_check": True,  # tell me when a newer hubble-cli is on PyPI (checked at most once a day)
    "permissions": {"allow": [], "deny": []},
}


class ConfigError(Exception):
    pass


_ENV_PREFIXES = ("HUBBLE_", "AIHUB_")  # AIHUB_ is the pre-rename name, kept as a fallback
_ALLOWED_ENV_KEYS = ("BRAVE_API_KEY", "TAVILY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")


def _read_env_file(path: Path) -> Dict[str, str]:
    """Read HUBBLE_*/AIHUB_* variables and permitted search/provider keys from a .env file."""
    out: Dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                if key.startswith(_ENV_PREFIXES) or key in _ALLOWED_ENV_KEYS:
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
TRUSTED_ONLY = {"permission_mode", "allow_secret_files", "additional_dirs", "shell",
                "shell_sandbox", "sandbox_network", "sandbox_writable", "hooks", "mcp_servers"}
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
    from hubble.plugins import plugin_settings
    plugin_hooks, plugin_servers = plugin_settings(root, trusted)
    for event, entries in plugin_hooks.items():
        settings["hooks"] = {**settings.get("hooks", {}), event: list(settings.get("hooks", {}).get(event, [])) + entries}
    if plugin_servers:
        settings["mcp_servers"] = {**plugin_servers, **settings.get("mcp_servers", {})}
    settings = _merge(settings, {k: v for k, v in (overrides or {}).items() if v is not None})

    env: Dict[str, str] = {}
    for env_file in (HOME_DIR / ".env", PROJECT_DIR / ".env"):
        for k, v in _read_env_file(env_file).items():
            env.setdefault(k, v)
    env.update({k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES) or k in _ALLOWED_ENV_KEYS})

    for k in _ALLOWED_ENV_KEYS:
        if k in env and k not in os.environ:
            os.environ[k] = env[k]
    if env.get("TAVILY_API_KEY"):
        settings.setdefault("web_search", {})["tavily_api_key"] = env["TAVILY_API_KEY"]
    if env.get("BRAVE_API_KEY"):
        settings.setdefault("web_search", {})["brave_api_key"] = env["BRAVE_API_KEY"]

    if not settings.get("api_key"):
        settings["api_key"] = env.get("HUBBLE_API_KEY") or env.get("OPENAI_API_KEY") or env.get("AIHUB_API_KEY", "")
    if (overrides is None or overrides.get("model") is None) and env.get("HUBBLE_MODEL"):
        settings["model"] = env["HUBBLE_MODEL"]
    if overrides is None or overrides.get("base_url") is None:
        settings["base_url"] = env.get("HUBBLE_BASE_URL") or env.get("OPENAI_BASE_URL") or env.get("AIHUB_BASE_URL") or settings["base_url"]
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


def detect_context_window(model: str, default: int = 128000) -> int:
    """Detect model context window length from model name or identifier.

    Provides accurate window limits for frontier and open-weights models:
    - Gemini 1.5/2.5 Pro / Ultra: 2,000,000 tokens
    - Other Gemini: 1,000,000 tokens
    - Codestral: 256,000 tokens
    - Claude 3 / 3.5 / 3.7 / Opus: 200,000 tokens
    - GPT-4o, o1, Llama-3.1/3.2/3.3, Qwen-2.5, Mistral Large, DeepSeek-V3/R1: 128,000 tokens
    - DeepSeek-Coder: 64,000 tokens
    - 32k models (Ministral, Mistral-Nemo, InternLM, Phi-3): 32,768 tokens
    - 16k models (GPT-3.5, Llama-2, Codellama, Mistral-7B): 16,384 tokens
    - 8k models (legacy GPT-4): 8,192 tokens
    """
    if not model or not isinstance(model, str):
        return default
    m = model.lower()

    # 2M models
    if any(k in m for k in ("gemini-1.5-pro", "gemini-2.5-pro", "gemini-ultra")):
        return 2_000_000
    # 1M models
    if "gemini" in m:
        return 1_000_000
    # 256k models
    if "codestral" in m:
        return 256_000
    # 200k models
    if "claude" in m:
        return 200_000
    # 128k models
    if any(k in m for k in ("gpt-4o", "gpt-4-turbo", "o1", "o3", "llama-3.1", "llama-3.2", "llama-3.3",
                           "mistral-large", "qwen-2.5", "qwen2.5", "nemotron", "deepseek-v3", "deepseek-r1",
                           "deepseek-chat", "deepseek-reasoner")):
        return 128_000
    # 64k models
    if any(k in m for k in ("deepseek-coder", "deepseek")):
        return 64_000
    # 32k models
    if any(k in m for k in ("32k", "ministral", "mistral-nemo", "intern-s1", "intern-s2", "internvl", "phi-3")):
        return 32_768
    # 16k models
    if any(k in m for k in ("16k", "gpt-3.5", "llama-2", "codellama", "mistral-7b", "mistral-tiny")):
        return 16_384
    # 8k models
    if any(k in m for k in ("8k", "gpt-4-0314", "gpt-4-0613")):
        return 8_192
    if "gpt-4" in m and "32k" not in m:
        return 8_192

    return default
