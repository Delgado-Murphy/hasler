"""YouTube Music in moOde's Playlists view.

moOde has no plugin API: its Library and Search only know local files and its
radio list. What it does show is MPD's stored playlists, each with a cover.
So this keeps a set of them up to date - one per ytm preset, plus Liked songs
and the owner's playlists when signed in - all named ``YT · …``:

    #EXTM3U
    #EXTIMG:local
    #EXTINF:211,ROSALÍA - DESPECHÁ
    http://127.0.0.1:8099/ytm/<video id>

Line order is forced from both sides: MPD only reads #EXTINF if #EXTM3U is
the first line, and moOde only reads its playlist metadata from the first two
lines (NUMBER_EXT_TAGS) - so line 2 is moOde's cover flag ("local" = use
playlist-covers/<name>.jpg), and there is no room for its #EXTGENRE.

Tapping one in moOde loads those proxy URLs into the queue; they play through
the same redirect proxy as a preset. MPD reads the #EXTINF text as the song's
*Name*; YtmSource.tag_queue() then gives each one its real Title/Artist/Album
so moOde shows them properly (and the cover patch finds the art).

Only playlists this module wrote are ever touched: a manifest in the state
directory lists them, and a name that drops out of the set is deleted. A
radio exported here is a fixed ~50 tracks - endless radio needs the proxy's
top-up, which only runs for queues haslerd started itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

PLAYLIST_DIR = Path("/var/lib/mpd/playlists")
COVERS_DIR = Path("/var/local/www/imagesw/playlist-covers")   # moOde's PLAYLIST_COVERS_ROOT
PREFIX = "YT · "
SYNC_INTERVAL_S = 6 * 3600
MAX_LIBRARY_LISTS = 30          # the owner's playlists exported, at most
MAX_TRACKS = 500                # per playlist; MPD and moOde stay snappy
MANIFEST = "ytm-playlists.json"


def seconds(duration: str) -> int:
    """'3:31' / '1:02:03' -> seconds; anything else -> -1 (unknown, per #EXTINF)."""
    try:
        parts = [int(p) for p in str(duration).split(":")]
    except ValueError:
        return -1
    total = 0
    for p in parts:
        total = total * 60 + p
    return total if parts else -1


def safe_name(name: str) -> str:
    """A playlist name MPD and moOde can both use as a file name."""
    name = re.sub(r"[\x00-\x1f/\\]+", " ", name).strip().lstrip(".")
    return re.sub(r"\s{2,}", " ", name)[:80] or "Untitled"


def render_m3u(tracks: list[dict], prefix: str, cover: bool = False) -> str:
    lines = ["#EXTM3U"] + (["#EXTIMG:local"] if cover else [])
    for t in tracks[:MAX_TRACKS]:
        label = " - ".join(x for x in (t.get("artist", ""), t.get("title", "")) if x) or t["videoId"]
        lines.append(f"#EXTINF:{seconds(t.get('duration', ''))},{label.replace(chr(10), ' ')}")
        lines.append(prefix + t["videoId"])
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".hasler-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o664)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def fetch_bytes(url: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "haslerd"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(2_000_000)


class PlaylistSync:
    """Writes and prunes the ``YT · …`` playlists on a background thread."""

    def __init__(
        self,
        source: Any,                       # YtmSource: .prefix, ._remember()
        catalog: Any,                      # YtmCatalog
        state_dir: Path | None,
        playlist_dir: Path = PLAYLIST_DIR,
        covers_dir: Path = COVERS_DIR,
        fetch: Callable[[str], bytes] = fetch_bytes,
        interval_s: float = SYNC_INTERVAL_S,
    ) -> None:
        self.source = source
        self.catalog = catalog
        self.state_dir = state_dir
        self.playlist_dir = playlist_dir
        self.covers_dir = covers_dir
        self._fetch = fetch
        self.interval_s = interval_s
        self._presets: list[Any] = []
        self._kick = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()      # one sync at a time

    # -- control ------------------------------------------------------------

    def update(self, presets: list[Any]) -> None:
        """New preset list (startup, key page save): sync soon."""
        self._presets = [p for p in presets if getattr(p, "source", "") == "ytm"]
        self.kick()

    def kick(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="ytm-playlists", daemon=True)
            self._thread.start()
        self._kick.set()

    def close(self) -> None:
        self._stop.set()
        self._kick.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._kick.wait(self.interval_s)
            self._kick.clear()
            if self._stop.is_set():
                break
            try:
                self.sync_once()
            except Exception:
                log.warning("YouTube Music playlists for moOde: sync failed", exc_info=True)

    # -- the work -----------------------------------------------------------

    def wanted(self) -> list[tuple[str, list[dict], str]]:
        """(playlist name, tracks, cover url) for everything that should exist."""
        out: list[tuple[str, list[dict], str]] = []
        cat = self.catalog
        if cat is None or not getattr(cat, "available", False):
            return out
        for p in self._presets:
            tracks = cat.radio(p.uri) if "list=RD" in p.uri else cat.tracks(p.uri)
            if tracks:
                out.append((p.name, tracks, tracks[0].get("thumb", "")))
        if getattr(cat, "signed_in", False):
            for item in (cat.library() or [])[:MAX_LIBRARY_LISTS + 1]:
                tracks = cat.tracks(item["uri"])
                if tracks:
                    out.append((item["title"], tracks, item.get("thumb") or tracks[0].get("thumb", "")))
        return out

    def sync_once(self) -> list[str]:
        """Write the current set, prune the rest. Returns the names written."""
        with self._lock:
            if not self.playlist_dir.is_dir():
                log.debug("no MPD playlist directory at %s", self.playlist_dir)
                return []
            written: list[str] = []
            seen: set[str] = set()
            for name, tracks, cover in self.wanted():
                full = safe_name(PREFIX + name)
                if full in seen:
                    continue
                seen.add(full)
                self.source._remember(tracks)            # names + art for tag_queue / covers
                has_cover = self._cover(full, cover)
                try:
                    write_atomic(self.playlist_dir / f"{full}.m3u",
                                 render_m3u(tracks, self.source.prefix, has_cover).encode())
                except OSError:
                    log.warning("could not write playlist %r", full, exc_info=True)
                    continue
                written.append(full)
            self._prune(set(written))
            log.info("YouTube Music playlists for moOde: %d up to date", len(written))
            return written

    def _cover(self, name: str, url: str) -> bool:
        """Save the playlist's cover for moOde. True if one is in place."""
        path = self.covers_dir / f"{name}.jpg"
        if url and self.covers_dir.is_dir() and os.access(self.covers_dir, os.W_OK):
            try:
                write_atomic(path, self._fetch(url))
            except Exception:
                log.debug("cover for %r failed", name, exc_info=True)
        return path.exists()

    def _manifest(self) -> Path | None:
        return self.state_dir / MANIFEST if self.state_dir is not None else None

    def _prune(self, keep: set[str]) -> None:
        manifest = self._manifest()
        previous: set[str] = set()
        if manifest is not None and manifest.exists():
            try:
                previous = set(json.loads(manifest.read_text()))
            except (OSError, ValueError):
                previous = set()
        for name in previous - keep:
            if not name.startswith(PREFIX):
                continue                      # never anything we didn't name
            for path in (self.playlist_dir / f"{name}.m3u", self.covers_dir / f"{name}.jpg"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.debug("could not remove %s", path, exc_info=True)
        if manifest is not None:
            try:
                write_atomic(manifest, json.dumps(sorted(keep)).encode())
            except OSError:
                log.debug("could not write %s", manifest, exc_info=True)
