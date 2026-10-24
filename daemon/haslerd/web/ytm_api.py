"""``/api/ytm/*``: the YouTube Music browser's JSON API (the key page's tabs).

Every answer carries ``"available": bool`` (false = no catalogue on this radio:
no ytm source or no ytmusicapi - the page hides YouTube Music) and lists
items of the one shape documented in player/sources/ytm_browse.py, each with a
``preset`` ready for ``POST /api/play`` or a key.

    GET  /api/ytm/search?q=&type=all|songs|albums|artists|playlists|community
         -> {available, q, type, items, results (= items, for older pages)}
    GET  /api/ytm/suggest?q=          -> {available, q, suggestions: [str]}
    GET  /api/ytm/moods               -> {available, sections: [{title, items: [mood]}]}
    GET  /api/ytm/mood?params=        -> {available, params, items: [playlist]}
    GET  /api/ytm/charts?country=ES   -> {available, country, countries, items: [chart], artists}
    GET  /api/ytm/artist/<UC…>        -> {available, found, id, title, subtitle, thumb, description,
                                          preset (top songs), radio, shuffle, songs, albums, singles, related}
    GET  /api/ytm/album/<MPREb_…|OLAK5uy_…> -> {available, found, id, title, subtitle, thumb, explicit,
                                          preset, radio, artists, year, duration, tracks}
    GET  /api/ytm/home                -> {available, signed_in, sections: [{title, items}]}
    GET  /api/ytm/curated             -> {available, sections: [{title, items}]}: our checked picks
                                         (config/catalog/ytm-curated.json), one shelf per theme
    GET  /api/ytm/library?section=liked|playlists|history|artists|mixes
         -> {available, signed_in, section, items, preset (liked: the whole list)}
         (no section: the playlists, Liked first - what older pages ask for)
    GET  /api/ytm/related/<vid>       -> {available, vid, sections: [{title, items}]}
    GET  /api/ytm/lyrics/<vid>        -> {available, vid, lyrics: str|null, source}
    POST /api/ytm/radio {seed: "song:<vid>"|"album:<OLAK…>"|"artist:<UC…>", name?}
         -> plays it here; {ok, preset}
    POST /api/ytm/like {vid, rating: LIKE|INDIFFERENT}
         -> {ok, vid, rating}; 403 not signed in, 409 not the song playing now
    GET/POST/DELETE /api/ytm/account  (sign-in; WebApp.ytm_account/sign_in/sign_out)

Settings ``ytm.hide_explicit`` (default false: explicit is a badge only) drops
explicit songs and albums from every list here. Writes go through the
server's usual CSRF checks; none of this takes the layout lock.
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..boxconfig import shipped_data
from ..player.sources import ytm_browse as yb
from ..player.sources import ytm_refs
from . import keymap

log = logging.getLogger(__name__)

PREFIX = "/api/ytm/"
# A Like may name the song that was playing this long ago: the page's
# now-playing card can be a poll behind a track change.
LIKE_GRACE_S = 90
LISTS = ("items", "results", "songs", "albums", "singles", "tracks")
CURATED_FILE = shipped_data("catalog/ytm-curated.json")

# The curated picks' shelves. An entry goes on the first shelf (in this order)
# whose test it passes, so nothing shows twice; what fits none is "More picks".
# The page shows them in CURATED_ORDER.
CURATED_SHELVES: tuple[tuple[str, Callable[[dict], bool]], ...] = (
    ("Spain", lambda e: e["region"] == "Spain"),
    ("Classic albums", lambda e: e["kind"] == "album"),
    ("Jazz & blues", lambda e: bool({"jazz", "blues"} & e["genres"])),
    ("Classical & soundtracks", lambda e: bool({"classical", "soundtrack"} & e["genres"])),
    ("Latin", lambda e: "latin" in e["genres"]),
    ("World", lambda e: bool({"world", "reggae"} & e["genres"])),
    ("Electronic & ambient", lambda e: bool({"electronic", "dance", "ambient", "lounge"} & e["genres"])),
    ("Rock & indie", lambda e: bool({"rock", "indie", "metal"} & e["genres"]) and not e["decade"]),
    ("Moods & activities", lambda e: bool(e["moods"]) and not e["decade"]),
    ("Decades", lambda e: bool(e["decade"])),
    ("Soul, funk & gospel", lambda e: bool({"soul", "funk", "gospel"} & e["genres"])),
    ("Pop & hip-hop", lambda e: bool({"pop", "hits", "hip-hop"} & e["genres"])),
)
CURATED_ORDER = ("Spain", "Classic albums", "Jazz & blues", "Classical & soundtracks", "World", "Latin",
                 "Moods & activities", "Decades", "Rock & indie", "Electronic & ambient",
                 "Soul, funk & gospel", "Pop & hip-hop", "More picks")
CURATED_MIN = 4


def curated_item(raw: dict) -> dict | None:
    """One curated entry as a browser item: a playlist, or an album (OLAK…)."""
    pid = ytm_refs.strip_vl(str(raw.get("id") or ""))
    title = str(raw.get("title") or "").strip()
    if not title or not ytm_refs.LIST_ID.match(pid):
        return None
    kind = "album" if raw.get("kind") == "album" and ytm_refs.OLAK_ID.match(pid) else "playlist"
    preset = yb.make_preset(kind, title, ytm_refs.PLAYLIST.format(id=pid), id=pid)
    why = str(raw.get("why") or "").strip()
    owner = str(raw.get("owner") or "")
    out = yb.item(kind, title, why or owner, str(raw.get("thumbnail") or ""), pid, preset)
    out.update({"why": why, "owner": owner, "tracks": raw.get("tracks") if isinstance(raw.get("tracks"), int) else None})
    return out


def curated_sections(doc: Any) -> list[dict]:
    """ytm-curated.json -> shelves [{title, items}]; empty shelves are left out."""
    entries = doc.get("entries") if isinstance(doc, dict) else None
    shelves: dict[str, list[dict]] = {title: [] for title, _ in CURATED_SHELVES}
    shelves["More picks"] = []
    seen: set[str] = set()
    for raw in entries if isinstance(entries, list) else []:
        if not isinstance(raw, dict):
            continue
        it = curated_item(raw)
        if it is None or it["id"] in seen:
            continue
        seen.add(it["id"])
        facts = {"kind": it["type"], "region": raw.get("region"), "decade": raw.get("decade"),
                 "genres": {str(g).lower() for g in raw.get("genres") or []},
                 "moods": {str(m).lower() for m in raw.get("moods") or []}}
        title = next((t for t, test in CURATED_SHELVES if test(facts)), "More picks")
        shelves[title].append(it)
    # A shelf of one or two looks like a mistake: those join "More picks".
    for t in CURATED_ORDER[:-1]:
        if 0 < len(shelves[t]) < CURATED_MIN:
            shelves["More picks"].extend(shelves.pop(t))
    return [{"title": t, "items": shelves[t]} for t in CURATED_ORDER if shelves.get(t)]


class Forbidden(Exception):
    """A request the radio understood and refuses (403)."""


class YtmApi:
    def __init__(self, app: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.clock = clock
        self._recent: collections.deque[tuple[str, float]] = collections.deque(maxlen=8)
        self._lock = threading.Lock()
        self.curated_file: str | Path = CURATED_FILE
        self._curated: list[dict] | None = None

    # -- plumbing --------------------------------------------------------

    @property
    def catalog(self) -> Any:
        return self.app.catalog

    @property
    def browse(self) -> Any:
        return getattr(self.catalog, "browse", None)

    def available(self) -> bool:
        return self.app._ytm_available()

    def signed_in(self) -> bool:
        try:
            return bool(self.catalog.signed_in)
        except Exception:
            return False

    def note_playing(self, vid: str) -> None:
        """The now-playing track the page was shown (WebApp._track)."""
        if not ytm_refs.VIDEO_ID.match(vid or ""):
            return
        with self._lock:
            if not self._recent or self._recent[-1][0] != vid:
                self._recent.append((vid, self.clock()))
            else:
                self._recent[-1] = (vid, self.clock())

    def _clean(self, doc: dict) -> dict:
        """Drop explicit items everywhere when the owner asked for that."""
        if not self.app.ytm_settings().get("hide_explicit"):
            return doc
        out = dict(doc)
        for key in LISTS:
            if isinstance(out.get(key), list):
                out[key] = yb.strip_explicit(out[key])
        if isinstance(out.get("sections"), list):
            out["sections"] = [{**s, "items": yb.strip_explicit(s.get("items") or [])}
                               for s in out["sections"] if isinstance(s, dict)]
        return out

    # -- GET ----------------------------------------------------------------

    def get(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict]:
        """``GET /api/ytm/…`` (query from parse_qs). Raises ValueError -> 400."""
        def q(name: str, default: str = "") -> str:
            return (query.get(name) or [default])[0]

        rest = path[len(PREFIX):]
        if rest == "account":
            return 200, self.app.ytm_account()
        if rest == "search":
            return 200, self.search(q("q"), q("type", "all"))
        if rest == "library":
            return 200, self.library(q("section"))
        browse = self.browse if self.available() else None
        if rest == "suggest":
            return 200, {"available": browse is not None, "q": q("q"),
                         "suggestions": browse.suggest(q("q")) if browse else []}
        if rest == "moods":
            return 200, {"available": browse is not None, "sections": browse.moods() if browse else []}
        if rest == "mood":
            params = q("params")
            if not ytm_refs.PARAMS.match(params):
                raise ValueError("params: not a Moods & genres page")
            return 200, self._clean({"available": browse is not None, "params": params,
                                     "items": browse.mood(params) if browse else []})
        if rest == "charts":
            doc = browse.charts(q("country", yb.DEFAULT_COUNTRY)) if browse else \
                {"country": q("country", yb.DEFAULT_COUNTRY).upper(), "countries": [], "items": [], "artists": []}
            return 200, {"available": browse is not None, **doc}
        if rest == "curated":
            return 200, {"available": self.available(), "sections": self.curated()}
        if rest == "home":
            return 200, self._clean({"available": browse is not None, "signed_in": self.signed_in(),
                                     "sections": browse.home() if browse else []})
        kind, _, ident = rest.partition("/")
        if kind == "artist" and ident:
            if not ytm_refs.CHANNEL_ID.match(ident):
                raise ValueError("not an artist id (UC…)")
            doc = browse.artist(ident) if browse else {}
            return 200, self._clean({"available": browse is not None, "found": bool(doc), **doc})
        if kind == "album" and ident:
            if not (ytm_refs.MPRE_ID.match(ident) or ytm_refs.OLAK_ID.match(ident)):
                raise ValueError("not an album id (MPREb_… or OLAK5uy_…)")
            doc = browse.album(ident) if browse else {}
            return 200, self._clean({"available": browse is not None, "found": bool(doc), **doc})
        if kind == "related" and ident:
            if not ytm_refs.VIDEO_ID.match(ident):
                raise ValueError("not a video id")
            return 200, self._clean({"available": browse is not None, "vid": ident,
                                     "sections": browse.related(ident) if browse else []})
        if kind == "lyrics" and ident:
            if not ytm_refs.VIDEO_ID.match(ident):
                raise ValueError("not a video id")
            doc = browse.lyrics(ident) if browse else {"lyrics": None, "source": ""}
            return 200, {"available": browse is not None, "vid": ident, **doc}
        return 404, {"ok": False, "errors": [], "error": "not found"}

    def curated(self) -> list[dict]:
        """Our picks, read once (the file ships with the release)."""
        with self._lock:
            if self._curated is None:
                try:
                    self._curated = curated_sections(json.loads(Path(self.curated_file).read_text("utf-8")))
                except (OSError, ValueError):
                    log.warning("no curated YouTube Music picks at %s", self.curated_file, exc_info=True)
                    self._curated = []
            return self._curated

    def search(self, query: str, kind: str) -> dict:
        kind = kind or "all"
        if not self.available():
            return {"available": False, "q": query, "type": kind, "items": [], "results": []}
        browse = self.browse
        if browse is None:      # a catalogue without the browser: its own search
            items = self.catalog.search(query, kind)
        else:
            items = browse.search(query, kind)
        doc = self._clean({"available": True, "q": query, "type": kind, "items": items})
        doc["results"] = doc["items"]
        return doc

    def library(self, section: str) -> dict:
        if not self.available():
            return {"available": False, "signed_in": False, "section": section or "playlists", "items": []}
        signed = self.signed_in()
        browse = self.browse
        if browse is None:
            items = self.catalog.library() if not section or section == "playlists" else []
            return {"available": True, "signed_in": signed, "section": section or "playlists", "items": items}
        section = section or "playlists"
        doc: dict[str, Any] = {"available": True, "signed_in": signed, "section": section,
                               "items": browse.library(section)}
        if section == "liked" and signed:
            doc["preset"] = yb.liked_item()["preset"]
        return self._clean(doc)

    # -- POST -------------------------------------------------------------

    def post(self, path: str, body: Any) -> tuple[int, dict] | None:
        rest = path[len(PREFIX):]
        body = body if isinstance(body, dict) else {}
        if rest == "radio":
            return 200, {"ok": True, "preset": self.radio(body)}
        if rest == "like":
            try:
                return 200, {"ok": True, **self.like(body)}
            except Forbidden as exc:
                return 403, {"ok": False, "errors": [], "error": str(exc), "signed_in": self.signed_in()}
            except LookupError as exc:
                return 409, {"ok": False, "errors": [], "error": str(exc)}
            except RuntimeError as exc:
                return 502, {"ok": False, "errors": [], "error": str(exc)}
        return None

    def radio(self, body: dict) -> dict:
        """Start an endless radio from a song, an album or an artist; its preset."""
        seed = str(body.get("seed") or "").strip()
        name = str(body.get("name") or "").strip()
        what, _, sid = seed.partition(":")
        if what == "artist":
            raw = {"source": "ytm", "kind": "artist", "id": sid, "mode": "radio", "name": name or "Artist radio"}
        else:
            raw = {"source": "ytm", "kind": "radio", "seed": seed, "name": name or "YouTube Music radio"}
        errors: list[dict] = []
        spec = keymap.preset_from_page({**raw, "n": 0}, "radio", errors)
        if spec is None:
            raise keymap.Invalid(errors)
        preset = {**raw, "uri": spec["ytm"], "shuffle": False}
        self.app.play(preset)
        return preset

    def like(self, body: dict) -> dict:
        vid = str(body.get("vid") or "")
        rating = str(body.get("rating") or "LIKE").upper()
        if not ytm_refs.VIDEO_ID.match(vid):
            raise ValueError("vid: not a video id")
        if rating not in ("LIKE", "INDIFFERENT"):
            raise ValueError("rating must be LIKE or INDIFFERENT")
        if not self.available():
            raise ValueError("YouTube Music isn't available on this radio")
        if not self.signed_in():
            raise Forbidden("sign in to YouTube Music (Settings) to like songs")
        if not self._playing_recently(vid):
            raise LookupError("only the song playing now can be liked")
        try:
            done = self.catalog.rate(vid, rating)
        except PermissionError as exc:
            raise Forbidden(str(exc)) from None
        if not done:
            raise RuntimeError("YouTube Music didn't take that just now - try again")
        return {"vid": vid, "rating": rating}

    def _playing_recently(self, vid: str) -> bool:
        track = self.app._track()          # also notes it (note_playing)
        if track and track.get("vid") == vid:
            return True
        now = self.clock()
        with self._lock:
            return any(v == vid and now - t <= LIKE_GRACE_S for v, t in self._recent)
