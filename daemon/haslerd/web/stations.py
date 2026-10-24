"""Radio stations for the page: the checked collection, moOde's list, the internet.

The owner lives in Spain, and moOde's built-in list (a couple of hundred
stations, almost none Spanish) is no help there. So a search draws on:

1. **The collection** shipped in the repo (config/stations/collection.json,
   built by scripts/stations/build_collection.py): about 600 stations from
   some 120 countries - the Spanish national and regional broadcasters first
   (RNE, SER, COPE, RAC1, Catalunya Ràdio, Canal Sur, EITB, Radio Galega...),
   then public, community and genre stations from every region. Each stream
   was played by MPD on the radio itself (a Pi in Spain) when it was added.
   Searched first (Spanish stations ahead of the rest), and what an empty
   search shows (the Spanish ones, in the file's order). The page's station
   browser downloads the whole file (``GET /api/stations/collection``) and
   filters it itself; ``collection_doc`` serves it.
2. **moOde's own list** (its sqlite ``cfg_radio``), read-only as before.
3. **Radio Browser** (radio-browser.info), the free community directory of
   ~50,000 health-checked streams, asked over its JSON API. Spanish stations
   first (one query with ``countrycode=ES``, one without), then the rest.
   Unchecked by us, so the merged search only asks it when the collection
   has fewer than ``INTERNET_BELOW`` matches; ``search_internet`` (``GET
   /api/stations?scope=internet``, the page's "Search the internet too")
   asks it alone.

Matching is accent- and case-insensitive on name, genre, region/country,
language and keywords, so "radio clasica" finds "Radio Clásica" and "rac"
finds "RAC1". Results are merged in that order, deduped by stream URL, and
capped at ``LIMIT``.

Radio Browser is the only part that leaves the box, and it is optional: its
queries run with short timeouts in a worker thread under an overall budget, a
dead mirror is skipped for the next one, and an unreachable directory is a log
line - the other two lists still answer at once. Answers are cached per query
for ``CACHE_TTL`` so a page that searches as you type does not hammer it.

Every entry is ``{"name", "url", "genre", "country", "source", "checked"}`` -
the first four are what the page has always read (``country`` is the display
place, "Spain · Catalonia"); ``source`` is "curated", "moode" or
"radio-browser"; ``checked`` is true for the collection's own stations.
Collection hits also carry ``id``, ``cc`` (ISO country code) and ``genres``;
directory hits may carry ``codec``, ``bitrate`` and ``favicon``.
"""

from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
import logging
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from ..boxconfig import shipped_data

log = logging.getLogger(__name__)

MOODE_DB = "/var/local/www/db/moode-sqlite3.db"
LIMIT = 50
# Shipped with the release (config/stations in the release root, which is the
# checkout root when running from the repo - boxconfig.find_release).
CURATED_FILE = shipped_data("stations/collection.json")
# The merged search leaves the internet out once the collection has this many matches.
INTERNET_BELOW = 5

# Radio Browser publishes a DNS round-robin of mirrors; a fixed short list is
# simpler than a DNS SRV lookup in the stdlib and has been stable for years.
MIRRORS = (
    "https://de1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
)
USER_AGENT = "haslerd/1 (Hasler radio)"  # the directory asks clients to name themselves
TIMEOUT = 3.0        # per HTTP request
BUDGET = 4.0         # the whole directory lookup, as seen by a search
CACHE_TTL = 600.0    # seconds an answer is reused for the same query
DIRECTORY_LIMIT = 40  # stations asked for per query

# fetch(url, timeout) -> parsed JSON. Injected by tests; the real one is _fetch.
Fetch = Callable[[str, float], object]


def fold(text: str) -> str:
    """Lower-case, accent-free: "Ràdio Clásica" -> "radio clasica"."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _matches(q: str, *fields: str) -> bool:
    """``q`` (already folded) in any field; spaces optional, so "los 40" finds "LOS40"."""
    if not q:
        return True
    squeezed = q.replace(" ", "")
    for field in fields:
        f = fold(field)
        if q in f or squeezed in f.replace(" ", ""):
            return True
    return False


# -- 1. the curated list --------------------------------------------------------

# path -> (mtime, [(station, extra search terms)])
_curated_cache: dict[Path, tuple[float, list[tuple[dict, str]]]] = {}


def _curated(path: Path) -> list[tuple[dict, str]]:
    """The collection's stations, re-read when the file changes; [] if it is missing.

    Each file entry: name, url, genre, region and optional ``keywords`` - extra
    search terms that are not shown (the group, "Catalunya" for "Catalonia",
    "Radio Nacional de España" for "RNE") - plus, in the collection, ``id``,
    ``country`` (ISO code), ``genres`` and ``language``. Kept in the file's
    order (Spanish national stations, then by region, then the world).
    """
    try:
        mtime = path.stat().st_mtime
        hit = _curated_cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("no curated station list at %s", path, exc_info=True)
        return []
    entries = doc.get("stations", []) if isinstance(doc, dict) else doc
    countries = doc.get("countries", {}) if isinstance(doc, dict) else {}
    languages = doc.get("languages", {}) if isinstance(doc, dict) else {}
    out = []
    for e in entries:
        if not (isinstance(e, dict) and e.get("name") and str(e.get("url", "")).startswith(("http://", "https://"))):
            continue
        station = {
            "name": e["name"],
            "url": e["url"],
            "genre": e.get("genre", ""),
            "country": e.get("region") or countries.get(e.get("country", ""), "Spain"),
            "source": "curated",
            "checked": True,
        }
        for field in ("id", "genres", "codec", "bitrate"):
            if e.get(field):
                station[field] = e[field]
        if e.get("country"):
            station["cc"] = e["country"]
        terms = " ".join([
            e.get("keywords", ""), " ".join(e.get("genres") or []), countries.get(e.get("country", ""), ""),
            languages.get(e.get("language", ""), ""),
        ])
        out.append((station, terms))
    _curated_cache[path] = (mtime, out)
    return out


def _spanish(station: dict) -> bool:
    # es.json-style entries (no ISO code) were all Spanish.
    return station.get("cc", "ES") == "ES"


def load_curated(path: Path | None = None) -> list[dict]:
    """What an empty search shows: the collection's Spanish stations, in order."""
    path = path or CURATED_FILE  # looked up per call (tests switch it)
    return [dict(station) for station, _ in _curated(path) if _spanish(station)]


def search_curated(query: str, path: Path | None = None) -> list[dict]:
    """Collection matches, Spanish stations first (each part in the file's order)."""
    path = path or CURATED_FILE  # looked up per call (tests switch it)
    q = fold(query)
    found = [
        dict(s) for s, keywords in _curated(path)
        if _matches(q, s["name"], s["genre"], s["country"], keywords)
    ]
    return [s for s in found if _spanish(s)] + [s for s in found if not _spanish(s)]


def by_id(station_id: str, path: Path | None = None) -> dict | None:
    """The collection station with this id, or None."""
    path = path or CURATED_FILE  # looked up per call (tests switch it)
    for station, _ in _curated(path):
        if station.get("id") == station_id:
            return dict(station)
    return None


def id_for_url(url: str, path: Path | None = None) -> str | None:
    """The collection id of a stream URL (scheme/case/trailing slash ignored), or None."""
    path = path or CURATED_FILE  # looked up per call (tests switch it)
    key = _key(url)
    for station, _ in _curated(path):
        if station.get("id") and _key(station["url"]) == key:
            return station["id"]
    return None


# path -> (mtime, raw bytes, gzipped bytes, etag)
_doc_cache: dict[Path, tuple[float, bytes, bytes, str]] = {}


def collection_doc(path: Path | None = None) -> tuple[bytes, bytes, str]:
    """(JSON bytes, the same gzipped, ETag) of the whole collection, for the
    page's browser. Compressed once per file change (about 250 KB -> 60 KB).
    Raises OSError if the file is missing."""
    path = path or CURATED_FILE  # looked up per call (tests switch it)
    mtime = path.stat().st_mtime
    hit = _doc_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1], hit[2], hit[3]
    raw = path.read_bytes()
    packed = gzip.compress(raw, compresslevel=9, mtime=0)
    etag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
    _doc_cache[path] = (mtime, raw, packed, etag)
    return raw, packed, etag


# -- 2. moOde's list ------------------------------------------------------------

def search_moode(query: str, db: str = MOODE_DB, limit: int = LIMIT) -> list[dict]:
    """moOde stations whose name, genre or country contains ``query`` (all, if empty).

    Opened read-only per call: moOde owns that database and rewrites it, and a
    lookup a few times a minute costs nothing. The rows are filtered in Python
    rather than with LIKE so that matching folds accents the same way as the
    other lists (sqlite's LIKE only folds ASCII case).
    """
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        log.debug("no moOde station list at %s", db, exc_info=True)
        return []
    try:
        rows = con.execute(
            "SELECT name, station, genre, country FROM cfg_radio"
            " WHERE station LIKE 'http%' ORDER BY name COLLATE NOCASE"
        ).fetchall()
    except sqlite3.Error:
        log.debug("station query failed", exc_info=True)
        return []
    finally:
        con.close()
    q = fold(query)
    return [
        {"name": n, "url": u, "genre": g or "", "country": c or "", "source": "moode", "checked": False}
        for n, u, g, c in rows
        if _matches(q, n or "", g or "", c or "")
    ][:limit]


# -- 3. Radio Browser -----------------------------------------------------------

def _fetch(url: str, timeout: float) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class RadioBrowser:
    """A small client for radio-browser.info: mirror fallback, Spanish first, cache.

    ``search`` never raises: any failure is logged and answers []. Not cached
    on failure, so the next keystroke tries again (against the next mirror).
    """

    def __init__(
        self,
        fetch: Fetch = _fetch,
        mirrors: tuple[str, ...] = MIRRORS,
        timeout: float = TIMEOUT,
        ttl: float = CACHE_TTL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fetch = fetch
        self.mirrors = list(mirrors)
        self.timeout = timeout
        self.ttl = ttl
        self.clock = clock
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float, list[dict]]] = {}
        self.server: str | None = None  # the first mirror that answered

    def search(self, query: str) -> list[dict]:
        q = fold(query)
        if len(q) < 2:  # one letter matches half the world; not worth a request
            return []
        with self.lock:
            hit = self.cache.get(q)
            if hit and self.clock() - hit[0] < self.ttl:
                return hit[1]
        # The two queries side by side: one round trip of latency, not two.
        with concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="radio-browser-q") as pool:
            asks = [pool.submit(self._query, query, countrycode="ES"), pool.submit(self._query, query)]
        answers, error = [], None
        for ask in asks:
            try:
                answers.append(ask.result())
            except (urllib.error.URLError, OSError, ValueError) as exc:
                answers.append([])
                error = exc
        if error is not None and not any(answers):
            log.info("radio-browser unreachable: %s", error)
            return []
        spanish, everyone = answers
        out, seen = [], set()
        for raw in spanish + everyone:  # Spanish first, each already by popularity
            station = self._station(raw)
            if station and _key(station["url"]) not in seen:
                seen.add(_key(station["url"]))
                out.append(station)
        if error is None:  # a half answer is used, but not kept
            with self.lock:
                now = self.clock()
                # Drop stale answers as new ones arrive, so typing for weeks stays small.
                self.cache = {k: v for k, v in self.cache.items() if now - v[0] < self.ttl}
                self.cache[q] = (now, out)
        return out

    def _query(self, name: str, **extra: str) -> list:
        params = {
            "name": name.strip(),
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
            "limit": str(DIRECTORY_LIMIT),
            **extra,
        }
        path = "/json/stations/search?" + urllib.parse.urlencode(params)
        # The remembered mirror first, then the others in order.
        order = ([self.server] if self.server else []) + [m for m in self.mirrors if m != self.server]
        error: Exception | None = None
        for base in order:
            try:
                doc = self.fetch(base + path, self.timeout)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.debug("radio-browser mirror %s failed: %s", base, exc)
                error = exc
                continue
            self.server = base
            return doc if isinstance(doc, list) else []
        raise error or OSError("no radio-browser mirrors")

    @staticmethod
    def _station(raw: object) -> dict | None:
        if not isinstance(raw, dict):
            return None
        url = (raw.get("url_resolved") or raw.get("url") or "").strip()
        name = (raw.get("name") or "").strip()
        if not name or not url.startswith(("http://", "https://")):
            return None
        tags = [t for t in (raw.get("tags") or "").split(",") if t][:3]
        station = {
            "name": name,
            "url": url,
            "genre": ", ".join(tags),
            "country": raw.get("country") or "",
            "source": "radio-browser",
            "checked": False,
        }
        if raw.get("codec") and raw["codec"] != "UNKNOWN":
            station["codec"] = raw["codec"]
        if raw.get("bitrate"):
            station["bitrate"] = raw["bitrate"]
        if raw.get("favicon"):
            station["favicon"] = raw["favicon"]
        return station


DIRECTORY = RadioBrowser()
# A lookup that outlives its budget keeps running here (and fills the cache for
# the next keystroke) while the search answers without it.
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="radio-browser")


def _key(url: str) -> str:
    """Dedupe key: the same stream over http and https, or with a trailing slash, is one."""
    return url.strip().split("://", 1)[-1].rstrip("/").lower()


# -- the search -----------------------------------------------------------------

def _dedupe(found: list[dict], limit: int) -> list[dict]:
    out, seen = [], set()
    for station in found:
        if _key(station["url"]) not in seen:
            seen.add(_key(station["url"]))
            out.append(station)
            if len(out) >= limit:
                break
    return out


def search(
    query: str,
    db: str = MOODE_DB,
    limit: int = LIMIT,
    curated: Path | None = None,
    directory: RadioBrowser | None | str = "default",
    budget: float = BUDGET,
) -> list[dict]:
    """Collection matches, then moOde's, then Radio Browser's; deduped by URL.

    An empty query is the collection's Spanish stations alone: moOde's whole
    list (and the world's) is no sensible default. Radio Browser is asked only
    while the collection has fewer than ``INTERNET_BELOW`` matches (the page
    offers "Search the internet too" otherwise). ``directory=None`` searches
    offline; the default is the module's shared ``DIRECTORY`` (looked up per
    call, so a test can switch it off).
    """
    curated = curated or CURATED_FILE  # looked up per call (tests switch it)
    if directory == "default":
        directory = DIRECTORY
    if not query.strip():
        return load_curated(curated)[:limit]
    checked = search_curated(query, curated)
    pending = _pool.submit(directory.search, query) if directory and len(checked) < INTERNET_BELOW else None
    found = checked + search_moode(query, db, limit)
    if pending:
        try:
            found += pending.result(timeout=budget)
        except concurrent.futures.TimeoutError:
            log.info("radio-browser too slow for %r; answering without it", query)
        except Exception:  # noqa: BLE001 - the directory is optional, never fatal
            log.warning("radio-browser search failed", exc_info=True)
    return _dedupe(found, limit)


def search_internet(
    query: str,
    limit: int = LIMIT,
    curated: Path | None = None,
    directory: RadioBrowser | None | str = "default",
    budget: float = BUDGET + 2,
) -> list[dict]:
    """Radio Browser alone (``scope=internet``): every hit ``checked: False``,
    minus streams the collection already has (those the page shows as checked)."""
    curated = curated or CURATED_FILE  # looked up per call (tests switch it)
    if directory == "default":
        directory = DIRECTORY
    if not directory or not query.strip():
        return []
    try:
        found = _pool.submit(directory.search, query).result(timeout=budget)
    except concurrent.futures.TimeoutError:
        log.info("radio-browser too slow for %r", query)
        return []
    except Exception:  # noqa: BLE001
        log.warning("radio-browser search failed", exc_info=True)
        return []
    ours = {_key(s["url"]) for s, _ in _curated(curated)}
    return _dedupe([dict(s, checked=False) for s in found if _key(s["url"]) not in ours], limit)
