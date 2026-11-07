"""``/api/sleep``: the sleep timer (haslerd/sleep.py).

    GET    /api/sleep    {"sleep": {"ends_in_s", "fading", "minutes"} | null}
    POST   /api/sleep    {"minutes": 15 | 30 | 60 | any number > 0, <= 240}
                         -> {"ok": true, "sleep": {...}}; 400 for a bad number,
                         409 while the radio is in standby or shutting down
    DELETE /api/sleep    -> {"ok": true, "cancelled": bool, "sleep": null}

``/api/status`` carries the same ``sleep`` doc, so the page needn't ask. POST
and DELETE pass the server's guard like every other change (Content-Type,
Host, Origin; web/server.py ``request_refusal``) - they never reach here
otherwise. Each set and cancel is one INFO line (sleep.py).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..sleep import SleepError, SleepTimer

PREFIX = "/api/sleep"


class SleepApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any, timer: SleepTimer) -> None:
        self.app = app
        self.timer = timer

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        if (urlparse(path).path.rstrip("/") or "/") != PREFIX:
            return None
        method = method.upper()
        if method == "GET":
            return 200, {"sleep": self.timer.status()}
        if method == "POST":
            body = body if isinstance(body, dict) else {}
            if "minutes" not in body:
                return 400, {"ok": False, "errors": [], "error": "expected {\"minutes\": n}"}
            try:
                with self.app.lock:         # standby can't flip under the check
                    doc = self.timer.set(body["minutes"])
            except SleepError as exc:
                return exc.status, {"ok": False, "errors": [], "error": str(exc)}
            return 200, {"ok": True, "sleep": doc}
        if method == "DELETE":
            return 200, {"ok": True, "cancelled": self.timer.cancel("the page"), "sleep": None}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}
