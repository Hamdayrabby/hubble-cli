"""Cost/quality routing: easy work on a fast, cheap model; hard work on a strong one.

Configured with `/route` (saved as settings["routing"] = {"fast": spec, "strong": spec}), where a
spec is "model" (current provider) or "provider:model".

  - Each prompt is classified from what it asks for: questions, explanations, lookups and short
    messages go to fast; changes, fixes, refactors, debugging, long or code-heavy prompts go to
    strong. A leading "!" on the prompt is not used (that is a shell command); "/route" shows
    the last decision and why.
  - Read-only sub-agents (research) default to fast; edit sub-agents stay on strong.
  - Escalation: if the fast model struggles mid-task (an empty reply, repeated tool errors or
    unparseable tool calls), the rest of that task runs on strong.

The classifier is a transparent keyword heuristic on purpose: it costs nothing, never calls a
model, and its reasons are shown, so it is easy to predict and to override (/model or /route off).
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

STRONG_WORDS = re.compile(
    r"\b(fix|implement|refactor|rewrite|add|build|create|write|migrate|debug|optimi[sz]e|port|design|"
    r"architect|integrate|upgrade|change|modify|update|delete|remove|rename|test[s]?|feature|bug|"
    r"failing|error|crash|broken|security|performance|race|deadlock|leak)\b", re.I)
FAST_WORDS = re.compile(
    r"\b(what|where|which|who|when|why|how does|how do|explain|show|list|find|search|look up|summari[sz]e|"
    r"describe|tell me|meaning|difference|compare|check if|is there|does it|translate)\b", re.I)
CODE_HINT = re.compile(r"```|Traceback|Exception|^\s+at |error:|\.py\b|\.ts\b|\.js\b|\.go\b|\.rs\b|\.java\b",
                       re.I | re.M)


@dataclass
class Route:
    tier: str                 # "fast" | "strong"
    reasons: List[str]


def classify(prompt: str) -> Route:
    text = prompt or ""
    words = len(text.split())
    strong = [m.group(0).lower() for m in STRONG_WORDS.finditer(text)]
    fast = [m.group(0).lower() for m in FAST_WORDS.finditer(text)]
    reasons: List[str] = []
    score = 0
    if strong:
        score += 2 * len(set(strong))
        reasons.append("asks for " + ", ".join(sorted(set(strong))[:3]))
    if fast:
        score -= len(set(fast))
        reasons.append("asks " + ", ".join(sorted(set(fast))[:3]))
    if CODE_HINT.search(text):
        score += 2
        reasons.append("contains code or an error trace")
    if words > 120:
        score += 2
        reasons.append(f"long prompt ({words} words)")
    elif words <= 12 and not strong:
        score -= 1
        reasons.append("short message")
    tier = "strong" if score > 0 else "fast"
    return Route(tier, reasons or ["no strong signal; defaulting to fast"])


def parse_spec(spec: str, default_provider: str) -> Tuple[str, str]:
    """'provider:model' or 'model' -> (provider, model). Model ids may contain '/'."""
    spec = (spec or "").strip()
    if ":" in spec and not spec.startswith(("http:", "https:")):
        prov, _, model = spec.partition(":")
        if prov and model and "/" not in prov:
            return prov, model
    return default_provider, spec


def routing_config(settings: Dict) -> Optional[Dict[str, str]]:
    cfg = settings.get("routing") or {}
    if not isinstance(cfg, dict) or cfg.get("enabled") is False:
        return None
    if cfg.get("fast") and cfg.get("strong"):
        return {"fast": str(cfg["fast"]), "strong": str(cfg["strong"])}
    return None


class Struggle:
    """Counts the signs that a (fast) model is out of its depth during one task."""

    def __init__(self):
        self.empty = 0
        self.tool_errors = 0
        self.bad_args = 0

    def note_turn(self, text: str, tool_calls: int):
        if not text.strip() and not tool_calls:
            self.empty += 1

    def note_tool(self, output: str, is_error: bool):
        if not is_error:
            return
        if output.startswith("Error: Tool arguments are not a valid JSON") or "missing required argument" in output:
            self.bad_args += 1
        elif not output.startswith(("The user denied", "Permission denied", "Blocked by hook")):
            self.tool_errors += 1

    def reason(self) -> Optional[str]:
        if self.empty >= 1:
            return "it returned an empty reply"
        if self.bad_args >= 2:
            return "it made malformed tool calls"
        if self.tool_errors >= 4:
            return "its tool calls kept failing"
        return None
