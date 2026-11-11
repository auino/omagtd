#!/usr/bin/env python3
"""Draw the web app's icons, with the standard library only.

The plugin ships no build step, so the PNGs are checked in and this script is
only run when the mark changes. It is a deliberately small rasteriser: a
rounded square, one check, 4x supersampling for smooth edges.

    python3 tools/make_icons.py

Writes web/icons/icon-192.png, icon-512.png and icon-maskable-512.png.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BACKGROUND = (28, 28, 30)      # of-dark surface, so the icon matches the app
MARK = (154, 134, 240)         # of-dark accent
RADIUS = 0.22                  # corner radius, as a fraction of the side
THICKNESS = 0.085              # check stroke, as a fraction of the side

# The check, in a 0..1 box. A maskable icon is inset so the platform's crop
# never clips it.
CHECK = ((0.28, 0.52), (0.43, 0.67), (0.73, 0.35))


def _distance(px: float, py: float, ax: float, ay: float,
              bx: float, by: float) -> float:
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    length = vx * vx + vy * vy
    t = 0.0 if not length else max(0.0, min(1.0, (wx * vx + wy * vy) / length))
    dx, dy = px - (ax + t * vx), py - (ay + t * vy)
    return (dx * dx + dy * dy) ** 0.5


def _inside_round_rect(px: float, py: float, radius: float) -> bool:
    cx = min(max(px, radius), 1.0 - radius)
    cy = min(max(py, radius), 1.0 - radius)
    return (px - cx) ** 2 + (py - cy) ** 2 <= radius * radius


def _coverage(x: int, y: int, size: int, *, maskable: bool) -> tuple[int, int, int, int]:
    samples = 0.0
    background_hits = 0.0
    mark_hits = 0.0
    steps = 2
    for sy in range(steps):
        for sx in range(steps):
            px = (x + (sx + 0.5) / steps) / size
            py = (y + (sy + 0.5) / steps) / size
            samples += 1

            # Maskable icons are full-bleed; any icon is a rounded square.
            if maskable or _inside_round_rect(px, py, RADIUS):
                background_hits += 1

            # Inset the mark on maskable icons, into the safe 80% circle.
            mx, my = px, py
            if maskable:
                mx = (px - 0.5) * 1.28 + 0.5
                my = (py - 0.5) * 1.28 + 0.5

            near = min(
                _distance(mx, my, *CHECK[0], *CHECK[1]),
                _distance(mx, my, *CHECK[1], *CHECK[2]),
            )
            if near <= THICKNESS / 2:
                mark_hits += 1

    background = background_hits / samples
    mark = min(mark_hits / samples, background)
    alpha = background
    if alpha <= 0:
        return (0, 0, 0, 0)
    # Composite the mark over the background in premultiplied terms, then
    # un-premultiply for the straight-alpha PNG.
    r = round((MARK[0] * mark + BACKGROUND[0] * (background - mark)) / background)
    g = round((MARK[1] * mark + BACKGROUND[1] * (background - mark)) / background)
    b = round((MARK[2] * mark + BACKGROUND[2] * (background - mark)) / background)
    return (r, g, b, round(alpha * 255))


def _png(size: int, *, maskable: bool) -> bytes:
    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter: none
        for x in range(size):
            rows.extend(_coverage(x, y, size, maskable=maskable))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "web" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    for size, maskable, name in (
        (192, False, "icon-192.png"),
        (512, False, "icon-512.png"),
        (512, True, "icon-maskable-512.png"),
    ):
        target = out / name
        target.write_bytes(_png(size, maskable=maskable))
        print(f"wrote {target.relative_to(out.parent.parent)} ({size}x{size})")


if __name__ == "__main__":
    main()
