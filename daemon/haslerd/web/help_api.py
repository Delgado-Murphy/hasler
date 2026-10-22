"""``/api/help``: "Let the makers help (24 h)" - remote help over Tailscale.

Opt-in and off by default (docs/ota.md section 5, docs/remote-help.md). The
owner presses Start on the key page and confirms with a press on the radio
(web/confirm.py, action ``help``); the root helper ``hasler-help`` starts a
throwaway tailscaled and ``tailscale up`` without any key, so Tailscale hands
out a one-time login URL. The page shows it ("Send this link to the
makers"); a maker opens it while signed in to the makers' tailnet and the
radio joins. After 24 h (a systemd timer the helper set, so it holds even
when haslerd is down), Stop, or a reboot, the radio logs out and tailscaled
stops. Not approved within an hour: it ends too.

    GET  /api/help        {available, state, status_text, network, login_url, started_at, expires_at,
                           approve_by, address, dns_name, tailnet, hostname,
                           peers_active, ssh, page, last, error}
    POST /api/help/start  {"confirm": token}   (POST /api/confirm/request
                                                {"action": "help"} first)
    POST /api/help/stop   {}                   (no press: stopping is always safe)

``state``: off | starting | waiting (the login URL is ready, no maker has
approved yet) | connected | ending. ``connected`` only means the radio is on
the makers' tailnet; ``peers_active`` ([{name, user}], from the helper:
tailnet devices with a live session to the radio in the last ~2 min) says
whether a maker is actually on it now, and ``status_text`` says so: "A maker
is connected now (wmurphy@github)." or "Connected - waiting for a maker.". ``last`` is how the previous session
ended ({"at", "why": stopped|expired|unapproved|wrong-tailnet|failed}) until
the next start or a reboot. Times are epoch seconds.

The login URL lets whoever opens it add this radio to *their* tailnet, so it
is treated like a password: never logged, only returned to the page, and
gone once the radio is connected. ``web.help_tailnet`` in haslerd.yaml
(default MAKERS_TAILNET; "" = no check) is the makers' tailnet name: a radio that joins any other tailnet ends
help at once ("wrong-tailnet").

Errors are {"ok": false, "error"}: 403 not confirmed on the radio, 409 no
panel to confirm with, 503 Tailscale isn't installed or the helper failed.
The state lives in the helper (systemd units, /run), so it survives a
haslerd restart.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable
from urllib.parse import urlparse

from . import integration
from .server import Unavailable

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-help"
PATHS = ("/api/help", "/api/help/start", "/api/help/stop")
START_TIMEOUT_S = 60.0
TIMEOUT_S = 30.0
CACHE_S = 2.0            # the page polls; one sudo per poll window is plenty
STATES = ("off", "starting", "waiting", "connected", "ending")
NETWORK = "Delgado & Murphy's help network"     # the tailnet "delgado-murphy.org.github"
# The makers' tailnet: help ends at once on any other (owner: "keep tailscale
# tight"). Default for every radio; web.help_tailnet overrides, "" switches
# the check off.
MAKERS_TAILNET = "delgado-murphy.org.github"
STATUS_TEXT = {
    "off": "Off.",
    "starting": "Starting…",
    "waiting": "Waiting for a maker to approve the link.",
    "connected": "Connected - waiting for a maker.",
    "ending": "Ending…",
}
WHY_TEXT = {
    "stopped": "Stopped from the key page.",
    "expired": "The 24 hours were up.",
    "unapproved": "Nobody approved the link within an hour.",
    "wrong-tailnet": "The radio joined a tailnet that isn't the makers', so help was ended.",
    "failed": "Remote help couldn't start.",
}


def slug(text: str, limit: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:limit].strip("-")


def tailnet_hostname(room: str, radio_id: str) -> str:
    """hasler-<room>-<4 hex of the radio id>: what the makers see in the
    Tailscale admin. Fits the helper's check (lowercase, digits, dashes, <= 63)."""
    room_part = slug(room, 40)
    if room_part.startswith("hasler"):
        room_part = room_part[len("hasler"):].strip("-")
    short = slug(radio_id, 4) or "0000"
    return "-".join(p for p in ("hasler", room_part, short) if p)


def default_runner(args: list[str], timeout: float) -> tuple[int, str]:
    """sudo -n hasler-help ARGS -> (returncode, stdout). stdout can hold the
    login URL: it is returned, never logged."""
    p = subprocess.run(["sudo", "-n", HELPER, *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 and p.stderr.strip():
        log.warning("hasler-help %s: %s", args[0], p.stderr.strip()[-300:])
    return p.returncode, p.stdout


def peers_from(raw: Any) -> list[dict]:
    """The helper's ``peers_active``, checked: [{name, user}] (user may be None)."""
    out = []
    for p in raw if isinstance(raw, list) else []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        user = p.get("user")
        out.append({"name": str(p["name"])[:80], "user": str(user)[:120] if user else None})
    return out


def makers_text(peers: list[dict]) -> str:
    """"A maker is connected now (wmurphy@github)." - who, by login, else device."""
    who = []
    for p in peers:
        label = p.get("user") or p.get("name")
        if label and label not in who:
            who.append(label)
    lead = "A maker is" if len(who) <= 1 else f"{len(who)} makers are"
    return f"{lead} connected now ({', '.join(who)})." if who else "A maker is connected now."


class HelpApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(
        self,
        app: Any,
        runner: Callable[[list[str], float], tuple[int, str]] = default_runner,
        helper_present: Callable[[], bool] = lambda: os.access(HELPER, os.X_OK),
        clock: Callable[[], float] = time.time,
        tailnet: str | None = None,
    ) -> None:
        self.app = app
        self.runner = runner
        self.helper_present = helper_present
        self.clock = clock
        self.tailnet = (tailnet or "").strip() or None
        self._lock = threading.Lock()
        self._busy: str | None = None       # "starting" | "ending" while the helper works
        self._cache: tuple[float, dict] | None = None
        self._error: str | None = None
        self._makers: tuple = ()             # who was on at the last look (for the journal)

    # -- routing ------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PATHS:
            return None
        method = method.upper()
        if p == "/api/help" and method == "GET":
            return 200, self.doc()
        if p == "/api/help/start" and method == "POST":
            return 200, {"ok": True, **self.start(body)}
        if p == "/api/help/stop" and method == "POST":
            return 200, {"ok": True, **self.stop()}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    # -- the helper ---------------------------------------------------------

    def _call(self, args: list[str], timeout: float = TIMEOUT_S) -> dict:
        try:
            rc, out = self.runner(args, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Unavailable(f"remote help: the helper didn't answer ({type(exc).__name__})") from None
        doc = None
        for line in reversed((out or "").strip().splitlines()):
            try:
                doc = json.loads(line)
                break
            except ValueError:
                continue
        if not isinstance(doc, dict):
            raise Unavailable(f"remote help: the helper failed (exit {rc})")
        return doc

    def _status(self, fresh: bool = False) -> dict:
        now = self.clock()
        if not fresh and self._cache is not None and now - self._cache[0] < CACHE_S:
            return self._cache[1]
        st = self._call(["status"])
        self._cache = (now, st)
        return st

    # -- reading ------------------------------------------------------------

    def hostname(self) -> str:
        room = ""
        try:
            room = self.app.room.name()
        except Exception:
            log.debug("remote help: no room name", exc_info=True)
        return tailnet_hostname(room, integration.radio_id(self.app.machine_id_file))

    def doc(self) -> dict:
        if not self.helper_present():
            return self._shape(None, available=False)
        busy = self._busy
        try:
            st = self._status()
        except Unavailable as exc:
            return self._shape(None, available=True, error=str(exc))
        if busy is None:
            st = self._enforce(st)
        doc = self._shape(st, available=bool(st.get("installed")), busy=busy)
        makers = tuple(p["user"] or p["name"] for p in doc["peers_active"])
        if makers != self._makers:
            # "did a maker actually get on?" answered by the journal (no URL here)
            log.info("remote help: %s", doc["status_text"] if makers else "no maker connected now")
            self._makers = makers
        return doc

    def _enforce(self, st: dict) -> dict:
        """Belt and braces next to the helper's timers: past the end, a dead
        tailscaled, or a tailnet that isn't the makers' -> stop now."""
        why = None
        if st.get("active") and st.get("expires_at") and self.clock() >= float(st["expires_at"]):
            why = "expired"
        elif st.get("stale"):
            why = "failed"
        elif (self.tailnet and st.get("active") and st.get("backend") == "Running"
              and st.get("tailnet") and str(st["tailnet"]).lower() != self.tailnet.lower()):
            why = "wrong-tailnet"
            log.warning("remote help: joined tailnet %r, not %r - ending it", st["tailnet"], self.tailnet)
        if why is None:
            return st
        with self._lock:
            if self._busy is not None:
                return st
            self._busy = "ending"
        try:
            log.info("remote help: ending (%s)", why)
            st = self._call(["stop", why])
            self._cache = None
        except Unavailable as exc:
            self._error = str(exc)
        finally:
            self._busy = None
        return st

    def _shape(self, st: dict | None, available: bool, busy: str | None = None,
               error: str | None = None) -> dict:
        st = st or {}
        if busy:
            state = busy
        elif not st.get("active"):
            state = "off"
        elif st.get("backend") == "Running":
            state = "connected"
        elif st.get("auth_url"):
            state = "waiting"
        else:
            state = "starting"
        address = st.get("address") if state == "connected" else None
        last = st.get("last") if isinstance(st.get("last"), dict) else None
        if last is not None:
            last = {"at": last.get("at"), "why": last.get("why"),
                    "text": WHY_TEXT.get(str(last.get("why")), "")}
        on = state in ("starting", "waiting", "connected")
        peers = peers_from(st.get("peers_active")) if state == "connected" else []
        return {
            "available": bool(available),
            "state": state,
            "status_text": makers_text(peers) if peers else STATUS_TEXT[state],
            "peers_active": peers,
            "network": NETWORK,
            # the one-time join link: only while it is waiting for a maker
            "login_url": st.get("auth_url") if state == "waiting" else None,
            "hostname": st.get("hostname") if on else None,
            "started_at": st.get("started_at") if on else None,
            "expires_at": st.get("expires_at") if on else None,
            "approve_by": st.get("approve_by") if state in ("starting", "waiting") else None,
            "address": address,
            "dns_name": st.get("dns_name") if state == "connected" else None,
            "tailnet": st.get("tailnet") if state == "connected" else None,
            "expected_tailnet": self.tailnet,
            # how the makers reach it, once connected (docs/remote-help.md)
            "ssh": f"pi@{address}" if address else None,
            "page": f"http://{address}:8080/" if address else None,
            "last": last if state == "off" else None,
            "error": error or st.get("error") or self._error,
        }

    # -- doing --------------------------------------------------------------

    def start(self, body: Any) -> dict:
        body = body if isinstance(body, dict) else {}
        if not self.helper_present():
            raise Unavailable("remote help isn't installed on this radio")
        self.app.confirm.consume(body.get("confirm"), "help", {})
        with self._lock:
            if self._busy is not None:
                return self._shape(self._cache[1] if self._cache else None, True, busy=self._busy)
            self._busy = "starting"
        name = self.hostname()
        self._error = None
        try:
            log.info("remote help: starting as %s (confirmed on the radio)", name)
            st = self._call(["start", name], START_TIMEOUT_S)
            self._cache = (self.clock(), st)
        finally:
            self._busy = None
        if not st.get("ok"):
            log.warning("remote help: start failed: %s", st.get("error"))
            raise Unavailable(st.get("error") or "remote help couldn't start")
        log.info("remote help: %s", "connected" if st.get("backend") == "Running"
                 else "waiting for a maker to approve the link")
        return self._shape(st, True)

    def stop(self) -> dict:
        with self._lock:
            if self._busy == "ending":
                return self._shape(None, True, busy="ending")
            self._busy = "ending"
        try:
            log.info("remote help: stopping (from the key page)")
            st = self._call(["stop", "stopped"])
            self._cache = (self.clock(), st)
        finally:
            self._busy = None
        self._error = None
        if not st.get("ok"):
            raise Unavailable(st.get("error") or "remote help couldn't stop")
        return self._shape(st, True)
