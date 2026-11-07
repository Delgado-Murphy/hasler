"""``--fake`` only: what a page test cannot do with keys alone.

``POST /api/fake/phone`` ``{"source": "bluetooth", "device": "Pixel 11",
"title": "Song", "artist": "Band"}`` - a phone takes the speaker, as moOde's
renderer flag would report it; ``{"source": null}`` - it stops. The fake player
(player/base.FakePlayer) does the rest; the main loop's cast watch sees the
change exactly as on a radio.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..actions import RENDERERS

SOURCES = (*RENDERERS, "spotify")


class FakeApi:
    """The route above, for ``WebApp.extra_routes`` (main.py, ``--fake``)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        if urlparse(path).path.rstrip("/") != "/api/fake/phone":
            return None
        if method.upper() != "POST":
            return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}
        body = body if isinstance(body, dict) else {}
        player = self.app.actions.player
        if not hasattr(player, "phone_casts"):
            return 503, {"ok": False, "errors": [], "error": "not a fake player"}
        source = body.get("source")
        with self.app.lock:
            if source is None:
                player.phone_leaves()
            elif source in SOURCES:
                player.phone_casts(source, str(body.get("device") or "Phone"),
                                   str(body.get("title") or ""), str(body.get("artist") or ""))
            else:
                raise ValueError(f"source must be one of {', '.join(SOURCES)}, or null")
        return 200, {"ok": True, "source": source}
