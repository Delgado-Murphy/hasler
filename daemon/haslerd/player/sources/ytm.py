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

Boot (``state_dir=``, /var/lib/haslerd). A cold box used to wait for Wi-Fi,
then expansion, then one resolve before the first note. Three things cut that:

* **State survives reboots** in ``ytm-state.json``: each preset's track ids,
  the opening track warm() chose, the names, the last-played preset and the
  resolved stream URLs with their expiry. At boot a press uses the stale list
  at once (warm() refreshes it in the background); a stale URL is caught by
  the stream check and resolved again, like any other refused link.
* **warm() goes last-played first**, and until a round has fully succeeded
  (the box booted before its Wi-Fi) retries the failed presets every
  ``BOOT_RETRY_S`` rather than every ``WARM_RETRY_S``.
* **The opening track is on disk.** ``prepare_for_boot(preset)`` - called
  when the radio goes to standby or shuts down cleanly - downloads the track
  that preset will open on into ``ytm-cache/`` (one per preset, at most
  ``CACHE_MAX_BYTES``). The proxy then answers that track's usual URL with
  the file itself instead of a redirect, so the queue is unchanged, MPD needs
  no network for its first song and the rest resolve once Wi-Fi is up. Served
  by the proxy rather than dropped into MPD's music directory because MPD over
  TCP refuses absolute paths, a relative one needs a database update first,
  and the daemon has no business writing into moOde's library.

Why not just redirect (``stream_dir=``, production: <state_dir>/ytm-stream).
googlevideo throttles one plain GET of a whole track to roughly real time
(~32 KB/s in bursts, measured), so MPD's decoder starved every few seconds
("Decoder is too slow; playing silence") - while Range requests come back at
full speed. So the proxy now fetches the track itself in ``STREAM_CHUNK``
Range requests into ``<stream_dir>/.tmp-<id>`` and streams that growing file
to MPD as soon as the first chunk is on disk, honouring MPD's own Range
requests (a seek far past the download is fetched directly). One download per
id however many requests ask for it; the finished file is renamed to
``<id>.m4a`` and kept for a replay. The prefetch downloads the *next* track
too, so a track change starts from disk. At most ``STREAM_KEEP`` tracks and
``STREAM_MAX_BYTES`` stay (least recently used go first). If the ranged fetch
fails outright (no first chunk), the proxy falls back to the 302 and logs it.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
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
# Until a round has fully succeeded since start-up (booted before the Wi-Fi),
# the failed presets are retried this often - for at most BOOT_FAST_S.
BOOT_RETRY_S = 5
BOOT_FAST_S = 10 * 60
# Playlists carry dead entries (removed, region-locked). If the opening track
# won't resolve, warm() tries this many in all before giving up for the round.
WARM_TRIES = 3

# Kept in the state directory (see the module docstring).
STATE_FILE = "ytm-state.json"
CACHE_DIR = "ytm-cache"
# One opening track per preset; a longer one is simply not cached.
CACHE_MAX_BYTES = 15 * 1024 * 1024
# Never fill the card: skip the download unless this much is free beyond it.
DISK_RESERVE_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT_S = 180
TMP_PREFIX = ".tmp-"
MIME = {".m4a": "audio/mp4", ".mp4": "audio/mp4", ".webm": "audio/webm",
        ".opus": "audio/ogg", ".ogg": "audio/ogg", ".mp3": "audio/mpeg"}

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
META_CAP = 20000   # whole libraries are exported to moOde (ytm_playlists)
TAG_POLL_S = 2.0   # how often tag_queue looks for a changed MPD queue

# Ranged streaming (see "Why not just redirect" in the module docstring).
# Where the tracks go: <state_dir>/ytm-stream, or stream_dir=.
STREAM_DIR = "ytm-stream"
STREAM_CHUNK = 1024 * 1024          # one Range request
STREAM_RETRIES = 3                  # per chunk, before the download gives up
STREAM_TIMEOUT_S = 15               # per Range request
STREAM_KEEP = 3                     # tracks kept: the current one and the next two
STREAM_MAX_BYTES = 60 * 1024 * 1024 # ... and never more than this (the newest is exempt)
# A request may wait this long for its first bytes (a cold yt-dlp resolve on a
# Pi takes several seconds) before the proxy gives up and redirects instead.
STREAM_READY_S = 40
# A reader stuck this long with no new bytes gives up (closes the connection).
STREAM_STALL_S = 30
# A seek landing further than this past what is on disk is fetched directly
# from googlevideo rather than waiting for the download to get there.
STREAM_DIRECT_GAP = 2 * 1024 * 1024
STREAM_UA = "Music Player Daemon"
CTYPE_EXT = {"audio/mp4": ".m4a", "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mpeg": ".mp3"}


class _Download:
    """One track being (or done being) fetched into the stream dir.

    ``have`` bytes from offset 0 are on disk at ``path``; readers wait on
    ``cond`` for it to grow. All fields change only under ``cond``.
    """

    def __init__(self, vid: str, path: Path) -> None:
        self.vid = vid
        self.path = path
        self.cond = threading.Condition()
        self.size: int | None = None
        self.ctype = "application/octet-stream"
        self.have = 0
        self.done = False
        self.failed = False

    @property
    def active(self) -> bool:
        return not (self.done or self.failed)


class _RangeRefused(Exception):
    """googlevideo answered a Range request with something other than 206."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def fetch_range(url: str, first: int, last: int, timeout: float = STREAM_TIMEOUT_S) -> tuple[bytes, int | None, str | None]:
    """One ``Range: bytes=first-last`` GET -> (body, total size or None, Content-Type).

    Raises ``_RangeRefused`` for any answer but 206 (a plain 200 means the
    server ignored the range: that is the throttled path, not ours).
    """
    req = urllib.request.Request(url, headers={"Range": f"bytes={first}-{last}", "User-Agent": STREAM_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 206:
                raise _RangeRefused(resp.status)
            total = None
            m = re.fullmatch(r"\s*bytes\s+\d+-\d+/(\d+)\s*", resp.headers.get("Content-Range") or "")
            if m:
                total = int(m.group(1))
            ctype = (resp.headers.get("Content-Type") or "").split(";", 1)[0].strip() or None
            return resp.read(), total, ctype
    except urllib.error.HTTPError as err:
        raise _RangeRefused(err.code) from None


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
    # Same MPD as radio: the router does not stop one to play the other.
    backend = "mpd"

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
        state_dir: str | Path | None = None,
        stream_dir: str | Path | None = None,
    ) -> None:
        self._mpd = mpd
        # ytm_playlists.PlaylistSync, attached by build_sources; told about
        # preset changes via warm().
        self.playlists: Any = None
        self._queue_version: str | None = None
        self._tagger: threading.Thread | None = None
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
        # (vid, ok, error) after each yt-dlp resolve - haslerd/ytdlp_lane.py
        # counts the failures and starts the lane when tracks stop resolving.
        self.on_resolve: Callable[[str, bool, str], None] | None = None

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
        # playlist uri -> the video id warm() resolved to open on: a pre-picked
        # random start for shuffle, the first *playable* track otherwise.
        self._starts: dict[str, str] = {}
        # Playlists loaded from the state file: a press may use them however
        # old, until warm() (or a failed refresh's fallback) has a fresh list.
        self._stale_ok: set[str] = set()
        # The ytm preset played last (warmed first; prepare_for_boot's default).
        self._last_uri: str | None = None
        # playlist uri -> file name in the cache dir of its opening track.
        self._files: dict[str, str] = {}
        self._prepare_lock = threading.RLock()  # one download (or prune) at a time
        self._save_lock = threading.Lock()
        self._state_file = Path(state_dir) / STATE_FILE if state_dir is not None else None
        self._cache_dir = Path(state_dir) / CACHE_DIR if state_dir is not None else None
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
        # Ranged streaming; None = off (redirect only). id -> _Download, least
        # recently used first. Guarded by _stream_lock.
        self._stream_dir = Path(stream_dir) if stream_dir is not None else None
        self._streams: dict[str, _Download] = {}
        self._stream_lock = threading.Lock()
        self._load_state()
        self._load_streams()

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
                        vid = self._starts.pop(preset.uri, None)
                    start = ids.index(vid) if vid in ids else random.randrange(len(ids))
                    client.play(start)
                else:
                    # Kept, not popped: in order, the opening track is stable.
                    with self._cache_lock:
                        vid = self._starts.get(preset.uri)
                    if vid in ids:
                        client.play(ids.index(vid))
                    else:
                        client.play()
        except Exception as exc:
            raise YtmError(f"MPD refused ytm preset {preset.n}: {exc}") from exc
        self._queue = ids
        self._last_uri = preset.uri
        self.save_state()
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

    # -- queues haslerd didn't build ---------------------------------------

    def tag_queue(self) -> int:
        """Name our tracks in a queue someone else loaded (moOde's Playlists).

        A ``YT · …`` playlist loads with MPD's Name tag only. Give each of our
        URLs its Title/Artist/Album and drop the Name, so moOde treats it like a
        preset's track. Only rescans when MPD's queue version changes. Returns
        how many songs were tagged.
        """
        if not self._tags_ok:
            return 0
        tagged = 0
        try:
            with self._mpd_lock:
                client = self._mpd()
                version = str(client.status().get("playlist", ""))
                if version == self._queue_version:
                    return 0
                for entry in client.playlistinfo() or []:
                    file = str(entry.get("file", ""))
                    if not file.startswith(self.prefix) or entry.get("title"):
                        continue
                    vid = file[len(self.prefix):]
                    with self._cache_lock:
                        meta = self._meta.get(vid)
                    if not meta:
                        continue
                    songid = entry.get("id")
                    for tag, key in (("Title", "title"), ("Artist", "artist"), ("Album", "album")):
                        if meta.get(key):
                            client.addtagid(songid, tag, meta[key])
                    if entry.get("name"):
                        client.cleartagid(songid, "Name")
                    tagged += 1
                # Our own tagging bumped the version; remember the new one.
                self._queue_version = str(client.status().get("playlist", ""))
        except Exception:
            log.debug("tag_queue failed", exc_info=True)
            self._queue_version = None
        if tagged:
            log.info("named %d YouTube Music tracks loaded from moOde", tagged)
        return tagged

    def _start_tagger(self) -> None:
        if self._tagger is not None:
            return

        def loop() -> None:
            while not self._warm_stop.wait(TAG_POLL_S):
                self.tag_queue()

        self._tagger = threading.Thread(target=loop, name="ytm-tagger", daemon=True)
        self._tagger.start()

    def cover(self, vid: str) -> str:
        """Best cover image for a video id: the catalogue's square album art if
        we have it, else YouTube's own thumbnail (always exists, 4:3)."""
        with self._cache_lock:
            thumb = (self._meta.get(vid) or {}).get("thumb", "")
        return thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    def close(self) -> None:
        self._warm_stop.set()
        self._warm_kick.set()
        self.save_state()
        if self.playlists is not None:
            self.playlists.close()
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
        (``WARM_RETRY_S`` after a round where anything failed; just the failed
        presets every ``BOOT_RETRY_S`` until a round has fully succeeded since
        start-up). The last-played preset goes first. Calling it again
        replaces the list and starts a fresh round at once - the web page does
        that when presets change.
        """
        self._warm_presets = [p for p in presets if p.source == "ytm"]
        if self.playlists is not None:
            self.playlists.update(self._warm_presets)
        self._forget_files({p.uri for p in self._warm_presets})
        self._start_tagger()
        if self._warm_thread is not None:
            self._warm_kick.set()
            return
        if not self._warm_presets:
            return
        self._warm_interval_s = interval_s

        def loop() -> None:
            started = time.monotonic()
            settled = False                 # a round has fully succeeded
            retry: list[Preset] | None = None
            while not self._warm_stop.is_set():
                todo = retry if retry is not None else self._ordered(self._warm_presets)
                failed = [p for p in todo if not self.warm_once(p)]
                self.save_state()
                if not failed:
                    settled = True
                    wait, retry = interval_s, None
                elif not settled and time.monotonic() - started < BOOT_FAST_S:
                    wait, retry = min(BOOT_RETRY_S, interval_s), failed
                else:
                    wait, retry = min(WARM_RETRY_S, interval_s), None
                if self._warm_kick.wait(wait):
                    retry = None            # new list, or the network came up
                self._warm_kick.clear()

        self._warm_thread = threading.Thread(target=loop, name="ytm-warm", daemon=True)
        self._warm_thread.start()
        log.info("warming %d ytm preset(s) every %ds", len(self._warm_presets), interval_s)

    def network_up(self) -> None:
        """Connectivity just (re)appeared: start a warm-up round now. Optional -
        the boot retry finds the network within ``BOOT_RETRY_S`` anyway."""
        if self._warm_thread is not None:
            self._warm_kick.set()

    def _ordered(self, presets: list[Preset]) -> list[Preset]:
        """The last-played preset first: it is the one the boot resume plays."""
        last = self._last_uri
        return sorted(presets, key=lambda p: p.uri != last)

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
                # A pick not yet played keeps its place, so the track
                # prepare_for_boot put on disk is still the one the box opens on.
                with self._cache_lock:
                    kept = self._starts.get(preset.uri)
                if kept in ids:
                    candidates = [ids.index(kept)] + [c for c in candidates if ids[c] != kept]
            else:
                candidates = list(range(tries))
            for start in candidates[:tries]:
                if self._disk_path(ids[start]) is not None or self.resolve(ids[start]) is not None:
                    with self._cache_lock:
                        self._starts[preset.uri] = ids[start]
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

    def local_file(self, path: str) -> Path | None:
        """The opening track on disk for a proxy request, if prepare_for_boot
        cached it - served as-is instead of a redirect. Starts the prefetch."""
        parts = path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "ytm" or not VIDEO_ID.match(parts[1]):
            return None
        found = self._disk_path(parts[1])
        if found is not None:
            log.info("ytm proxy: %s from the boot cache", parts[1])
            self._prefetch_after(parts[1])
        return found

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
        except Exception as exc:
            log.warning("yt-dlp could not be run for %s", vid, exc_info=True)
            self._resolved(vid, False, f"{type(exc).__name__}: {exc}")
            return None
        if result.returncode != 0:
            gone = any(marker in (result.stderr or "") for marker in GONE_MARKERS)
            self._mark_dead(vid, DEAD_GONE_S if gone else DEAD_OTHER_S)
            log.warning("yt-dlp failed for %s: %s", vid, _tail(result.stderr))
            if not gone:                  # a removed track is not yt-dlp's fault
                self._resolved(vid, False, result.stderr or "")
            return None
        for line in (result.stdout or "").splitlines():
            if line.startswith("http"):
                self._resolved(vid, True, "")
                return line.strip()
        log.warning("yt-dlp gave no URL for %s", vid)
        self._resolved(vid, False, "no URL")
        return None

    def _resolved(self, vid: str, ok: bool, error: str) -> None:
        """Tell the yt-dlp lane (haslerd/ytdlp_lane.py) how a resolve went."""
        hook = self.on_resolve
        if hook is not None:
            try:
                hook(vid, ok, _tail(error, 2000))
            except Exception:
                log.debug("on_resolve hook failed", exc_info=True)

    def canary(self, ytdlp: str, vid: str, min_bytes: int = 65536) -> dict:
        """The yt-dlp lane's canary: ``ytdlp`` (a slot's binary) resolving
        ``vid`` exactly as a key press does - same flags, same cookies, same
        user and PATH (deno) - then its first ``min_bytes`` by Range. Nothing
        is cached or marked dead."""
        return canary_with(ytdlp, vid, min_bytes, self._cookie_args(), self._run)

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
        A list loaded from the state file is used however old until a fresh
        one arrives, and a press whose expansion fails falls back to any old
        list rather than to silence. ``force`` (warm()) never falls back: it
        must see the failure, or it would resolve tracks with no network.
        """
        single = _single_video_id(uri)
        if single:
            return [single]
        now = time.time()
        with self._cache_lock:
            hit = self._playlists.get(uri)
            stale_ok = uri in self._stale_ok
        if hit and not force and (hit[0] > now or stale_ok):
            return list(hit[1])

        ids = self._expand_catalog(uri) if self._catalog_ready() else None
        if not ids:
            ids = self._expand_ytdlp(uri)
        if ids is None:
            if hit and not force:
                log.info("could not expand %s; playing its last known list", uri)
                return list(hit[1])
            return []   # a failure: not cached, so the next press tries again
        ttl = self.playlist_ttl_s if ttl_s is None else ttl_s
        with self._cache_lock:
            self._playlists[uri] = (now + ttl, ids)
            self._stale_ok.discard(uri)
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

    # -- state across reboots ---------------------------------------------

    def save_state(self) -> None:
        """Write what a cold boot needs to ``ytm-state.json``. Never raises."""
        if self._state_file is None:
            return
        now = time.time()
        keep = {p.uri for p in self._warm_presets}
        if self._last_uri:
            keep.add(self._last_uri)
        with self._cache_lock:
            keep.update(self._starts, self._files)
            playlists = {u: {"expires": exp, "ids": ids}
                         for u, (exp, ids) in self._playlists.items() if u in keep}
            wanted = {vid for entry in playlists.values() for vid in entry["ids"]}
            data = {
                "version": 1,
                "last": self._last_uri,
                "playlists": playlists,
                "starts": {u: v for u, v in self._starts.items() if u in keep},
                "files": dict(self._files),
                "urls": {v: [exp, url] for v, (exp, url) in self._urls.items()
                         if exp > now and v in wanted},
                "meta": {v: m for v, m in self._meta.items() if v in wanted},
            }
        try:
            body = json.dumps(data, separators=(",", ":")).encode()
            with self._save_lock:
                _write_atomic(self._state_file, body)
        except (OSError, TypeError, ValueError):
            log.warning("could not save the ytm state to %s", self._state_file, exc_info=True)

    def _load_state(self) -> None:
        """Pick up the last run's state. A missing or broken file is a cold start."""
        if self._state_file is None:
            return
        self._clear_tmp()
        try:
            data = json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("ignoring an unreadable %s", self._state_file, exc_info=True)
            return
        if not isinstance(data, dict):
            return
        now = time.time()

        def valid(vid: Any) -> bool:
            return isinstance(vid, str) and bool(VIDEO_ID.match(vid))

        with self._cache_lock:
            for uri, entry in (data.get("playlists") or {}).items():
                try:
                    ids = [v for v in entry["ids"] if valid(v)]
                    self._playlists[uri] = (float(entry["expires"]), ids)
                    self._stale_ok.add(uri)
                except (KeyError, TypeError, ValueError):
                    continue
            for uri, vid in (data.get("starts") or {}).items():
                if valid(vid):
                    self._starts[uri] = vid
            for vid, hit in (data.get("urls") or {}).items():
                try:
                    exp, url = float(hit[0]), str(hit[1])
                except (IndexError, TypeError, ValueError):
                    continue
                if valid(vid) and exp > now and url.startswith("http"):
                    self._urls[vid] = (exp, url)
            for vid, meta in (data.get("meta") or {}).items():
                if valid(vid) and isinstance(meta, dict):
                    self._meta[vid] = {k: str(meta.get(k) or "") for k in ("title", "artist", "album", "thumb")}
            for uri, name in (data.get("files") or {}).items():
                if isinstance(name, str) and valid(name.split(".", 1)[0]) and "/" not in name:
                    self._files[uri] = name
        last = data.get("last")
        self._last_uri = last if isinstance(last, str) else None
        log.info("ytm state: %d playlist(s), %d stream URL(s), %d cached opener(s) from %s",
                 len(self._playlists), len(self._urls), len(self._files), self._state_file)

    # -- the opening track on disk ------------------------------------------

    def prepare_for_boot(self, preset: Preset | None = None, wait_s: float = 0.0) -> bool:
        """Put the track ``preset`` will open on next time onto disk.

        For standby and clean shutdown: the boot resume then plays its first
        song from the card. ``preset`` is the one the box will resume (None =
        the last-played ytm preset); anything but a ytm preset is a no-op.
        Downloads on a background thread; ``wait_s`` waits up to that long for
        it (shutdown). True if the opener is on disk when this returns.
        """
        if self._cache_dir is None:
            return False
        if preset is None:
            preset = next((p for p in self._warm_presets if p.uri == self._last_uri), None)
        if preset is None or preset.source != "ytm":
            return False
        self._last_uri = preset.uri
        worker = threading.Thread(target=self._prepare, args=(preset,), name="ytm-boot-cache", daemon=True)
        worker.start()
        if wait_s > 0:
            worker.join(wait_s)
        with self._cache_lock:
            name = self._files.get(preset.uri)
        return name is not None and self._disk_path(name.split(".", 1)[0]) is not None

    def _prepare(self, preset: Preset) -> None:
        with self._prepare_lock:
            try:
                ids = self._expand(preset.uri)
                with self._cache_lock:
                    vid = self._starts.get(preset.uri)
                if vid not in ids and ids:
                    self.warm_once(preset)          # picks (and checks) the opener
                    with self._cache_lock:
                        vid = self._starts.get(preset.uri)
                if not vid or vid not in ids:
                    log.info("ytm preset %s: no opening track known; nothing cached", preset.n)
                    return
                path = self._disk_path(vid) or self._download(vid)
                if path is None:
                    return
                with self._cache_lock:
                    self._files[preset.uri] = path.name
                self._prune()
                log.info("ytm preset %s opens from the card next boot (%s, %d kB)",
                         preset.n, vid, path.stat().st_size // 1024)
            except Exception:
                log.warning("could not cache the opening track of ytm preset %s", preset.n, exc_info=True)
            finally:
                self.save_state()

    def _download(self, vid: str) -> Path | None:
        """yt-dlp ``vid``'s audio into the cache dir, atomically. None on any failure."""
        assert self._cache_dir is not None
        stem = TMP_PREFIX + vid
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._clear_tmp(stem)
            free = shutil.disk_usage(self._cache_dir).free
            if free < CACHE_MAX_BYTES + DISK_RESERVE_BYTES:
                log.warning("only %d MB free; not caching %s", free // 2**20, vid)
                return None
            cmd = [self.ytdlp, "-f", AUDIO_FORMAT, "--max-filesize", str(CACHE_MAX_BYTES),
                   "--no-playlist", "--no-progress", "--no-mtime",
                   "-o", str(self._cache_dir / (stem + ".%(ext)s")), *self._cookie_args(),
                   WATCH_URL.format(id=vid)]
            result = self._run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT_S, check=False)
            done = [p for p in self._cache_dir.glob(stem + ".*")
                    if p.suffix not in (".part", ".ytdl") and ".temp" not in p.suffixes]
            if result.returncode != 0 or len(done) != 1:
                log.warning("could not download %s for the boot cache: %s", vid, _tail(result.stderr))
                return None
            size = done[0].stat().st_size
            if not 0 < size <= CACHE_MAX_BYTES:
                log.info("not caching %s: %d bytes", vid, size)
                return None
            final = self._cache_dir / (vid + done[0].suffix)
            os.chmod(done[0], 0o644)
            os.replace(done[0], final)
            return final
        except subprocess.TimeoutExpired:
            log.warning("yt-dlp took too long downloading %s", vid)
            return None
        except OSError:
            # A full card lands here too; the box boots the slow way, that's all.
            log.warning("could not write %s to the boot cache", vid, exc_info=True)
            return None
        finally:
            self._clear_tmp(stem)

    def _disk_path(self, vid: str) -> Path | None:
        if self._cache_dir is None:
            return None
        with self._cache_lock:
            names = [n for n in self._files.values() if n.split(".", 1)[0] == vid]
        for name in names:
            path = self._cache_dir / name
            if path.is_file():
                return path
        return None

    def _forget_files(self, uris: set[str]) -> None:
        """Presets changed: drop the cached openers of presets that are gone."""
        with self._cache_lock:
            gone = [u for u in self._files if u not in uris]
            for uri in gone:
                del self._files[uri]
        # Mid-download, leave it: that download prunes when it is done.
        if gone and self._prepare_lock.acquire(blocking=False):
            try:
                self._prune()
            finally:
                self._prepare_lock.release()

    def _prune(self) -> None:
        """Delete every finished file in the cache dir no preset points at."""
        if self._cache_dir is None:
            return
        with self._cache_lock:
            wanted = set(self._files.values())
        try:
            for path in self._cache_dir.iterdir():
                if path.name not in wanted and not path.name.startswith(TMP_PREFIX):
                    path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("could not prune the ytm boot cache", exc_info=True)

    def _clear_tmp(self, stem: str = TMP_PREFIX) -> None:
        """Remove half-written downloads (all of them, or one video's)."""
        if self._cache_dir is None:
            return
        try:
            for path in self._cache_dir.glob(stem + "*"):
                path.unlink(missing_ok=True)
        except OSError:
            log.debug("could not clear %s*", stem, exc_info=True)

    # -- ranged streaming ---------------------------------------------------

    def stream(self, path: str) -> _Download | None:
        """The download serving a proxy request, once its first chunk is on disk.

        None when streaming is off, the path isn't ours, or the ranged fetch
        failed before any data arrived - the caller then redirects. Starts the
        prefetch on success.
        """
        if self._stream_dir is None:
            return None
        parts = path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "ytm" or not VIDEO_ID.match(parts[1]):
            return None
        vid = parts[1]
        if self._is_dead(vid):
            return None
        dl = self._ensure_download(vid)
        if dl is None or not self._wait_ready(dl, STREAM_READY_S):
            log.warning("ytm proxy: ranged fetch of %s failed; redirecting MPD to googlevideo", vid)
            return None
        self._prefetch_after(vid)
        return dl

    def _wait_ready(self, dl: _Download, timeout: float) -> bool:
        """Wait until the size and the first bytes are known. False if it failed."""
        deadline = time.monotonic() + timeout
        with dl.cond:
            while not (dl.have > 0 and dl.size is not None) and not dl.failed:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                dl.cond.wait(left)
            return not dl.failed

    def _wait_bytes(self, dl: _Download, pos: int) -> int:
        """For a reader at ``pos``: bytes ready on disk there (> 0), 0 = fetch this
        part directly (far past the download), -1 = give up."""
        with dl.cond:
            while True:
                if dl.have > pos:
                    return dl.have - pos
                if dl.failed or dl.done:
                    return -1               # done and still short: past the end
                if pos >= dl.have + STREAM_DIRECT_GAP:
                    return 0
                before = dl.have
                dl.cond.wait(STREAM_STALL_S)
                if dl.have == before and dl.active:
                    log.warning("ytm proxy: download of %s stalled", dl.vid)
                    return -1

    def _open_stream(self, dl: _Download) -> Any:
        """Open the download's file. Under its lock, so the rename can't race it;
        an open handle keeps reading across the rename (and a prune)."""
        with dl.cond:
            return open(dl.path, "rb")

    def fetch_direct(self, vid: str, first: int, last: int) -> bytes:
        """Bytes ``first..last`` straight from googlevideo, for a far seek."""
        url = self.resolve(vid)
        if url is None:
            raise OSError(f"{vid} no longer resolves")
        return fetch_range(url, first, last)[0]

    def _ensure_download(self, vid: str) -> _Download | None:
        """The one download of ``vid``: an existing one (running or finished),
        else a new one on a background thread. None if streaming is off."""
        if self._stream_dir is None:
            return None
        with self._stream_lock:
            dl = self._streams.pop(vid, None)
            if dl is not None and dl.failed:
                dl = None
            if dl is not None and dl.done and not dl.path.is_file():
                dl = None                   # deleted behind our back
            fresh = dl is None
            if fresh:
                dl = _Download(vid, self._stream_dir / (TMP_PREFIX + vid))
            self._streams[vid] = dl         # most recently used last
        if fresh:
            threading.Thread(target=self._download_stream, args=(dl,),
                             name="ytm-stream", daemon=True).start()
        return dl

    def _download_stream(self, dl: _Download) -> None:
        vid = dl.vid
        try:
            if self._fetch_stream(dl):
                return
        except Exception:
            log.warning("ytm stream download of %s failed", vid, exc_info=True)
        with dl.cond:
            dl.failed = True
            dl.cond.notify_all()
        with self._stream_lock:
            if self._streams.get(vid) is dl:
                del self._streams[vid]
        try:
            dl.path.unlink(missing_ok=True)
        except OSError:
            pass

    def _fetch_stream(self, dl: _Download) -> bool:
        """Download ``dl`` chunk by chunk. True when complete and renamed."""
        assert self._stream_dir is not None
        vid = dl.vid
        url = self.resolve(vid)
        if url is None:
            return False
        self._stream_dir.mkdir(parents=True, exist_ok=True)
        clen = _clen(url)
        free = shutil.disk_usage(self._stream_dir).free
        if free < (clen or CACHE_MAX_BYTES) + DISK_RESERVE_BYTES:
            log.warning("only %d MB free; not streaming %s through the card", free // 2**20, vid)
            return False
        self._prune_streams()
        reresolved = False
        started = time.monotonic()
        with open(dl.path, "wb") as fh:
            pos, size, tries = 0, clen, 0
            while size is None or pos < size:
                if self._warm_stop.is_set():
                    return False            # shutting down
                last = pos + STREAM_CHUNK - 1
                if size is not None:
                    last = min(last, size - 1)
                want = last - pos + 1
                try:
                    data, total, ctype = fetch_range(url, pos, last)
                except _RangeRefused as exc:
                    if exc.status in (403, 410) and not reresolved:
                        log.info("googlevideo refused %s (%s); resolving again", vid, exc)
                        reresolved = True
                        with self._cache_lock:
                            self._urls.pop(vid, None)
                        url = self.resolve(vid)
                        if url is None:
                            return False
                        continue
                    if exc.status in (403, 410, 200) or tries >= STREAM_RETRIES:
                        log.warning("ytm stream %s: Range %d-%d refused: %s", vid, pos, last, exc)
                        return False
                    tries += 1
                    time.sleep(0.5 * tries)
                    continue
                except (OSError, ValueError, http.client.HTTPException) as exc:   # timeouts, resets, short reads
                    if tries >= STREAM_RETRIES:
                        log.warning("ytm stream %s: Range %d-%d failed: %s", vid, pos, last, exc)
                        return False
                    tries += 1
                    time.sleep(0.5 * tries)
                    continue
                tries = 0
                if not data:
                    if size is None:
                        size = pos          # the server says that's all
                        break
                    log.warning("ytm stream %s: empty chunk at %d of %d", vid, pos, size)
                    return False
                if total is not None:
                    size = total
                fh.write(data)
                fh.flush()
                pos += len(data)
                if size is None and len(data) < want:
                    size = pos              # short read, no total: the end
                with dl.cond:
                    if ctype and ctype != "application/octet-stream":
                        dl.ctype = ctype
                    elif dl.ctype == "application/octet-stream":
                        dl.ctype = _url_mime(url) or dl.ctype
                    dl.size = size
                    dl.have = pos
                    dl.cond.notify_all()
        final = self._stream_dir / (vid + CTYPE_EXT.get(dl.ctype, ".bin"))
        with dl.cond:
            os.chmod(dl.path, 0o644)
            os.replace(dl.path, final)
            dl.path = final
            dl.size = dl.have
            dl.done = True
            dl.cond.notify_all()
        log.info("ytm stream %s: %d kB in %.1f s", vid, dl.have // 1024, time.monotonic() - started)
        self._prune_streams()
        return True

    def _prune_streams(self) -> None:
        """Keep the ``STREAM_KEEP`` most recently used tracks and at most
        ``STREAM_MAX_BYTES`` of them; running downloads are never touched, nor
        is the most recent finished one, however big."""
        if self._stream_dir is None:
            return
        doomed: list[Path] = []
        with self._stream_lock:
            order = list(self._streams.values())           # oldest first
            finished = [d for d in order if d.done]
            total = sum(d.size or 0 for d in finished)
            excess = len(order) - STREAM_KEEP
            for dl in finished[:-1]:
                if excess <= 0 and total <= STREAM_MAX_BYTES:
                    break
                del self._streams[dl.vid]
                doomed.append(dl.path)
                excess -= 1
                total -= dl.size or 0
            keep = {d.path.name for d in self._streams.values()}
        for path in doomed:
            path.unlink(missing_ok=True)
        try:
            for path in self._stream_dir.iterdir():     # strays from older runs
                if path.name not in keep:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_streams(self) -> None:
        """Pick up finished tracks from the last run; drop half-written ones."""
        if self._stream_dir is None:
            return
        try:
            paths = sorted(self._stream_dir.iterdir(), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for path in paths:
            vid, ext = path.stem, path.suffix
            ctype = next((c for c, e in CTYPE_EXT.items() if e == ext), None)
            if path.name.startswith(TMP_PREFIX) or not VIDEO_ID.match(vid) or ctype is None:
                path.unlink(missing_ok=True)
                continue
            dl = _Download(vid, path)
            dl.size = dl.have = path.stat().st_size
            dl.ctype, dl.done = ctype, True
            self._streams[vid] = dl
        self._prune_streams()

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
            if nxt and nxt != vid and self._disk_path(nxt) is None and not self._is_dead(nxt):
                self._ensure_download(nxt)  # on its own thread; no-op if streaming is off
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
                local = source.local_file(self.path)
            except Exception:
                log.exception("ytm boot cache lookup failed for %s", self.path)
                local = None
            if local is not None:
                try:
                    if self._send_file(local):
                        return
                except (BrokenPipeError, ConnectionResetError):
                    return                  # MPD skipped or seeked away mid-file
            try:
                dl = source.stream(self.path)
            except Exception:
                log.exception("ytm stream failed for %s", self.path)
                dl = None
            if dl is not None:
                try:
                    if self._send_stream(dl):
                        return
                except (BrokenPipeError, ConnectionResetError):
                    return                  # MPD skipped or seeked away mid-track
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

        def _send_file(self, path: Path) -> bool:
            """200, or 206 for one ``Range``, with the file's bytes. False if it
            vanished (pruned) - the caller falls back to the redirect."""
            try:
                fh = open(path, "rb")
            except OSError:
                return False
            with fh:
                size = os.fstat(fh.fileno()).st_size
                span = _byte_range(self.headers.get("Range"), size)
                if span is None:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return True
                start, end, partial = span
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command == "HEAD":
                    return True
                fh.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = fh.read(min(64 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            return True

        def _send_stream(self, dl: _Download) -> bool:
            """Like _send_file, from a download that may still be growing.
            False if its file vanished before we got it open (redirect instead)."""
            with dl.cond:
                size, ctype = dl.size, dl.ctype
            assert size is not None             # stream() waited for it
            span = _byte_range(self.headers.get("Range"), size)
            if span is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                return True
            try:
                fh = source._open_stream(dl)
            except OSError:
                return False
            with fh:
                start, end, partial = span
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command == "HEAD":
                    return True
                pos = start
                while pos <= end:
                    ready = source._wait_bytes(dl, pos)
                    if ready < 0:
                        # Failed or stalled: cut the body short. MPD notices the
                        # short read and reconnects or skips - better than hanging.
                        self.close_connection = True
                        return True
                    if ready == 0:
                        last = min(end, pos + STREAM_CHUNK - 1)
                        try:
                            chunk = source.fetch_direct(dl.vid, pos, last)[: last - pos + 1]
                        except Exception as exc:
                            log.warning("ytm proxy: direct fetch of %s at %d failed: %s", dl.vid, pos, exc)
                            self.close_connection = True
                            return True
                    else:
                        fh.seek(pos)
                        chunk = fh.read(min(64 * 1024, ready, end - pos + 1))
                    if not chunk:
                        self.close_connection = True
                        return True
                    self.wfile.write(chunk)
                    pos += len(chunk)
            return True

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


def _clen(url: str) -> int | None:
    """googlevideo's ``clen=`` (the stream's byte length), if the URL has one."""
    try:
        n = int(parse_qs(urlparse(url).query)["clen"][0])
        return n if n > 0 else None
    except (KeyError, IndexError, ValueError):
        return None


def _url_mime(url: str) -> str | None:
    """googlevideo's ``mime=audio/mp4`` (itag 140), if the URL has one."""
    mime = (parse_qs(urlparse(url).query).get("mime") or [""])[0]
    return mime or None


def _expire_stamp(url: str) -> float | None:
    try:
        return float(parse_qs(urlparse(url).query)["expire"][0])
    except (KeyError, IndexError, ValueError):
        return None


def _byte_range(header: str | None, size: int) -> tuple[int, int, bool] | None:
    """``Range: bytes=a-b`` -> (first, last, partial), inclusive. The whole file
    (partial False) if absent or not one byte range we understand; None if
    unsatisfiable (416)."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not m or not (m.group(1) or m.group(2)):
        return 0, size - 1, False
    first, last = m.group(1), m.group(2)
    if not first:                                   # the last N bytes
        n = int(last)
        return (max(0, size - n), size - 1, True) if n > 0 else None
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        return None
    return start, end, True


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hasler-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)     # signed stream URLs: the daemon's business only
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


CANARY_NET = re.compile(r"Temporary failure in name resolution|Name or service not known|Network is unreachable|"
                        r"No route to host|Connection refused|Connection reset|timed out|getaddrinfo failed", re.I)


def looks_like_audio(data: bytes) -> bool:
    """MP4/M4A (ftyp), WebM/Matroska (EBML), Ogg, MP3 - what bestaudio can be."""
    return (data[4:8] == b"ftyp" or data[:4] == b"\x1a\x45\xdf\xa3" or data[:4] == b"OggS"
            or data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0))


def canary_with(ytdlp: str, vid: str, min_bytes: int, cookie_args: list[str],
                runner: Callable[..., Any] = subprocess.run,
                fetch: Callable[..., tuple[bytes, int | None, str | None]] | None = None) -> dict:
    """-> {"result": pass|fail|offline, "stage": resolve|fetch|audio, "error", "bytes", "ctype"}."""
    fetch = fetch or fetch_range
    cmd = [ytdlp, "-f", AUDIO_FORMAT, "-g", *cookie_args, WATCH_URL.format(id=vid)]
    try:
        result = runner(cmd, capture_output=True, text=True, timeout=90, check=False)
    except Exception as exc:
        return {"result": "fail", "stage": "resolve", "error": f"{type(exc).__name__}: {exc}",
                "cookies": bool(cookie_args)}
    url = next((x.strip() for x in (result.stdout or "").splitlines() if x.startswith("http")), None)
    if result.returncode != 0 or not url:
        err = _tail(result.stderr) or "no URL"
        return {"result": "offline" if CANARY_NET.search(err) else "fail", "stage": "resolve", "error": err,
                "cookies": bool(cookie_args)}
    try:
        body, _total, ctype = fetch(url, 0, min_bytes - 1)
    except _RangeRefused as exc:
        return {"result": "fail", "stage": "fetch", "error": f"HTTP {exc.status}, not 206", "cookies": bool(cookie_args)}
    except Exception as exc:
        return {"result": "offline" if CANARY_NET.search(str(exc)) else "fail", "stage": "fetch",
                "error": f"{type(exc).__name__}: {exc}", "cookies": bool(cookie_args)}
    if len(body) < min_bytes or not looks_like_audio(body):
        return {"result": "fail", "stage": "audio", "error": f"{len(body)} bytes of {ctype or '?'}, not audio",
                "cookies": bool(cookie_args)}
    return {"result": "pass", "bytes": len(body), "ctype": ctype, "cookies": bool(cookie_args)}


def _tail(text: str | None, limit: int = 300) -> str:
    text = (text or "").strip()
    return text[-limit:]
