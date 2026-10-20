"""``/api/spotify/account``: which Spotify account the radio plays as - and
signing it in from the key page, with no laptop.

go-librespot (v0.10.3) keeps the account in ``state.json`` next to its
config.yml (``credentials: {username, data}``; ``data`` is a reusable login
token - never logged, never shown). It signs in by itself once, at start,
with whatever ``credentials: type:`` says, *only when state.json has no
credentials*: with some, it just uses them. So:

* **Sign in / Change account** (a press on the radio, web/confirm.py action
  ``spotify``, ``{"do": "sign_in"}``): state.json goes to
  ``state.json.before-sign-in``, its credentials are cleared, config.yml's
  credentials type becomes ``device_auth`` and go-librespot restarts. It asks
  Spotify for a pairing code (RFC 8628) and offers it on its local API,
  ``GET /auth/code`` -> ``{url, code, expires_at}`` (204 = none). The page
  shows "Go to spotify.com/pair and enter ABCD-EFGH"; once the owner approves
  on their phone, go-librespot stores that account in state.json by itself and
  the page says "Signed in as jd******fe". ``device_auth`` stays: with
  credentials stored it is the same as any other type, and a radio whose
  login was lost asks with a code again instead of a URL nobody can open.
  A code that expires makes go-librespot exit; systemd restarts it 5 s later
  with a new code.
* **Cancel** (no press: it only puts back what was there before the press):
  the account from ``state.json.before-sign-in`` again, or signed out.
  Nobody approved within ``SIGN_IN_MAX_S``: the same, by itself - while a code
  is waiting go-librespot plays nothing, guests included.
* **Sign out** (a press, ``{"do": "sign_out"}``): state.json goes to
  ``state.json.before-sign-out``, the credentials are cleared and the type
  becomes ``zeroconf`` (``persist_credentials`` off): no account of its own,
  so Spotify presets stop, but guests on the Wi-Fi can still pick the radio in
  their Spotify app.

Guests never replace the owner's account: zeroconf sessions are stored only
with ``credentials: type: zeroconf`` *and* ``persist_credentials: true``,
which this never sets; when a guest leaves, go-librespot goes back to the
stored account.

The account shown is go-librespot's own masking of the username
(``ObfuscateUsername``: ``jd******fe``) - from state.json, not /status, which
names whoever is casting. go-librespot writes state.json while it runs, so
every write here is followed by ``systemctl try-restart`` (the one line sudoers
allows) and checked once it is back; an overwrite in between is written again.

    GET    /api/spotify/account          the doc below
    POST   /api/spotify/account/start    {"confirm": token}   sign in / change account
    POST   /api/spotify/account/cancel   {}                   stop signing in, put back
    DELETE /api/spotify/account          {"confirm": token}   sign out
    (POST /api/spotify/account/sign_out, the same, for clients that only POST)

    {available, signed_in, user, flow_state, url, code, expires_at,
     sign_in_until, previous, error}

``flow_state``: signed_in | signed_out | starting (restarted, no code yet) |
waiting (``url``/``code`` to show) | failed (``error`` says why). ``previous``
(while signing in): the account Cancel puts back, masked, or null.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .. import boxconfig
from ..player import glr_config
from ..player.renderers import GO_LIBRESPOT_CONFIG
from .server import Unavailable

log = logging.getLogger(__name__)

PATHS = ("/api/spotify/account", "/api/spotify/account/start",
         "/api/spotify/account/cancel", "/api/spotify/account/sign_out")
AUTH_CODE_URL = "http://127.0.0.1:3678/auth/code"
SIGN_IN_TYPE = "device_auth"
SIGNED_OUT_TYPE = "zeroconf"
SIGN_IN_MAX_S = 15 * 60.0     # a code nobody approves: put the old account back
NO_CODE_S = 60.0              # restarted this long ago and still no code: say so
BEFORE_SIGN_IN = "state.json.before-sign-in"
BEFORE_SIGN_OUT = "state.json.before-sign-out"
LEGACY = "credentials.json"   # pre-state.json go-librespot; read if state.json has no username


def mask(username: str) -> str:
    """go-librespot's ``ObfuscateUsername``: ``jd******fe``, ``ab***yz@x.com``."""
    if "@" in username:
        name, domain = username.split("@", 1)
        return mask(name) + "@" + domain
    if len(username) < 5:
        return username
    return username[:2] + "*" * (len(username) - 4) + username[-2:]


def fetch_auth_code(timeout: float = 1.5) -> dict | None:
    """go-librespot's pending pairing code ``{url, code, expires_at}``, or None
    (204, not running, garbage). The code is returned, never logged."""
    try:
        with urllib.request.urlopen(AUTH_CODE_URL, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            doc = json.loads(resp.read() or b"null")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("code"), str) or not isinstance(doc.get("url"), str):
        return None
    return {"url": doc["url"][:300], "code": doc["code"][:40],
            "expires_at": str(doc.get("expires_at") or "")[:40] or None}


def _read_json(path: Path) -> dict | None:
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("can't read %s", path)
        return None
    return doc if isinstance(doc, dict) else None


def _creds(doc: dict | None) -> tuple[str, Any]:
    c = (doc or {}).get("credentials")
    c = c if isinstance(c, dict) else {}
    user = c.get("username") if isinstance(c.get("username"), str) else ""
    return user, c.get("data")


class SpotifyAccountApi:
    """The routes above, for ``WebApp.extra_routes``; ``doc()`` also rides
    along in /api/casting's Spotify Connect target (web/sources_api.py)."""

    def __init__(
        self,
        app: Any,
        config: Path = GO_LIBRESPOT_CONFIG,
        restart: Callable[[], None] = glr_config.restart_go_librespot,
        auth_code: Callable[[], dict | None] = fetch_auth_code,
        clock: Callable[[], float] = time.time,
        timer: Callable[[float, Callable[[], None]], Any] | None = None,
    ) -> None:
        self.app = app
        self.config = Path(config)
        self.dir = self.config.parent
        self.state = self.dir / "state.json"
        self.restart = restart
        self.auth_code = auth_code
        self.clock = clock
        self.timer = timer or self._thread_timer
        self._lock = threading.RLock()
        self._started: float | None = None      # when this sign-in was asked (or found)
        self._restarted: float | None = None
        self._error: str | None = None
        self._pending_timer: Any = None

    # -- routing ------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PATHS:
            return None
        method = method.upper()
        body = body if isinstance(body, dict) else {}
        if p == "/api/spotify/account":
            if method == "GET":
                return 200, self.doc()
            if method == "DELETE":
                return 200, {"ok": True, **self.sign_out(body)}
        elif method == "POST":
            if p.endswith("/start"):
                return 200, {"ok": True, **self.start(body)}
            if p.endswith("/cancel"):
                return 200, {"ok": True, **self.cancel()}
            if p.endswith("/sign_out"):
                return 200, {"ok": True, **self.sign_out(body)}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    # -- reading --------------------------------------------------------------

    def _account(self) -> tuple[str, bool]:
        """(username, has credentials) as go-librespot will load them."""
        user, data = _creds(_read_json(self.state))
        if not user:
            legacy = _read_json(self.dir / LEGACY)
            if legacy is not None:
                user, data = _creds({"credentials": legacy})
        return user, bool(user and data)

    def _signing_in(self) -> bool:
        return glr_config.credentials_type(self.config) == SIGN_IN_TYPE and not self._account()[1]

    def doc(self) -> dict:
        base = {"available": False, "signed_in": False, "user": None, "flow_state": "signed_out",
                "url": None, "code": None, "expires_at": None, "sign_in_until": None,
                "previous": None, "error": None}
        if not self.config.is_file():
            return {**base, "error": "Spotify isn't set up on this radio"}
        with self._lock:
            user, signed_in = self._account()
            doc = {**base, "available": True, "signed_in": signed_in,
                   "user": mask(user) if signed_in else None}
            ctype = glr_config.credentials_type(self.config)
            if signed_in:
                if self._started is not None:
                    log.info("Spotify signed in as %s from the key page", mask(user))
                self._forget()
                return {**doc, "flow_state": "signed_in"}
            if ctype != SIGN_IN_TYPE:
                self._forget(keep_error=True)       # "nobody approved in time" stays until the next try
                return {**doc, "flow_state": "failed" if self._error else "signed_out", "error": self._error}
            # Signing in: maybe asked before haslerd restarted - give it the full time from now.
            now = self.clock()
            if self._started is None:
                self._started = now
                self._schedule(SIGN_IN_MAX_S)
            if now - self._started >= SIGN_IN_MAX_S:
                self._give_up()
                return self.doc()
            prev_user, prev_has = _creds(_read_json(self.dir / BEFORE_SIGN_IN))
            doc.update(sign_in_until=self._started + SIGN_IN_MAX_S,
                       previous=mask(prev_user) if prev_user and prev_has else None)
            code = None
            try:
                code = self.auth_code()
            except Exception:
                log.debug("auth code fetch failed", exc_info=True)
            if code:
                return {**doc, "flow_state": "waiting", **code}
            since = self._restarted if self._restarted is not None else self._started
            if now - since >= NO_CODE_S:
                return {**doc, "flow_state": "failed",
                        "error": "Spotify hasn't sent a code yet. Is the radio online? It keeps trying."}
            return {**doc, "flow_state": "starting"}

    # -- changing -----------------------------------------------------------

    def start(self, body: dict) -> dict:
        self._need_spotify()
        self.app.confirm.consume(body.get("confirm"), "spotify", {"do": "sign_in"})
        with self._lock:
            state = _read_json(self.state) or {}
            user, had = self._account()
            if not self._signing_in():
                # Again while a code waits: keep the backup of what was there before the first.
                boxconfig.atomic_write_text(self.dir / BEFORE_SIGN_IN, json.dumps(self._with_legacy(state)), mode=0o600)
            self._set_type(SIGN_IN_TYPE)
            self._write_creds(state, "", None)
            self._started, self._error = self.clock(), None
            self._schedule(SIGN_IN_MAX_S)
            log.info("Spotify sign-in started from the key page (was %s)", mask(user) if had else "signed out")
        return self.doc()

    def cancel(self, why: str = "cancelled from the key page") -> dict:
        with self._lock:
            if not self._signing_in():
                raise ValueError("the radio isn't signing in to Spotify")
            self._put_back()
            log.info("Spotify sign-in %s", why)
        return self.doc()

    def sign_out(self, body: dict) -> dict:
        self._need_spotify()
        self.app.confirm.consume(body.get("confirm"), "spotify", {"do": "sign_out"})
        with self._lock:
            state = _read_json(self.state) or {}
            user, had = self._account()
            if had:
                boxconfig.atomic_write_text(self.dir / BEFORE_SIGN_OUT, json.dumps(self._with_legacy(state)), mode=0o600)
            self._set_type(SIGNED_OUT_TYPE)
            self._write_creds(state, "", None)
            self._forget()
            self._error = None
            log.info("Spotify signed out from the key page (was %s)", mask(user) if had else "signed out")
        return self.doc()

    def resume(self) -> None:
        """At haslerd's start: a sign-in left waiting gets its deadline back."""
        try:
            self.doc()
        except Exception:
            log.debug("spotify account resume failed", exc_info=True)

    # -- internals --------------------------------------------------------------

    def _need_spotify(self) -> None:
        if not self.config.is_file():
            raise Unavailable("Spotify isn't set up on this radio")
        if not os.access(self.dir, os.W_OK):
            raise Unavailable(f"can't write {self.dir}")

    def _with_legacy(self, state: dict) -> dict:
        """state.json as go-librespot sees it: credentials.json folded in."""
        if not _creds(state)[0]:
            legacy = _read_json(self.dir / LEGACY)
            if legacy is not None:
                state = {**state, "credentials": {"username": _creds({"credentials": legacy})[0],
                                                  "data": _creds({"credentials": legacy})[1]}}
        return state

    def _set_type(self, ctype: str) -> None:
        try:
            glr_config.set_credentials_type(self.config, ctype)
        except OSError as exc:
            raise Unavailable(f"can't write go-librespot's settings: {exc.strerror or exc}") from None

    def _write_creds(self, state: dict, user: str, data: Any) -> None:
        """state.json with these credentials, then the restart, then a check
        that a go-librespot write in between didn't undo it (once more if so)."""
        legacy = self.dir / LEGACY
        if not user and legacy.exists():
            # An empty username would make go-librespot read the old file instead.
            os.replace(legacy, self.dir / (LEGACY + ".old"))
        for attempt in (1, 2):
            doc = dict(_read_json(self.state) or state)
            doc["credentials"] = {"username": user, "data": data}
            boxconfig.atomic_write_text(self.state, json.dumps(doc), mode=0o600)
            try:
                self.restart()
            except Exception as exc:
                raise Unavailable(f"saved, but go-librespot didn't restart: {exc}") from None
            self._restarted = self.clock()
            if _creds(_read_json(self.state)) == (user, data):
                return
            log.warning("go-librespot rewrote state.json during the restart; writing it again (%d)", attempt)
        raise Unavailable("go-librespot kept its old account - try again")

    def _put_back(self) -> None:
        before = _read_json(self.dir / BEFORE_SIGN_IN)
        user, data = _creds(before)
        state = _read_json(self.state) or {}
        if user and data:
            self._write_creds(state, user, data)      # type stays device_auth: same as any, with these
        else:
            self._set_type(SIGNED_OUT_TYPE)
            self._write_creds(state, "", None)
        self._forget()

    def _give_up(self) -> None:
        try:
            if self._signing_in():
                self._put_back()
                log.info("Spotify sign-in: nobody approved the code within %d min, put back", SIGN_IN_MAX_S // 60)
                self._error = "Nobody approved the code in time, so nothing changed."
        except Exception as exc:
            log.warning("Spotify sign-in: couldn't put the old account back: %s", exc)
            self._error = str(exc)
        self._forget(keep_error=True)

    def _forget(self, keep_error: bool = False) -> None:
        self._started = self._restarted = None
        if not keep_error:
            self._error = None
        t, self._pending_timer = self._pending_timer, None
        if t is not None and hasattr(t, "cancel"):
            t.cancel()

    def _schedule(self, seconds: float) -> None:
        t, self._pending_timer = self._pending_timer, None
        if t is not None and hasattr(t, "cancel"):
            t.cancel()
        self._pending_timer = self.timer(seconds, self._deadline)

    def _deadline(self) -> None:
        with self._lock:
            if self._started is not None and self.clock() - self._started >= SIGN_IN_MAX_S - 1:
                self._give_up()

    @staticmethod
    def _thread_timer(seconds: float, fn: Callable[[], None]) -> Any:
        t = threading.Timer(seconds, fn)
        t.daemon = True
        t.start()
        return t
