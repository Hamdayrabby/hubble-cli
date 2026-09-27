"""Model registry: curated categories plus the latest scan from test_models.py."""

import json
from pathlib import Path
from typing import Any, Dict, List

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCAN_FILE = PROJECT_DIR / "available_models.json"

DEFAULT_MODEL = "codestral-latest"

MODEL_CATEGORIES = {
    "Coding Specialists": [
        "codestral-latest",
        "codestral-2508",
        "mistral-code-latest",
        "mistral-code-fim-latest",
    ],
    "Reasoning & Logic": [
        "nvidia/nemotron-3-super-120b-a12b",
        "intern-s2-preview",
        "intern-s1-mini",
        "intern-s1",
        "intern-s1-pro",
    ],
    "Fast Chat & General": [
        "ministral-14b-latest",
        "open-mistral-nemo",
        "ministral-8b-latest",
        "ministral-3b-latest",
        "mistral-tiny-latest",
        "qwen-8b",
    ],
    "Multimodal & Vision": [
        "meta/llama-3.2-11b-vision-instruct",
        "internvl3.5-latest",
        "internvl-latest",
    ],
}

RELIABLE_MODELS = [
    "codestral-latest",
    "codestral-2508",
    "mistral-code-latest",
    "mistral-code-fim-latest",
    "nvidia/nemotron-3-super-120b-a12b",
    "ministral-14b-latest",
    "open-mistral-nemo",
    "ministral-8b-latest",
    "intern-s2-preview",
    "intern-s1-mini",
]


def load_scanned_models(path: Path = SCAN_FILE) -> List[Dict[str, Any]]:
    """Working models found by the last `python test_models.py` run."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("working_models", [])
    except (OSError, ValueError):
        return []


def load_unknown_models(path: Path = SCAN_FILE) -> Dict[str, str]:
    """Models the last scan could not judge (rate limited, timed out, overloaded) -> reason.
    These are "unknown", not "unavailable": the model may work fine in a real chat."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            results = json.load(f).get("all_results", [])
    except (OSError, ValueError):
        return {}
    return {r["model"]: r.get("reason", "") for r in results
            if isinstance(r, dict) and r.get("model") and r.get("available") is None}


def _availability(name: str, scanned: Dict[str, Any], unknown: Dict[str, str], have_scan: bool):
    if not have_scan or name in unknown:
        return None
    return name in scanned


def all_models() -> List[Dict[str, Any]]:
    """Curated models first, then any extra scanned ones.

    Each entry has model, category, latency_ms and available (None when there is no scan yet,
    or when the scan hit only rate limits/timeouts for that model).
    """
    scanned = {m["model"]: m for m in load_scanned_models() if m.get("model")}
    unknown = load_unknown_models()
    have_scan = bool(scanned)
    out: List[Dict[str, Any]] = []
    seen = set()
    for category, names in MODEL_CATEGORIES.items():
        for name in names:
            out.append({"model": name, "category": category,
                        "latency_ms": scanned.get(name, {}).get("latency_ms"),
                        "available": _availability(name, scanned, unknown, have_scan),
                        "note": unknown.get(name, ""),
                        "context_length": scanned.get(name, {}).get("context_length")})
            seen.add(name)
    for name, info in scanned.items():
        if name not in seen:
            out.append({"model": name, "category": "Other Verified",
                        "latency_ms": info.get("latency_ms"), "available": True,
                        "context_length": info.get("context_length")})
    return out


def resolve_model(query: str, candidates: List[str]) -> List[str]:
    """Exact match wins, otherwise case-insensitive substring matches."""
    q = query.strip().lower()
    exact = [m for m in candidates if m.lower() == q]
    if exact:
        return exact
    return [m for m in candidates if q in m.lower()]
