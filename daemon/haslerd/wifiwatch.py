"""Out of setup mode by itself: the hotspot watchdog.

moOde starts its "Hasler" hotspot when wlan0 has no address 90 s after boot
(worker.php, checkForIpAddr then activateHotspot) and then never looks back:
the hotspot profile is up, NetworkManager does not replace an active
connection with an autoconnect one, and the worker does not try again. So a
power cut where the router takes longer to boot than the radio (most do) left
the radio in setup mode - warm-white breath, no music - until somebody pulled
the plug.

This asks the root helper (scripts/hasler-wifi-join "retry") to look for the
saved networks now and then while the radio is a hotspot and nobody is on it.
If one is back, the helper joins it and restarts the radio onto it.

When: first ``FIRST_S`` after the hotspot appears (time for a phone that is
already on its way), then backing off along ``EVERY_S``; a wrong password
waits ``WRONG_PASSWORD_S`` (it needs a person: the setup page). A phone on the
hotspot, or the radio playing (Bluetooth works without Wi-Fi), puts it off by
``BUSY_S``. Nothing is tried when moOde was told to be a hotspot on purpose
("Activate Hotspot") or has no network saved.

While the radio has no network but is not yet a hotspot, it also keeps the
Wi-Fi scan fresh (``Wifi.refresh``) so the setup page has a list even on chips
that can't scan as a hotspot. And once, 30 s after haslerd starts, it makes
sure the hotspot can start at all (``Wifi.ensure_hotspot``: a password if it
has none, its address if the profile lost it).

Polled from the main loop like Connectivity; the helper call (up to a minute
or two) runs on its own thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

FIRST_S = 180.0
EVERY_S = (300.0, 600.0, 900.0, 1800.0)      # then every 30 min, for as long as it takes
WRONG_PASSWORD_S = 3600.0
BUSY_S = 60.0
ERROR_S = 600.0
SCAN_EVERY_S = 30.0
ENSURE_AFTER_S = 30.0       # the hotspot check (Wifi.ensure_hotspot), once, off the boot's back


class HotspotWatchdog:
    def __init__(
        self,
        wifi: Any,
        hotspot: Callable[[], bool],
        idle: Callable[[], bool] = lambda: True,
        offline: Callable[[], bool] = lambda: False,
        threaded: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.wifi = wifi
        self._hotspot = hotspot
        self._idle = idle
        self._offline = offline
        self._threaded = threaded
        self._clock = clock
        self._busy = threading.Lock()
        self._since: float | None = None      # hotspot seen since
        self._next: float | None = None       # next try
        self._step = 0
        self._next_scan = 0.0
        self._ensure_at: float | None = None
        self.attempts = 0
        self.last: dict = {}
        self.stopped = ""                     # why it gave up for this hotspot session

    # -- main loop ----------------------------------------------------------------

    def poll(self, now: float) -> None:
        if self._ensure_at is None:
            self._ensure_at = now + ENSURE_AFTER_S
        elif self._ensure_at > 0 and now >= self._ensure_at:
            if self._spawn(self._ensure):
                self._ensure_at = 0.0          # done (or tried): once per start
            return
        try:
            hotspot = bool(self._hotspot())
        except Exception:
            hotspot = False
        if not hotspot:
            if self._since is not None:
                log.info("setup mode over")
            self._since = self._next = None
            self._step, self.stopped = 0, ""
            if now >= self._next_scan and self._safe(self._offline):
                self._next_scan = now + SCAN_EVERY_S
                self._spawn(self._refresh)
            return
        if self._since is None:
            self._since, self._next = now, now + FIRST_S
            log.info("setup mode: will look for the saved Wi-Fi in %.0f s", FIRST_S)
        if self.stopped or self._next is None or now < self._next:
            return
        later = self._next
        self._next = now + EVERY_S[-1]        # until the helper answers
        if not self._spawn(self._try):
            self._next = later                # (a scan still running: next poll)

    def try_now(self, force: bool = False) -> dict:
        """The setup page's "Try my Wi-Fi again" (synchronous; the page's phone is
        on the hotspot, so ``force`` - it will drop off for a moment)."""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "result": "running", "error": "already looking"}
        try:
            return self._attempt(force)
        finally:
            self._busy.release()

    def status(self) -> dict:
        now = self._clock()
        return {
            "active": self._since is not None,
            "next_in": None if (self._next is None or self.stopped) else max(0, round(self._next - now)),
            "attempts": self.attempts,
            "last": dict(self.last),
            "stopped": self.stopped,
        }

    # -- internals --------------------------------------------------------------------

    @staticmethod
    def _safe(fn: Callable[[], bool]) -> bool:
        try:
            return bool(fn())
        except Exception:
            return False

    def _spawn(self, fn: Callable[[], Any]) -> bool:
        if not self._busy.acquire(blocking=False):
            return False

        def run() -> None:
            try:
                fn()
            finally:
                self._busy.release()

        if self._threaded:
            threading.Thread(target=run, name="wifiwatch", daemon=True).start()
        else:
            run()
        return True

    def _ensure(self) -> None:
        ensure = getattr(self.wifi, "ensure_hotspot", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:
                log.debug("hotspot check failed", exc_info=True)

    def _refresh(self) -> None:
        try:
            self.wifi.refresh()
        except Exception:
            log.debug("wifi refresh failed", exc_info=True)

    def _try(self) -> None:
        # Here, off the main loop: "idle" asks the player (health.py).
        if not self._safe(self._idle):
            self._next = self._clock() + BUSY_S
            return
        self._attempt(False)

    def _attempt(self, force: bool) -> dict:
        try:
            reply = self.wifi.retry(force=force)
        except Exception as exc:
            reply = {"ok": False, "result": "error", "error": str(exc)}
        self._schedule(reply)
        return reply

    def _schedule(self, reply: dict) -> None:
        now = self._clock()
        result = str(reply.get("result") or "error")
        self.last = {"result": result, "reason": str(reply.get("reason") or ""),
                     "ssid": str(reply.get("ssid") or ""), "error": str(reply.get("error") or "")}
        if result == "busy":
            self._next = now + BUSY_S
            return
        self.attempts += 1
        if result == "joined":
            log.warning("setup mode: %s is back - restarting onto it", reply.get("ssid"))
            self._next = now + EVERY_S[-1]
        elif result == "no-network":
            # No saved network, or moOde was set to "Activate Hotspot" on purpose.
            self.stopped = "no-network"
            log.info("setup mode: nothing saved to look for")
        elif result == "not-hotspot":
            self._since = self._next = None
        elif result == "failed" and reply.get("reason") == "secrets":
            log.warning("setup mode: the saved Wi-Fi refused its password")
            self._next = now + WRONG_PASSWORD_S
        elif result in ("not-visible", "failed"):
            delay = EVERY_S[min(self._step, len(EVERY_S) - 1)]
            self._step += 1
            log.info("setup mode: saved Wi-Fi %s; again in %.0f s",
                     "not there" if result == "not-visible" else "did not answer", delay)
            self._next = now + delay
        else:
            log.warning("setup mode: retry failed: %s", reply.get("error") or result)
            self._next = now + ERROR_S
