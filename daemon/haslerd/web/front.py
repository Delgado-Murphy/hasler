"""The key page on the radio's main port: http://hasler.local/ (no :8080).

moOde's nginx already owns port 80. ``scripts/hasler-web-front`` (root,
through sudo; config/sudoers/haslerd allows exactly that file) puts one site
file of ours in its ``sites-enabled``: port 80 hands every name but the
loopback ones to haslerd on 127.0.0.1:8080, and moOde's own pages move to
:8081 (``MOODE_PORT``). haslerd keeps listening on :8080 itself, so bookmarks,
Home Assistant (``_hasler._tcp`` says 8080), multiroom peers and the radio's
own scripts carry on as they were. moOde's files are never edited.

A moOde update and moOde's HTTPS switch empty ``sites-enabled``; so this
checks, at start and every ``CHECK_EVERY_S``, that port 80 really reaches
this haslerd (``GET /api/front`` through nginx answers ``"via_front": true``)
and runs ``hasler-web-front ensure`` when it doesn't. ``web.front: false``
in box.yaml takes the site away again (moOde back on port 80).

nginx tells haslerd who asked and on which address (``X-Hasler-Peer``,
``X-Hasler-Local``); web/server.py believes those only on a connection from
loopback (``front_addrs``), so the Host/Origin check, the IPv6 "this network
only" rule and the loopback-only canary see the real client, not nginx.

``/api/settings`` carries ``front`` (``doc()``): the page prints
``hasler.local`` on its cards while the front works, ``hasler.local:8080``
when it doesn't, and links moOde's pages at the right port (``moode_url``).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import subprocess
import threading
from typing import Any, Callable

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-web-front"
FRONT_PORT = 80          # nginx (moOde's), the main port
MOODE_PORT = 8081        # moOde's pages behind the front (8000 is MPD's HTTP stream)
CHECK_EVERY_S = 600.0
RETRY_S = 60.0           # while port 80 doesn't reach the page
FIRST_CHECK_S = 5.0      # after the server is up; nginx usually is long before
PROBE_TIMEOUT_S = 3.0
# Any name but the loopback ones reaches the front (localhost stays moOde's).
PROBE_HOST = "hasler.local"


class WebFront:
    def __init__(self, enabled: bool = True, helper: str = HELPER,
                 run: Callable[..., Any] = subprocess.run,
                 probe: Callable[[], bool] | None = None) -> None:
        self.enabled = enabled
        self.helper = helper
        self._run = run
        self._probe = probe or self.probe_http
        # None until the first check: the page then keeps :8080 on its cards.
        self.active: bool | None = None
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        """The helper is installed (not on a laptop running --fake)."""
        return os.access(self.helper, os.X_OK)

    def doc(self) -> dict:
        return {"active": bool(self.active), "port": FRONT_PORT, "moode_port": MOODE_PORT}

    # -- checking -------------------------------------------------------------

    @staticmethod
    def probe_http(host: str = "127.0.0.1", port: int = FRONT_PORT) -> bool:
        """Does port 80 reach this haslerd through the front? (``GET /api/front``)"""
        conn = http.client.HTTPConnection(host, port, timeout=PROBE_TIMEOUT_S)
        try:
            conn.request("GET", "/api/front", headers={"Host": PROBE_HOST})
            resp = conn.getresponse()
            body = resp.read(4096)
            if resp.status != 200:
                return False
            return json.loads(body or b"{}").get("via_front") is True
        except (OSError, ValueError, http.client.HTTPException):
            return False
        finally:
            conn.close()

    def helper_call(self, verb: str) -> dict:
        try:
            out = self._run(["sudo", "-n", self.helper, verb], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        try:
            return json.loads((out.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "error": (out.stderr or "no answer").strip()[:200]}

    def check(self) -> bool:
        """Make port 80 the key page's (or moOde's again with web.front: false).
        Returns whether it is the key page's now."""
        with self._lock:
            if not self.enabled:
                if self.available:
                    doc = self.helper_call("remove")
                    if doc.get("changed"):
                        log.info("web: port 80 handed back to moOde (web.front: false)")
                self.active = False
                return False
            if self._probe():
                if self.active is not True:
                    log.info("web: the key page is on port 80 too (moOde's pages on :%d)", MOODE_PORT)
                self.active, self.error = True, ""
                return True
            if not self.available:
                self.active = False
                return False
            doc = self.helper_call("ensure")
            ok = bool(doc.get("ok")) and self._probe()
            if ok:
                log.info("web: the key page is on port 80 again%s (moOde's pages on :%d)",
                         " (the nginx site was put back)" if doc.get("changed") else "", MOODE_PORT)
                self.error = ""
            else:
                error = str(doc.get("error") or "port 80 does not reach the key page")
                if error != self.error:
                    log.warning("web: the key page is only on :8080: %s", error)
                self.error = error
            self.active = ok
            return ok

    # -- the background check ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="web-front", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(FIRST_CHECK_S):
            return
        retry = RETRY_S
        while True:
            try:
                ok = self.check()
            except Exception:   # noqa: BLE001 - never take haslerd down over port 80
                log.exception("web: the port 80 check failed")
                ok = False
            # Not there yet (nginx starting after us at boot, moOde mid-update):
            # sooner, backing off to the usual pace if it stays that way.
            if ok or not self.enabled:
                wait, retry = CHECK_EVERY_S, RETRY_S
            else:
                wait, retry = retry, min(retry * 2, CHECK_EVERY_S)
            if self._stop.wait(wait):
                return
