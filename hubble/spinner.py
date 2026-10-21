"""Animated activity indicator: pulsing glyph, rotating verbs and a shimmer sweeping over the text.

Used through rich.live.Live, which calls __rich__ on every refresh, so the animation is purely
time-based and costs nothing between frames.
"""

import random
import time
from typing import Optional

from rich.live import Live
from rich.text import Text

VERBS = [
    "Thinking", "Stargazing", "Orbiting", "Focusing the lens", "Aligning mirrors", "Charting the stars",
    "Scanning the cosmos", "Decoding signals", "Calculating trajectories", "Mapping nebulae", "Pondering",
    "Drifting through code", "Collecting photons", "Warping", "Consulting the stars", "Tuning the antenna",
    "Surveying galaxies", "Cogitating", "Tinkering", "Noodling", "Contemplating", "Launching probes",
]
GLYPHS = ["🔭", "✦", "✧", "⌾", "◎", "◇", "◈", "◆", "✦", "🔭"]

# Warm shimmer palette: base, near highlight, highlight.
BASE, NEAR, PEAK = "#d7875f", "#ffaf87", "bold #ffe7d7"
GLYPH_STYLE = "bold #ff875f"


class Shimmer:
    def __init__(self, label: Optional[str] = None, hint: str = "ctrl+c to interrupt", details: bool = True):
        self.start = time.time()
        self.details = details
        self.label = label
        self.hint = hint
        self.tokens = 0
        self._offset = random.randrange(len(VERBS))

    def set_label(self, label: Optional[str]):
        self.label = label

    def __rich__(self) -> Text:
        t = time.time() - self.start
        glyph = GLYPHS[int(t * 10) % len(GLYPHS)]
        words = (self.label or VERBS[(int(t / 3.5) + self._offset) % len(VERBS)]) + "…"
        sweep = len(words) + 10
        pos = (t * 16) % sweep - 5  # highlight position; runs off both ends for a pause between sweeps
        out = Text(f"{glyph} ", style=GLYPH_STYLE)
        for i, ch in enumerate(words):
            d = abs(i - pos)
            out.append(ch, style=PEAK if d < 1.2 else NEAR if d < 3 else BASE)
        if not self.details:
            return out
        details = [f"{int(t)}s"]
        if self.tokens:
            details.append(f"↓ {self.tokens:,} tokens")
        if self.hint:
            details.append(self.hint)
        out.append(f"  ({' · '.join(details)})", style="dim")
        return out

    def to_formatted_text(self):
        """Build formatted text tuples for prompt_toolkit with the exact Hubble shimmer wave."""
        t = time.time() - self.start
        glyph = GLYPHS[int(t * 10) % len(GLYPHS)]
        words = (self.label or VERBS[(int(t / 3.5) + self._offset) % len(VERBS)]) + "…"
        sweep = len(words) + 10
        pos = (t * 16) % sweep - 5
        frags = [("fg:#ff875f bold", f"{glyph} ")]
        for i, ch in enumerate(words):
            d = abs(i - pos)
            style = "fg:#ffe7d7 bold" if d < 1.2 else "fg:#ffaf87" if d < 3 else "fg:#d7875f"
            frags.append((style, ch))
        if self.details:
            details = [f"{int(t)}s"]
            if self.tokens:
                details.append(f"↓ {self.tokens:,} tokens")
            if self.hint:
                details.append(self.hint)
            frags.append(("fg:#6c6c80", f"  ({' · '.join(details)})"))
        return frags


# Set by the REPL: returns a one-line status (model, context, tokens) or None. Every live display
# during a run (spinner, sub-agent board, streaming text) draws it underneath itself, so the
# status stays pinned at the bottom while the agent works, not just while you type.
FOOTER = None


class Footed:
    """Wraps a live renderable and adds the status footer below it."""

    def __init__(self, inner):
        self.inner = inner

    def __getattr__(self, name):  # keep e.g. .shimmer.tokens working on the wrapped object
        return getattr(self.inner, name)

    def __rich__(self):
        footer = None
        if FOOTER is not None:
            try:
                footer = FOOTER()
            except Exception:
                footer = None
        if footer is None:
            return self.inner
        from rich.console import Group
        return Group(self.inner, Text(""), footer)


def is_prompt_proxy_active() -> bool:
    """True when running inside prompt_toolkit's patch_stdout context."""
    import sys
    try:
        from prompt_toolkit.patch_stdout import StdoutProxy
        return isinstance(sys.stdout, StdoutProxy)
    except Exception:
        return False


_ACTIVE_SHIMMER: Optional["ShimmerLive"] = None


def start_shimmer(console, label: Optional[str] = None) -> "ShimmerLive":
    global _ACTIVE_SHIMMER
    if _ACTIVE_SHIMMER is not None:
        try:
            _ACTIVE_SHIMMER.stop()
        except Exception:
            pass
    live = ShimmerLive(Shimmer(label), console)
    live.start()
    _ACTIVE_SHIMMER = live
    return live


class ShimmerLive:
    """Live display with the same start/stop interface as rich's Status."""

    def __init__(self, shimmer: Shimmer, console):
        self.shimmer = shimmer
        self._stopped = False
        if not is_prompt_proxy_active():
            self.live = Live(Footed(shimmer), console=console, refresh_per_second=15, transient=True)
        else:
            self.live = None

    def start(self):
        if not self._stopped and self.live is not None:
            try:
                self.live.start()
            except Exception:
                pass

    def stop(self):
        global _ACTIVE_SHIMMER
        if _ACTIVE_SHIMMER is self:
            _ACTIVE_SHIMMER = None
        if self._stopped:
            return
        self._stopped = True
        if self.live is not None:
            try:
                self.live.stop()
            except Exception:
                pass
