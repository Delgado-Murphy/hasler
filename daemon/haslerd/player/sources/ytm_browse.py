"""Browsing YouTube Music for the key page, and looking up what a preset means.

ytmusicapi (through ``YtmCatalog``'s one client and its lock) answers the
page's browser - search, suggestions, Moods & genres, charts, artists, albums,
home, the owner's library, related songs, lyrics - and the ytm source's
press-time lookups of preset references (ytm_refs.py: an artist's top songs,
this week's chart, a mood list that YouTube re-issues).

Every answer is a list of **items** of one shape, so the page renders them
all the same way and can put any of them on a key without a second request::

    {"type": "song|album|artist|playlist|chart|mood", "title", "subtitle",
     "thumb",            # googleusercontent art rewritten to =w226-h226
     "id",               # videoId / MPREb_ or OLAK5uy_ / UC… / playlist id / mood params
     "preset": {…},      # ready for POST /api/play or a key (None for a mood category)
     "radio": {…}|None,  # an endless radio from this item, when there is one
     "explicit": bool,   # a badge; settings ytm.hide_explicit drops them instead
     "artists": [{"id", "name"}], "album": {"id", "title"}|None,
     "uri", "name"}      # the preset's (or radio's) link and name, for older pages

Presets are ``{"source": "ytm", "kind", …ref fields, "uri", "shuffle", "name"}``;
``uri`` is filled whenever it is known, so a key keeps playing through yt-dlp
when ytmusicapi is down.

Caching: in memory per kind of answer (search 10 min LRU 200, suggestions
10 min, a mood page / charts 6 h, the country list 7 d, an artist / album /
lyrics 1 d, home 30 min, library 5 min). Moods and charts are also kept on the
card (``<state>/ytm-cache/browse``): after a boot the browser opens from there
at once, however old, and refreshes behind the page's back. A failed call
never empties a cache: the last good answer stays, and with none the answer
is simply empty. Browse calls use a 3 s request timeout and wait at most 5 s
for the catalogue lock.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .ytm_refs import (
    CHANNEL_ID, LIKED, LIST_ID, MPRE_ID, OLAK_ID, PLAYLIST, SONG_RADIO, VIDEO_ID, WATCH_LIST,
    chart_list, link_for, norm, strip_vl,
)

log = logging.getLogger(__name__)

TIMEOUT_S = 3.0             # per HTTP request, for browsing
LOCK_WAIT_S = 5.0           # behind a slow radio top-up, give up rather than hang the page
MAX_ITEMS = 50

MIN = 60
HOUR = 3600
DAY = 24 * HOUR
TTL = {
    "search": 10 * MIN, "suggest": 10 * MIN, "moods": DAY, "mood": 6 * HOUR,
    "charts": 6 * HOUR, "countries": 7 * DAY, "artist": DAY, "album": DAY, "album_id": 7 * DAY,
    "home": 30 * MIN, "library": 5 * MIN, "related": 6 * HOUR, "lyrics": DAY,
}
SIZE = {"search": 200, "suggest": 200, "mood": 100, "charts": 80, "artist": 100, "album": 100,
        "related": 100, "lyrics": 100}
DISK = ("moods", "mood", "charts", "countries")

SEARCH_TYPES = {
    "all": (None,),
    "songs": ("songs",),
    "albums": ("albums",),
    "artists": ("artists",),
    # YouTube Music's own lists first, then everyone's.
    "playlists": ("featured_playlists", "playlists"),
    "community": ("community_playlists",),
}
LIBRARY_SECTIONS = ("liked", "playlists", "history", "artists", "mixes")
MOODS_BROWSE = "FEmusic_moods_and_genres_category"
ART_SIZE = "=w226-h226-l90-rj"
DEFAULT_COUNTRY = "ES"


class YtmBrowse:
    """The browser over a ``YtmCatalog`` (its ``_call``, lock and sign-in)."""

    def __init__(self, catalog: Any, cache_dir: str | Path | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.catalog = catalog
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.clock = clock
        self._lock = threading.Lock()
        self._caches: dict[str, collections.OrderedDict] = collections.defaultdict(collections.OrderedDict)
        self._refreshing: set[str] = set()

    # -- plumbing ----------------------------------------------------------

    @property
    def available(self) -> bool:
        try:
            return bool(self.catalog.available)
        except Exception:
            return False

    @property
    def signed_in(self) -> bool:
        try:
            return bool(self.catalog.signed_in)
        except Exception:
            return False

    def _call(self, what: str, fn: Callable[[Any], Any], **kw: Any) -> Any:
        """One locked catalogue call; None on any failure (never cached)."""
        kw.setdefault("timeout", TIMEOUT_S)
        kw.setdefault("wait", LOCK_WAIT_S)
        return self.catalog._call(what, fn, None, **kw)

    def _cached(self, ns: str, key: str, fetch: Callable[[], Any], default: Any = None) -> Any:
        """``fetch()`` through the ``ns`` cache. ``fetch`` returns None on failure."""
        now = self.clock()
        ttl = TTL[ns]
        with self._lock:
            hit = self._caches[ns].get(key)
            if hit is not None:
                self._caches[ns].move_to_end(key)
        if hit is None and ns in DISK:
            hit = self._disk_load(ns, key)
            if hit is not None:
                self._put(ns, key, hit[0], hit[1], save=False)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        if hit is not None and ns in DISK:
            # Old but on the card: show it now, fetch the new one behind.
            self._refresh_behind(ns, key, fetch)
            return hit[1]
        value = fetch()
        if value is None:
            return hit[1] if hit is not None else default
        self._put(ns, key, now, value)
        return value

    def _put(self, ns: str, key: str, at: float, value: Any, save: bool = True) -> None:
        if save and ns in DISK:
            self._disk_save(ns, key, at, value)     # first: what the page has seen is on the card
        with self._lock:
            cache = self._caches[ns]
            cache[key] = (at, value)
            cache.move_to_end(key)
            while len(cache) > SIZE.get(ns, 50):
                cache.popitem(last=False)

    def _refresh_behind(self, ns: str, key: str, fetch: Callable[[], Any]) -> None:
        name = f"{ns}:{key}"
        with self._lock:
            if name in self._refreshing:
                return
            self._refreshing.add(name)

        def run() -> None:
            try:
                value = fetch()
                if value is not None:
                    self._put(ns, key, self.clock(), value)
            except Exception:
                log.debug("background refresh of %s failed", name, exc_info=True)
            finally:
                with self._lock:
                    self._refreshing.discard(name)

        threading.Thread(target=run, name="ytm-browse-refresh", daemon=True).start()

    def forget(self, *namespaces: str) -> None:
        """Drop cached answers (sign-in changed, a song was liked)."""
        with self._lock:
            for ns in namespaces or list(self._caches):
                self._caches[ns].clear()

    def _disk_path(self, ns: str, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        safe = key if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", key) else hashlib.sha1(key.encode()).hexdigest()[:20]
        return self.cache_dir / f"{ns}-{safe or 'all'}.json"

    def _disk_load(self, ns: str, key: str) -> tuple[float, Any] | None:
        path = self._disk_path(ns, key)
        if path is None or not path.exists():
            return None
        try:
            doc = json.loads(path.read_text())
            if doc.get("key") != key:
                return None
            return float(doc["at"]), doc["value"]
        except (OSError, ValueError, KeyError, TypeError):
            log.info("ignoring unreadable %s", path)
            return None

    def _disk_save(self, ns: str, key: str, at: float, value: Any) -> None:
        path = self._disk_path(ns, key)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump({"key": key, "at": at, "value": value}, fh, ensure_ascii=False)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except (OSError, TypeError, ValueError):
            log.warning("could not write %s", path, exc_info=True)

    # -- search ----------------------------------------------------------

    def search(self, query: str, kind: str = "all") -> list[dict]:
        query = " ".join((query or "").split())[:200]
        if kind not in SEARCH_TYPES:
            raise ValueError(f"search type must be one of: {', '.join(SEARCH_TYPES)}")
        if not query:
            return []

        def fetch() -> list[dict] | None:
            got: list[dict] = []
            ok = False
            for flt in SEARCH_TYPES[kind]:
                raw = self._call("search", lambda c, f=flt: c.search(query, filter=f, limit=20))
                if raw is None:
                    continue
                ok = True
                got.extend(r for r in raw if isinstance(r, dict))
            if not ok:
                return None
            return _dedupe([it for it in (any_item(r, hint=_hint(kind)) for r in got) if it])[:MAX_ITEMS]

        return self._cached("search", f"{kind}:{query.casefold()}", fetch, [])

    def suggest(self, query: str) -> list[str]:
        query = " ".join((query or "").split())[:100]
        if not query:
            return []

        def fetch() -> list[str] | None:
            raw = self._call("suggestions", lambda c: c.get_search_suggestions(query))
            if raw is None:
                return None
            out = []
            for s in raw:
                text = s if isinstance(s, str) else (
                    "".join(r.get("text", "") for r in s.get("suggestion", []) if isinstance(r, dict))
                    if isinstance(s, dict) and isinstance(s.get("suggestion"), list)
                    else str(s.get("text") or "") if isinstance(s, dict) else "")
                if text and text not in out:
                    out.append(text)
            return out[:10]

        return self._cached("suggest", query.casefold(), fetch, [])

    # -- Moods & genres --------------------------------------------------

    def moods(self) -> list[dict]:
        """``[{"title", "items": [mood item]}]``: category names in Spanish."""
        def fetch() -> list | None:
            raw = self._call("mood categories", lambda c: c.get_mood_categories(), language="es")
            if not raw:
                raw = self._call("mood categories", lambda c: c.get_mood_categories())
            if not isinstance(raw, dict) or not raw:
                return None
            return [{"title": str(t), "cats": [{"title": str(c.get("title") or ""), "params": str(c["params"])}
                                                for c in cats or [] if isinstance(c, dict) and c.get("params")]}
                    for t, cats in raw.items()]

        sections = self._cached("moods", "all", fetch, [])
        return [{"title": s["title"], "items": [mood_item(c["title"], c["params"]) for c in s.get("cats", [])]}
                for s in sections if isinstance(s, dict)]

    def _mood_raw(self, params: str) -> list[dict]:
        def fetch() -> list | None:
            from ...web.catalog import walk_playlists  # noqa: PLC0415 - the shared walker

            # get_mood_playlists can't parse genre pages: walk the browse JSON.
            raw = self._call("mood playlists", lambda c: c._send_request(
                "browse", {"browseId": MOODS_BROWSE, "params": params}))
            if raw is None:
                return None
            seen, out = set(), []
            for found in walk_playlists(raw):
                if found["id"] not in seen and LIST_ID.match(found["id"]):
                    seen.add(found["id"])
                    out.append(found)
            return out

        return self._cached("mood", params, fetch, [])

    def mood(self, params: str) -> list[dict]:
        return [mood_playlist_item(f, params) for f in self._mood_raw(params)]

    # -- charts ------------------------------------------------------------

    def _charts_raw(self, country: str) -> dict:
        def fetch() -> dict | None:
            raw = self._call("charts", lambda c: c.get_charts(country=country))
            if not isinstance(raw, dict):
                return None
            options = ((raw.get("countries") or {}).get("options") if isinstance(raw.get("countries"), dict)
                       else None) or []
            codes = sorted({str(o) for o in options if isinstance(o, str) and re.fullmatch(r"[A-Z]{2}", o)})
            if codes:
                self._put("countries", "all", self.clock(), codes)
            return {"videos": _list(raw.get("videos")), "genres": _list(raw.get("genres")),
                    "artists": _list(raw.get("artists"))}

        return self._cached("charts", country, fetch, {})

    def charts(self, country: str = DEFAULT_COUNTRY) -> dict:
        """``{"country", "countries", "items": [chart…], "artists": [artist…]}``."""
        country = (country or DEFAULT_COUNTRY).upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("country must be two letters, like ES (ZZ = global)")
        raw = self._charts_raw(country)
        items = [it for it in (chart_item(p, country) for p in raw.get("videos", []) + raw.get("genres", [])) if it]
        artists = [it for it in (artist_item(a) for a in raw.get("artists", [])) if it]
        countries = self._cached("countries", "all", lambda: None, [])
        return {"country": country, "countries": countries or [country], "items": items, "artists": artists}

    # -- artists and albums ------------------------------------------------

    def _artist_raw(self, uc: str) -> dict:
        def fetch() -> dict | None:
            raw = self._call("artist", lambda c: c.get_artist(uc))
            return raw if isinstance(raw, dict) else None
        return self._cached("artist", uc, fetch, {})

    def artist(self, uc: str) -> dict:
        if not CHANNEL_ID.match(uc or ""):
            raise ValueError("not an artist id (UC…)")
        raw = self._artist_raw(uc)
        if not raw:
            return {}
        name = str(raw.get("name") or "Artist")
        songs = raw.get("songs") if isinstance(raw.get("songs"), dict) else {}
        top = strip_vl(str(songs.get("browseId") or ""))
        radio_id, shuffle_id = str(raw.get("radioId") or ""), str(raw.get("shuffleId") or "")
        listeners = str(raw.get("monthlyListeners") or raw.get("subscribers") or "")
        return {
            "id": uc, "title": name,
            "subtitle": f"{listeners} monthly listeners" if raw.get("monthlyListeners") else listeners,
            "thumb": art(_thumb(raw)),
            "description": str(raw.get("description") or "")[:2000],
            "preset": make_preset("artist", name, PLAYLIST.format(id=top) if LIST_ID.match(top) else "",
                                  id=uc, mode="top"),
            "radio": make_preset("artist", f"{name} radio",
                                 WATCH_LIST.format(id=radio_id) if LIST_ID.match(radio_id) else "",
                                 id=uc, mode="radio"),
            "shuffle": make_preset("artist", f"{name} shuffle",
                                   WATCH_LIST.format(id=shuffle_id) if LIST_ID.match(shuffle_id) else "",
                                   id=uc, mode="shuffle"),
            "songs": [it for it in (song_item(s, artists_default=[{"id": uc, "name": name}])
                                    for s in _results(raw.get("songs"))) if it],
            "albums": [it for it in (album_item(a, artists_default=[{"id": uc, "name": name}])
                                     for a in _results(raw.get("albums"))) if it],
            "singles": [it for it in (album_item(a, artists_default=[{"id": uc, "name": name}])
                                      for a in _results(raw.get("singles"))) if it],
            "related": [it for it in (artist_item(a) for a in _results(raw.get("related"))) if it],
        }

    def _album_raw(self, bid: str) -> dict:
        if OLAK_ID.match(bid):
            mpre = self._cached("album_id", bid, lambda: self._call(
                "album id", lambda c: c.get_album_browse_id(bid)) or None, "")
            if not MPRE_ID.match(str(mpre or "")):
                return {}
            bid = str(mpre)

        def fetch() -> dict | None:
            raw = self._call("album", lambda c: c.get_album(bid))
            return {**raw, "browseId": bid} if isinstance(raw, dict) else None
        return self._cached("album", bid, fetch, {})

    def album(self, bid: str) -> dict:
        if not (MPRE_ID.match(bid or "") or OLAK_ID.match(bid or "")):
            raise ValueError("not an album id (MPREb_… or OLAK5uy_…)")
        raw = self._album_raw(bid)
        if not raw:
            return {}
        head = album_item({**raw, "resultType": "album"})
        if head is None:
            return {}
        thumb = head["thumb"]
        own = {"name": raw.get("title"), "id": raw.get("browseId")}
        # get_album's tracks name their album as a plain string (or not at all).
        tracks = [it for it in (song_item({**t, "album": t["album"] if isinstance(t.get("album"), dict) else own},
                                          thumb_default=thumb, artists_default=head["artists"])
                                for t in _list(raw.get("tracks"))) if it]
        return {**{k: head[k] for k in ("id", "title", "subtitle", "thumb", "explicit", "preset", "radio",
                                        "artists")},
                "year": str(raw.get("year") or ""), "duration": str(raw.get("duration") or ""),
                "tracks": tracks}

    # -- home, library, related, lyrics ----------------------------------

    def home(self) -> list[dict]:
        signed = self.signed_in

        def fetch() -> list | None:
            raw = self._call("home", lambda c: c.get_home(limit=8))
            return raw if isinstance(raw, list) else None

        return _shelves(self._cached("home", "me" if signed else "anon", fetch, []))

    def library(self, section: str) -> list[dict]:
        if section not in LIBRARY_SECTIONS:
            raise ValueError(f"section must be one of: {', '.join(LIBRARY_SECTIONS)}")
        if not self.signed_in:
            return []

        def fetch() -> list | None:
            if section == "liked":
                raw = self._call("liked songs", lambda c: c.get_liked_songs(limit=200))
                return None if raw is None else [it for it in map(song_item, _list((raw or {}).get("tracks"))) if it]
            if section == "playlists":
                raw = self._call("library playlists", lambda c: c.get_library_playlists(limit=None))
                if raw is None:
                    return None
                items = [liked_item()]
                for p in _list(raw):
                    pid = strip_vl(str(p.get("playlistId") or ""))
                    if pid in (LIKED, "SE"):
                        continue            # Liked is first already; SE = "Episodes for later"
                    it = playlist_item(p)
                    if it:
                        items.append(it)
                return items
            if section == "history":
                raw = self._call("history", lambda c: c.get_history())
                return None if raw is None else [it for it in map(song_item, _list(raw)) if it]
            if section == "artists":
                raw = self._call("library artists", lambda c: c.get_library_artists(limit=100))
                return None if raw is None else [it for it in map(artist_item, _list(raw)) if it]
            raw = self._call("mixes", lambda c: c.get_home(limit=10))     # "mixes"
            if raw is None:
                return None
            out = []
            for shelf in _list(raw):
                mixy = "mix" in norm(shelf.get("title", ""))
                for entry in _list(shelf.get("contents")):
                    pid = str(entry.get("playlistId") or "")
                    if pid.startswith("RDTMAK") or (mixy and pid):
                        it = playlist_item(entry)
                        if it:
                            out.append(it)
            return _dedupe(out)

        return self._cached("library", section, fetch, [])

    def related(self, vid: str) -> list[dict]:
        if not VIDEO_ID.match(vid or ""):
            raise ValueError("not a video id")

        def fetch() -> list | None:
            def both(c: Any) -> Any:
                watch = c.get_watch_playlist(videoId=vid, limit=1) or {}
                bid = watch.get("related")
                return c.get_song_related(bid) if bid else []
            raw = self._call("related", both)
            return raw if isinstance(raw, list) else None

        return _shelves(self._cached("related", vid, fetch, []))

    def lyrics(self, vid: str) -> dict:
        if not VIDEO_ID.match(vid or ""):
            raise ValueError("not a video id")

        def fetch() -> dict | None:
            def both(c: Any) -> Any:
                watch = c.get_watch_playlist(videoId=vid, limit=1) or {}
                bid = watch.get("lyrics")
                return c.get_lyrics(bid) if bid else {}
            raw = self._call("lyrics", both)
            if not isinstance(raw, dict):
                return None
            text = raw.get("lyrics")
            return {"lyrics": text if isinstance(text, str) and text.strip() else None,
                    "source": str(raw.get("source") or "")}

        return self._cached("lyrics", vid, fetch, {"lyrics": None, "source": ""})

    # -- preset references (ytm.py, at press time) -----------------------

    def resolve(self, ref: dict) -> str | None:
        """The link a looked-up reference means right now, or None (unknown /
        YouTube Music unreachable - the caller falls back to the preset's uri)."""
        kind = ref.get("kind")
        try:
            if kind == "artist":
                raw = self._artist_raw(str(ref.get("id") or "")) if CHANNEL_ID.match(str(ref.get("id") or "")) else {}
                mode = ref.get("mode") or "top"
                if mode == "top":
                    songs = raw.get("songs") if isinstance(raw.get("songs"), dict) else {}
                    pid = strip_vl(str(songs.get("browseId") or ""))
                    return PLAYLIST.format(id=pid) if LIST_ID.match(pid) else None
                rid = str(raw.get("radioId" if mode == "radio" else "shuffleId") or "")
                return WATCH_LIST.format(id=rid) if LIST_ID.match(rid) else None
            if kind == "chart":
                want = ref.get("list") or "daily"
                for p in self._charts_raw(str(ref.get("country") or DEFAULT_COUNTRY)).get("videos", []):
                    pid = strip_vl(str(p.get("playlistId") or ""))
                    if chart_list(str(p.get("title") or "")) == want and LIST_ID.match(pid):
                        return PLAYLIST.format(id=pid)
                return None
            if kind == "mood":
                title = norm(ref.get("title", ""))
                for found in self._mood_raw(str(ref.get("params") or "")):
                    if norm(found["title"]) == title:
                        return PLAYLIST.format(id=found["id"])
                return None
            if kind == "album":
                raw = self._album_raw(str(ref.get("id") or ""))
                pid = str(raw.get("audioPlaylistId") or raw.get("playlistId") or "")
                return PLAYLIST.format(id=pid) if LIST_ID.match(pid) else None
        except Exception:
            log.warning("could not look up ytm preset %s", ref, exc_info=True)
            return None
        return link_for(ref) or None


# -- presets and items -------------------------------------------------------


def make_preset(kind: str, name: str, uri: str = "", **ref: str) -> dict:
    out: dict[str, Any] = {"source": "ytm", "kind": kind}
    out.update({k: v for k, v in ref.items() if v})
    out.update({"uri": uri, "shuffle": False, "name": (name or "YouTube Music")[:60]})
    return out


def item(type_: str, title: str, subtitle: str, thumb: str, id_: str, preset: dict | None,
         radio: dict | None = None, explicit: bool = False, artists: list | None = None,
         album: dict | None = None) -> dict:
    compat = preset if preset and preset.get("uri") else (radio if radio and radio.get("uri") else preset or {})
    return {
        "type": type_, "title": title, "subtitle": subtitle, "thumb": art(thumb), "id": id_,
        "preset": preset, "radio": radio, "explicit": bool(explicit),
        "artists": artists or [], "album": album,
        "uri": str((compat or {}).get("uri") or ""), "name": str((compat or {}).get("name") or title)[:60],
    }


def song_item(raw: dict, thumb_default: str = "", artists_default: list | None = None) -> dict | None:
    vid = str(raw.get("videoId") or "")
    if not VIDEO_ID.match(vid) or raw.get("isAvailable") is False:
        return None
    title = str(raw.get("title") or "Song")
    artists = _artist_refs(raw) or list(artists_default or [])
    alb = raw.get("album") if isinstance(raw.get("album"), dict) else None
    album = {"id": str(alb.get("id") or ""), "title": str(alb.get("name") or "")} if alb and alb.get("name") else None
    names = ", ".join(a["name"] for a in artists)
    radio = make_preset("radio", f"{title} radio", SONG_RADIO.format(vid=vid), seed=f"song:{vid}")
    return item("song", title, " · ".join(x for x in (names, album["title"] if album else "") if x),
                _thumb(raw) or thumb_default, vid, radio, dict(radio), bool(raw.get("isExplicit")), artists, album)


def album_item(raw: dict, artists_default: list | None = None) -> dict | None:
    bid = str(raw.get("browseId") or "")
    pid = str(raw.get("audioPlaylistId") or raw.get("playlistId") or "")
    if not OLAK_ID.match(pid):
        pid = ""
    if not MPRE_ID.match(bid):
        bid = ""
    if not (bid or pid):
        return None
    title = str(raw.get("title") or "Album")
    artists = _artist_refs(raw) or list(artists_default or [])
    names = ", ".join(a["name"] for a in artists)
    kind = str(raw.get("type") or "Album")
    preset = make_preset("album", title, PLAYLIST.format(id=pid) if pid else "", id=pid or bid)
    radio = (make_preset("radio", f"{title} radio", link_for({"kind": "radio", "seed": f"album:{pid}"}),
                         seed=f"album:{pid}") if pid else None)
    sub = " · ".join(x for x in (kind, names, str(raw.get("year") or "")) if x)
    return item("album", title, sub, _thumb(raw), bid or pid, preset, radio, bool(raw.get("isExplicit")), artists)


def artist_item(raw: dict) -> dict | None:
    refs = _artist_refs(raw)
    uc = str(raw.get("browseId") or (refs[0]["id"] if refs else "") or "")
    if not CHANNEL_ID.match(uc):
        return None
    name = str(raw.get("artist") or raw.get("title") or (refs[0]["name"] if refs else "") or "Artist")
    rid = str(raw.get("radioId") or "")
    preset = make_preset("artist", name, "", id=uc, mode="top")
    radio = make_preset("artist", f"{name} radio", WATCH_LIST.format(id=rid) if LIST_ID.match(rid) else "",
                        id=uc, mode="radio")
    subs = raw.get("subscribers") or raw.get("monthlyListeners")
    return item("artist", name, f"Artist · {subs}" if subs else "Artist", _thumb(raw), uc, preset, radio,
                artists=[{"id": uc, "name": name}])


def playlist_item(raw: dict) -> dict | None:
    pid = strip_vl(str(raw.get("playlistId") or raw.get("browseId") or ""))
    if not LIST_ID.match(pid) or pid.startswith(("MPRE", "UC")):
        return None
    if pid == LIKED:
        return liked_item()
    title = str(raw.get("title") or "Playlist")
    author = raw.get("author")
    if isinstance(author, list):
        author = ", ".join(str(a.get("name")) for a in author if isinstance(a, dict) and a.get("name"))
    count = raw.get("itemCount") or raw.get("count")
    sub = " · ".join(x for x in (str(author or ""), f"{count} songs" if count else "") if x)
    if not sub:
        sub = str(raw.get("description") or "Playlist")[:80]
    preset = make_preset("playlist", title, PLAYLIST.format(id=pid), id=pid)
    return item("playlist", title, sub, _thumb(raw), pid, preset)


def liked_item() -> dict:
    preset = make_preset("liked", "Liked songs", PLAYLIST.format(id=LIKED))
    return item("playlist", "Liked songs", "Your likes", "", LIKED, preset)


def chart_item(raw: dict, country: str) -> dict | None:
    pid = strip_vl(str(raw.get("playlistId") or raw.get("browseId") or ""))
    if not LIST_ID.match(pid):
        return None
    title = str(raw.get("title") or "Chart")
    which = chart_list(title)
    if which:
        preset = make_preset("chart", title, PLAYLIST.format(id=pid), country=country, list=which)
    else:
        preset = make_preset("playlist", title, PLAYLIST.format(id=pid), id=pid)
    return item("chart", title, "Chart · updated by YouTube" if which else "Chart", _thumb(raw), pid, preset)


def mood_item(title: str, params: str) -> dict:
    return item("mood", title, "", "", params, None)


def mood_playlist_item(found: dict, params: str) -> dict:
    title = str(found.get("title") or "Playlist")
    preset = make_preset("mood", title, PLAYLIST.format(id=found["id"]), id=found["id"], title=title, params=params)
    return item("playlist", title, "YouTube Music", str(found.get("thumb") or ""), found["id"], preset)


def any_item(raw: dict, hint: str | None = None) -> dict | None:
    """A search result or shelf entry of any kind -> an item (None = skip)."""
    rtype = str(raw.get("resultType") or hint or "").lower()
    if rtype in ("song", "video"):
        return song_item(raw)
    if rtype in ("album", "single", "ep"):
        return album_item(raw)
    if rtype == "artist":
        return artist_item(raw)
    if rtype == "playlist":
        return playlist_item(raw)
    if rtype:
        return None             # episodes, podcasts, profiles, uploads
    bid = str(raw.get("browseId") or "")
    if raw.get("videoId"):
        return song_item(raw)
    if bid.startswith("MPRE") or str(raw.get("audioPlaylistId") or "").startswith("OLAK"):
        return album_item(raw)
    if bid.startswith("UC"):
        return artist_item(raw)
    if raw.get("playlistId") or bid.startswith("VL"):
        return playlist_item(raw)
    return None


def strip_explicit(items: list[dict]) -> list[dict]:
    return [it for it in items if not (isinstance(it, dict) and it.get("explicit"))]


def art(url: str) -> str:
    """googleusercontent artwork at the page's tile size (226 px square)."""
    url = str(url or "")
    try:
        host = urlparse(url).netloc
    except ValueError:
        return ""
    if host.endswith("googleusercontent.com") and "=" in url.rsplit("/", 1)[-1]:
        return re.sub(r"=[^=/]*$", ART_SIZE, url)
    return url


# -- helpers -----------------------------------------------------------------


def _hint(kind: str) -> str | None:
    return {"songs": "song", "albums": "album", "artists": "artist", "playlists": "playlist",
            "community": "playlist"}.get(kind)


def _thumb(raw: dict) -> str:
    thumbs = raw.get("thumbnails") or raw.get("thumbnail") or []
    if isinstance(thumbs, dict):
        thumbs = thumbs.get("thumbnails") or []
    for t in reversed(thumbs if isinstance(thumbs, list) else []):
        if isinstance(t, dict) and str(t.get("url") or "").startswith("https://"):
            return str(t["url"])
    thumb = raw.get("thumb")
    return thumb if isinstance(thumb, str) and thumb.startswith("https://") else ""


def _artist_refs(raw: dict) -> list[dict]:
    out = []
    for a in raw.get("artists") or [] if isinstance(raw.get("artists"), list) else []:
        if isinstance(a, dict) and a.get("name"):
            out.append({"id": str(a.get("id") or ""), "name": str(a["name"])})
    return out


def _list(raw: Any) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("results") or []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _results(raw: Any) -> list[dict]:
    """get_artist's sections: {"browseId", "results": [...]} (or a bare list)."""
    if isinstance(raw, dict):
        return _list(raw.get("results"))
    return _list(raw)


def _shelves(raw: list) -> list[dict]:
    out = []
    for shelf in raw or []:
        if not isinstance(shelf, dict):
            continue
        items = _dedupe([it for it in (any_item(e) for e in _list(shelf.get("contents"))) if it])
        if items:
            out.append({"title": str(shelf.get("title") or ""), "items": items})
    return out


def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = (it["type"], it["id"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out
