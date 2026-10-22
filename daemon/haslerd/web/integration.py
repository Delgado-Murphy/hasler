"""What the Home Assistant integration reads from the radio: who it is, and the art.

* ``GET /api/info`` -> ``{"id", "name", "variant", "version", "api": 1,
  "multiroom": {...}}`` (WebApp.info adds ``multiroom``, which other Haslers
  read: multiroom/group.py). ``id``
  is the first 12 hex digits of sha256(/etc/machine-id): stable for the life of
  the SD card's OS, never the machine-id itself (systemd asks that it not be
  exposed), and the same value the HA integration uses as its unique_id.
  ``name`` is the radio's room name (web/room.py; WebApp.info), else its host
  name (what ``<name>.local`` answers to).

* ``GET /api/art/current`` -> the picture for what is playing: the radio
  station's logo from moOde's library (``cfg_radio`` row whose stream URL is
  the preset's, logo at ``/var/local/www/imagesw/radio-logos/<name>.jpg``) or
  the YouTube Music track's thumbnail, fetched once and kept in a small
  memory cache. 404 when there is nothing to show. ``/api/status`` carries
  ``"art": "/api/art/current?k=<10 hex>"`` (or null): ``k`` changes when the
  picture does, so HA and browsers re-fetch exactly then.

The radio is found by HA over mDNS: ``config/avahi/hasler.service`` (shipped by
the release integration to /etc/avahi/services/) announces ``_hasler._tcp`` on
port 8080 with TXT ``api=1`` and ``path=/api/info``. A release file is the same
on every radio, so the per-radio values (id, name, variant, version) are not
in TXT: the integration's zeroconf step reads ``/api/info``.
"""

from __future__ import annotations

import hashlib
import logging
import socket
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

API_VERSION = 1
MACHINE_ID = Path("/etc/machine-id")
MOODE_LOGOS = Path("/var/local/www/imagesw/radio-logos")
ART_MAX_BYTES = 2 * 1024 * 1024
ART_CACHE = 8                 # remote pictures kept in memory
LOOKUP_TTL_S = 300.0          # stream URL -> logo file, re-checked this often
FETCH_TIMEOUT_S = 4.0


def radio_id(machine_id_file: Path = MACHINE_ID) -> str:
    """sha256(machine-id)[:12]; from the host name if there is no machine-id (a laptop)."""
    try:
        seed = machine_id_file.read_text().strip()
    except OSError:
        seed = ""
    if not seed:
        seed = "host:" + socket.gethostname()
    return hashlib.sha256(seed.encode()).hexdigest()[:12]


def radio_name() -> str:
    try:
        return socket.gethostname().split(".")[0] or "hasler"
    except OSError:
        return "hasler"


def info(variant: str, version: str, machine_id_file: Path = MACHINE_ID) -> dict:
    return {"id": radio_id(machine_id_file), "name": radio_name(), "variant": variant,
            "version": version, "api": API_VERSION}


def _default_fetch(url: str, timeout: float) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "haslerd"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        ctype = response.headers.get("Content-Type", "")
        return response.read(ART_MAX_BYTES + 1), ctype


class ArtProxy:
    """Finds and serves the current picture. Thread-safe; never raises to the caller."""

    def __init__(self, station_db: str, logos_dir: Path = MOODE_LOGOS,
                 fetch: Callable[[str, float], tuple[bytes, str]] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.station_db = station_db
        self.logos_dir = Path(logos_dir)
        self.fetch = fetch or _default_fetch
        self.clock = clock
        self._lock = threading.Lock()
        self._logos: dict[str, tuple[float, Path | None]] = {}
        self._cache: dict[str, tuple[bytes, str]] = {}

    # -- what to show ---------------------------------------------------------

    def _logo(self, stream: str) -> Path | None:
        now = self.clock()
        with self._lock:
            hit = self._logos.get(stream)
        if hit is not None and now - hit[0] < LOOKUP_TTL_S:
            return hit[1]
        path = None
        try:
            con = sqlite3.connect(f"file:{self.station_db}?mode=ro", uri=True, timeout=1)
            try:
                row = con.execute("SELECT name FROM cfg_radio WHERE station = ? LIMIT 1", (stream,)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            name = str(row[0])
            if "/" not in name and not name.startswith("."):
                candidate = self.logos_dir / f"{name}.jpg"
                if candidate.is_file():
                    path = candidate
        with self._lock:
            if len(self._logos) > 256:
                self._logos.clear()
            self._logos[stream] = (now, path)
        return path

    def locate(self, source: str | None, playing: dict | None, track: dict | None) -> tuple[str, str] | None:
        """("file", path) | ("url", https://...) for what is playing, or None."""
        if source == "ytm" and track:
            thumb = str(track.get("thumb") or "")
            if thumb.startswith(("https://", "http://")):
                return "url", thumb
            return None
        if source == "radio" and playing:
            uri = str(playing.get("uri") or "")
            if uri.startswith(("http://", "https://")):
                logo = self._logo(uri)
                if logo is not None:
                    return "file", str(logo)
        return None

    @staticmethod
    def key(where: tuple[str, str]) -> str:
        return hashlib.sha1(f"{where[0]}:{where[1]}".encode()).hexdigest()[:10]

    def url_for(self, source: str | None, playing: dict | None, track: dict | None) -> str | None:
        try:
            where = self.locate(source, playing, track)
        except Exception:
            log.debug("art lookup failed", exc_info=True)
            return None
        return None if where is None else f"/api/art/current?k={self.key(where)}"

    # -- the picture ------------------------------------------------------------

    def picture(self, where: tuple[str, str]) -> tuple[bytes, str] | None:
        kind, ref = where
        if kind == "file":
            try:
                data = Path(ref).read_bytes()[:ART_MAX_BYTES + 1]
            except OSError:
                return None
            if len(data) > ART_MAX_BYTES:
                return None
            ctype = "image/png" if ref.lower().endswith(".png") else "image/jpeg"
            return data, ctype
        with self._lock:
            hit = self._cache.get(ref)
        if hit is not None:
            return hit
        try:
            data, ctype = self.fetch(ref, FETCH_TIMEOUT_S)
        except Exception as exc:
            log.info("cover art fetch failed (%s)", type(exc).__name__)
            return None
        ctype = (ctype or "").split(";")[0].strip().lower()
        if len(data) > ART_MAX_BYTES or not ctype.startswith("image/"):
            return None
        with self._lock:
            while len(self._cache) >= ART_CACHE:
                self._cache.pop(next(iter(self._cache)))
            self._cache[ref] = (data, ctype)
        return data, ctype

    def current(self, source: str | None, playing: dict | None, track: dict | None
                ) -> tuple[bytes, str, str] | None:
        """(bytes, content type, etag) for what is playing, or None."""
        try:
            where = self.locate(source, playing, track)
            if where is None:
                return None
            pic = self.picture(where)
        except Exception:
            log.exception("cover art failed")
            return None
        if pic is None:
            return None
        return pic[0], pic[1], f'"{self.key(where)}"'


def now_playing(app: Any) -> tuple[str | None, dict | None, dict | None]:
    """(source, playing preset as the page sees it, ytm track) from a WebApp."""
    from . import keymap
    current = app.actions.player.current()
    source, _, _ = app._source(current)
    playing = keymap.preset_to_page(current) if current else None
    track = app._track() if source == "ytm" else None
    return source, playing, track
