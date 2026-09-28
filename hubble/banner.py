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
    return ["".join(_LETTERS[ch][row] for ch in word) for row in range(6)]


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
    out = Text("✦ ", style="bold #5f87ff")
    for i, ch in enumerate(NAME):
        out.append(ch, style=f"bold {_blend(GRADIENT, i / (len(NAME) - 1))}")
    return out


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
FPS = 20

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


def _draw_stars(grid, t: float):
    """Parallax starfield: stars drift left, nearer layers faster.

    Terminal cells are coarse, so each star is anti-aliased across the two cells it straddles:
    it fades out of one and into the next instead of jumping, which reads as smooth motion.
    """
    height, width = len(grid), len(grid[0])
    for x, row, speed, glyph, colors, phase in _stars(width, height):
        pos = (x - speed * t) % width
        c0 = int(pos)
        frac = pos - c0
        twinkle = (math.sin(t * 2.2 + phase) + 1) / 2          # smooth, not on/off
        color = _mix(colors[0], colors[1], twinkle)
        for c, w in ((c0, 1 - frac), ((c0 + 1) % width, frac)):
            if w > 0.2 and grid[row][c][0] == " ":
                grid[row][c] = (glyph, _mix(_SPACE, color, w))


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
            if ch == " " and i < len(cells) and not any(c != " " for c, _ in cells[:i]):
                continue  # leading spaces stay transparent
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

    tips = ["Ask for a change, a fix or an explanation, e.g. [italic]\"add tests for @src/utils.py\"[/italic]",
            "Type [bold]/[/bold] for commands, [bold]@[/bold] to attach a file, [bold]![/bold] to run a shell command"]
    if memory_files:
        tips.append(f"Project memory loaded from [bold]{escape(', '.join(memory_files))}[/bold]")
    else:
        tips.append("Run [bold]/init[/bold] to create HUBBLE.md so Hubble remembers this project")
    if resumable:
        tips.append(f"[bold]/resume[/bold] continues one of {resumable} earlier session{'s' if resumable != 1 else ''} here")
    if provider_count < 2:
        tips.append("[bold]/provider add[/bold] connects another OpenAI-compatible API")
    tip_lines = [Text.from_markup("[bold #af87ff]Tips for getting started[/bold #af87ff]")]
    tip_lines += [Text.from_markup(f"[dim]{i}.[/dim] {tip}") for i, tip in enumerate(tips, 1)]
    return [Text.from_markup(s) for s in status], tip_lines


def compose_home(width: int, height: int, t: float, status: List[Text], tips: List[Text], version: str) -> Text:
    return _grid_text(compose_grid(width, height, t, status, tips, version))


def compose_home_fragments(width: int, height: int, t: float, status: List[Text], tips: List[Text],
                           version: str) -> List[Tuple[str, str]]:
    """Same frame as prompt_toolkit fragments: no ANSI round trip, same-style runs merged."""
    return _grid_fragments(compose_grid(width, height, t, status, tips, version))


_PT_STYLES: dict = {}


def _pt_style(style: Any) -> str:
    """rich style (string or Style) -> prompt_toolkit style string. 'dim' becomes a darker color."""
    if not style:
        return ""
    key = style if isinstance(style, str) else str(style)
    cached = _PT_STYLES.get(key)
    if cached is not None:
        return cached
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


def compose_grid(width: int, height: int, t: float, status: List[Text], tips: List[Text], version: str):
    """Whole home screen on one animated starfield: logo, scene, status and tips.

    Picks the richest layout that fits `height` rows, dropping tips, then shrinking the scene,
    then the logo, so it also works in short terminals such as VS Code's panel.
    """
    width = max(40, min(width - 1, 110))
    height = max(height, 1)
    # Logo sizes: 2 = large pixel letters, 1 = block letters, 0 = one-line wordmark.
    logo_h = {2: 9, 1: 7, 0: 1}  # letter rows + shadow row + tagline
    top = 2 if width >= BIG_LOGO_WIDTH + 4 else (1 if width >= 58 else 0)
    mid = min(top, 1)
    # Richest to poorest. The name is the last thing to shrink: first drop the scene, then the
    # tips, then status lines, and only then fall back to a smaller logo.
    n_status = len(status)
    layouts = []
    for cand_size in dict.fromkeys([top, mid, 0]):
        for cand_art in (FULL, COMPACT, None):
            layouts.append((cand_size, cand_art, True, n_status))
        for cand_art in (COMPACT, None):
            layouts.append((cand_size, cand_art, False, n_status))
        for n in (min(2, n_status), min(1, n_status), 0):
            layouts.append((cand_size, None, False, n))
    size, art, with_tips, n_status = layouts[-1]
    for cand_size, cand_art, cand_tips, cand_n in layouts:
        if cand_art is not None and width < cand_art.width + 24:
            continue
        need = logo_h[cand_size] + 1 + (cand_art.height + 1 if cand_art else 0) + cand_n + \
            (1 + len(tips) if cand_tips else 0) + 1
        if need <= height:
            size, art, with_tips, n_status = cand_size, cand_art, cand_tips, cand_n
            break
    status = status[:n_status]
    use_big = size > 0
    logo_h = logo_h[size]

    rows_total = logo_h + 1 + (art.height + 1 if art else 0) + len(status) + (1 + len(tips) if with_tips else 0) + 1
    rows_total = min(rows_total, height) if rows_total > height else rows_total  # never exceed the budget
    grid = _new_grid(width, max(rows_total, 1))
    _draw_stars(grid, t)

    row = 0
    if use_big:
        logo = _big_logo_rows() if size == 2 else _logo_rows()
        lw = max(len(r) for r in logo)
        for r, line in enumerate(logo):
            # Letters shimmer: the gradient slowly slides across the word.
            body = line.rstrip()
            for c, ch in enumerate(body):  # opaque across the word so stars pass behind it
                if 2 + c < width:
                    shade = _blend(GRADIENT, ((c / max(lw - 1, 1)) + t * 0.08) % 1.0)
                    grid[row + r][2 + c] = (ch, (shade if ch == "█" else f"{shade} dim") if ch != " " else "")
        _stamp_text(grid, row + len(logo), 4, Text.assemble(("your model hub for code", "italic #8787af"),
                                                    ("   v" + version, "dim")))
    else:
        _stamp_text(grid, row, 2, Text.assemble(small_logo(), ("  v" + version, "dim")))
    row += logo_h + 1

    if art is not None:
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


def render_home(console: Console, *, animate: bool = False, **info):
    """Static home screen (non-interactive terminals, /home fallback)."""
    status, tips = home_info_lines(**info)
    height = console.height if console.is_terminal else 200
    console.print()
    console.print(compose_home(console.width, max(height - 3, 12), 2.4, status, tips, info["version"]))
    console.print()
