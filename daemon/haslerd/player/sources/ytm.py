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

* moOde's UI would show the queue as proxy URLs. When the YouTube Music
  catalogue (ytmusic.py) expanded the preset we know each track's title, artist
  and album, and queue it with ``addid`` + ``addtagid`` so MPD - and moOde,
  which reads MPD - shows real names. MPD allows tag edits on remote songs
  (since 0.19); if it refuses, the track plays untagged rather than not at all.
  yt-dlp's ``--flat-playlist`` path stays untagged.
* A resolver failure answers 502, which MPD treats as a broken song and skips.
  One region-locked track therefore costs a skip, not silence.
* Resolving the *next* queued id in the background after each request means a
  track change normally hits the cache rather than waiting on yt-dlp.
* ``warm()`` does the same for the *first* track of every ytm preset, in the
  background and again every ``WARM_INTERVAL_S``. Without it the first press
  pays for expansion plus one resolve - measured at ~13 s on a Pi 4.

Operational notes (these bite in practice, so they live here, not in a wiki):

* yt-dlp must stay current - YouTube changes break old releases within weeks.
  Run ``yt-dlp -U`` (or upgrade the pip/pipx install) from a timer.
* Since late 2025 yt-dlp needs an external JavaScript runtime (deno, or quickjs)
  to solve YouTube's player challenges. Without one, many formats are missing
  or resolution fails outright. Install deno on the Pi.
* ``cookies`` points at a Netscape-format cookies.txt exported from a browser
  logged into YouTube Music. Needed for private/library playlists, and it
  reduces "confirm you are not a bot" failures; keep the file readable only by
  the daemon user. Without it, the cookies the key page's sign-in wrote
  (``catalog.cookies_file``) are used if present - checked on every yt-dlp run,
  so signing in or out takes effect without a restart.

The catalogue (optional, ``catalog=``) changes three things when available:

* **Expansion** uses ytmusicapi (~0.3 s for a 100-track playlist) instead of
  ``yt-dlp --flat-playlist`` (several seconds), and learns names on the way.
  If it returns nothing, yt-dlp is tried as before.
* **Radio links** (``list=RD…``) become *endless*: when MPD asks for a track
  within ``TOPUP_WITHIN`` of the end of the queue, the prefetch thread fetches
  the next batch of the same radio, continuing from the last queued track, and
  appends only ids not already queued. The queue is capped at ``QUEUE_CAP`` by
  deleting already-played songs from the front.
* ``now_playing()`` names the current track for the key page.
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
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

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

# How often warm() refreshes each preset's playlist and opening track. Well
# inside a stream URL's lifetime, so a press between rounds still hits cache.
WARM_INTERVAL_S = 30 * 60
# After a round with failures (e.g. booted before the Wi-Fi), try again soon.
WARM_RETRY_S = 60
# Playlists carry dead entries (removed, region-locked). If the opening track
# won't resolve, warm() tries this many in all before giving up for the round.
WARM_TRIES = 3

# A video that won't resolve is remembered as dead for a while, so MPD skipping
# it, the prefetch and warm() don't each re-run yt-dlp on it every few seconds
# (seen on the bench: one removed video, a yt-dlp call every 2 s). Gone for good
# is gone for hours; anything else might be the network, so only briefly.
DEAD_GONE_S = 6 * 3600
DEAD_OTHER_S = 60
GONE_MARKERS = ("Video unavailable", "Private video", "This video has been removed",
                "account associated with this video has been terminated")

# Endless radio: top up when MPD reaches a track this close to the queue's end.
# Five tracks is a quarter of an hour of slack for one ~1 s catalogue call.
TOPUP_WITHIN = 5
# Never let an endless queue grow past this; played songs go from the front.
QUEUE_CAP = 500
# Names learned from the catalogue, kept for the tags and the key page.
META_CAP = 5000


def stream_ok(url: str, timeout: float = 4.0) -> bool:
    """Will googlevideo actually serve this link? One-byte range request.

    YouTube now and then refuses a freshly resolved link with 403 (seen on the
    bench: ~5 tracks in an evening). MPD doesn't retry - it skips the song - so
    the proxy checks first and re-resolves once. A timeout or network error
    counts as "fine": MPD will find out the same way, and a slow probe must not
    make every track slower.
    """
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "User-Agent": "Music Player Daemon"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 206)
    except urllib.error.HTTPError as err:
        return err.code not in (403, 410)
    except Exception:
        return True


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
        catalog: Any = None,
        probe: Callable[[str], bool] | None = None,
    ) -> None:
        self._mpd = mpd
        # stream_ok in production (moode.build_sources); None = trust yt-dlp.
        self._probe = probe
        # YtmCatalog or None. Asked per call whether it is available.
        self.catalog = catalog
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
        # id -> monotonic time until which it counts as dead. Guarded by _cache_lock.
        self._dead: dict[str, float] = {}
        # Queue order as we added it, used to guess the next id if MPD can't say.
        self._queue: list[str] = []
        # playlist uri -> the start index warm() resolved: a pre-picked random
        # start for shuffle, the first *playable* track otherwise.
        self._starts: dict[str, int] = {}
        self._warm_stop = threading.Event()
        self._warm_kick = threading.Event()     # "new preset list - go round now"
        self._warm_presets: list[Preset] = []
        self._warm_thread: threading.Thread | None = None
        # id -> {"title", "artist", "album", "thumb"}. Guarded by _cache_lock.
        self._meta: dict[str, dict[str, str]] = {}
        # MPD refused a tag once: don't ask again for every song of every press.
        self._tags_ok = True
        # The radio link the current queue was started from, if it is endless,
        # and a counter bumped by every play so a slow top-up can tell that the
        # queue it was extending has since been replaced.
        self._endless: str | None = None
        self._generation = 0
        self._topup_lock = threading.Lock()

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
                self._generation += 1
                self._endless = None
                client = self._mpd()
                client.clear()
                client.random(1 if preset.shuffle else 0)
                for vid in ids:
                    self._add(client, prefix + vid, vid)
                if preset.shuffle:
                    # MPD's random mode shuffles what comes *after* the current
                    # song; without a random start every press opens on track 1.
                    # Use the start warm() picked (and resolved) if there is one.
                    with self._cache_lock:
                        start = self._starts.pop(preset.uri, None)
                    if start is None or start >= len(ids):
                        start = random.randrange(len(ids))
                    client.play(start)
                else:
                    # Kept, not popped: in order, the opening track is stable.
                    with self._cache_lock:
                        start = self._starts.get(preset.uri)
                    if start is None or start >= len(ids):
                        client.play()
                    else:
                        client.play(start)
        except Exception as exc:
            raise YtmError(f"MPD refused ytm preset {preset.n}: {exc}") from exc
        self._queue = ids
        if _is_radio(preset.uri) and self._catalog_ready():
            self._endless = preset.uri
        log.info("playing ytm preset %s (%s), %d tracks%s", preset.n, preset.name, len(ids),
                 ", endless" if self._endless else "")
        if preset.shuffle and self._warm_thread is not None:
            # That start is spent; line up the next one so the next press is quick too.
            threading.Thread(
                target=self.warm_once, args=(preset,), name="ytm-warm", daemon=True
            ).start()

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

    def now_playing(self) -> dict | None:
        """The current MPD song, named, if it is ours and the catalogue named it.

        ``{"title", "artist", "album", "thumb", "videoId"}`` or None. For the
        key page; never raises.
        """
        try:
            with self._mpd_lock:
                file = str(self._mpd().currentsong().get("file", ""))
        except Exception:
            log.debug("ytm now_playing failed", exc_info=True)
            return None
        if not file.startswith(self.prefix):
            return None
        vid = file[len(self.prefix):]
        with self._cache_lock:
            meta = self._meta.get(vid)
        if meta is None:
            return None
        return {**meta, "videoId": vid}

    def close(self) -> None:
        self._warm_stop.set()
        self._warm_kick.set()
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

    # -- warm-up ----------------------------------------------------------

    def warm(self, presets: list[Preset], interval_s: float = WARM_INTERVAL_S) -> None:
        """Keep each ytm preset's playlist and opening track resolved, forever.

        Runs on a daemon thread: one round now, then every ``interval_s``
        (``WARM_RETRY_S`` after a round where anything failed). Calling it
        again replaces the list and starts a fresh round at once - the web
        page does that when presets change.
        """
        self._warm_presets = [p for p in presets if p.source == "ytm"]
        if self._warm_thread is not None:
            self._warm_kick.set()
            return
        if not self._warm_presets:
            return
        self._warm_interval_s = interval_s

        def loop() -> None:
            while not self._warm_stop.is_set():
                ok = all([self.warm_once(p) for p in list(self._warm_presets)])
                self._warm_kick.wait(interval_s if ok else min(WARM_RETRY_S, interval_s))
                self._warm_kick.clear()

        self._warm_thread = threading.Thread(target=loop, name="ytm-warm", daemon=True)
        self._warm_thread.start()
        log.info("warming %d ytm preset(s) every %ds", len(self._warm_presets), interval_s)

    def warm_once(self, preset: Preset) -> bool:
        """Expand ``preset`` and resolve the track it will open on. True if both worked."""
        try:
            interval = getattr(self, "_warm_interval_s", WARM_INTERVAL_S)
            # Fresh each round so playlist edits show up; kept until the next
            # round so a press in between skips expansion.
            ids = self._expand(preset.uri, ttl_s=interval + WARM_RETRY_S, force=True)
            if not ids:
                return False
            tries = min(WARM_TRIES, len(ids))
            if preset.shuffle:
                candidates = random.sample(range(len(ids)), tries)
            else:
                candidates = list(range(tries))
            for start in candidates:
                if self.resolve(ids[start]) is not None:
                    with self._cache_lock:
                        self._starts[preset.uri] = start
                    log.debug("warmed ytm preset %s: opens on track %d", preset.n, start)
                    return True
            log.warning("ytm preset %s: none of %d opening tracks would resolve", preset.n, tries)
            return False
        except Exception:
            log.debug("warming ytm preset %s failed", preset.n, exc_info=True)
            return False

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
        if url is not None and self._probe is not None and not self._probe(url):
            log.info("googlevideo refused the link for %s; resolving again", vid)
            with self._cache_lock:
                self._urls.pop(vid, None)
            url = self.resolve(vid)
            if url is not None and not self._probe(url):
                self._mark_dead(vid, DEAD_OTHER_S)
                url = None
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
        if self._is_dead(vid):
            return None
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
        cmd = [self.ytdlp, "-f", AUDIO_FORMAT, "-g", *self._cookie_args()]
        cmd.append(WATCH_URL.format(id=vid))
        try:
            result = self._run(cmd, capture_output=True, text=True, timeout=30, check=False)
        except Exception:
            log.warning("yt-dlp could not be run for %s", vid, exc_info=True)
            return None
        if result.returncode != 0:
            gone = any(marker in (result.stderr or "") for marker in GONE_MARKERS)
            self._mark_dead(vid, DEAD_GONE_S if gone else DEAD_OTHER_S)
            log.warning("yt-dlp failed for %s: %s", vid, _tail(result.stderr))
            return None
        for line in (result.stdout or "").splitlines():
            if line.startswith("http"):
                return line.strip()
        log.warning("yt-dlp gave no URL for %s", vid)
        return None

    def _mark_dead(self, vid: str, seconds: float) -> None:
        with self._cache_lock:
            self._dead[vid] = time.monotonic() + seconds

    def _is_dead(self, vid: str) -> bool:
        with self._cache_lock:
            until = self._dead.get(vid)
            if until is None:
                return False
            if until <= time.monotonic():
                del self._dead[vid]
                return False
            return True

    def _expand(self, uri: str, ttl_s: float | None = None, force: bool = False) -> list[str]:
        """Playlist (or watch) URL -> video ids, in playlist order.

        Cached for ``ttl_s`` (default ``playlist_ttl_s``); ``force`` skips the cache.
        """
        single = _single_video_id(uri)
        if single:
            return [single]
        now = time.time()
        with self._cache_lock:
            hit = self._playlists.get(uri)
        if hit and hit[0] > now and not force:
            return list(hit[1])

        ids = self._expand_catalog(uri) if self._catalog_ready() else None
        if not ids:
            ids = self._expand_ytdlp(uri)
        if ids is None:
            return []   # a failure: not cached, so the next press tries again
        ttl = self.playlist_ttl_s if ttl_s is None else ttl_s
        with self._cache_lock:
            self._playlists[uri] = (now + ttl, ids)
        return list(ids)

    def _expand_catalog(self, uri: str) -> list[str] | None:
        """ytmusicapi expansion, remembering names. None/[] = let yt-dlp try."""
        try:
            tracks = self.catalog.radio(uri) if _is_radio(uri) else self.catalog.tracks(uri)
        except Exception:
            log.warning("YouTube Music catalogue could not expand %s", uri, exc_info=True)
            return None
        self._remember(tracks)
        return [t["videoId"] for t in tracks]

    def _expand_ytdlp(self, uri: str) -> list[str] | None:
        cmd = [self.ytdlp, "--flat-playlist", "-J", *self._cookie_args(), uri]
        try:
            result = self._run(cmd, capture_output=True, text=True, timeout=120, check=False)
        except Exception:
            log.warning("yt-dlp could not be run for %s", uri, exc_info=True)
            return None
        if result.returncode != 0:
            log.warning("yt-dlp could not expand %s: %s", uri, _tail(result.stderr))
            return None
        try:
            data = json.loads(result.stdout)
        except (TypeError, ValueError):
            log.warning("yt-dlp gave unparseable JSON for %s", uri)
            return None
        return _ids_from_info(data)

    # -- catalogue, names and tags ----------------------------------------

    def _catalog_ready(self) -> bool:
        try:
            return self.catalog is not None and bool(self.catalog.available)
        except Exception:
            return False

    def _cookie_args(self) -> list[str]:
        """``--cookies`` for yt-dlp: the configured file, else the sign-in's, else none."""
        if self.cookies:
            return ["--cookies", self.cookies]
        path = getattr(self.catalog, "cookies_file", None) if self.catalog is not None else None
        try:
            if path and Path(path).exists():
                return ["--cookies", str(path)]
        except OSError:
            pass
        return []

    def _remember(self, tracks: list[dict]) -> None:
        with self._cache_lock:
            for t in tracks or []:
                vid = t.get("videoId")
                if isinstance(vid, str) and VIDEO_ID.match(vid):
                    self._meta.pop(vid, None)       # re-insert: newest last
                    self._meta[vid] = {k: str(t.get(k) or "") for k in ("title", "artist", "album", "thumb")}
            while len(self._meta) > META_CAP:
                del self._meta[next(iter(self._meta))]

    def _add(self, client: Any, url: str, vid: str) -> None:
        """Queue one track, tagged with its names when we know them. Under _mpd_lock."""
        with self._cache_lock:
            meta = self._meta.get(vid)
        if not meta or not self._tags_ok:
            client.add(url)
            return
        try:
            songid = client.addid(url)
        except Exception:
            log.debug("MPD addid failed; plain add", exc_info=True)
            client.add(url)
            return
        for tag, key in (("Title", "title"), ("Artist", "artist"), ("Album", "album")):
            if not meta.get(key):
                continue
            try:
                client.addtagid(songid, tag, meta[key])
            except Exception as exc:
                # The song is queued already; it just plays without a name.
                log.warning("MPD would not tag queued songs (%s); names off until restart", exc)
                self._tags_ok = False
                return

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
        try:
            self._maybe_top_up(vid)
        except Exception:
            log.warning("endless radio top-up failed", exc_info=True)

    # -- endless radio ----------------------------------------------------

    def _maybe_top_up(self, vid: str) -> bool:
        """If an endless radio is near the end of its queue, extend it. True if it did.

        Runs on the prefetch thread, never the request thread. One top-up at a
        time: a second request arriving mid-top-up simply skips.
        """
        uri, queue, generation = self._endless, list(self._queue), self._generation
        if not uri or not queue or vid not in queue:
            return False
        if queue.index(vid) < len(queue) - TOPUP_WITHIN:
            return False
        if not self._topup_lock.acquire(blocking=False):
            return False
        try:
            return self._top_up(uri, queue, generation)
        finally:
            self._topup_lock.release()

    def _top_up(self, uri: str, queue: list[str], generation: int) -> bool:
        tracks = self.catalog.radio(uri, after=queue[-1])
        known = set(queue)
        new = []
        for t in tracks or []:
            vid = t.get("videoId")
            if isinstance(vid, str) and VIDEO_ID.match(vid) and vid not in known:
                known.add(vid)
                new.append(vid)
        if not new:
            log.info("endless radio: nothing new from %s this time", uri)
            return False
        self._remember(tracks)
        prefix = self.prefix
        with self._mpd_lock:
            if generation != self._generation or self._endless != uri:
                return False                    # a new preset was pressed meanwhile
            client = self._mpd()
            status = client.status()
            length = int(status.get("playlistlength", 0) or 0)
            if length != len(queue):
                return False                    # someone else edited the queue: leave it be
            info = client.playlistinfo(length - 1)
            last = info[0] if isinstance(info, list) and info else info
            if str((last or {}).get("file", "")) != prefix + queue[-1]:
                return False
            dropped = 0
            excess = length + len(new) - QUEUE_CAP
            if excess > 0:
                current = int(status.get("song", 0) or 0)
                dropped = min(excess, current)  # only songs already played
                if dropped:
                    client.delete((0, dropped))
                new = new[: max(0, QUEUE_CAP - (length - dropped))]
            for vid in new:
                self._add(client, prefix + vid, vid)
            self._queue = queue[dropped:] + new
        log.info("endless radio: +%d tracks (dropped %d played)", len(new), dropped)
        return True

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


def _is_radio(uri: str) -> bool:
    """A YouTube radio/mix link: ``list=RD…`` (song RDAMVM…, artist RDEM…/RDAO…)."""
    lid = (parse_qs(urlparse(uri).query).get("list") or [""])[0]
    return lid.startswith("RD")


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
