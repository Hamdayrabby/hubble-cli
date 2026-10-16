"""Color schemes and interactive theme selector for Hubble CLI (matching Antigravity CLI).

Schemes:
  - terminal (system ANSI colors)
  - light (clean GitHub-inspired light mode)
  - solarized light (Ethan Schoonover's Solarized Light)
  - colorblind-friendly light (accessible high-contrast palette)
  - dark (modern VS Code / GitHub dark mode)
  - solarized dark (Solarized Dark)
  - colorblind-friendly dark (accessible high-contrast palette)
  - tokyo night (Tokyo Night vibrant neon/dark palette)
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from rich.theme import Theme


@dataclass
class ColorPalette:
    name: str
    description: str
    # prompt_toolkit style specs
    border: str
    user_prefix: str
    user_text: str
    assistant_title: str
    diff_add_bg: str
    diff_add_fg: str
    diff_del_bg: str
    diff_del_fg: str
    diff_num: str
    diff_code: str
    thought_title: str
    thought_text: str
    tool_icon: str
    tool_text: str
    task_icon: str
    task_text: str
    error: str
    warning: str
    link: str
    accent: str
    dim: str
    # pygments syntax highlight theme
    code_theme: str
    # prompt_toolkit completion menu & UI styles
    menu_bg: str = "#161b22"
    menu_fg: str = "#c9d1d9"
    menu_meta_bg: str = "#161b22"
    menu_meta_fg: str = "#8b949e"
    menu_sel_bg: str = "#21262d"
    menu_sel_fg: str = "#58a6ff bold"
    menu_sel_meta_bg: str = "#21262d"
    menu_sel_meta_fg: str = "#c9d1d9"
    scrollbar_bg: str = "#161b22"
    scrollbar_thumb: str = "#30363d"


COLOR_SCHEMES: Dict[str, ColorPalette] = {
    "terminal": ColorPalette(
        name="terminal",
        description="Default terminal palette",
        border="ansigray",
        user_prefix="ansicyan bold",
        user_text="",
        assistant_title="ansigreen bold",
        diff_add_bg="bg:#1a3a22",
        diff_add_fg="ansigreen",
        diff_del_bg="bg:#3a1a1a",
        diff_del_fg="ansired",
        diff_num="ansigray",
        diff_code="",
        thought_title="ansigray bold",
        thought_text="ansigray italic",
        tool_icon="ansiyellow",
        tool_text="",
        task_icon="ansimagenta",
        task_text="",
        error="ansired bold",
        warning="ansiyellow bold",
        link="ansicyan underline",
        accent="ansiblue bold",
        dim="ansigray",
        code_theme="monokai",
        menu_bg="#161b22",
        menu_fg="#c9d1d9",
        menu_meta_bg="#161b22",
        menu_meta_fg="#8b949e",
        menu_sel_bg="#21262d",
        menu_sel_fg="#58a6ff bold",
        menu_sel_meta_bg="#21262d",
        menu_sel_meta_fg="#c9d1d9",
        scrollbar_bg="#161b22",
        scrollbar_thumb="#38434f",
    ),
    "light": ColorPalette(
        name="light",
        description="Clean GitHub-inspired light mode",
        border="#d0d7de",
        user_prefix="#0969da bold",
        user_text="#24292f",
        assistant_title="#1a7f37 bold",
        diff_add_bg="bg:#dafbe1",
        diff_add_fg="#1a7f37",
        diff_del_bg="bg:#ffebe9",
        diff_del_fg="#cf222e",
        diff_num="#8c959f",
        diff_code="#24292f",
        thought_title="#57606a bold",
        thought_text="#57606a italic",
        tool_icon="#9a6700",
        tool_text="#24292f",
        task_icon="#8250df",
        task_text="#24292f",
        error="#cf222e bold",
        warning="#9a6700 bold",
        link="#0969da underline",
        accent="#0969da bold",
        dim="#6e7781",
        code_theme="friendly",
        menu_bg="#f6f8fa",
        menu_fg="#24292f",
        menu_meta_bg="#f6f8fa",
        menu_meta_fg="#6e7781",
        menu_sel_bg="#ddf4ff",
        menu_sel_fg="#0969da bold",
        menu_sel_meta_bg="#ddf4ff",
        menu_sel_meta_fg="#24292f",
        scrollbar_bg="#f6f8fa",
        scrollbar_thumb="#d0d7de",
    ),
    "solarized light": ColorPalette(
        name="solarized light",
        description="Solarized light theme",
        border="#93a1a1",
        user_prefix="#268bd2 bold",
        user_text="#657b83",
        assistant_title="#859900 bold",
        diff_add_bg="bg:#e9f2d1",
        diff_add_fg="#859900",
        diff_del_bg="bg:#fbe4e2",
        diff_del_fg="#dc322f",
        diff_num="#93a1a1",
        diff_code="#657b83",
        thought_title="#586e75 bold",
        thought_text="#586e75 italic",
        tool_icon="#b58900",
        tool_text="#657b83",
        task_icon="#6c71c4",
        task_text="#657b83",
        error="#dc322f bold",
        warning="#cb4b16 bold",
        link="#2aa198 underline",
        accent="#268bd2 bold",
        dim="#93a1a1",
        code_theme="solarized-light",
        menu_bg="#fdf6e3",
        menu_fg="#657b83",
        menu_meta_bg="#fdf6e3",
        menu_meta_fg="#93a1a1",
        menu_sel_bg="#eee8d5",
        menu_sel_fg="#268bd2 bold",
        menu_sel_meta_bg="#eee8d5",
        menu_sel_meta_fg="#586e75",
        scrollbar_bg="#fdf6e3",
        scrollbar_thumb="#93a1a1",
    ),
    "colorblind-friendly light": ColorPalette(
        name="colorblind-friendly light",
        description="High-contrast accessible light palette",
        border="#7f7f7f",
        user_prefix="#0072b2 bold",
        user_text="#111111",
        assistant_title="#0072b2 bold",
        diff_add_bg="bg:#e6f2f8",
        diff_add_fg="#0072b2",
        diff_del_bg="bg:#fcece4",
        diff_del_fg="#d55e00",
        diff_num="#7f7f7f",
        diff_code="#111111",
        thought_title="#444444 bold",
        thought_text="#444444 italic",
        tool_icon="#e69f00",
        tool_text="#111111",
        task_icon="#cc79a7",
        task_text="#111111",
        error="#d55e00 bold",
        warning="#e69f00 bold",
        link="#0072b2 underline",
        accent="#0072b2 bold",
        dim="#666666",
        code_theme="friendly",
        menu_bg="#f5f5f5",
        menu_fg="#111111",
        menu_meta_bg="#f5f5f5",
        menu_meta_fg="#666666",
        menu_sel_bg="#e1effa",
        menu_sel_fg="#0072b2 bold",
        menu_sel_meta_bg="#e1effa",
        menu_sel_meta_fg="#111111",
        scrollbar_bg="#f5f5f5",
        scrollbar_thumb="#b0b0b0",
    ),
    "dark": ColorPalette(
        name="dark",
        description="VS Code / GitHub dark mode",
        border="#30363d",
        user_prefix="#58a6ff bold",
        user_text="#c9d1d9",
        assistant_title="#3fb950 bold",
        diff_add_bg="bg:#0d351a",
        diff_add_fg="#3fb950",
        diff_del_bg="bg:#490202",
        diff_del_fg="#f85149",
        diff_num="#6e7681",
        diff_code="#c9d1d9",
        thought_title="#8b949e bold",
        thought_text="#8b949e italic",
        tool_icon="#d29922",
        tool_text="#c9d1d9",
        task_icon="#bc8cff",
        task_text="#c9d1d9",
        error="#f85149 bold",
        warning="#d29922 bold",
        link="#58a6ff underline",
        accent="#58a6ff bold",
        dim="#8b949e",
        code_theme="monokai",
        menu_bg="#161b22",
        menu_fg="#c9d1d9",
        menu_meta_bg="#161b22",
        menu_meta_fg="#8b949e",
        menu_sel_bg="#21262d",
        menu_sel_fg="#58a6ff bold",
        menu_sel_meta_bg="#21262d",
        menu_sel_meta_fg="#c9d1d9",
        scrollbar_bg="#161b22",
        scrollbar_thumb="#30363d",
    ),
    "solarized dark": ColorPalette(
        name="solarized dark",
        description="Solarized dark theme",
        border="#586e75",
        user_prefix="#268bd2 bold",
        user_text="#839496",
        assistant_title="#859900 bold",
        diff_add_bg="bg:#1b3b18",
        diff_add_fg="#859900",
        diff_del_bg="bg:#441416",
        diff_del_fg="#dc322f",
        diff_num="#586e75",
        diff_code="#839496",
        thought_title="#93a1a1 bold",
        thought_text="#93a1a1 italic",
        tool_icon="#b58900",
        tool_text="#839496",
        task_icon="#6c71c4",
        task_text="#839496",
        error="#dc322f bold",
        warning="#cb4b16 bold",
        link="#2aa198 underline",
        accent="#268bd2 bold",
        dim="#586e75",
        code_theme="solarized-dark",
        menu_bg="#002b36",
        menu_fg="#839496",
        menu_meta_bg="#002b36",
        menu_meta_fg="#586e75",
        menu_sel_bg="#073642",
        menu_sel_fg="#268bd2 bold",
        menu_sel_meta_bg="#073642",
        menu_sel_meta_fg="#93a1a1",
        scrollbar_bg="#002b36",
        scrollbar_thumb="#586e75",
    ),
    "colorblind-friendly dark": ColorPalette(
        name="colorblind-friendly dark",
        description="High-contrast accessible dark palette",
        border="#666666",
        user_prefix="#56b4e9 bold",
        user_text="#eeeeee",
        assistant_title="#56b4e9 bold",
        diff_add_bg="bg:#0f3b54",
        diff_add_fg="#56b4e9",
        diff_del_bg="bg:#4d3200",
        diff_del_fg="#e69f00",
        diff_num="#888888",
        diff_code="#eeeeee",
        thought_title="#aaaaaa bold",
        thought_text="#aaaaaa italic",
        tool_icon="#f0e442",
        tool_text="#eeeeee",
        task_icon="#cc79a7",
        task_text="#eeeeee",
        error="#e69f00 bold",
        warning="#f0e442 bold",
        link="#56b4e9 underline",
        accent="#56b4e9 bold",
        dim="#888888",
        code_theme="monokai",
        menu_bg="#181818",
        menu_fg="#eeeeee",
        menu_meta_bg="#181818",
        menu_meta_fg="#888888",
        menu_sel_bg="#2d3a44",
        menu_sel_fg="#56b4e9 bold",
        menu_sel_meta_bg="#2d3a44",
        menu_sel_meta_fg="#ffffff",
        scrollbar_bg="#181818",
        scrollbar_thumb="#444444",
    ),
    "tokyo night": ColorPalette(
        name="tokyo night",
        description="Tokyo Night vibrant aesthetics",
        border="#414868",
        user_prefix="#7aa2f7 bold",
        user_text="#c0caf5",
        assistant_title="#9ece6a bold",
        diff_add_bg="bg:#22372b",
        diff_add_fg="#9ece6a",
        diff_del_bg="bg:#3b1d28",
        diff_del_fg="#f7768e",
        diff_num="#565f89",
        diff_code="#c0caf5",
        thought_title="#565f89 bold",
        thought_text="#565f89 italic",
        tool_icon="#e0af68",
        tool_text="#c0caf5",
        task_icon="#bb9af7",
        task_text="#c0caf5",
        error="#f7768e bold",
        warning="#ff9e64 bold",
        link="#7dcfff underline",
        accent="#7aa2f7 bold",
        dim="#565f89",
        code_theme="dracula",
        menu_bg="#16161e",
        menu_fg="#c0caf5",
        menu_meta_bg="#16161e",
        menu_meta_fg="#565f89",
        menu_sel_bg="#283457",
        menu_sel_fg="#7aa2f7 bold",
        menu_sel_meta_bg="#283457",
        menu_sel_meta_fg="#c0caf5",
        scrollbar_bg="#16161e",
        scrollbar_thumb="#414868",
    ),
}

ORDERED_SCHEMES = [
    "terminal",
    "light",
    "solarized light",
    "colorblind-friendly light",
    "dark",
    "solarized dark",
    "colorblind-friendly dark",
    "tokyo night",
]


def get_palette(name: Optional[str] = None) -> ColorPalette:
    clean = (name or "").strip().lower()
    return COLOR_SCHEMES.get(clean, COLOR_SCHEMES["terminal"])


def _to_rich_style(style_str: str) -> str:
    if not style_str:
        return "none"
    s = style_str
    s = s.replace("ansiblue", "bright_blue").replace("ansicyan", "cyan")
    s = s.replace("ansigreen", "green").replace("ansired", "red")
    s = s.replace("ansiyellow", "yellow").replace("ansimagenta", "magenta")
    s = s.replace("ansigray", "dim")
    return s.strip()


def get_rich_theme(name: Optional[str] = None) -> Theme:
    p = get_palette(name)
    return Theme({
        "info": _to_rich_style(p.accent),
        "warning": _to_rich_style(p.warning),
        "danger": _to_rich_style(p.error),
        "error": _to_rich_style(p.error),
        "success": _to_rich_style(p.assistant_title),
        "dim": _to_rich_style(p.dim),
        "link": _to_rich_style(p.link),
    })


def get_prompt_style(name: Optional[str] = None) -> Dict[str, str]:
    """Build prompt_toolkit style mapping for bottom toolbar, dark completion menus, and scrollbars."""
    p = get_palette(name)
    return {
        # Bottom Toolbar
        "bottom-toolbar": "noreverse bg:default #6c6c80",
        "bottom-toolbar.text": "noreverse bg:default",
        "tb.rule": "#303040", "tb.sep": "#44445a", "tb.dim": "#6c6c80", "tb.val": "#b8b8cc",
        "tb.model": "bold #00d7ff", "tb.warn": "bold #ffaf00",
        "tb.mode-default": "#b8b8cc", "tb.mode-accept-edits": "bold #5fd787",
        "tb.mode-plan": "bold #5fafff", "tb.mode-yolo": "bold #ff5f5f",
        "tb.in": "#87afd7", "tb.out": "#af87d7", "tb.speed": "#5fd7af",

        # Autocompletion Menu Dropdown (Replaces raw white/grey default with sleek dark/theme card)
        "completion-menu": f"bg:{p.menu_bg} {p.menu_fg}",
        "completion-menu.completion": f"bg:{p.menu_bg} {p.menu_fg}",
        "completion-menu.completion.current": f"bg:{p.menu_sel_bg} {p.menu_sel_fg}",
        "completion-menu.meta.completion": f"bg:{p.menu_meta_bg} {p.menu_meta_fg}",
        "completion-menu.meta.completion.current": f"bg:{p.menu_sel_meta_bg} {p.menu_sel_meta_fg}",
        "completion-menu.multi-column-meta": f"bg:{p.menu_meta_bg} {p.menu_meta_fg}",

        # Scrollbar
        "scrollbar": f"bg:{p.scrollbar_bg}",
        "scrollbar.background": f"bg:{p.scrollbar_bg}",
        "scrollbar.button": f"bg:{p.scrollbar_thumb}",
        "scrollbar.arrow": f"bg:{p.scrollbar_bg} {p.menu_meta_fg}",

        # Inline hints and auto-suggestions
        "auto-suggestion": f"dim {p.dim}",
    }



def render_preview_lines(palette: ColorPalette, width: int = 56) -> List[Tuple[str, str]]:
    """Build formatted text lines for the live preview card shown on the right."""
    p = palette
    border_style = p.border

    def pad(text: str, fill: int) -> str:
        return text + " " * max(0, fill - len(text))

    content_w = width - 4

    out: List[Tuple[str, str]] = []

    # Card top border
    out.append((border_style, " ╭" + "─" * (width - 2) + "╮\n"))

    # Line 1: User prompt
    out.append((border_style, " │ "))
    out.append((p.user_prefix, "> you: "))
    out.append((p.user_text, pad("add a greeting function", content_w - 7)))
    out.append((border_style, " │\n"))

    # Line 2: Empty
    out.append((border_style, f" │ {' ' * content_w} │\n"))

    # Line 3: Assistant title
    out.append((border_style, " │ "))
    out.append((p.assistant_title, pad("Here's the change:", content_w)))
    out.append((border_style, " │\n"))

    # Line 4: Empty
    out.append((border_style, f" │ {' ' * content_w} │\n"))

    # Line 5: Unchanged code
    out.append((border_style, " │ "))
    out.append((p.diff_num, " 3   "))
    out.append((p.diff_code, pad('import "fmt"', content_w - 5)))
    out.append((border_style, " │\n"))

    # Line 6: Empty code line
    out.append((border_style, " │ "))
    out.append((p.diff_num, " 4   "))
    out.append((p.diff_code, pad("", content_w - 5)))
    out.append((border_style, " │\n"))

    # Line 7: Diff remove line
    out.append((border_style, " │ "))
    del_str = pad(' 5 - func main() {', content_w)
    out.append((f"{p.diff_del_bg} {p.diff_del_fg}", del_str))
    out.append((border_style, " │\n"))

    # Line 8: Diff add line 1
    out.append((border_style, " │ "))
    add_str1 = pad(' 5 + func greet(name string) {', content_w)
    out.append((f"{p.diff_add_bg} {p.diff_add_fg}", add_str1))
    out.append((border_style, " │\n"))

    # Line 9: Diff add line 2
    out.append((border_style, " │ "))
    add_str2 = pad(' 6 +     fmt.Printf("Hello, %s!\\n", name)', content_w)
    out.append((f"{p.diff_add_bg} {p.diff_add_fg}", add_str2))
    out.append((border_style, " │\n"))

    # Line 10: Closing brace
    out.append((border_style, " │ "))
    out.append((p.diff_num, " 7   "))
    out.append((p.diff_code, pad("}", content_w - 5)))
    out.append((border_style, " │\n"))

    # Line 11: Empty
    out.append((border_style, f" │ {' ' * content_w} │\n"))

    # Line 12: Thought Process header
    out.append((border_style, " │ "))
    out.append((p.thought_title, pad("▾ Thought Process", content_w)))
    out.append((border_style, " │\n"))

    # Line 13: Thought Process text
    out.append((border_style, " │ "))
    thought = pad("  I need to add a greeting function. I'll use fmt.Printf.", content_w)
    out.append((p.thought_text, thought))
    out.append((border_style, " │\n"))

    # Line 14: Tool use
    out.append((border_style, " │ "))
    out.append((p.tool_icon, "⚙ tool: "))
    out.append((p.tool_text, pad("write_file main.go", content_w - 8)))
    out.append((border_style, " │\n"))

    # Line 15: Task
    out.append((border_style, " │ "))
    out.append((p.task_icon, "◉ task: "))
    out.append((p.task_text, pad("Implementing greeting", content_w - 8)))
    out.append((border_style, " │\n"))

    # Line 16: Error
    out.append((border_style, " │ "))
    out.append((p.error, pad("✗ error: compilation failed", content_w)))
    out.append((border_style, " │\n"))

    # Line 17: Warning
    out.append((border_style, " │ "))
    out.append((p.warning, pad("⚠ warning: deprecation warning", content_w)))
    out.append((border_style, " │\n"))

    # Line 18: Link
    out.append((border_style, " │ "))
    out.append((p.accent, "→ link: "))
    out.append((p.link, pad("file:///path/to/main.go", content_w - 8)))
    out.append((border_style, " │\n"))

    # Line 19: Accent
    out.append((border_style, " │ "))
    out.append((p.accent, "★ accent: "))
    out.append((p.tool_text, pad("highlighted text", content_w - 10)))
    out.append((border_style, " │\n"))

    # Line 20: Dim
    out.append((border_style, " │ "))
    out.append((p.dim, pad("· dim: press Enter to continue", content_w)))
    out.append((border_style, " │\n"))

    # Bottom border
    out.append((border_style, " ╰" + "─" * (width - 2) + "╯\n"))

    return out


def choose_color_scheme(current: Optional[str] = None) -> Optional[str]:
    """Interactive full-screen or inline dual-pane color scheme picker with live preview.

    Left side: scrollable/selectable list of color schemes.
    Right side: live-rendered sample code and UI preview box matching Antigravity CLI.
    """
    from prompt_toolkit.application import Application
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    schemes = ORDERED_SCHEMES
    initial_name = (current or "").strip().lower()
    initial_idx = schemes.index(initial_name) if initial_name in schemes else 0

    state = {
        "idx": initial_idx,
    }

    def render_menu():
        out: List[Tuple[str, str]] = []
        out.append(("bold", "Choose your color scheme:\n\n"))
        for i, s in enumerate(schemes):
            sel = i == state["idx"]
            is_active = s == initial_name
            cursor = ">" if sel else " "
            check = "● " if is_active else "  "
            line = f" {cursor} {check}{s}\n"
            if sel:
                out.append(("bold fg:#00d7ff" if s != "light" else "bold fg:#0969da", line))
            else:
                out.append(("", line))
        out.append(("\n", "\n"))
        out.append(("fg:ansigray", "  ↑↓ / j k : Move\n"))
        out.append(("fg:ansigray", "  Enter    : Select & Save\n"))
        out.append(("fg:ansigray", "  Esc      : Cancel\n"))
        return FormattedText(out)

    def render_preview():
        selected_scheme = schemes[state["idx"]]
        palette = COLOR_SCHEMES[selected_scheme]
        lines = render_preview_lines(palette, width=54)
        return FormattedText(lines)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("k")
    @kb.add("c-p")
    def _(event):
        state["idx"] = (state["idx"] - 1) % len(schemes)

    @kb.add("down")
    @kb.add("j")
    @kb.add("c-n")
    @kb.add("tab")
    def _(event):
        state["idx"] = (state["idx"] + 1) % len(schemes)

    @kb.add("enter")
    def _(event):
        event.app.exit(result=schemes[state["idx"]])

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    layout = Layout(
        HSplit([
            VSplit([
                Window(FormattedTextControl(render_menu), width=32, dont_extend_height=True),
                Window(FormattedTextControl(render_preview), dont_extend_height=True),
            ]),
        ])
    )

    app = Application(
        layout=layout,
        key_bindings=kb,
        full_screen=False,
        erase_when_done=True,
        mouse_support=False,
    )

    try:
        return app.run()
    except (EOFError, KeyboardInterrupt):
        return None
