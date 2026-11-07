#!/usr/bin/env python3
"""Draw the key page's home-screen icons (daemon/haslerd/web/static/icons/).

The mark: the radio's dial glass (brass rim, amber needle) over its bank of
piano keys, on the page's dark Bakelite ground. Everything sits inside the
central 60 % of the square, so the same picture is safe as a "maskable" icon
(Android crops to a circle or squircle) and as Apple's touch icon.

Needs Pillow (not a haslerd dependency - the PNGs are committed):

    python3 scripts/make_icons.py

Re-run only when the mark changes; the output is deterministic.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - a developer tool
    sys.exit("make_icons.py needs Pillow: pip install pillow")

OUT = Path(__file__).resolve().parent.parent / "daemon" / "haslerd" / "web" / "static" / "icons"

# The page's palette (index.html :root).
GROUND = (0x17, 0x10, 0x0B)
VENEER = (0x24, 0x17, 0x0F)
WELL = (0x15, 0x0E, 0x09)
BRASS = (0xC9, 0xA1, 0x5A)
AMBER = (0xFF, 0xB2, 0x38)
CREAM = (0xED, 0xE3, 0xCC)
LINE2 = (0x5A, 0x41, 0x28)

SS = 4  # supersampling: draw big, shrink with a good filter


def mark(size: int) -> Image.Image:
    S = size * SS
    img = Image.new("RGB", (S, S), GROUND)
    d = ImageDraw.Draw(img)
    u = S / 100.0  # one unit = 1 % of the side

    def box(x0: float, y0: float, x1: float, y1: float) -> tuple[int, int, int, int]:
        return (round(x0 * u), round(y0 * u), round(x1 * u), round(y1 * u))

    # A soft veneer disc behind the mark (inside the maskable safe zone).
    d.ellipse(box(14, 14, 86, 86), fill=VENEER)

    # Dial glass: brass rim, dark well, scale ticks, amber glow band, needle.
    d.rounded_rectangle(box(24, 27, 76, 51), radius=round(6 * u), fill=BRASS)
    d.rounded_rectangle(box(26.5, 29.5, 73.5, 48.5), radius=round(4.5 * u), fill=WELL)
    d.rounded_rectangle(box(29, 37.5, 71, 40.5), radius=round(1.5 * u), fill=(0x6A, 0x46, 0x16))
    for i in range(9):
        x = 31 + i * 4.75
        tall = i % 2 == 0
        d.rectangle(box(x - 0.45, 32 if tall else 33.5, x + 0.45, 36), fill=LINE2 if not tall else BRASS)
    d.rounded_rectangle(box(53.2, 31, 55.2, 47), radius=round(1 * u), fill=AMBER)

    # The key bank: six cream keys and the amber PU key.
    n, x0, x1, gap = 7, 24, 76, 1.6
    w = (x1 - x0 - gap * (n - 1)) / n
    for i in range(n):
        kx = x0 + i * (w + gap)
        colour = AMBER if i == n - 1 else CREAM
        d.rounded_rectangle(box(kx, 56, kx + w, 73), radius=round(1.6 * u), fill=colour)
        d.rectangle(box(kx, 70.5, kx + w, 73), fill=tuple(int(c * 0.78) for c in colour))
    # The one playing sits down a little, lit like the page's pressed key.
    kx = x0 + 2 * (w + gap)
    d.rounded_rectangle(box(kx, 56, kx + w, 73), radius=round(1.6 * u), fill=VENEER)
    d.rounded_rectangle(box(kx, 57.5, kx + w, 73), radius=round(1.6 * u), fill=AMBER)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180),
                       ("favicon-32.png", 32)):
        mark(size).save(OUT / name, optimize=True)
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
