"""Spotify, played by a local go-librespot daemon and driven over its HTTP API.

Why go-librespot rather than moOde's own librespot renderer: moOde's renderer is
Spotify *Connect* only, so it makes sound only after a phone picks it. The whole
point of the box is a piano key that starts a playlist with no phone in the
room. go-librespot holds its own logged-in session (credentials persisted once,
at install) and exposes a local REST API that can start any ``spotify:`` URI by
itself. That is all this module talks to.

Verified against go-librespot **v0.10.3** (released 2026-10-03), using
``api-spec.yml`` at that tag (the spec is the source the daemon's router is
generated from) and ``daemon/api_server.go`` / ``daemon/player.go`` for status
codes:

* ``POST /player/shuffle_context``  ``{"shuffle_context": bool}``
* ``POST /player/play``             ``{"uri": str, "skip_to_uri": str?, "paused": bool?, "position": int?}``
* ``POST /player/pause``            no body
* ``POST /player/next``             optional ``{"uri": str}``; we send none
* ``GET  /status``                  200 + JSON with ``stopped`` / ``paused`` /
  ``buffering`` booleans, or **204 when there is no session**

Two details from the source matter more than they look:

1. The daemon reports most failures as non-200 *success-class* codes. A play
   with no logged-in session answers **204**, which ``urllib`` happily treats as
   success. So "it worked" here means exactly 200, nothing else.
2. Shuffle has to be set *before* play. Set after, go-librespot only reshuffles
   the upcoming queue and the playlist still starts on its first track, which
   is precisely the "every morning starts with the same song" bug.

Stdlib only, like the rest of the daemon: four POSTs and a GET do not justify a
dependency on a Pi that has to keep working through OS upgrades.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

from ..base import Preset

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:3678"


class SpotifyError(RuntimeError):
    """go-librespot refused or did not answer. Raised only from ``play``."""


class SpotifySource:
    """Implements the ``Source`` protocol against go-librespot's local API.

    ``play`` raises on failure so the router can log it and flash the lamp;
    a key that silently does nothing is the worst outcome on a box with no
    screen. ``stop``/``next_track``/``is_active`` never raise, because they are
    called from the panel loop, which must survive go-librespot being down,
    restarting, or not yet logged in.
    """

    name = "spotify"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 3.0,
        status_timeout: float = 0.5,
        cache_seconds: float = 1.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # is_active runs on the 50 Hz panel thread. Connection refused is
        # instant, but a wedged daemon would stall the loop for the full
        # timeout once per cache period - so status gets a much shorter one.
        self.status_timeout = status_timeout
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._active = False
        self._checked_at: float | None = None
        # Ignore http_proxy & co: this API is loopback-only, and a proxy set
        # for apt or pip in the environment must not swallow it.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- Source protocol -----------------------------------------------------

    def play(self, preset: Preset) -> None:
        """Start ``preset.uri``, shuffled if the preset says so.

        Shuffle is sent on every play, off as well as on: go-librespot keeps the
        option across plays, so without the explicit ``False`` an album key
        pressed after a shuffled-playlist key would come out shuffled too.
        """
        self._post("/player/shuffle_context", {"shuffle_context": bool(preset.shuffle)})
        self._post("/player/play", {"uri": preset.uri, "paused": False})
        # Optimistic: go-librespot buffers for a moment before reporting
        # playing, and the router must not read that gap as "it died".
        self._remember(True)
        log.info("spotify playing preset %s (%s)%s", preset.n, preset.name,
                 " shuffled" if preset.shuffle else "")

    def stop(self) -> None:
        """Pause rather than ``/player/stop``.

        ``/player/stop`` tears down the session; pause keeps it warm so the next
        key press starts in well under a second instead of re-authenticating.
        """
        self._remember(False)
        try:
            self._post("/player/pause")
        except SpotifyError as exc:
            log.info("spotify pause failed (%s) - continuing", exc)

    def next_track(self) -> None:
        try:
            self._post("/player/next")
        except SpotifyError as exc:
            log.info("spotify next failed (%s) - continuing", exc)

    def is_active(self) -> bool:
        """True only while go-librespot is actually playing (not paused/stopped).

        Cached for ``cache_seconds`` so the panel loop can ask every tick and
        cost go-librespot one request a second. Any failure reads as False:
        an unreachable player is, for the router's purposes, not playing.
        """
        now = time.monotonic()
        with self._lock:
            if self._checked_at is not None and now - self._checked_at < self.cache_seconds:
                return self._active
        active = self._fetch_active()
        self._remember(active)
        return active

    def close(self) -> None:
        """Nothing to release: every call is a fresh, short-lived request."""

    # -- internals -----------------------------------------------------------

    def _remember(self, active: bool) -> None:
        with self._lock:
            self._active = active
            self._checked_at = time.monotonic()

    def _fetch_active(self) -> bool:
        request = urllib.request.Request(self.base_url + "/status", method="GET")
        try:
            with self._opener.open(request, timeout=self.status_timeout) as response:
                if response.status != 200:
                    return False  # 204: no session, so certainly not playing
                status = json.loads(response.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Expected whenever go-librespot is down or restarting. Debug, not
            # info: this would otherwise log once a second.
            log.debug("spotify status unavailable (%s)", exc)
            return False
        if not isinstance(status, dict):
            return False
        return not status.get("stopped", True) and not status.get("paused", True)

    def _post(self, path: str, body: dict | None = None) -> None:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method="POST"
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            raise SpotifyError(f"{path} -> HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SpotifyError(f"{path} -> {exc}") from exc
        if status != 200:
            # 204 here means "no session" (not logged in), not success.
            raise SpotifyError(f"{path} -> HTTP {status}")
