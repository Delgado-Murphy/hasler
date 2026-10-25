"""The station browser's own routes: try a station, "My stations", reports.

For ``WebApp.extra_routes`` like web/sources_api.py. The collection itself
(``GET /api/stations/collection``, gzipped with an ETag) and the searches
(``GET /api/stations``) are served by server.py from web/stations.py.

``POST /api/stations/try`` ``{"id"}`` or ``{"url", "name"?}``
    Play a station now without putting it on a key (like the Test button,
    ``/api/play``). A collection id plays its stream. A raw link is probed
    first, the way MPD will fetch it (its user agent, the first 64 KB): a
    link that answers with a web page, an error or nothing is refused with
    ``{"ok": false, "error"}`` and nothing changes. Answers ``{"ok", "id",
    "url", "name", "codec", "bitrate"}``.

``GET /api/stations/mine`` / ``PUT /api/stations/mine``
    The owner's shortlist, separate from the keys: ``{"favourites": [id or
    url], "custom": [{"id", "name", "url"}], "order": [id or url], "recent":
    [{"id" or "url", "t"}]}``, kept in ``<state>/stations.json`` (written
    atomically). PUT replaces the parts it carries; ``recent`` is also
    appended to by the radio itself whenever a radio stream starts (a key, the
    Test button, a try): newest first, one entry per station, at most 50.

``POST /api/stations/report`` ``{"id" or "url", "reason", "note"?}``
    "Doesn't play?": one JSON line in ``<state>/reports.jsonl``. Stays on
    the radio (sharing is a later, opt-in step). At most ``REPORTS_PER_HOUR``.
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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..player.sources import hls
from . import stations

log = logging.getLogger(__name__)

PREFIX = "/api/stations"
PATHS = ("/api/stations/try", "/api/stations/mine", "/api/stations/report")   # server.EXTRA_PREFIXES
MINE_FILE = "stations.json"
REPORTS_FILE = "reports.jsonl"
RECENT_MAX = 50
FAVOURITES_MAX = 500
CUSTOM_MAX = 200
REPORTS_PER_HOUR = 10
REASONS = ("doesnt-play", "wrong-station", "bad-sound", "wrong-info", "other")
PROBE_UA = "Music Player Daemon 0.24"     # what the radio's MPD sends
PROBE_BYTES = 65536
PROBE_TIMEOUT = 8.0
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")

# probe(url) -> {"ok", "codec", "bitrate", "error"}. Injected by tests.
Probe = Callable[[str], dict]


def _is_url(text: str) -> bool:
    return isinstance(text, str) and text.startswith(("http://", "https://")) and len(text) <= 2048 \
        and not any(c.isspace() for c in text)


def _ref_ok(ref: Any) -> bool:
    return isinstance(ref, str) and (bool(ID.match(ref)) or _is_url(ref))


def probe(url: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """Fetch the first bytes of ``url`` as MPD would and say what it is."""
    request = urllib.request.Request(url, headers={
        "User-Agent": PROBE_UA, "Range": f"bytes=0-{PROBE_BYTES - 1}", "Icy-MetaData": "0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ctype = (response.headers.get("Content-Type") or "").lower()
            bitrate = response.headers.get("icy-br") or ""
            body, deadline = b"", time.monotonic() + timeout
            while len(body) < 4096 and time.monotonic() < deadline:
                chunk = response.read1(4096) if hasattr(response, "read1") else response.read(4096)
                if not chunk:
                    break
                body += chunk
    except urllib.error.HTTPError as exc:
        return {"ok": False, "codec": "", "bitrate": None, "error": f"the station answered HTTP {exc.code}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return {"ok": False, "codec": "", "bitrate": None, "error": f"no answer: {reason}"[:200]}
    return sniff(ctype, body, bitrate)


def sniff(ctype: str, body: bytes, bitrate: str = "") -> dict:
    """{"ok", "codec", "bitrate", "error"} from a content type and the first bytes."""
    head = body[:4096].lstrip()
    try:
        rate = int(str(bitrate).split(",")[0]) or None
    except ValueError:
        rate = None
    out = {"ok": True, "codec": "", "bitrate": rate, "error": ""}
    if head.startswith(b"#EXTM3U") and b"#EXT-X-" in head:
        return dict(out, codec="hls")
    if "mpegurl" in ctype or "scpls" in ctype or "pls" in ctype or head.startswith((b"#EXTM3U", b"[playlist]")):
        return dict(out, codec="playlist")
    if "text/html" in ctype or head[:15].lower().startswith((b"<!doctype html", b"<html")):
        return {"ok": False, "codec": "", "bitrate": None, "error": "that link is a web page, not a stream"}
    if not body:
        return {"ok": False, "codec": "", "bitrate": None, "error": "the station sent nothing"}
    if head.startswith(b"OggS"):
        return dict(out, codec="opus" if b"OpusHead" in head else "vorbis" if b"vorbis" in head else "ogg")
    if head.startswith(b"fLaC") or "flac" in ctype:
        return dict(out, codec="flac")
    if "aac" in ctype or "mp4" in ctype or head[:2] in (b"\xff\xf1", b"\xff\xf9"):
        return dict(out, codec="aac")
    if "mpeg" in ctype or head.startswith(b"ID3") or head[:1] == b"\xff":
        return dict(out, codec="mp3")
    if ctype.startswith("audio/") or "ogg" in ctype or "octet-stream" in ctype:
        return dict(out, codec=ctype.split("/")[-1].split(";")[0])
    return {"ok": False, "codec": "", "bitrate": None, "error": f"not audio ({ctype or 'no type'})"}


def write_atomic(path: Path, doc: Any) -> None:
    """Write JSON so a power cut leaves the old file or the new one, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def empty_mine() -> dict:
    return {"favourites": [], "custom": [], "order": [], "recent": []}


def _refs(raw: Any, field: str, cap: int) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{field} must be a list")
    out: list[str] = []
    for ref in raw:
        if not _ref_ok(ref):
            raise ValueError(f"{field}: {str(ref)[:60]!r} is neither a station id nor a stream link")
        if ref not in out:
            out.append(ref)
    if len(out) > cap:
        raise ValueError(f"{field}: at most {cap}")
    return out


def _custom(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("custom must be a list")
    out, ids = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("custom: each station is {name, url}")
        name = str(item.get("name") or "").strip()[:100]
        url = str(item.get("url") or "").strip()
        if not name or not _is_url(url):
            raise ValueError("custom: each station needs a name and an http(s) stream link")
        sid = str(item.get("id") or "").strip()
        if not ID.match(sid):
            sid = "custom-" + hashlib.sha256(url.encode()).hexdigest()[:10]
        if sid in ids:
            continue
        ids.add(sid)
        out.append({"id": sid, "name": name, "url": url})
    if len(out) > CUSTOM_MAX:
        raise ValueError(f"custom: at most {CUSTOM_MAX}")
    return out


def _recent(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("recent must be a list")
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("recent: each entry is {id or url, t}")
        ref = item.get("id") or item.get("url")
        if not _ref_ok(ref) or ref in seen:
            continue
        seen.add(ref)
        try:
            t = int(item.get("t") or 0)
        except (TypeError, ValueError):
            t = 0
        out.append({("url" if _is_url(ref) else "id"): ref, "t": t})
    return out[:RECENT_MAX]


class StationsApi:
    """The routes above, for ``WebApp.extra_routes``. ``played`` is the
    radio source's on-play hook (main.build_web)."""

    def __init__(
        self,
        app: Any,
        state_dir: Path | None,
        collection: Path | None = None,
        probe: Probe = probe,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.collection = collection or stations.CURATED_FILE
        self.mine_file = state_dir / MINE_FILE if state_dir is not None else None
        self.reports_file = state_dir / REPORTS_FILE if state_dir is not None else None
        self.probe = probe
        self.clock = clock
        self.lock = threading.Lock()
        self._mine_mem = empty_mine()        # without a state dir (dev runs)
        self._reports: collections.deque = collections.deque()

    # -- routing ------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, Any] | None:
        path = path.split("?", 1)[0]
        if path == "/api/stations/mine":
            if method == "GET":
                return 200, self.mine()
            if method in ("PUT", "POST"):
                return 200, {"ok": True, **self.put_mine(body)}
        elif path == "/api/stations/try" and method == "POST":
            return self.try_station(body)
        elif path == "/api/stations/report" and method == "POST":
            return self.report(body)
        return None

    # -- my stations --------------------------------------------------------

    def _load(self) -> dict:
        if self.mine_file is None:
            return json.loads(json.dumps(self._mine_mem))
        try:
            doc = json.loads(self.mine_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty_mine()
        except (OSError, ValueError):
            log.warning("unreadable %s; starting an empty shortlist", self.mine_file, exc_info=True)
            return empty_mine()
        mine = empty_mine()
        if isinstance(doc, dict):
            for field, check in (("favourites", lambda v: _refs(v, "favourites", FAVOURITES_MAX)),
                                 ("custom", _custom), ("order", lambda v: _refs(v, "order", 2000)),
                                 ("recent", _recent)):
                try:
                    mine[field] = check(doc.get(field, []))
                except ValueError:
                    log.warning("dropping a bad %s list in %s", field, self.mine_file)
        return mine

    def _save(self, mine: dict) -> None:
        if self.mine_file is None:
            self._mine_mem = mine
        else:
            write_atomic(self.mine_file, mine)

    def mine(self) -> dict:
        with self.lock:
            return self._load()

    def put_mine(self, body: Any) -> dict:
        if not isinstance(body, dict):
            raise ValueError("send {favourites, custom, order, recent}")
        with self.lock:
            mine = self._load()
            if "favourites" in body:
                mine["favourites"] = _refs(body["favourites"], "favourites", FAVOURITES_MAX)
            if "custom" in body:
                mine["custom"] = _custom(body["custom"])
            if "order" in body:
                mine["order"] = _refs(body["order"], "order", 2000)
            if "recent" in body:
                mine["recent"] = _recent(body["recent"])
            self._save(mine)
            return mine

    def played(self, preset: Any) -> None:
        """A radio stream started (any way): put it first in ``recent``. Never raises."""
        try:
            url = str(getattr(preset, "uri", "") or "")
            if not _is_url(url):
                return
            sid = stations.id_for_url(url, self.collection)
            entry = {"id": sid, "t": int(self.clock())} if sid else {"url": url, "t": int(self.clock())}
            ref = sid or url
            with self.lock:
                mine = self._load()
                rest = [r for r in mine["recent"] if (r.get("id") or r.get("url")) != ref]
                mine["recent"] = ([entry] + rest)[:RECENT_MAX]
                self._save(mine)
        except Exception:  # noqa: BLE001 - bookkeeping never stops the music
            log.warning("could not record a recent station", exc_info=True)

    # -- try ----------------------------------------------------------------

    def try_station(self, body: Any) -> tuple[int, dict]:
        if not isinstance(body, dict):
            raise ValueError("send {id} or {url}")
        sid = str(body.get("id") or "").strip()
        info = {"codec": "", "bitrate": None}
        if sid:
            station = stations.by_id(sid, self.collection)
            if station is None:
                custom = next((c for c in self.mine()["custom"] if c["id"] == sid), None)
                if custom is None:
                    return 404, {"ok": False, "error": f"no station {sid!r}"}
                station = {"name": custom["name"], "url": custom["url"]}
            url, name = station["url"], station["name"]
            info = {"codec": station.get("codec", ""), "bitrate": station.get("bitrate")}
        else:
            url = str(body.get("url") or "").strip()
            if not _is_url(url):
                raise ValueError("paste an http(s) stream link")
            got = self.probe(url)
            if not got.get("ok"):
                return 200, {"ok": False, "url": url, "codec": got.get("codec", ""),
                             "bitrate": got.get("bitrate"), "error": got.get("error") or "that link does not play"}
            info = {"codec": got.get("codec", ""), "bitrate": got.get("bitrate")}
            sid = stations.id_for_url(url, self.collection) or ""
            name = str(body.get("name") or "").strip()[:60] or (stations.by_id(sid, self.collection) or {}).get(
                "name", "") or url.split("://", 1)[-1][:60]
        if str(info.get("codec") or "").lower().startswith("hls"):
            hls.remember(url)       # plays through the local HLS proxy even without .m3u8 in the path
        self.app.play({"source": "radio", "uri": url, "name": name})
        return 200, {"ok": True, "id": sid or None, "url": url, "name": name, **info, "error": ""}

    # -- reports ------------------------------------------------------------

    def report(self, body: Any) -> tuple[int, dict]:
        if not isinstance(body, dict):
            raise ValueError("send {id or url, reason, note}")
        sid = str(body.get("id") or "").strip()
        url = str(body.get("url") or "").strip()
        if sid and not ID.match(sid):
            raise ValueError("not a station id")
        if not sid and not _is_url(url):
            raise ValueError("say which station: an id or its stream link")
        reason = str(body.get("reason") or "doesnt-play").strip()
        if reason not in REASONS:
            raise ValueError(f"reason must be one of: {', '.join(REASONS)}")
        note = str(body.get("note") or "").strip()[:500]
        now = self.clock()
        with self.lock:
            while self._reports and now - self._reports[0] > 3600:
                self._reports.popleft()
            if len(self._reports) >= REPORTS_PER_HOUR:
                return 429, {"ok": False, "error": "that's a lot of reports - try again in an hour"}
            self._reports.append(now)
            station = stations.by_id(sid, self.collection) if sid else None
            entry = {"t": int(now), "id": sid or stations.id_for_url(url, self.collection),
                     "url": url or (station or {}).get("url", ""),
                     "name": (station or {}).get("name", ""), "reason": reason, "note": note}
            if self.reports_file is not None:
                self.reports_file.parent.mkdir(parents=True, exist_ok=True)
                with self.reports_file.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            log.info("station report: %s %s (%s)", entry["id"] or entry["url"], reason, note[:60])
        return 200, {"ok": True}
