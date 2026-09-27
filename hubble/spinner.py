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
GLYPHS = ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"]

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


def start_shimmer(console, label: Optional[str] = None) -> "ShimmerLive":
    live = ShimmerLive(Shimmer(label), console)
    live.start()
    return live


class ShimmerLive:
    """Live display with the same start/stop interface as rich's Status."""

    def __init__(self, shimmer: Shimmer, console):
        self.shimmer = shimmer
        self.live = Live(shimmer, console=console, refresh_per_second=15, transient=True)

    def start(self):
        self.live.start()

    def stop(self):
        self.live.stop()
