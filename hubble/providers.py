"""Multiple OpenAI-compatible providers.

The built-in "aihub" provider comes from settings/.env, and connects to the AIHub gateway (or
another OpenAI-compatible base URL you set) by default. Extra providers added with
`/provider add` live in ~/.hubble/providers.json; their API keys go to the OS credential store
(see keystore.py), or into that file in plain text only where no store exists. Each provider
has its own model scan file.
"""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from hubble import keystore
from hubble.models import SCAN_FILE, all_models
from hubble.provider import normalize_base_url
from hubble.settings import HOME_DIR

# The built-in provider is the AIHub gateway (HUBBLE_API_KEY / HUBBLE_BASE_URL), so it is named
# after it. Versions up to 4.1 called it "hubble"; saved settings, sessions and /fallback choices
# that still say "hubble" map to it.
DEFAULT_PROVIDER = "aihub"
LEGACY_DEFAULT_PROVIDER = "hubble"
PROVIDERS_FILE = HOME_DIR / "providers.json"
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

# (name, base URL, API kind). "anthropic" = Claude's native Messages API; everything else speaks
# the OpenAI chat-completions API.
KNOWN_EXAMPLES = [
    ("anthropic", "https://api.anthropic.com", "anthropic"),
    ("openai", "https://api.openai.com/v1", "openai"),
    ("openrouter", "https://openrouter.ai/api/v1", "openai"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "openai"),
    ("groq", "https://api.groq.com/openai/v1", "openai"),
    ("mistral", "https://api.mistral.ai/v1", "openai"),
    ("deepseek", "https://api.deepseek.com/v1", "openai"),
    ("xai", "https://api.x.ai/v1", "openai"),
    ("together", "https://api.together.xyz/v1", "openai"),
    ("fireworks", "https://api.fireworks.ai/inference/v1", "openai"),
    ("cerebras", "https://api.cerebras.ai/v1", "openai"),
    ("nvidia", "https://integrate.api.nvidia.com/v1", "openai"),
    ("moonshot", "https://api.moonshot.ai/v1", "openai"),
    ("ollama", "http://localhost:11434/v1", "openai"),
    ("lmstudio", "http://localhost:1234/v1", "openai"),
]
KINDS = ("openai", "anthropic")


def detect_kind(base_url: str) -> str:
    from urllib.parse import urlparse
    return "anthropic" if (urlparse(base_url).hostname or "") == "api.anthropic.com" else "openai"


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    check_models: bool = True  # probe each model for availability (costs one tiny request per model)
    kind: str = "openai"       # openai | anthropic


def make_client(cfg: "ProviderConfig"):
    """The right API client for a provider's kind, tagged with the provider's name (for stats)."""
    if cfg.kind == "anthropic":
        from hubble.anthropic_provider import AnthropicProvider
        client = AnthropicProvider(cfg.base_url, cfg.api_key)
    else:
        from hubble.provider import OpenAICompatProvider
        client = OpenAICompatProvider(cfg.base_url, cfg.api_key)
    client.hubble_name = cfg.name
    return client


def _read_file() -> Dict[str, Any]:
    try:
        data = json.loads(PROVIDERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_file(data: Dict[str, Any]):
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROVIDERS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(PROVIDERS_FILE)


def load_providers(settings: Dict[str, Any]) -> Dict[str, ProviderConfig]:
    out: Dict[str, ProviderConfig] = {}
    if settings.get("api_key"):
        url = normalize_base_url(settings["base_url"])
        out[DEFAULT_PROVIDER] = ProviderConfig(DEFAULT_PROVIDER, url, settings["api_key"], True, detect_kind(url))
    for name, entry in _read_file().items():
        if isinstance(entry, dict) and entry.get("base_url") and entry.get("api_key"):
            key = keystore.resolve(entry["api_key"])
            if not key:
                continue  # key lives in a credential store that is not reachable here
            url = normalize_base_url(entry["base_url"])
            kind = entry.get("kind") if entry.get("kind") in KINDS else detect_kind(url)
            out[name] = ProviderConfig(name, url, key, bool(entry.get("check_models", kind != "anthropic")), kind)
    # A standard ANTHROPIC_API_KEY in the environment is picked up as a ready-made Claude provider.
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env_key and not any(c.kind == "anthropic" for c in out.values()) and "anthropic" not in out:
        base = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com"
        out["anthropic"] = ProviderConfig("anthropic", normalize_base_url(base), env_key, False, "anthropic")
    return out


def save_provider(cfg: ProviderConfig):
    """The key goes to the OS credential store when there is one; plain text only as a fallback."""
    data = _read_file()
    stored = keystore.store(cfg.name, cfg.api_key) or cfg.api_key
    data[cfg.name] = {"base_url": cfg.base_url, "api_key": stored, "check_models": cfg.check_models,
                      "kind": cfg.kind}
    _write_file(data)


def secure_existing_keys() -> Tuple[int, int]:
    """Move plain-text keys in providers.json into the credential store. Returns (moved, left)."""
    data = _read_file()
    moved = left = 0
    for name, entry in data.items():
        if not isinstance(entry, dict) or not entry.get("api_key") or keystore.is_ref(entry["api_key"]):
            continue
        ref = keystore.store(name, entry["api_key"])
        if ref:
            entry["api_key"] = ref
            moved += 1
        else:
            left += 1
    if moved:
        _write_file(data)
    return moved, left


def remove_provider(name: str) -> bool:
    data = _read_file()
    if name not in data:
        return False
    keystore.delete((data[name] or {}).get("api_key", ""))
    del data[name]
    _write_file(data)
    path = scan_file(name)
    if path.exists():
        path.unlink()
    return True


def scan_file(name: str) -> Path:
    return SCAN_FILE if name == DEFAULT_PROVIDER else HOME_DIR / "models" / f"{name}.json"


def verify(base_url: str, api_key: str, timeout: float = 20.0, kind: str = "openai") -> Tuple[bool, str, List[str]]:
    """Check the endpoint and key by listing models. Returns (ok, message, model_ids)."""
    if kind == "anthropic":
        from hubble.anthropic_provider import AnthropicProvider
        from hubble.provider import ProviderError
        try:
            models = AnthropicProvider(base_url, api_key, max_retries=1).list_models()
        except ProviderError as e:
            status = str(e).split(":")[0]
            if status in ("HTTP 401", "HTTP 403"):
                return False, f"the API key was rejected ({status})", []
            return False, f"could not list models: {e}"[:200], []
        ids = [m["id"] for m in models]
        return (True, f"connected, {len(ids)} Claude models available", ids) if ids else \
            (False, "connected, but the key has access to no models", [])
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = httpx.get(f"{base_url}/models", headers=headers, timeout=timeout)
    except httpx.HTTPError as e:
        return False, f"could not connect: {type(e).__name__}: {e}"[:200], []
    if resp.status_code in (401, 403):
        return False, f"the API key was rejected (HTTP {resp.status_code})", []
    if resp.status_code == 404:
        return False, (f"no /models endpoint at {base_url} (HTTP 404). Check the base URL; "
                       "it usually ends in /v1"), []
    if resp.status_code != 200:
        body = resp.text.strip()
        detail = "" if body.startswith("<") else f": {body[:150]}"
        return False, f"HTTP {resp.status_code} from {base_url}/models{detail}", []
    try:
        data = resp.json()
    except ValueError:
        return False, f"{base_url}/models did not return JSON; is this an OpenAI-compatible base URL?", []
    entries = data.get("data", data) if isinstance(data, dict) else data
    ids = [m["id"] for m in entries if isinstance(m, dict) and m.get("id")] if isinstance(entries, list) else []
    if not ids:
        return False, "connected, but the provider listed no models", []
    return True, f"connected, {len(ids)} models listed", ids


def save_listing(name: str, base_url: str, ids: List[str], available: Optional[List[Dict[str, Any]]] = None):
    """Record the model list without probing, so the picker can show models right away.
    `available`: models known to work without a probe (e.g. Claude's /v1/models only lists
    models the key can use), as [{model, context_length}]."""
    path = scan_file(name)
    existing: Dict[str, Any] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    existing.update({"base_url": base_url, "all_ids": ids})
    if available is not None:
        existing["working_models"] = [{"model": m["model"], "latency_ms": None, "owner": "anthropic", "sample": "",
                                       "context_length": m.get("context_length")} for m in available]
        existing["timestamp"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    existing.setdefault("working_models", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")


def provider_models(name: str) -> List[Dict[str, Any]]:
    """Model entries for one provider: model, category, latency_ms, available (None = not checked)."""
    if name == DEFAULT_PROVIDER:
        return all_models()
    try:
        data = json.loads(scan_file(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    working = {m["model"]: m for m in data.get("working_models", []) if m.get("model")}
    results = {r["model"]: r for r in data.get("all_results", []) if isinstance(r, dict) and r.get("model")}
    out = [{"model": m, "category": "Available", "latency_ms": w.get("latency_ms"), "available": True,
           "context_length": w.get("context_length")} for m, w in working.items()]
    for m in data.get("all_ids", []):
        if m in working:
            continue
        r = results.get(m)
        if r is None:
            out.append({"model": m, "category": "Not checked", "latency_ms": None, "available": None})
        elif r.get("available") is None:
            # Rate limited / timed out during the scan: unknown, not broken.
            out.append({"model": m, "category": "Unknown", "latency_ms": None, "available": None,
                        "note": r.get("reason", "")})
        else:
            out.append({"model": m, "category": "Unavailable", "latency_ms": None, "available": False})
    return out


def resolve_fallback(settings: Dict[str, Any], providers: Dict[str, "ProviderConfig"], provider: str,
                     model: str) -> Optional[Tuple[str, str]]:
    """Pick (provider, model) to retry with when `model` on `provider` is rate limited or down.

    Order: a fallback set for this provider (/fallback), then fallback_model if this provider
    has it, then the fastest verified model on the same provider, then fallback_model on the
    built-in aihub provider. Returns None when fallback is off or nothing suitable exists.
    """
    per_map = settings.get("fallback_models") or {}
    per_provider = per_map.get(provider)
    if per_provider is None and provider == DEFAULT_PROVIDER:
        per_provider = per_map.get(LEGACY_DEFAULT_PROVIDER)  # saved before the rename
    if per_provider == "off":
        return None
    if per_provider and per_provider != model:
        return provider, per_provider
    default = settings.get("fallback_model")
    if not default and not per_provider:
        return None
    known = provider_models(provider)
    names = {m["model"] for m in known if m.get("available") is not False}
    if default and default != model and default in names:
        return provider, default
    verified = sorted((m for m in known if m.get("available") and m["model"] != model and m.get("latency_ms")),
                      key=lambda m: m["latency_ms"])
    if verified:
        return provider, verified[0]["model"]
    if default and provider != DEFAULT_PROVIDER and DEFAULT_PROVIDER in providers:
        if default in {m["model"] for m in provider_models(DEFAULT_PROVIDER) if m.get("available") is not False}:
            return DEFAULT_PROVIDER, default
    return None


def normalize_provider_name(name: Optional[str]) -> str:
    """Map a possibly-legacy provider slug from an old session file to the current one."""
    return DEFAULT_PROVIDER if not name or name == LEGACY_DEFAULT_PROVIDER else name
