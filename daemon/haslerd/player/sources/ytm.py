"""YouTube Music through MPD, by way of a tiny local redirect proxy.

Why a proxy at all: MPD can play any HTTP URL, but YouTube's stream URLs are
signed, IP-bound and expire after roughly six hours. Resolving a whole playlist
up front would mean (a) waiting 2-5 s per track on a Pi before the first note,
and (b) a queue full of dead links by the next evening. So instead the MPD queue
holds *stable* URLs of the form ``http://127.0.0.1:<port>/ytm/<video id>``, and
this module answers each one at the moment MPD actually asks for it: resolve the
id with yt-dlp, reply ``302 Location: <googlevideo URL>``, and MPD follows the
redirect and streams directly from YouTube. Nothing audio-sized passes through
Python.

Consequences worth knowing:

* moOde's UI shows the queue as proxy URLs, not titles. That is the price of
  late resolution; titles could be added later from the ``--flat-playlist`` JSON.
* A resolver failure answers 502, which MPD treats as a broken song and skips.
  One region-locked track therefore costs a skip, not silence.
* Resolving the *next* queued id in the background after each request means a
  track change normally hits the cache rather than waiting on yt-dlp.

Operational notes (these bite in practice, so they live here, not in a wiki):

* yt-dlp must stay current - YouTube changes break old releases within weeks.
  Run ``yt-dlp -U`` (or upgrade the pip/pipx install) from a timer.
* Since late 2025 yt-dlp needs an external JavaScript runtime (deno, or quickjs)
  to solve YouTube's player challenges. Without one, many formats are missing
  or resolution fails outright. Install deno on the Pi.
* ``cookies`` points at a Netscape-format cookies.txt exported from a browser
  logged into YouTube Music. Needed for private/library playlists, and it
  reduces "confirm you are not a bot" failures; keep the file readable only by
  the daemon user.
"""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..base import Preset

log = logging.getLogger(__name__)

# YouTube video ids are exactly 11 characters of URL-safe base64. Validating
# this strictly is what keeps arbitrary strings out of a yt-dlp command line.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

WATCH_URL = "https://music.youtube.com/watch?v={id}"
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio"

# A re-press of the same key within this window skips the playlist expansion,
# which on a Pi is the slow part of starting music (it can take several seconds
# for a long playlist). Short, because playlists do get edited.
PLAYLIST_TTL_S = 10 * 60

# Never hand out a stream URL this close to its own ``expire=`` stamp: MPD may
# take a while to reach a prefetched track, and a long track must not expire
# mid-play on reconnect.
EXPIRY_MARGIN_S = 30 * 60


class YtmError(RuntimeError):
    """A ytm preset could not be started. Raised by ``play`` only."""


class YtmSource:
    """The ``ytm`` source: playlist expansion, the redirect proxy and prefetch.

    ``mpd`` is a factory returning a connected MPDClient-like object; the router
    owns connection management. ``runner`` stands in for ``subprocess.run`` so
    tests never execute yt-dlp.
    """

    name = "ytm"

    def __init__(
        self,
        mpd: Callable[[], Any],
        port: int = 8099,
        ytdlp: str = "yt-dlp",
        cookies: str | None = None,
        cache_ttl_s: float = 5 * 3600,
        runner: Callable[..., Any] = subprocess.run,
        playlist_ttl_s: float = PLAYLIST_TTL_S,
    ) -> None:
        self._mpd = mpd
        self.port = port
        self.ytdlp = ytdlp
        self.cookies = cookies
        self.cache_ttl_s = cache_ttl_s
        self.playlist_ttl_s = playlist_ttl_s
        self._run = runner

        # id -> (expires_at, url). Guarded by _cache_lock.
        self._urls: dict[str, tuple[float, str]] = {}
        # playlist uri -> (expires_at, [ids])
        self._playlists: dict[str, tuple[float, list[str]]] = {}
        self._cache_lock = threading.Lock()
        # Per-id locks so a prefetch and MPD's own request for the same id
        # resolve it once rather than racing two yt-dlp processes.
        self._id_locks: dict[str, threading.Lock] = {}
        # The MPD client is not thread-safe; the proxy's prefetch reads the
        # queue from a handler thread while the panel loop may be calling play.
        self._mpd_lock = threading.Lock()
        # Queue order as we added it, used to guess the next id if MPD can't say.
        self._queue: list[str] = []

        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._server_lock = threading.Lock()

    # -- Source protocol --------------------------------------------------

    def play(self, preset: Preset) -> None:
        """Queue the playlist into MPD and start it.

        Raises ``YtmError`` on failure, like ``SpotifySource.play``: the router
        catches it, logs it and flashes the lamp, and the boot resume retries.
        """
        self.start()
        try:
            ids = self._expand(preset.uri)
        except Exception as exc:
            raise YtmError(f"could not expand ytm preset {preset.n}: {exc}") from exc
        if not ids:
            # Leave whatever is playing alone: a dead key is better than a key
            # that silences the radio. Still an error, so the lamp says so.
            raise YtmError(f"ytm preset {preset.n} ({preset.name}): no playable entries")
        prefix = self.prefix
        try:
            with self._mpd_lock:
                client = self._mpd()
                client.clear()
                client.random(1 if preset.shuffle else 0)
                for vid in ids:
                    client.add(prefix + vid)
                if preset.shuffle:
                    # MPD's random mode shuffles what comes *after* the current
                    # song; without a random start every press opens on track 1.
                    client.play(random.randrange(len(ids)))
                else:
                    client.play()
        except Exception as exc:
            raise YtmError(f"MPD refused ytm preset {preset.n}: {exc}") from exc
        self._queue = ids
        log.info("playing ytm preset %s (%s), %d tracks", preset.n, preset.name, len(ids))

    def stop(self) -> None:
        try:
            with self._mpd_lock:
                self._mpd().stop()
        except Exception:
            log.exception("could not stop MPD")

    def next_track(self) -> None:
        try:
            with self._mpd_lock:
                self._mpd().next()
        except Exception:
            log.exception("could not skip to next track")

    def is_active(self) -> bool:
        """True only when MPD is playing one of *our* proxy URLs.

        Radio presets also play through MPD, so "MPD is playing" alone would
        make the router believe ytm owns the box when a radio station does.
        """
        try:
            with self._mpd_lock:
                client = self._mpd()
                if client.status().get("state") != "play":
                    return False
                return str(client.currentsong().get("file", "")).startswith(self.prefix)
        except Exception:
            log.debug("ytm is_active check failed", exc_info=True)
            return False

    def close(self) -> None:
        with self._server_lock:
            server, thread = self._server, self._thread
            self._server = self._thread = None
        if server is None:
            return
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            log.debug("ytm proxy shutdown failed", exc_info=True)
        if thread is not None:
            thread.join(timeout=5)

    # -- proxy server -----------------------------------------------------

    @property
    def prefix(self) -> str:
        return f"http://127.0.0.1:{self.port}/ytm/"

    def start(self) -> None:
        """Start the redirect proxy if it isn't running. Idempotent.

        Binds 127.0.0.1 only: MPD is on the same box, and nothing on the LAN
        has any business turning video ids into signed stream URLs on our IP.
        ``port=0`` picks a free port, after which ``self.port`` is the real one.
        """
        with self._server_lock:
            if self._server is not None:
                return
            server = ThreadingHTTPServer(("127.0.0.1", self.port), _make_handler(self))
            server.daemon_threads = True
            self.port = server.server_address[1]
            thread = threading.Thread(
                target=server.serve_forever, name="ytm-proxy", daemon=True
            )
            thread.start()
            self._server, self._thread = server, thread
            log.info("ytm proxy listening on 127.0.0.1:%s", self.port)

    def handle(self, path: str) -> tuple[int, str | None]:
        """Map a request path to (status, Location). Pure enough to test alone."""
        parts = path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "ytm" or not VIDEO_ID.match(parts[1]):
            return 404, None
        vid = parts[1]
        url = self.resolve(vid)
        if url is None:
            return 502, None
        self._prefetch_after(vid)
        return 302, url

    # -- resolution -------------------------------------------------------

    def resolve(self, vid: str) -> str | None:
        """Video id -> direct audio URL, cached until shortly before it expires."""
        cached = self._cached_url(vid)
        if cached:
            return cached
        with self._cache_lock:
            lock = self._id_locks.setdefault(vid, threading.Lock())
        with lock:
            cached = self._cached_url(vid)  # someone else may have just done it
            if cached:
                return cached
            url = self._ytdlp_url(vid)
            if url is None:
                return None
            expires = time.time() + self.cache_ttl_s
            stamp = _expire_stamp(url)
            if stamp is not None:
                expires = min(expires, stamp - EXPIRY_MARGIN_S)
            with self._cache_lock:
                now = time.time()
                for key in [k for k, (exp, _) in self._urls.items() if exp <= now]:
                    del self._urls[key]
                self._urls[vid] = (expires, url)
            return url

    def _cached_url(self, vid: str) -> str | None:
        with self._cache_lock:
            hit = self._urls.get(vid)
        if hit and hit[0] > time.time():
            return hit[1]
        return None

    def _ytdlp_url(self, vid: str) -> str | None:
        cmd = [self.ytdlp, "-f", AUDIO_FORMAT, "-g"]
        if self.cookies:
            cmd += ["--cookies", self.cookies]
        cmd.append(WATCH_URL.format(id=vid))
        try:
            result = self._run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except Exception:
            log.warning("yt-dlp could not be run for %s", vid, exc_info=True)
            return None
        if result.returncode != 0:
            log.warning("yt-dlp failed for %s: %s", vid, _tail(result.stderr))
            return None
        for line in (result.stdout or "").splitlines():
            if line.startswith("http"):
                return line.strip()
        log.warning("yt-dlp gave no URL for %s", vid)
        return None

    def _expand(self, uri: str) -> list[str]:
        """Playlist (or watch) URL -> video ids, in playlist order."""
        single = _single_video_id(uri)
        if single:
            return [single]
        now = time.time()
        with self._cache_lock:
            hit = self._playlists.get(uri)
        if hit and hit[0] > now:
            return list(hit[1])

        cmd = [self.ytdlp, "--flat-playlist", "-J"]
        if self.cookies:
            cmd += ["--cookies", self.cookies]
        cmd.append(uri)
        try:
            result = self._run(cmd, capture_output=True, text=True, timeout=120, check=False)
        except Exception:
            log.warning("yt-dlp could not be run for %s", uri, exc_info=True)
            return []
        if result.returncode != 0:
            log.warning("yt-dlp could not expand %s: %s", uri, _tail(result.stderr))
            return []
        try:
            data = json.loads(result.stdout)
        except (TypeError, ValueError):
            log.warning("yt-dlp gave unparseable JSON for %s", uri)
            return []
        ids = _ids_from_info(data)
        with self._cache_lock:
            self._playlists[uri] = (now + self.playlist_ttl_s, ids)
        return list(ids)

    # -- prefetch ---------------------------------------------------------

    def _prefetch_after(self, vid: str) -> None:
        """Resolve whatever MPD will ask for next, off the request thread."""
        threading.Thread(
            target=self._prefetch, args=(vid,), name="ytm-prefetch", daemon=True
        ).start()

    def _prefetch(self, vid: str) -> None:
        try:
            nxt = self._next_id(vid)
            if nxt and nxt != vid and not self._cached_url(nxt):
                log.debug("prefetching %s", nxt)
                self.resolve(nxt)
        except Exception:
            log.debug("ytm prefetch failed", exc_info=True)

    def _next_id(self, vid: str) -> str | None:
        """Ask MPD for its next song (honours shuffle); fall back to our order."""
        try:
            with self._mpd_lock:
                client = self._mpd()
                pos = client.status().get("nextsong")
                if pos is not None:
                    info = client.playlistinfo(pos)
                    entry = info[0] if isinstance(info, list) and info else info
                    file = str((entry or {}).get("file", ""))
                    if file.startswith(self.prefix):
                        candidate = file[len(self.prefix):]
                        if VIDEO_ID.match(candidate):
                            return candidate
        except Exception:
            log.debug("could not read next song from MPD", exc_info=True)
        queue = self._queue
        if vid in queue:
            i = queue.index(vid)
            if i + 1 < len(queue):
                return queue[i + 1]
        return None


def _make_handler(source: YtmSource) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "haslerd-ytm/1"

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            try:
                status, location = source.handle(self.path)
            except Exception:
                log.exception("ytm proxy error for %s", self.path)
                status, location = 502, None
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

        do_HEAD = do_GET

        def log_message(self, fmt: str, *args: Any) -> None:
            # MPD hits this once per track; keep it out of the journal at INFO.
            log.debug("ytm proxy: " + fmt, *args)

    return Handler


def _single_video_id(uri: str) -> str | None:
    """A watch/youtu.be URL with no ``list=`` is one track: skip yt-dlp entirely."""
    parsed = urlparse(uri)
    query = parse_qs(parsed.query)
    if "list" in query:
        return None
    if parsed.netloc.endswith("youtu.be"):
        candidate = parsed.path.strip("/")
    else:
        candidate = (query.get("v") or [""])[0]
    return candidate if VIDEO_ID.match(candidate) else None


def _ids_from_info(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if entries is None:
        vid = data.get("id")
        return [vid] if isinstance(vid, str) and VIDEO_ID.match(vid) else []
    ids = []
    for entry in entries:
        vid = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(vid, str) and VIDEO_ID.match(vid):
            ids.append(vid)
    return ids


def _expire_stamp(url: str) -> float | None:
    try:
        return float(parse_qs(urlparse(url).query)["expire"][0])
    except (KeyError, IndexError, ValueError):
        return None


def _tail(text: str | None, limit: int = 300) -> str:
    text = (text or "").strip()
    return text[-limit:]
