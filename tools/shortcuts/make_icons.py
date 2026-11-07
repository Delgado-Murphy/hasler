#!/usr/bin/env python3
"""Draw the home-screen icons for the radio's /do/<action> pages.

    python3 tools/shortcuts/make_icons.py

Writes daemon/haslerd/web/static/icons/do-<action>.png (192 px): the key
page's dark ground, a brass ring and one glyph (+, −, 1-6, standby,
play/pause, next). Needs Pillow; run on a maker's Mac, the PNGs are committed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "daemon"))
from haslerd.web.phone_api import ACTIONS, slug  # noqa: E402

OUT = ROOT / "daemon" / "haslerd" / "web" / "static" / "icons"
S = 192
SCALE = 4                    # drawn large, scaled down: smooth edges
GROUND = (23, 16, 11)
DISC = (36, 23, 15)
BRASS = (201, 161, 90)
AMBER = (255, 178, 56)
FONT = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"


def draw(action_id: str) -> Image.Image:
    n = S * SCALE
    im = Image.new("RGB", (n, n), GROUND)
    d = ImageDraw.Draw(im)
    c = n / 2
    r = n * 0.36
    d.ellipse((c - r, c - r, c + r, c + r), fill=DISC, outline=BRASS, width=int(n * 0.022))
    w = int(n * 0.05)
    u = n * 0.13            # glyph half-size
    if action_id in ("louder", "quieter"):
        d.rounded_rectangle((c - u, c - w / 2, c + u, c + w / 2), radius=w / 2, fill=AMBER)
        if action_id == "louder":
            d.rounded_rectangle((c - w / 2, c - u, c + w / 2, c + u), radius=w / 2, fill=AMBER)
    elif action_id.startswith("key/"):
        font = ImageFont.truetype(FONT, int(n * 0.34))
        d.text((c, c + n * 0.01), action_id.split("/")[1], font=font, fill=AMBER, anchor="mm")
    elif action_id == "standby":
        rr = u * 1.05
        d.arc((c - rr, c - rr, c + rr, c + rr), start=-60, end=240, fill=AMBER, width=w)
        d.rounded_rectangle((c - w / 2, c - rr * 1.15, c + w / 2, c - rr * 0.1), radius=w / 2, fill=AMBER)
    elif action_id == "playpause":
        d.polygon([(c - u * 1.2, c - u), (c - u * 1.2, c + u), (c + u * 0.15, c)], fill=AMBER)
        bw = w * 0.9
        for x in (c + u * 0.45, c + u * 0.95):
            d.rounded_rectangle((x, c - u, x + bw, c + u), radius=bw / 3, fill=AMBER)
    elif action_id == "next":
        d.polygon([(c - u * 1.1, c - u), (c - u * 1.1, c + u), (c + u * 0.5, c)], fill=AMBER)
        d.rounded_rectangle((c + u * 0.6, c - u, c + u * 0.6 + w, c + u), radius=w / 3, fill=AMBER)
    return im.resize((S, S), Image.LANCZOS)


def main() -> None:
    for aid in ACTIONS:
        path = OUT / f"do-{slug(aid)}.png"
        draw(aid).save(path, optimize=True)
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
