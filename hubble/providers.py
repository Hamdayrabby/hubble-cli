"""Multiple OpenAI-compatible providers.

The built-in "hubble" provider comes from settings/.env, and connects to the AIHub gateway (or
another OpenAI-compatible base URL you set) by default. Extra providers added with
`/provider add` live in ~/.hubble/providers.json (API keys are stored there in plain text,
like a .env file). Each provider has its own model scan file.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from hubble.models import SCAN_FILE, all_models
from hubble.provider import normalize_base_url
from hubble.settings import HOME_DIR

DEFAULT_PROVIDER = "hubble"
# Renamed from "aihub" when this CLI was renamed to Hubble; old session files may still record it.
LEGACY_DEFAULT_PROVIDER = "aihub"
PROVIDERS_FILE = HOME_DIR / "providers.json"
NAME_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

KNOWN_EXAMPLES = [
    ("openrouter", "https://openrouter.ai/api/v1"),
    ("groq", "https://api.groq.com/openai/v1"),
    ("openai", "https://api.openai.com/v1"),
    ("mistral", "https://api.mistral.ai/v1"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("ollama", "http://localhost:11434/v1"),
]


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    check_models: bool = True  # probe each model for availability (costs one tiny request per model)


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
        out[DEFAULT_PROVIDER] = ProviderConfig(DEFAULT_PROVIDER, normalize_base_url(settings["base_url"]),
                                               settings["api_key"], True)
    for name, entry in _read_file().items():
        if isinstance(entry, dict) and entry.get("base_url") and entry.get("api_key"):
            out[name] = ProviderConfig(name, normalize_base_url(entry["base_url"]), entry["api_key"],
                                       bool(entry.get("check_models", True)))
    return out


def save_provider(cfg: ProviderConfig):
    data = _read_file()
    data[cfg.name] = {"base_url": cfg.base_url, "api_key": cfg.api_key, "check_models": cfg.check_models}
    _write_file(data)


def remove_provider(name: str) -> bool:
    data = _read_file()
    if name not in data:
        return False
    del data[name]
    _write_file(data)
    path = scan_file(name)
    if path.exists():
        path.unlink()
    return True


def scan_file(name: str) -> Path:
    return SCAN_FILE if name == DEFAULT_PROVIDER else HOME_DIR / "models" / f"{name}.json"


def verify(base_url: str, api_key: str, timeout: float = 20.0) -> Tuple[bool, str, List[str]]:
    """Check the endpoint and key by listing models. Returns (ok, message, model_ids)."""
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


def save_listing(name: str, base_url: str, ids: List[str]):
    """Record the model list without probing, so the picker can show models right away."""
    path = scan_file(name)
    existing: Dict[str, Any] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    existing.update({"base_url": base_url, "all_ids": ids})
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
    checked = {r["model"] for r in data.get("all_results", []) if r.get("model")}
    out = [{"model": m, "category": "Available", "latency_ms": w.get("latency_ms"), "available": True,
           "context_length": w.get("context_length")} for m, w in working.items()]
    for m in data.get("all_ids", []):
        if m not in working:
            out.append({"model": m, "category": "Unavailable" if m in checked else "Not checked",
                        "latency_ms": None, "available": False if m in checked else None})
    return out


def resolve_fallback(settings: Dict[str, Any], providers: Dict[str, "ProviderConfig"], provider: str,
                     model: str) -> Optional[Tuple[str, str]]:
    """Pick (provider, model) to retry with when `model` on `provider` is rate limited or down.

    Order: a fallback set for this provider (/fallback), then fallback_model if this provider
    has it, then the fastest verified model on the same provider, then fallback_model on the
    built-in hubble provider. Returns None when fallback is off or nothing suitable exists.
    """
    per_provider = (settings.get("fallback_models") or {}).get(provider)
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
