import pytest
from hubble.theme import (
    COLOR_SCHEMES,
    ORDERED_SCHEMES,
    get_palette,
    get_rich_theme,
    render_preview_lines,
)
from hubble.ui import ACTIVE_THEME, apply_theme, render_diff


def test_theme_registry():
    expected = [
        "terminal",
        "light",
        "solarized light",
        "colorblind-friendly light",
        "dark",
        "solarized dark",
        "colorblind-friendly dark",
        "tokyo night",
    ]
    assert ORDERED_SCHEMES == expected
    for name in expected:
        assert name in COLOR_SCHEMES
        p = COLOR_SCHEMES[name]
        assert p.name == name
        assert p.user_prefix
        assert p.user_text
        assert p.diff_add_fg or p.diff_add_bg
        assert p.diff_del_fg or p.diff_del_bg


def test_get_palette_fallback():
    assert get_palette("tokyo night").name == "tokyo night"
    assert get_palette("TOKYO NIGHT").name == "tokyo night"
    assert get_palette("solarized dark").name == "solarized dark"
    assert get_palette("nonexistent_theme").name == "terminal"
    assert get_palette(None).name == "terminal"


def test_get_rich_theme():
    theme = get_rich_theme("dark")
    assert "info" in theme.styles
    assert "error" in theme.styles
    assert "warning" in theme.styles


def test_render_preview_lines():
    for name in ORDERED_SCHEMES:
        palette = get_palette(name)
        lines = render_preview_lines(palette, width=54)
        assert len(lines) > 15
        text_content = "".join(part[1] for part in lines)
        assert "add a greeting function" in text_content
        assert "func greet(name string)" in text_content
        assert "Thought Process" in text_content
        assert "write_file main.go" in text_content


def test_apply_theme_integration():
    apply_theme("tokyo night")
    from hubble.ui import ACTIVE_THEME as current
    assert current == "tokyo night"

    diff = "--- a/test.py\n+++ b/test.py\n@@ -1,1 +1,2 @@\n-old\n+new"
    rendered = render_diff(diff)
    assert rendered is not None
    # Reset back to terminal
    apply_theme("terminal")


def test_get_prompt_style():
    from hubble.theme import get_prompt_style
    # Default terminal prompt style
    style_term = get_prompt_style("terminal")
    assert "" in style_term
    assert style_term[""] == "#00d7ff"
    assert "prompt" in style_term
    assert style_term["prompt"] == "bold #00d7ff"
    assert "completion-menu" in style_term
    assert "completion-menu.completion.current" in style_term
    assert "scrollbar.button" in style_term
    assert "bottom-toolbar" in style_term
    # Terminal menu is dark slate (not white/grey)
    assert "#161b22" in style_term["completion-menu"]
    assert "#58a6ff" in style_term["completion-menu.completion.current"]

    # Tokyo Night prompt style
    style_tokyo = get_prompt_style("tokyo night")
    assert style_tokyo[""] == "#7aa2f7"
    assert style_tokyo["prompt"] == "#7aa2f7 bold"
    assert "#16161e" in style_tokyo["completion-menu"]
    assert "#7aa2f7" in style_tokyo["completion-menu.completion.current"]


def test_logo_selection_and_rendering():
    from hubble.banner import compose_grid, home_info_lines, _logo_rows
    # 1. Full 6-row ANSI Shadow block letters intact
    rows = _logo_rows()
    assert len(rows) == 6
    assert "██╗  ██╗" in rows[0]
    assert "╚═╝  ╚═╝" in rows[5]

    st, tips = home_info_lines(version="4.3.4", provider="hubble", model="m", mode="default", root="x", session_id=None,
                               memory_files=[], model_count=5, provider_count=1, resumable=0, show_provider=False)

    def lines(g):
        return ["".join(c for c, _ in r) for r in g]

    # Regular clean wordmark logo
    grid_reg = lines(compose_grid(100, 30, 1.0, st, tips, "4.3.4", logo_style="regular"))
    assert "▲ H\u2009U\u2009B\u2009B\u2009L\u2009E" in grid_reg[0]

    # Block logo (size 1)
    grid_block = lines(compose_grid(100, 30, 1.0, st, tips, "4.3.4", logo_style="block"))
    assert "██╗  ██╗" in grid_block[0]

    # Small 1-line wordmark (size 0)
    grid_small = lines(compose_grid(100, 30, 1.0, st, tips, "4.3.4", logo_style="small"))
    assert "▲ Hubble" in grid_small[0]


