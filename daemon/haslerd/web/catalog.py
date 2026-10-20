"""The Browse catalogue: ready-made things to play or put on a key.

Shelves of radio stations, YouTube Music playlists and artist radios, grouped
by section (Recommended, Spanish radio by genre, one per kind of music, Moods,
Kids & family). Every item is already a preset - ``source`` + ``uri`` + a
suggested ``name`` - so the page can hand it straight to ``POST /api/play``
or to a key without a second lookup.

Two layers, so the page is fast and nothing here can stall it:

* **Static file** - ``config/catalog/catalog.json``, generated on the radio
  (so it reflects what YouTube Music offers in Spain) and checked in. Every
  playlist in it was opened with ``get_playlist`` and every stream fetched with
  curl from the Pi when it was written. ``sections()`` serves it from memory.
* **Live refresh** - YouTube Music's own lists (``RDCLAK5uy_…``) are curated by
  YouTube and occasionally re-issued under a new id or new artwork. Once a day
  ``refresh()`` re-reads the Moods & genres pages and, for each static item
  that remembers its YouTube Music title (``ytm_title``), takes the current id
  and artwork for that title. Items it can't find keep their static entry -
  an official list dropping off a genre page usually still plays. The result
  is cached in the state directory so a restart doesn't need the network.

``sections()`` never waits for any of that: when the data is stale it starts
the refresh in a daemon thread and returns what it has.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..boxconfig import shipped_data

log = logging.getLogger(__name__)

# Shipped with the release (config/catalog in the release root).
STATIC_FILE = shipped_data("catalog/catalog.json")
CACHE_FILE = "catalog-live.json"
MAX_AGE_S = 24 * 3600          # re-read YouTube Music's lists once a day
RETRY_S = 3600                 # after a failed refresh, wait this long
ITEM_KEYS = ("type", "title", "subtitle", "thumb", "source", "uri", "name")
TYPES = ("radio", "playlist", "radio_mix", "album")
SOURCES = ("radio", "ytm", "spotify")
PLAYLIST = "https://music.youtube.com/playlist?list={id}"
MOODS_BROWSE = "FEmusic_moods_and_genres_category"
MORE_RADIO_LIMIT = 30

Index = dict[str, dict[str, str]]      # normalised YouTube Music title -> {"id", "thumb"}


class Catalog:
    """The Browse shelves. Thread-safe; ``sections()`` never touches the network.

    ``ytm_catalog`` is the daemon's ``YtmCatalog`` (its locked client is reused,
    one request at a time). ``fetch`` replaces the live read entirely - a
    callable returning an ``Index`` - and is what tests pass. ``radio_search``
    (``query -> [{"name", "url"|"url_resolved", …}]``, e.g. the Radio Browser
    search) backs ``more_radio()``; without it that returns []. ``data_dir`` is
    where the live cache is kept (None: memory only).
    """

    def __init__(
        self,
        ytm_catalog: Any = None,
        radio_search: Callable[[str], list[dict]] | None = None,
        fetch: Callable[[], Index] | None = None,
        data_dir: str | Path | None = None,
        static_file: str | Path = STATIC_FILE,
        max_age_s: float = MAX_AGE_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ytm_catalog = ytm_catalog
        self.radio_search = radio_search
        self._fetch = fetch if fetch is not None else (self._fetch_ytm if ytm_catalog is not None else None)
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.static_file = Path(static_file)
        self.max_age_s = max_age_s
        self.clock = clock

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._static = _load_static(self.static_file)
        self._fetched_at = 0.0
        self._last_attempt = 0.0
        self._sections = self._static
        cached = self._load_cache()
        if cached is not None:
            self._fetched_at, index = cached
            self._sections = merge(self._static, index)

    # -- reading -----------------------------------------------------------

    def sections(self) -> list[dict]:
        """``[{"id", "title", "subtitle", "items": [item…]}]``, immediately.

        Kicks off a background refresh when the live data is older than a day.
        """
        self.refresh_async()
        sections = self._sections            # one reference read: never half-merged
        return [
            {
                "id": s["id"], "title": s["title"], "subtitle": s.get("subtitle", ""),
                "items": [{k: str(item.get(k) or "") for k in ITEM_KEYS} for item in s["items"]],
            }
            for s in sections
        ]

    def more_radio(self, query: str) -> list[dict]:
        """Radio stations from ``radio_search`` as Browse items (for a "more…" button)."""
        if self.radio_search is None or not (query or "").strip():
            return []
        try:
            found = self.radio_search(query.strip()) or []
        except Exception as exc:
            log.warning("radio search for the catalogue failed: %s: %s", type(exc).__name__, exc)
            return []
        out, seen = [], set()
        for st in found:
            if not isinstance(st, dict):
                continue
            url = str(st.get("url_resolved") or st.get("url") or "").strip()
            name = str(st.get("name") or "").strip()
            if not url.startswith(("http://", "https://")) or not name or url in seen:
                continue
            seen.add(url)
            sub = st.get("genre") or st.get("tags") or st.get("country") or "Radio"
            thumb = str(st.get("favicon") or st.get("thumb") or "")
            out.append({
                "type": "radio", "title": name[:80], "subtitle": str(sub)[:80],
                "thumb": thumb if thumb.startswith("https://") else "",
                "source": "radio", "uri": url, "name": name[:60],
            })
            if len(out) >= MORE_RADIO_LIMIT:
                break
        return out

    # -- refreshing --------------------------------------------------------

    @property
    def stale(self) -> bool:
        now = self.clock()
        return (
            self._fetch is not None
            and now - self._fetched_at >= self.max_age_s
            and now - self._last_attempt >= RETRY_S
        )

    def refresh_async(self, force: bool = False) -> bool:
        """Start ``refresh()`` in a daemon thread if due (or ``force``). True if started."""
        if self._fetch is None:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if not (force or self.stale):
                return False
            self._last_attempt = self.clock()   # claimed now, so a burst of page loads starts one
            self._thread = threading.Thread(target=self.refresh, kwargs={"force": True},
                                            name="catalog-refresh", daemon=True)
            self._thread.start()
            return True

    def refresh(self, force: bool = False) -> bool:
        """Re-read YouTube Music's lists and lay them over the static file.

        Synchronous (the web page calls ``refresh_async``). True if new data was
        applied; on any failure the current shelves stay as they are.
        """
        if self._fetch is None or not (force or self.stale):
            return False
        self._last_attempt = self.clock()
        try:
            index = self._fetch()
        except Exception as exc:
            log.warning("catalogue refresh failed: %s: %s", type(exc).__name__, str(exc)[:200])
            return False
        if not isinstance(index, dict) or not index:
            log.info("catalogue refresh: YouTube Music returned nothing; keeping what we have")
            return False
        merged = merge(self._static, index)
        self._sections = merged                 # reference swap
        self._fetched_at = self.clock()
        self._save_cache(index)
        changed = sum(
            a.get("uri") != b.get("uri")
            for s0, s1 in zip(self._static, merged) for a, b in zip(s0["items"], s1["items"])
        )
        log.info("catalogue refreshed: %d YouTube Music titles seen, %d links updated", len(index), changed)
        return True

    def wait(self, timeout: float | None = None) -> None:
        """Join a running background refresh (tests, shutdown)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # -- the live read ------------------------------------------------------

    def _fetch_ytm(self) -> Index:
        """YouTube Music's Moods & genres pages -> ``Index``. One request at a time.

        ytmusicapi 1.12's ``get_mood_playlists`` can't parse the genre pages
        (they have a "From The Start" shelf with no link), so the browse JSON is
        walked directly - the same request the web app makes.
        """
        ytm = self.ytm_catalog
        if ytm is None or not getattr(ytm, "available", False):
            return {}
        cats = ytm._call("mood categories", lambda c: c.get_mood_categories(), {}) or {}
        index: Index = {}
        for entries in cats.values() if isinstance(cats, dict) else []:
            for cat in entries or []:
                params = cat.get("params") if isinstance(cat, dict) else None
                if not params:
                    continue
                raw = ytm._call(
                    "mood playlists",
                    lambda c, p=params: c._send_request("browse", {"browseId": MOODS_BROWSE, "params": p}),
                    None,
                )
                for found in walk_playlists(raw):
                    index.setdefault(norm(found["title"]), {"id": found["id"], "thumb": found["thumb"]})
        return index

    # -- cache ---------------------------------------------------------------

    @property
    def cache_file(self) -> Path | None:
        return self.data_dir / CACHE_FILE if self.data_dir is not None else None

    def _load_cache(self) -> tuple[float, Index] | None:
        path = self.cache_file
        if path is None or not path.exists():
            return None
        try:
            doc = json.loads(path.read_text())
            index = doc["index"]
            if not isinstance(index, dict):
                return None
            return float(doc.get("fetched_at") or 0), index
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("ignoring unreadable %s", path)
            return None

    def _save_cache(self, index: Index) -> None:
        path = self.cache_file
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump({"fetched_at": self._fetched_at, "index": index}, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError:
            log.warning("could not write %s", path, exc_info=True)


# -- helpers -----------------------------------------------------------------


def norm(title: str) -> str:
    return " ".join(str(title or "").split()).casefold()


def merge(static: list[dict], index: Index) -> list[dict]:
    """The static sections with current ids/artwork for items YouTube Music still lists.

    Only items carrying ``ytm_title`` (YouTube Music's own playlists) are
    touched; their display title, subtitle and suggested name stay as curated.
    """
    out = copy.deepcopy(static)
    for section in out:
        seen: set[str] = set()
        kept = []
        for item in section["items"]:
            live = index.get(norm(item.get("ytm_title", ""))) if item.get("ytm_title") else None
            if isinstance(live, dict):
                pid = str(live.get("id") or "")
                if pid.startswith("RDCLAK") and pid.replace("_", "").replace("-", "").isalnum():
                    item["uri"] = PLAYLIST.format(id=pid)
                thumb = str(live.get("thumb") or "")
                if thumb.startswith("https://"):
                    item["thumb"] = thumb
            if item["uri"] in seen:      # a re-issued list can collide with another item
                continue
            seen.add(item["uri"])
            kept.append(item)
        section["items"] = kept
    return out


def walk_playlists(node: Any) -> list[dict]:
    """Every playlist tile (``musicTwoRowItemRenderer``) in a browse response."""
    out: list[dict] = []

    def text(x: Any) -> str:
        if not isinstance(x, dict):
            return ""
        return x.get("simpleText") or "".join(r.get("text", "") for r in x.get("runs", []) if isinstance(r, dict))

    def visit(n: Any) -> None:
        if isinstance(n, list):
            for v in n:
                visit(v)
            return
        if not isinstance(n, dict):
            return
        tile = n.get("musicTwoRowItemRenderer")
        if isinstance(tile, dict):
            nav = tile.get("navigationEndpoint") or {}
            bid = str((nav.get("browseEndpoint") or {}).get("browseId") or "")
            pid = bid[2:] if bid.startswith("VL") else str((nav.get("watchPlaylistEndpoint") or {}).get("playlistId") or "")
            title = text(tile.get("title"))
            if pid and title:
                thumbs = (((tile.get("thumbnailRenderer") or {}).get("musicThumbnailRenderer") or {})
                          .get("thumbnail") or {}).get("thumbnails") or []
                thumb = next((t["url"] for t in reversed(thumbs)
                              if isinstance(t, dict) and str(t.get("url", "")).startswith("https://")), "")
                out.append({"id": pid, "title": title, "thumb": thumb})
            return
        for v in n.values():
            visit(v)

    visit(node)
    return out


def _load_static(path: Path) -> list[dict]:
    """The checked-in catalogue, keeping only well-formed sections and items."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        log.warning("no catalogue at %s; Browse will be empty", path)
        return []
    sections = []
    raw_sections = doc.get("sections") if isinstance(doc, dict) else None
    for raw in raw_sections if isinstance(raw_sections, list) else []:
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        items = []
        for item in raw.get("items") or []:
            if (
                isinstance(item, dict)
                and item.get("type") in TYPES
                and item.get("source") in SOURCES
                and str(item.get("uri") or "").startswith(("http://", "https://", "spotify:"))
                and item.get("title")
            ):
                item = dict(item)
                item.setdefault("name", str(item["title"])[:60])
                items.append(item)
        sections.append({"id": str(raw["id"]), "title": str(raw.get("title") or raw["id"]),
                         "subtitle": str(raw.get("subtitle") or ""), "items": items})
    return sections
