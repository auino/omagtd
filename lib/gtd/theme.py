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
