"""Hubble home screen: gradient block-letter logo, status line and context-aware tips."""

import time
from typing import Any, List, Optional, Tuple

from rich.console import Console
from rich.markup import escape
from rich.text import Text

NAME = "Hubble"

# "ANSI Shadow" style letters, 6 rows each.
_LETTERS = {
    "H": ["██╗  ██╗", "██║  ██║", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "U": ["██╗   ██╗", "██║   ██║", "██║   ██║", "██║   ██║", "╚██████╔╝", " ╚═════╝ "],
    "B": ["██████╗ ", "██╔══██╗", "██████╔╝", "██╔══██╗", "██████╔╝", "╚═════╝ "],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "E": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
}

GRADIENT = [(0, 215, 255), (95, 135, 255), (175, 95, 255), (255, 95, 215)]  # cyan -> violet -> pink


def _logo_rows(word: str = NAME.upper()) -> List[str]:
    return ["".join(_LETTERS[ch][row] for ch in word) for row in range(len(_LETTERS["H"]))]


# Large logo: 5x7 pixel letters, each pixel two cells wide (terminal cells are about twice as
# tall as wide, so this keeps pixels square), with a dim drop shadow one cell down-right.
_PIXELS = {
    "H": ["X...X", "X...X", "X...X", "XXXXX", "X...X", "X...X", "X...X"],
    "U": ["X...X", "X...X", "X...X", "X...X", "X...X", "X...X", ".XXX."],
    "B": ["XXXX.", "X...X", "X...X", "XXXX.", "X...X", "X...X", "XXXX."],
    "L": ["X....", "X....", "X....", "X....", "X....", "X....", "XXXXX"],
    "E": ["XXXXX", "X....", "X....", "XXXX.", "X....", "X....", "XXXXX"],
}


def _big_logo_rows(word: str = NAME.upper()) -> List[str]:
    cells = []
    for r in range(7):
        line = ""
        for ch in word:
            line += "".join("██" if p == "X" else "  " for p in _PIXELS[ch][r]) + "  "
        cells.append(list(line.rstrip() + " "))
    cells.append([" "] * len(cells[0]))
    width = max(len(r) for r in cells)
    grid = [row + [" "] * (width - len(row)) for row in cells]
    for r in range(len(grid) - 1, 0, -1):
        for c in range(width - 1, 0, -1):
            if grid[r][c] == " " and grid[r - 1][c - 1] == "█":
                grid[r][c] = "░"
    return ["".join(row).rstrip() for row in grid]


BIG_LOGO_WIDTH = max(len(r) for r in _big_logo_rows())


def _blend(stops: List[Tuple[int, int, int]], t: float) -> str:
    t = min(max(t, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(t), len(stops) - 2)
    f = t - i
    a, b = stops[i], stops[i + 1]
    r, g, bl = (round(a[k] + (b[k] - a[k]) * f) for k in range(3))
    return f"#{r:02x}{g:02x}{bl:02x}"


def gradient_logo() -> Text:
    rows = _logo_rows()
    width = max(len(r) for r in rows)
    out = Text()
    for row in rows:
        out.append("  ")
        for col, ch in enumerate(row):
            # Shadow characters get a dimmer tone so the letters read as raised.
            style = _blend(GRADIENT, col / max(width - 1, 1))
            out.append(ch, style=style if ch == "█" else f"{style} dim")
        out.append("\n")
    return out


def small_logo() -> Text:
    """Tiny one-liner used in /home fallback and very short terminals."""
    out = Text("✦ ", style="bold #5f87ff")
    for i, ch in enumerate(NAME):
        out.append(ch, style=f"bold {_blend(GRADIENT, i / (len(NAME) - 1))}")
    return out


def wordmark_logo(version: str = "", t: float = 0.0) -> List[Text]:
    """Two-line premium wordmark: NAME in bold gradient with letter spacing + italic tagline.

    This is the *default* logo: clean, readable, feels like a proper CLI brand
    (think Antigravity / Vercel style) without towering 6-row block letters.
    The gradient slowly slides so it stays lively in the animation.
    """
    # Line 1: ✦ icon + gradient letters with thin-space letter spacing for presence
    line1 = Text()
    line1.append("\u2726 ", style="bold #5f87ff")   # icon, cyan start of gradient
    word = NAME.upper()
    for i, ch in enumerate(word):
        shade = _blend(GRADIENT, ((i / max(len(word) - 1, 1)) + t * 0.07) % 1.0)
        line1.append(ch, style=f"bold {shade}")
        if i < len(word) - 1:
            line1.append("\u2009", style="")   # thin space between letters: premium wordmark feel
    # Line 2: italic dim tagline + version
    ver = f"  v{version}" if version else ""
    line2 = Text.from_markup(f"[italic #6a6a8a]your model hub for code[/italic #6a6a8a][dim]{ver}[/dim]")
    return [line1, line2]


# ----- Hubble space scene ---------------------------------------------------
#
# Everything is a pure function of time t (seconds), so the final frame is deterministic and
# the animation looks the same at any frame rate.

import math
import random as _random

class _Art:
    """One telescope/planet design. The scene engine works with any size."""

    def __init__(self, telescope, planet, lens_row):
        self.telescope = telescope
        self.planet = planet
        self.width = max(len(r) for r in telescope)
        self.lens_row = lens_row
        self.lens_col = telescope[lens_row].index("◯")
        self.height = len(telescope) + 2  # one spare row above and below for the bobbing
        self.ring_row = next(i for i, r in enumerate(planet) if "(" in r)


FULL = _Art([
    "      ┌─────────────┐",
    "      │▒▒▒▒▒▒▒▒▒▒▒▒▒│",
    "      └──────┬──────┘",
    " ╭───────────┴────────────╮",
    " │ ▪ ▪ ▪   H U B B L E    ├─╮",
    " │════════════════════════│◯│",
    " │ ▪ ▪ ▪                  ├─╯",
    " ╰───────────┬────────────╯",
    "      ┌──────┴──────┐",
    "      │▒▒▒▒▒▒▒▒▒▒▒▒▒│",
    "      └─────────────┘",
], [
    "   .-~~~~-.",
    " -(        )-",
    "   `-.__.-'",
], lens_row=5)

COMPACT = _Art([
    "     ┌▒▒▒▒▒▒▒▒▒▒┐",
    " ╭───┴──────────┴───╮",
    " │▪ ▪ H U B B L E   ├◯",
    " ╰───┬──────────┬───╯",
    "     └▒▒▒▒▒▒▒▒▒▒┘",
], [
    "  .-~~-.",
    "-(      )-",
    "  `-..-'",
], lens_row=2)

# Kept for callers/tests that refer to the full design's height.
SCENE_H = FULL.height
SURFACE = "░▒▓▒░ ░▒ ▒▓▓▒░  ░"       # planet surface bands, scrolled to look like rotation
FPS = 60

# Three parallax layers: (count per 100 columns, speed in columns per second, glyphs, colors dim/bright)
STAR_LAYERS = [
    (9, 1.5, ".", ("#303044", "#4a4a66")),
    (5, 4.0, "·+", ("#5a5a7a", "#8a8aa8")),
    (2, 9.0, "✦*", ("#9a9ab8", "#ffffff")),
]


def pick_art(height: int) -> Optional[_Art]:
    """Largest design that fits the terminal with room for the logo line; None if too short."""
    if height >= FULL.height + 4:
        return FULL
    if height >= COMPACT.height + 3:
        return COMPACT
    return None


def _stars(width: int, height: int):
    rng = _random.Random(7)
    stars = []
    for density, speed, glyphs, colors in STAR_LAYERS:
        for _ in range(max(1, width * density // 100) * height // 3):
            stars.append((rng.uniform(0, width), rng.randrange(height), speed, rng.choice(glyphs), colors,
                          rng.uniform(0, 6.28)))
    return stars


def _telescope_style(ch: str, c: int, t: float, art: _Art) -> str:
    if ch == "▒":
        # A glint of sunlight sweeps across the solar panels.
        glint = (t * 14) % (art.width + 20) - 10
        d = abs(c - glint)
        return "bold #d7e7ff" if d < 1 else "#87afff" if d < 3 else "#5f87ff"
    if ch == "▪":
        return "#ffaf00" if int(t * 2 + c) % 3 else "bold #ffd75f"   # blinking status lights
    if ch == "◯":
        pulse = (math.sin(t * 5) + 1) / 2
        return "bold #fff3b0" if pulse > 0.66 else "bold #ffd75f" if pulse > 0.33 else "#d7af00"
    if ch == "═":
        return "#87afd7"
    if ch.isalpha():
        return f"bold {_blend(GRADIENT, c / art.width)}"
    return "#bcbcbc"


def _new_grid(width: int, height: int):
    return [[(" ", "")] * width for _ in range(height)]


_BLEND_CACHE: dict = {}


def _mix(dim: str, bright: str, w: float) -> str:
    """Color between two hex colors (w 0..1), quantized to 12 steps so styles stay cacheable."""
    q = max(0, min(12, round(w * 12)))
    key = (dim, bright, q)
    hit = _BLEND_CACHE.get(key)
    if hit:
        return hit
    a = [int(dim[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bright[i:i + 2], 16) for i in (1, 3, 5)]
    f = q / 12
    out = "#" + "".join(f"{round(a[k] + (b[k] - a[k]) * f):02x}" for k in range(3))
    _BLEND_CACHE[key] = out
    return out


_SPACE = "#101018"  # color a star fades to as it leaves a cell


def _draw_stars(grid, t: float, row_start: int = 0, row_end: Optional[int] = None):
    """Parallax starfield: stars drift left, nearer layers faster.

    Terminal cells are coarse, so each star is anti-aliased across the two cells it straddles:
    it fades out of one and into the next instead of jumping, which reads as smooth motion.
    When row_start and row_end are specified, stars are only drawn within that vertical span.
    """
    height, width = len(grid), len(grid[0])
    r_start = max(0, row_start)
    r_end = min(height, row_end if row_end is not None else height)
    if r_end <= r_start:
        return
    span = r_end - r_start
    for x, row, speed, glyph, colors, phase in _stars(width, span):
        target_row = r_start + row
        if target_row >= r_end:
            continue
        pos = (x - speed * t) % width
        c0 = int(pos)
        frac = pos - c0
        twinkle = (math.sin(t * 2.2 + phase) + 1) / 2          # smooth, not on/off
        color = _mix(colors[0], colors[1], twinkle)
        for c, w in ((c0, 1 - frac), ((c0 + 1) % width, frac)):
            if w > 0.2 and grid[target_row][c][0] == " ":
                grid[target_row][c] = (glyph, _mix(_SPACE, color, w))


def _draw_comet(grid, row: int, t: float, period: float = 6.0):
    width = len(grid[0])
    ct = (t % period) / 1.2
    if ct < 1:
        head = int(width * (1 - ct))
        for k, (ch, st) in enumerate((("●", "bold #ffffff"), ("━", "#d7d7ff"), ("━", "#8787af"), ("─", "#5f5f87"))):
            if 0 <= head + k < width:
                grid[row][head + k] = (ch, st)


def _stamp(grid, sprite, top, left, style_of):
    # Opaque between first and last visible char, so stars never show through.
    height, width = len(grid), len(grid[0])
    for r, line in enumerate(sprite):
        body = line.rstrip()
        first = len(body) - len(body.lstrip())
        for c in range(first, len(body)):
            if 0 <= top + r < height and 0 <= left + c < width:
                ch = body[c]
                grid[top + r][left + c] = (ch, style_of(ch, r, c) if ch != " " else "")


def _draw_scene(grid, top: int, t: float, art: _Art):
    """Telescope (bobbing), rotating planet and the light beam between them, starting at row `top`."""
    width = len(grid[0])
    bob = round(math.sin(t * 0.9) * 0.8)                 # gentle float: rests between drifts
    tx, ty = 2, top + 1 + bob
    _stamp(grid, art.telescope, ty, tx, lambda ch, r, c: _telescope_style(ch, c, t, art))

    ring = art.planet[art.ring_row]
    px = width - len(ring) - 3
    planet_top = ty + art.lens_row - art.ring_row  # ring level with the lens, so the beam always connects
    _stamp(grid, art.planet, planet_top, px,
           lambda ch, r, c: "#d7af87" if ch == "-" and r == art.ring_row else "#ff875f")
    spin = int(t * 6)
    ring_y = planet_top + art.ring_row
    if 0 <= ring_y < len(grid):
        for c in range(ring.index("(") + 1, ring.index(")")):
            band = SURFACE[(c + spin) % len(SURFACE)]
            grid[ring_y][px + c] = (band, "#d75f5f" if band == "▓" else "#ff875f" if band == "▒" else "#ffaf87")

    beam_row = ty + art.lens_row
    if 0 <= beam_row < len(grid):
        start, end = tx + art.lens_col + 2, px
        for c in range(start, end):
            phase = (c - start - int(t * 12)) % 5
            if grid[beam_row][c][0] == " " or phase == 0:
                grid[beam_row][c] = ("•" if phase == 0 else "·", "bold #ffd75f" if phase == 0 else "#af8700")


def _grid_text(grid) -> Text:
    out = Text()
    for i, row in enumerate(grid):
        for ch, style in row:
            out.append(ch, style=style or None)
        if i < len(grid) - 1:
            out.append("\n")
    return out


def space_scene(width: int, t: float = 2.4, art: _Art = FULL) -> Text:
    """Just the scene: starfield, comet, telescope, planet, beam (art.height rows)."""
    grid = _new_grid(max(width, art.width + 24), art.height)
    _draw_stars(grid, t)
    _draw_comet(grid, 0, t)
    _draw_scene(grid, 0, t, art)
    return _grid_text(grid).append("\n")


# ----- retro arcade scene (Space Invaders style) ----------------------------
#
# Also a pure function of t: invaders march in discrete steps like the 1978 cabinet, the ship
# glides and fires on a fixed rhythm, and which invaders are gone is replayed from the shots
# fired so far in the current wave, so any frame can be drawn on its own.

INVADERS = [  # (frame A, frame B, color); one row each, top to bottom
    ("▞█▚", "▚█▞", "#ff5fd7"),
    ("▛▀▜", "▙▄▟", "#00d7ff"),
]
_SHIP_ROWS = [
    "▲",
    "▟█▙",
    "▗▄▄▄▟█████▙▄▄▄▖",
    "◢████ H U B B L E ████◣",
    "▀▀▀▜█████████▛▀▀▀",
    "▀▼▀   ▀▼▀",
]
SHIP_W = max(len(r) for r in _SHIP_ROWS)
SHIP = [r.center(SHIP_W) for r in _SHIP_ROWS]  # the HUBBLE starship, nose on top
UFO = "<=●=>"


class _Arcade:
    height = 5 + len(SHIP)  # score/UFO row, 2 invader rows, 2 rows of open space, the ship
    width = 44              # smallest field that still looks like a formation
    COLS = 8            # invaders per row
    GAP = 5             # columns from one invader to the next
    STEP = 0.45         # seconds per march step
    SHOT_EVERY = 0.7
    SHOT_SPEED = 14.0   # rows per second
    WAVE = 14.0         # seconds before a fresh wave marches in

    def field(self, width: int, left: Optional[int] = None) -> Tuple[int, int]:
        """(left, field width) of the play area inside a grid `width` wide; centered by default."""
        fw = max(self.width, min(width - 4, 76))
        if left is None:
            left = max(2, (width - fw) // 2)
        return left, fw

    def _march(self, t: float, span: int) -> int:
        """Formation x offset: steps right, then left, one column per STEP (triangle wave)."""
        steps = int(t / self.STEP)
        period = 2 * span
        k = steps % period if period else 0
        return k if k <= span else period - k

    def _ship_x(self, t: float, fw: int) -> float:
        """Left column of the ship: cruises around the middle of the field."""
        room = max(0, fw - SHIP_W)
        return room / 2 + room / 2 * 0.85 * math.sin(t * 0.6)

    def state(self, t: float, fw: int):
        """(offset, dead {(row, i)}, explosions [(row, col, age)], bullets [(y, x)], score)."""
        wave_t = t % self.WAVE
        wave_start = t - wave_t
        formation = (self.COLS - 1) * self.GAP + 3
        span = max(0, fw - formation)
        dead, booms, bullets, score = set(), [], [], 0
        n = int(wave_t / self.SHOT_EVERY)
        for k in range(n + 1):
            fired = wave_start + k * self.SHOT_EVERY
            if fired > t:
                break
            x = int(round(self._ship_x(fired, fw))) + SHIP_W // 2   # the nose cannon
            # Bullet leaves row 5 (ship tip) and climbs; invader rows are 1 and 2.
            hit = None
            for row in (1, 0):  # lowest invader row first, the one a bullet meets first
                travel = (5 - (row + 1)) / self.SHOT_SPEED
                at = fired + travel
                off = self._march(at, span)
                i, rem = divmod(x - off, self.GAP)
                if 0 <= i < self.COLS and rem < 3 and (row, i) not in dead:
                    hit = (row, i, at)
                    break
            if hit and hit[2] <= t:
                row, i, at = hit
                dead.add((row, i))
                score += 30 if row == 0 else 20
                if t - at < 0.35:
                    booms.append((row + 1, self._march(at, span) + i * self.GAP + 1, t - at))
            else:
                y = 5 - (t - fired) * self.SHOT_SPEED
                end = (hit[0] + 1) if hit else -1
                if y > end:
                    bullets.append((int(y), x))
        total_score = int(t / self.WAVE) * 400 + score
        return self._march(t, span), dead, booms, bullets, total_score

    def draw(self, grid, top: int, t: float, left: Optional[int] = None):
        width = len(grid[0])
        left, fw = self.field(width, left)
        fw = min(fw, width - left)

        def put(r, c, ch, style):
            if 0 <= top + r < len(grid) and 0 <= left + c < min(width, left + fw):
                grid[top + r][left + c] = (ch, style)

        off, dead, booms, bullets, score = self.state(t, fw)
        frame = int(t / self.STEP) % 2
        for j, ch in enumerate(f"SCORE {score:05d}"):
            put(0, j, ch, "#5fd787")
        hi = "HI 09990"
        for j, ch in enumerate(hi):
            put(0, fw - len(hi) + j, ch, "#878787")
        # Mothership crosses the top every so often (over the score, like the real thing).
        ut = t % 9.0
        if ut < 3.0:
            ux = int((fw + len(UFO)) * ut / 3.0) - len(UFO)
            for j, ch in enumerate(UFO):
                put(0, ux + j, ch, "bold #ff5f5f" if ch == "●" and int(t * 8) % 2 else "#ff8787")
        for row, (a, b, color) in enumerate(INVADERS):
            sprite = a if frame == 0 else b
            for i in range(self.COLS):
                if (row, i) in dead:
                    continue
                for j, ch in enumerate(sprite):
                    put(row + 1, off + i * self.GAP + j, ch, color)
        for r, c, age in booms:
            ch = "✶" if age < 0.12 else "✷" if age < 0.24 else "·"
            for dc, g in ((-1, "╲"), (0, ch), (1, "╱")):
                put(r, c + dc, g if age < 0.24 else " " if dc else ch, "bold #ffd75f")
        for y, x in bullets:
            if 0 <= y <= 5:
                put(y, x, "│", "bold #ffffaf")
        sx = int(round(self._ship_x(t, fw)))
        flame = int(t * 10) % 3
        name_row = next(i for i, r in enumerate(SHIP) if "H U B B L E" in r)
        for r, line in enumerate(SHIP):
            for j, ch in enumerate(line):
                if ch == " " and r != name_row:
                    continue
                if ch == "▲":
                    style = "bold #ffffff" if int(t * 6) % 2 else "bold #d7e7ff"
                elif ch == "▼":
                    ch = ("▼", "▽", "▾")[(flame + j) % 3]
                    style = ("bold #ffd75f", "#ff8700", "#ff5f00")[(flame + j) % 3]
                elif ch.isalpha():
                    style = f"bold {_blend(GRADIENT, ((j / SHIP_W) + t * 0.15) % 1.0)}"  # glowing name
                elif r == name_row and ch == " ":
                    style = ""  # opaque inside the hull, so stars pass behind the ship
                elif ch in "◢◣":
                    style = "#ff5f87"  # wing-tip lights
                else:
                    style = "#afc7e7" if r < name_row else "#5f87af"
                put(5 + r, sx + j, ch, style)


ARCADE = _Arcade()


# ----- composed home screen -------------------------------------------------

_MEASURE = Console(width=400, color_system="truecolor", force_terminal=True, legacy_windows=False,
                   file=__import__("io").StringIO())


_CELL_CACHE: dict = {}


def _cells(text: Text) -> List[Tuple[str, Any]]:
    """Per-character (char, style) for one line of rich Text (cached: status/tips never change)."""
    key = id(text)
    hit = _CELL_CACHE.get(key)
    if hit is not None and hit[0] is text:
        return hit[1]
    cells: List[Tuple[str, Any]] = []
    for seg in text.render(_MEASURE):
        if seg.text == "\n":
            continue
        for ch in seg.text:
            cells.append((ch, seg.style))
    if len(_CELL_CACHE) > 256:
        _CELL_CACHE.clear()
    _CELL_CACHE[key] = (text, cells)
    return cells


def _stamp_text(grid, row: int, col: int, text: Text):
    """Opaque text line: stars stay out of the span from first to last visible character."""
    cells = _cells(text)
    width = len(grid[0])
    last = max((i for i, (ch, _) in enumerate(cells) if ch != " "), default=-1)
    for i, (ch, style) in enumerate(cells[: last + 1]):
        if 0 <= col + i < width and 0 <= row < len(grid):
            grid[row][col + i] = (ch, style)


def home_info_lines(*, version: str, provider: str, model: str, mode: str, root: str, session_id: Optional[str],
                    memory_files: List[str], model_count: Optional[int], provider_count: int, resumable: int,
                    show_provider: bool, scan_note: str = "") -> Tuple[List[Text], List[Text]]:
    """(status lines, tip lines) as rich Text, shared by the static and animated home screens."""
    via = f"[dim]{escape(provider)}[/dim] · " if show_provider else ""
    if scan_note:
        count = f" [dim]({escape(scan_note)})[/dim]"
    else:
        count = f" [dim]({model_count} models)[/dim]" if model_count else ""
    mode_color = {"default": "white", "accept-edits": "green", "plan": "blue", "yolo": "red"}.get(mode, "white")
    status = [f"[bold]model  [/bold]  {via}[#00d7ff]{escape(model)}[/#00d7ff]{count}",
              f"[bold]mode   [/bold]  [{mode_color}]{mode}[/{mode_color}] [dim](Shift+Tab to change)[/dim]",
              f"[bold]cwd    [/bold]  {escape(root)}"]
    if session_id:
        status.append(f"[bold]session[/bold]  [dim]{session_id}[/dim]")

    tip_lines = [
        Text.from_markup("[dim]Type [bold white]/[/bold white] for commands · [bold white]@[/bold white] to attach files · [bold white]Shift+Tab[/bold white] to cycle modes[/dim]"),
    ]
    if memory_files:
        tip_lines.append(Text.from_markup(f"[dim]Project memory active: [bold white]{escape(', '.join(memory_files))}[/bold white][/dim]"))
    elif resumable:
        tip_lines.append(Text.from_markup(f"[dim]Run [bold white]/resume[/bold white] to continue an earlier session ({resumable} available)[/dim]"))
    else:
        tip_lines.append(Text.from_markup("[dim]Run [bold white]/init[/bold white] to create HUBBLE.md (project memory)[/dim]"))
    return [Text.from_markup(s) for s in status], tip_lines


def compose_home(width: int, height: int, t: float, status: List[Text], tips: List[Text], version: str,
                 logo_style: Optional[str] = None) -> Text:
    return _grid_text(compose_grid(width, height, t, status, tips, version, logo_style=logo_style))


def compose_home_fragments(width: int, height: int, t: float, status: List[Text], tips: List[Text],
                           version: str, logo_style: Optional[str] = None) -> List[Tuple[str, str]]:
    """Same frame as prompt_toolkit fragments: no ANSI round trip, same-style runs merged."""
    return _grid_fragments(compose_grid(width, height, t, status, tips, version, logo_style=logo_style))


_PT_STYLES: dict = {}


def _pt_style(style: Any) -> str:
    """rich style (string or Style) -> prompt_toolkit style string. 'dim' becomes a darker color."""
    if not style:
        return ""
    key = style if isinstance(style, str) else str(style)
    cached = _PT_STYLES.get(key)
    if cached is not None:
        return cached

    # Fast-path common hex styles used in animations without calling Style.parse()
    if isinstance(style, str):
        if style.startswith("#") and len(style) == 7:
            out = f"fg:{style}"
            _PT_STYLES[key] = out
            return out
        if style.startswith("bold #") and len(style) == 12:
            out = f"bold fg:{style[5:]}"
            _PT_STYLES[key] = out
            return out
        if style.endswith(" dim") and style.startswith("#") and len(style) == 11:
            hex_part = style[:7]
            try:
                r = int(hex_part[1:3], 16)
                g = int(hex_part[3:5], 16)
                b = int(hex_part[5:7], 16)
                out = f"fg:#{int(r * 0.55):02x}{int(g * 0.55):02x}{int(b * 0.55):02x}"
                _PT_STYLES[key] = out
                return out
            except ValueError:
                pass

    from rich.style import Style
    try:
        st = style if isinstance(style, Style) else Style.parse(style)
    except Exception:
        _PT_STYLES[key] = ""
        return ""
    parts = []
    if st.color is not None:
        r, g, b = st.color.get_truecolor()
        if st.dim:
            r, g, b = int(r * 0.55), int(g * 0.55), int(b * 0.55)
        parts.append(f"fg:#{r:02x}{g:02x}{b:02x}")
    elif st.dim:
        parts.append("fg:#808080")
    if st.bold:
        parts.append("bold")
    if st.italic:
        parts.append("italic")
    if st.strike:
        parts.append("strike")
    out = " ".join(parts)
    if len(_PT_STYLES) > 1024:
        _PT_STYLES.clear()
    _PT_STYLES[key] = out
    return out


def _grid_fragments(grid) -> List[Tuple[str, str]]:
    frags: List[Tuple[str, str]] = []
    for row in grid:
        cur, buf = None, []
        for ch, st in row:
            s = _pt_style(st) if ch != " " else ""
            if s != cur and buf:
                frags.append((cur or "", "".join(buf)))
                buf = []
            cur = s
            buf.append(ch)
        if buf:
            frags.append((cur or "", "".join(buf)))
        frags.append(("", "\n"))
    return frags


def compose_grid(width: int, height: int, t: float, status: List[Text], tips: List[Text], version: str,
                 logo_style: Optional[str] = None):
    """Whole home screen on one animated starfield: logo, scene, status and tips.

    Picks the richest layout that fits `height` rows, dropping tips, then shrinking the scene,
    then the logo, so it also works in short terminals such as VS Code's panel.
    """
    width = max(40, min(width - 1, 110))
    height = max(height, 1)
    # Logo sizes:
    #   "big" / "pixel" / "large" -> 2: pixel-art logo (9 rows)
    #   "block"                   -> 1: ANSI Shadow block letters (7 rows)
    #   default                   -> "word": 2-row gradient wordmark (clean, compact)
    #   "small"                   -> "small": 1-row wordmark (minimum)
    if logo_style == "small":
        logo_mode = "small"
    elif logo_style in ("big", "pixel", "large"):
        logo_mode = "pixel"
    elif logo_style in ("block", "ascii", "ansi"):
        logo_mode = "block"
    else:
        logo_mode = "word"   # default / "regular" / "normal": clean 2-line wordmark

    logo_h_map = {"pixel": 9, "block": 7, "word": 2, "small": 1}
    logo_h = logo_h_map[logo_mode]

    # Richest to poorest: the starship scene comes first, then the bigger logo, then tips,
    # then status lines.
    n_status = len(status)
    shrink = [(True, n_status), (False, n_status), (False, min(2, n_status)), (False, min(1, n_status)),
              (False, 0)]
    # For logo degradation: pixel -> block -> word -> small
    logo_order = {"pixel": ["pixel", "block", "word", "small"],
                  "block": ["block", "word", "small"],
                  "word": ["word", "small"],
                  "small": ["small"]}
    art_candidates = (ARCADE, None)
    # Build layout candidates from richest to sparsest
    layouts = [(lm, a, tp, n)
                for a in art_candidates
                for lm in logo_order[logo_mode]
                for tp, n in shrink]
    # default to sparsest
    logo_mode_use, art, with_tips, n_status = "small", None, False, 0

    def art_rows(a):
        return a.height + 1 if a is not None else 0

    for cand_lm, cand_art, cand_tips, cand_n in layouts:
        if cand_art is ARCADE and width < ARCADE.width + 4:
            continue
        lh = logo_h_map[cand_lm]
        need = lh + 1 + art_rows(cand_art) + cand_n + (1 + len(tips) if cand_tips else 0) + 1
        if need <= height:
            logo_mode_use, art, with_tips, n_status = cand_lm, cand_art, cand_tips, cand_n
            break
    status = status[:n_status]
    logo_h = logo_h_map[logo_mode_use]

    rows_total = logo_h + 1 + art_rows(art) + len(status) + (1 + len(tips) if with_tips else 0) + 1
    rows_total = min(rows_total, height) if rows_total > height else rows_total  # never exceed the budget
    grid = _new_grid(width, max(rows_total, 1))

    row = 0
    if logo_mode_use == "pixel":
        logo = _big_logo_rows()
        lw = max(len(r) for r in logo)
        for r, line in enumerate(logo):
            body = line.rstrip()
            for c, ch in enumerate(body):
                if 2 + c < width:
                    shade = _blend(GRADIENT, ((c / max(lw - 1, 1)) + t * 0.08) % 1.0)
                    grid[row + r][2 + c] = (ch, (shade if ch == "█" else f"{shade} dim") if ch != " " else "")
        _stamp_text(grid, row + len(logo), 4, Text.assemble(("your model hub for code", "italic #8787af"),
                                                    ("   v" + version, "dim")))
    elif logo_mode_use == "block":
        logo = _logo_rows()
        lw = max(len(r) for r in logo)
        for r, line in enumerate(logo):
            body = line.rstrip()
            for c, ch in enumerate(body):
                if 2 + c < width:
                    shade = _blend(GRADIENT, ((c / max(lw - 1, 1)) + t * 0.08) % 1.0)
                    grid[row + r][2 + c] = (ch, (shade if ch == "█" else f"{shade} dim") if ch != " " else "")
        _stamp_text(grid, row + len(logo), 4, Text.assemble(("your model hub for code", "italic #8787af"),
                                                    ("   v" + version, "dim")))
    elif logo_mode_use == "word":
        wm = wordmark_logo(version, t)
        _stamp_text(grid, row, 2, wm[0])
        _stamp_text(grid, row + 1, 2, wm[1])
    else:
        _stamp_text(grid, row, 2, Text.assemble(small_logo(), ("  v" + version, "dim")))
    row += logo_h + 1

    if art is ARCADE:
        _draw_stars(grid, t, row_start=row, row_end=row + ARCADE.height)
        ARCADE.draw(grid, row, t)
        row += ARCADE.height + 1
    elif isinstance(art, _Art):
        _draw_stars(grid, t, row_start=row, row_end=row + art.height)
        _draw_comet(grid, row, t)
        _draw_scene(grid, row, t, art)
        row += art.height + 1

    for line in status:
        _stamp_text(grid, row, 2, line)
        row += 1
    if with_tips:
        row += 1
        for line in tips:
            _stamp_text(grid, row, 2, line)
            row += 1
    return grid


def to_ansi(text: Text, width: int) -> str:
    """Render rich Text to a truecolor ANSI string (for prompt_toolkit's ANSI formatted text)."""
    con = Console(width=width, color_system="truecolor", force_terminal=True, legacy_windows=False,
                  file=__import__("io").StringIO())
    con.print(text, end="", soft_wrap=True, crop=True)
    return con.file.getvalue()


def render_home(console: Console, *, animate: bool = False, logo_style: Optional[str] = None, **info):
    """Static home screen (non-interactive terminals, /home fallback)."""
    status, tips = home_info_lines(**info)
    height = console.height if console.is_terminal else 200
    console.print()
    console.print(compose_home(console.width, max(height - 3, 12), 2.4, status, tips, info["version"], logo_style=logo_style))
    console.print()
