"""The accent the reports are dressed in.

The fullscreen app has a palette setting — "omarchy" to follow the active
desktop theme, "of-light" or "of-dark" to pin its own — and the reports follow
the same choice, so the mark on a page wears the same accent as the window it
came from. Only the accent travels into a report: the page keeps its white
paper and dark ink, so an export stays readable and printable whatever the
desktop is wearing.

This module owns the one fact the reports cannot get from the database — where
the desktop theme keeps its colours and how its file spells them — so that
:mod:`gtd.exporter` stays a pure function of the document it is handed.
"""

from __future__ import annotations

import os
import re

# The palettes the `theme` setting accepts; the same three names the fullscreen
# app's switcher offers.
THEMES = ("omarchy", "of-light", "of-dark")

# The accent each of the app's own presets names, from app/GtdTheme.qml.
PRESET_ACCENTS = {
    "of-light": "#6e5ae0",
    "of-dark": "#9a86f0",
}

# The whole preset palettes, mirroring app/GtdTheme.qml. The web client reads
# these through /theme.css so the phone wears the same colours as the window.
PRESET_PALETTES = {
    "of-light": {
        "background": "#ffffff", "text": "#1c1c1e", "dim": "#8a8a8e",
        "line": "#d8d8dc", "accent": "#6e5ae0", "urgent": "#d64541",
        "orange": "#e8a33d", "blue": "#3e7bfa", "purple": "#8e6fd8",
        "pink": "#d9649a", "green": "#5aa469", "grey": "#9aa0a6",
        "font": '"Adwaita Sans", system-ui, sans-serif',
    },
    "of-dark": {
        "background": "#1c1c1e", "text": "#f2f2f7", "dim": "#9a9aa0",
        "line": "#3a3a3f", "accent": "#9a86f0", "urgent": "#d64541",
        "orange": "#e8a33d", "blue": "#4a7cc7", "purple": "#a98ce0",
        "pink": "#e07bb0", "green": "#5aa469", "grey": "#8a8a8e",
        "font": '"Adwaita Sans", system-ui, sans-serif',
    },
}

# What a machine with no Omarchy theme still paints, taken from Color.qml's
# own starting values. Kept here so /theme.css never hands back a blank page.
FALLBACK_PALETTE = {
    "background": "#101315", "text": "#cacccc", "dim": "#707880",
    "line": "#2b3033", "accent": "#cacccc", "urgent": "#a55555",
    "orange": "#eb927b", "blue": "#cacccc", "purple": "#ad8ee6",
    "pink": "#e07bb0", "green": "#7cb87c", "grey": "#707880",
    "font": "monospace",
}


# Color.qml's own starting accent: what is in force before any theme file has
# answered. A machine with no Omarchy theme still gets a usable accent.
FALLBACK_ACCENT = "#cacccc"

# A colors.toml line: `key = "#rrggbb"`, quotes optional, comments allowed.
_HEX = "#[0-9A-Fa-f]{6}"


def _colors_file() -> str:
    """The active desktop theme's colors.toml, the path Color.qml reads.

    Color.qml builds the path from HOME, not from the XDG variables, so this
    does the same.
    """
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".local", "state", "omarchy",
                        "current", "theme", "colors.toml")


def _value(raw: str, key: str) -> str:
    """The hex colour a colors.toml gives `key`, or "" when it says nothing."""
    match = re.search(r"^\s*" + re.escape(key) + r"\s*=\s*[\"']?(" + _HEX + r")",
                      raw, re.M)
    return match.group(1) if match else ""


def desktop_accent() -> str:
    """The active desktop theme's accent, or Color.qml's fallback.

    Reads the theme's accent, falling back to color4 the way Color.qml does for
    a theme written before the separate accent key existed.
    """
    try:
        with open(_colors_file(), "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return FALLBACK_ACCENT
    return _value(raw, "accent") or _value(raw, "color4") or FALLBACK_ACCENT


def accent(name: str | None = None) -> str:
    """The accent a palette name means: a preset's, or the desktop theme's.

    "omarchy" (the default), a name the app does not know, and no name at all
    all resolve the desktop theme together, the way the app treats an unset
    palette setting.
    """
    key = str(name or "omarchy").strip().lower()
    return PRESET_ACCENTS.get(key) or desktop_accent()


def palette(name: str | None = None) -> dict[str, str]:
    """The colours the web client paints itself in.

    A preset answers with its own dictionary; "omarchy" (the default) reads the
    active desktop theme the way Color.qml does, and falls back key by key so a
    partial theme file still produces a usable page.
    """
    key = str(name or "omarchy").strip().lower()
    preset = PRESET_PALETTES.get(key)
    if preset:
        return dict(preset)

    try:
        with open(_colors_file(), "r", encoding="utf-8", errors="replace") as handle:
            raw = handle.read()
    except OSError:
        return dict(FALLBACK_PALETTE)

    def pick(*names: str) -> str:
        for name in names:
            value = _value(raw, name)
            if value:
                return value
        return ""

    result = dict(FALLBACK_PALETTE)
    accent = pick("accent", "color4") or FALLBACK_ACCENT
    for key, value in (
        ("background", pick("background")),
        ("text", pick("foreground")),
        ("dim", pick("dark_foreground")),
        ("accent", accent),
        ("urgent", pick("red", "urgent")),
        ("orange", pick("orange")),
        ("blue", pick("blue", "color4")),
        ("purple", pick("magenta")),
        ("pink", pick("pink")),
        ("green", pick("green")),
        ("grey", pick("dark_foreground")),
    ):
        if value:
            result[key] = value

    # The desktop theme does not name a hairline; GtdTheme.qml derives one from
    # the popup text at 18%. The same mix is computed here so the web client
    # gets a real colour instead of a CSS `color-mix` Safari 16 cannot read.
    result["line"] = _mix(result["text"], result["background"], 0.18)
    result["font"] = "monospace"
    return result


def _mix(foreground: str, background: str, amount: float) -> str:
    """`amount` of `foreground` over `background`, as #rrggbb."""
    try:
        fg = [int(foreground.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
        bg = [int(background.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    except (ValueError, IndexError):
        return "#808080"
    return "#%02x%02x%02x" % tuple(
        round(f * amount + b * (1 - amount)) for f, b in zip(fg, bg))


def stylesheet(name: str | None = None) -> str:
    """The palette as the CSS custom properties the web client reads.

    Served at /theme.css, so the look follows the `theme` setting without a
    second definition of the colours living in JavaScript.
    """
    values = palette(name)
    lines = [":root {"]
    for key in ("background", "text", "dim", "line", "accent", "urgent",
                "orange", "blue", "purple", "pink", "green", "grey"):
        lines.append(f"  --{key}: {values[key]};")
    lines.append(f"  --font: {values['font']};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def pdf_colour(colour: str) -> str:
    """A hex colour as the RGB triple a PDF content stream paints with."""
    text = str(colour or "").strip().lstrip("#")
    if len(text) != 6:
        return "0 0 0"
    try:
        red, green, blue = (int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return "0 0 0"
    return "%.2f %.2f %.2f" % (red / 255.0, green / 255.0, blue / 255.0)
