"""``GET /api/checkin/preview``: "See what it sends" - today's check-in, verbatim.

The opt-in daily check-in (docs/ota.md §5) is built and sent by the update
package (update/hasler_update/checkin.py) after the daily check, from the
updater's own files plus two documents haslerd already serves
(``/api/diagnostics`` and ``/api/health``). So that the page shows exactly
what would go, this route runs that very builder - ``hasler-update checkin
--preview --stdin`` - and hands it the two documents on stdin instead of over
HTTP (no request to ourselves from inside a request). One builder, one
payload: the switch on the Updates page (web/software.py ``checkin``) only
says whether it is sent.

    GET /api/checkin/preview -> {"payload": {...}, "bytes": n, "text": "<the JSON, pretty>"}
    404 {"ok": false, "error"} on a radio without the updater (an rsync bench box).

The launcher is ``/usr/local/sbin/hasler-update`` on a radio; in the repo
(``--fake``) it is ``update/bin/hasler-update`` next to the release root.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .server import Unavailable

log = logging.getLogger(__name__)

PATHS = ("/api/checkin/preview",)
LAUNCHER = Path("/usr/local/sbin/hasler-update")
TIMEOUT_S = 20.0


class NotHere(Exception):
    """No updater on this radio - HTTP 404."""


def find_launcher(release: Path | None = None) -> Path | None:
    if os.access(LAUNCHER, os.X_OK):
        return LAUNCHER
    if release is None:
        from .. import boxconfig
        release = boxconfig.find_release()
    candidate = Path(release) / "update" / "bin" / "hasler-update"
    return candidate if candidate.is_file() else None


class CheckinApi:
    """The route above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any, launcher: Path | None = None, run: Callable[..., Any] = subprocess.run) -> None:
        self.app = app
        self.launcher = launcher
        self.run = run

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PATHS:
            return None
        if method.upper() != "GET":
            return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}
        try:
            return 200, {"ok": True, **self.preview()}
        except NotHere as exc:
            return 404, {"ok": False, "errors": [], "error": str(exc), "installed": False}

    def _launcher(self) -> Path:
        if self.launcher is None:
            paths = getattr(self.app, "box_paths", None)
            self.launcher = find_launcher(getattr(paths, "release", None))
        if self.launcher is None:
            raise NotHere("the updater isn't on this radio, so there is no check-in to show")
        return self.launcher

    def preview(self) -> dict:
        """Today's payload, from the updater's builder fed haslerd's own documents."""
        from .. import health
        from . import diagnostics

        launcher = self._launcher()
        docs = {"diagnostics": diagnostics.owner_doc(self.app), "health": health.health_report(self.app)}
        argv = [sys.executable, "-I", str(launcher), "checkin", "--preview", "--stdin"]
        env = {k: v for k, v in os.environ.items() if k.startswith("HASLER_") or k in ("PATH", "LANG", "LC_ALL", "TZ")}
        try:
            out = self.run(argv, input=json.dumps(docs), capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise Unavailable(f"the updater couldn't build the check-in ({type(exc).__name__})") from None
        payload = None
        for line in reversed((out.stdout or "").strip().splitlines()):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
        if out.returncode != 0 or not isinstance(payload, dict):
            log.warning("hasler-update checkin --preview failed (exit %s): %s", out.returncode, (out.stderr or "")[-300:])
            raise Unavailable("the updater couldn't build the check-in")
        compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return {"payload": payload, "bytes": len(compact.encode("utf-8")),
                "text": json.dumps(payload, indent=1, ensure_ascii=False)}
