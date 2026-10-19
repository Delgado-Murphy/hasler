"""The YouTube Music catalogue: search, the owner's library, radio, track names.

yt-dlp is the right tool for turning a video id into a stream (see ytm.py) and a
poor one for everything else: ``--flat-playlist`` takes seconds, knows nothing
about artists or albums, and returns no artwork. ``ytmusicapi`` speaks the same
private JSON API the music.youtube.com web app uses, so it answers in well under
a second on a Pi and hands back what the key page and moOde want to show -
titles, artists, albums, thumbnails - plus the two things yt-dlp cannot do at
all: search, and "radio from this song/artist" that keeps going.

Shape of the thing:

* **Optional and lazy.** ytmusicapi is imported on first use and only lives in
  the Pi's ``pi`` extra; on a laptop (or if the import breaks) ``available`` is
  False and everything returns empty, so the ytm source falls back to yt-dlp and
  the web page hides its search box. Nothing here may stop the radio playing.
* **One client, one lock.** The YTMusic object holds a requests session and
  some lazily computed headers and is not documented as thread-safe; the web
  page, the warm-up thread and the endless-radio top-up all reach it from their
  own threads. Every call goes through ``_call`` under ``_lock``. Each call is
  well under a second, so the serialisation costs nothing a human notices.
* **Every external call degrades to a log line** and an empty answer. The one
  exception is ``sign_in``, which raises ``ValueError`` with a sentence a human
  can act on, because the person pasting headers is waiting for an answer.

Presets stay ordinary music.youtube.com links (``keymap.YTM_LINK`` already
accepts them), so a preset made from a search result also works when the
catalogue is unavailable - yt-dlp can expand every one of them:

    song radio    https://music.youtube.com/watch?v=<vid>&list=RDAMVM<vid>
    artist radio  https://music.youtube.com/watch?list=<radioId>       (RDEM…)
    album         https://music.youtube.com/playlist?list=<playlistId> (OLAK5uy_…)
    playlist      https://music.youtube.com/playlist?list=<id, no "VL">
    liked songs   https://music.youtube.com/playlist?list=LM           (signed in)

Search needs no second request: on ytmusicapi 1.12 an artist search result
already carries ``radioId`` (RDEM…, ~95% of results) and an album result its
``playlistId`` (OLAK5uy_…, ~98%). The few without one are dropped rather than
resolved with get_artist/get_album, which would add ~0.5 s each and push a
search past the 1.5 s budget. Measured on the Pi 4: 0.4-0.75 s per search.

Signing in (the owner's account - Liked songs, private playlists): the key page
takes request headers copied from a signed-in desktop browser, or just the
Cookie header. Two files are written to the secrets directory (``<state>/secrets``,
0700; the state directory itself on a box from before it), both 0600:

* ``ytm-browser.json`` - ytmusicapi "browser" auth (its ``setup()`` output).
* ``ytm-cookies.txt`` - the same cookies in Netscape format for yt-dlp, so
  private playlists and liked songs also *resolve*, and YouTube's "confirm you
  are not a bot" checks bite less. ytm.py picks it up per call, no restart.

They are the account's login. They are never logged, never echoed back to the
page, and ``sign_out`` deletes both.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ...boxconfig import secret_file

log = logging.getLogger(__name__)

BROWSER_FILE = "ytm-browser.json"
COOKIES_FILE = "ytm-cookies.txt"

SEARCH_KINDS = ("songs", "artists", "albums", "playlists")
MAX_RESULTS = 20
RADIO_LIMIT = 50            # get_watch_playlist gives ~50 whatever you ask for
REQUEST_TIMEOUT_S = 10      # ytmusicapi's default session waits 30 s
LIKED = "LM"

BASE = "https://music.youtube.com"
SONG_RADIO = BASE + "/watch?v={vid}&list=RDAMVM{vid}"
WATCH_LIST = BASE + "/watch?list={id}"
PLAYLIST = BASE + "/playlist?list={id}"

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_ID = re.compile(r"^[A-Za-z0-9_-]{2,64}$")

# ytmusicapi computes the SAPISIDHASH Authorization from this cookie on every
# request (helpers.sapisid_from_cookie). Without it, no signed-in call can work.
SAPISID_COOKIE = "__Secure-3PAPISID"
# The saved file must carry *some* SAPISIDHASH Authorization, or ytmusicapi
# mistakes it for OAuth and refuses to load it. Its value is recomputed per
# request, so a placeholder is enough.
AUTH_PLACEHOLDER = "SAPISIDHASH 0_0"
# cookies.txt expiry: far enough out that yt-dlp never discards a cookie on its
# own. Google expires the session server-side long before (months).
COOKIE_LIFETIME_S = 2 * 365 * 24 * 3600


class YtmCatalog:
    """ytmusicapi behind a lock, with the owner's sign-in kept in ``state_dir``.

    ``api`` stands in for the ``ytmusicapi`` module (needs ``YTMusic`` and
    ``setup``) so tests never touch the network; None imports the real one on
    first use.
    """

    def __init__(self, state_dir: str | Path | None, api: Any = None) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self._api = api
        self._import_failed = False
        self._lock = threading.RLock()
        self._client: Any = None
        self._client_auth: str | None = None   # which login the client was built with
        self._name: str | None = None          # account name, cached after one lookup

    # -- files ------------------------------------------------------------

    @property
    def browser_file(self) -> Path | None:
        return secret_file(self.state_dir, BROWSER_FILE) if self.state_dir else None

    @property
    def cookies_file(self) -> Path | None:
        """Netscape cookies for yt-dlp. ytm.py checks it exists before each use."""
        return secret_file(self.state_dir, COOKIES_FILE) if self.state_dir else None

    @property
    def signed_in(self) -> bool:
        path = self.browser_file
        return bool(path and path.exists())

    # -- the client -------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._module() is not None

    def _module(self) -> Any:
        if self._api is None and not self._import_failed:
            try:
                import ytmusicapi  # noqa: PLC0415 - optional, Pi only

                self._api = ytmusicapi
            except Exception:
                self._import_failed = True
                log.info("ytmusicapi not installed; YouTube Music search and names are off")
        return self._api

    def _new_client(self, auth: str | None) -> Any:
        api = self._module()
        kwargs: dict[str, Any] = {}
        if api is not None and getattr(api, "__name__", "") == "ytmusicapi":
            # Only for the real module: a session with a timeout, so a wedged
            # request cannot hold the lock (and the web page) for 30 s.
            try:
                import functools

                import requests

                session = requests.Session()
                session.request = functools.partial(session.request, timeout=REQUEST_TIMEOUT_S)
                kwargs["requests_session"] = session
            except Exception:
                log.debug("no requests session with a timeout", exc_info=True)
        # No language/location: location="ES" was seen to return empty searches.
        return api.YTMusic(auth, **kwargs) if auth else api.YTMusic(**kwargs)

    def _get_client(self) -> Any:
        """The shared client, rebuilt when the sign-in changes. Call under _lock."""
        auth = str(self.browser_file) if self.signed_in else None
        if self._client is None or self._client_auth != auth:
            self._client = self._new_client(auth)
            self._client_auth = auth
        return self._client

    def _call(self, what: str, fn: Callable[[Any], Any], default: Any) -> Any:
        """Run ``fn(client)`` under the lock; on any failure, log and return ``default``."""
        if not self.available:
            return default
        try:
            with self._lock:
                return fn(self._get_client())
        except Exception as exc:
            log.warning("YouTube Music %s failed: %s: %s", what, type(exc).__name__, str(exc)[:200])
            return default

    # -- search -----------------------------------------------------------

    def search(self, query: str, kind: str = "songs") -> list[dict]:
        """Up to 20 results, each ready to become a preset (see module docstring).

        ``{"type", "title", "subtitle", "thumb", "uri", "name"}``; ``uri`` is
        the preset link, ``name`` a suggested preset name.
        """
        query = (query or "").strip()
        if kind not in SEARCH_KINDS:
            raise ValueError(f"search type must be one of: {', '.join(SEARCH_KINDS)}")
        if not query:
            return []
        raw = self._call("search", lambda c: c.search(query[:200], filter=kind), [])
        mapper = {"songs": _song_item, "artists": _artist_item,
                  "albums": _album_item, "playlists": _playlist_item}[kind]
        out = []
        for item in raw or []:
            if not isinstance(item, dict):
                continue
            mapped = mapper(item)
            if mapped is not None:
                out.append(mapped)
            if len(out) >= MAX_RESULTS:
                break
        return out

    # -- the owner's library ---------------------------------------------

    def library(self) -> list[dict]:
        """Liked songs, then the account's playlists. [] when not signed in."""
        if not self.signed_in:
            return []
        items = [{
            "type": "playlist", "title": "Liked songs", "subtitle": "Your likes",
            "thumb": "", "uri": PLAYLIST.format(id=LIKED), "name": "Liked songs",
        }]
        raw = self._call("library", lambda c: c.get_library_playlists(limit=None), [])
        for entry in raw or []:
            if not isinstance(entry, dict):
                continue
            pid = _strip_vl(str(entry.get("playlistId") or ""))
            if pid in (LIKED, "SE") or not LIST_ID.match(pid):
                continue    # Liked is listed first already; SE = "Episodes for later"
            title = str(entry.get("title") or "Playlist")
            count = entry.get("count")
            items.append({
                "type": "playlist", "title": title,
                "subtitle": f"{count} songs" if count else "Playlist",
                "thumb": _thumb(entry), "uri": PLAYLIST.format(id=pid), "name": title[:60],
            })
        return items

    # -- tracks -----------------------------------------------------------

    def tracks(self, uri: str) -> list[dict]:
        """A playlist, album (OLAK…) or Liked (LM) link -> its playable tracks."""
        list_id = _list_id(uri)
        if not list_id:
            return []
        if list_id == LIKED and not self.signed_in:
            log.warning("Liked songs need the YouTube Music sign-in (key page)")
            return []
        data = self._call("playlist", lambda c: c.get_playlist(list_id, limit=None), {})
        return _tracks((data or {}).get("tracks"))

    def radio(self, uri: str, after: str | None = None) -> list[dict]:
        """A radio link -> ~50 tracks. ``after`` (a video id) continues from there.

        Continuing from the last queued track is what makes a radio endless:
        measured, about half of each further batch is new.
        """
        list_id = _list_id(uri)
        if not list_id:
            return []
        if after and VIDEO_ID.match(after):
            vid: str | None = after
        else:
            vid = (parse_qs(urlparse(uri).query).get("v") or [""])[0]
            if not VIDEO_ID.match(vid or ""):
                seed = list_id[6:] if list_id.startswith("RDAMVM") else ""
                vid = seed if VIDEO_ID.match(seed) else None
        data = self._call(
            "radio",
            lambda c: c.get_watch_playlist(videoId=vid, playlistId=list_id, radio=True, limit=RADIO_LIMIT),
            {},
        )
        return _tracks((data or {}).get("tracks"))

    # -- account ----------------------------------------------------------

    def account(self) -> dict:
        """``{"signed_in": bool, "name": str}``. The name is looked up once."""
        if not self.signed_in:
            return {"signed_in": False, "name": ""}
        if self._name is None:
            info = self._call("account lookup", lambda c: c.get_account_info(), None)
            name = str((info or {}).get("accountName") or "") if isinstance(info, dict) else ""
            if name:
                self._name = name
            return {"signed_in": True, "name": name}
        return {"signed_in": True, "name": self._name}

    def sign_in(self, headers_raw: str) -> dict:
        """Store a login pasted from a browser; validate it; return ``account()``.

        Accepts the request headers of any signed-in music.youtube.com request
        (Firefox "Copy Request Headers", Chrome's raw view) or only the value of
        the Cookie header. Raises ValueError with a human message - and leaves
        no files behind - if it can't be used. Never logs what was pasted.
        """
        if not self.available:
            raise ValueError("YouTube Music sign-in needs ytmusicapi on the radio (pip install ytmusicapi)")
        if self.state_dir is None:
            raise ValueError("no state directory to keep the sign-in in")
        text = (headers_raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text:
            raise ValueError("paste the request headers (or the Cookie) from music.youtube.com")

        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        if len(lines) == 1:
            cookie = re.sub(r"(?i)^cookie\s*:\s*", "", lines[0]).strip()
            block = None
        else:
            cookie, rest = _cookie_from_headers(lines)
            # One plain "cookie: …" line whatever form it came in; setup()
            # copes with Chrome's other quirks itself.
            block = "\n".join([*rest, f"cookie: {cookie}"])
        pairs = _parse_cookie(cookie)
        if not pairs:
            raise ValueError("no Cookie found - copy the headers of a request to music.youtube.com while signed in")
        if SAPISID_COOKIE not in pairs:
            raise ValueError(
                "that cookie has no SAPISID (__Secure-3PAPISID) - copy it from music.youtube.com "
                "while signed in, not from a private window that has signed out"
            )
        if block is None:
            # Just a cookie: the minimal header block setup() accepts.
            block = f"cookie: {cookie}\nx-goog-authuser: 0"
        elif not re.search(r"(?im)^x-goog-authuser\s*:", block):
            block += "\nx-goog-authuser: 0"

        api = self._module()
        try:
            headers = json.loads(api.setup(filepath=None, headers_raw=block))
        except Exception as exc:
            # setup()'s messages name missing headers, never their values.
            raise ValueError(f"could not read those headers: {_safe_message(exc)}") from None
        if not isinstance(headers, dict):
            raise ValueError("could not read those headers")
        if "SAPISIDHASH" not in str(_get_ci(headers, "authorization") or ""):
            for key in [k for k in headers if k.lower() == "authorization"]:
                del headers[key]
            headers["authorization"] = AUTH_PLACEHOLDER

        browser, cookies = self.browser_file, self.cookies_file
        assert browser is not None and cookies is not None
        with self._lock:
            try:
                _write_private(browser, json.dumps(headers, ensure_ascii=True, indent=2, sort_keys=True))
                _write_private(cookies, _netscape_cookies(pairs))
                self._client = None
                self._name = None
                info = self._get_client().get_account_info()
                name = str((info or {}).get("accountName") or "") if isinstance(info, dict) else ""
            except Exception as exc:
                self._forget()
                log.warning("YouTube Music sign-in rejected: %s", type(exc).__name__)
                raise ValueError(
                    "YouTube Music did not accept that sign-in. Copy the headers again from a "
                    "music.youtube.com tab where you are signed in."
                ) from None
            self._name = name or None
        log.info("YouTube Music signed in%s", f" as {name}" if name else "")
        return {"signed_in": True, "name": name}

    def sign_out(self) -> dict:
        with self._lock:
            self._forget()
        log.info("YouTube Music signed out")
        return self.account()

    def _forget(self) -> None:
        for path in (self.browser_file, self.cookies_file):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning("could not delete %s", path, exc_info=True)
        self._client = None
        self._client_auth = None
        self._name = None


# -- result mapping ----------------------------------------------------------


def _song_item(item: dict) -> dict | None:
    vid = str(item.get("videoId") or "")
    if not VIDEO_ID.match(vid):
        return None
    title = str(item.get("title") or "Song")
    artists = _artists(item)
    album = str((item.get("album") or {}).get("name") or "") if isinstance(item.get("album"), dict) else ""
    return {
        "type": "song", "title": title,
        "subtitle": " · ".join(x for x in (artists, album) if x),
        "thumb": _thumb(item), "uri": SONG_RADIO.format(vid=vid), "name": f"{title} radio"[:60],
    }


def _artist_item(item: dict) -> dict | None:
    radio = str(item.get("radioId") or item.get("shuffleId") or "")
    if not LIST_ID.match(radio):
        return None     # ~5% of artists: no radio offered in search; skipped, not resolved
    name = str(item.get("artist") or item.get("title") or "Artist")
    return {
        "type": "artist", "title": name, "subtitle": "Artist radio",
        "thumb": _thumb(item), "uri": WATCH_LIST.format(id=radio), "name": f"{name} radio"[:60],
    }


def _album_item(item: dict) -> dict | None:
    pid = str(item.get("playlistId") or "")
    if not LIST_ID.match(pid):
        return None     # ~2%: no audio playlist in the search result; skipped
    title = str(item.get("title") or "Album")
    parts = [str(item.get("type") or "Album"), _artists(item), str(item.get("year") or "")]
    return {
        "type": "album", "title": title, "subtitle": " · ".join(p for p in parts if p),
        "thumb": _thumb(item), "uri": PLAYLIST.format(id=pid), "name": title[:60],
    }


def _playlist_item(item: dict) -> dict | None:
    pid = _strip_vl(str(item.get("browseId") or item.get("playlistId") or ""))
    if not LIST_ID.match(pid):
        return None
    title = str(item.get("title") or "Playlist")
    author = item.get("author")
    if isinstance(author, list):
        author = ", ".join(str(a.get("name")) for a in author if isinstance(a, dict) and a.get("name"))
    count = item.get("itemCount")
    parts = [str(author or ""), f"{count} songs" if count else ""]
    return {
        "type": "playlist", "title": title, "subtitle": " · ".join(p for p in parts if p) or "Playlist",
        "thumb": _thumb(item), "uri": PLAYLIST.format(id=pid), "name": title[:60],
    }


def _tracks(raw: Any) -> list[dict]:
    """ytmusicapi tracks (playlist or watch shape) -> ``{"videoId", "title", …}``."""
    out, seen = [], set()
    for t in raw or []:
        if not isinstance(t, dict) or t.get("isAvailable") is False:
            continue
        vid = str(t.get("videoId") or "")
        if not VIDEO_ID.match(vid) or vid in seen:
            continue
        seen.add(vid)
        album = t.get("album")
        out.append({
            "videoId": vid,
            "title": str(t.get("title") or ""),
            "artist": _artists(t),
            "album": str(album.get("name") or "") if isinstance(album, dict) else "",
            "thumb": _thumb(t),
            # get_playlist says "duration", get_watch_playlist "length".
            "duration": str(t.get("duration") or t.get("length") or ""),
        })
    return out


def _artists(item: dict) -> str:
    artists = item.get("artists")
    if not isinstance(artists, list):
        return ""
    return ", ".join(str(a["name"]) for a in artists if isinstance(a, dict) and a.get("name"))


def _thumb(item: dict) -> str:
    """The largest thumbnail (ytmusicapi lists them smallest first)."""
    thumbs = item.get("thumbnails") or item.get("thumbnail") or []
    if isinstance(thumbs, dict):
        thumbs = thumbs.get("thumbnails") or []
    for t in reversed(thumbs if isinstance(thumbs, list) else []):
        if isinstance(t, dict) and str(t.get("url") or "").startswith("https://"):
            return str(t["url"])
    return ""


def _strip_vl(pid: str) -> str:
    return pid[2:] if pid.startswith("VL") else pid


def _list_id(uri: str) -> str | None:
    lid = (parse_qs(urlparse(uri or "").query).get("list") or [""])[0]
    return lid if LIST_ID.match(lid) else None


# -- sign-in helpers ---------------------------------------------------------


def _cookie_from_headers(lines: list[str]) -> tuple[str, list[str]]:
    """(Cookie value, the other lines) from a header block.

    Handles ``cookie: v`` and Chrome's two-line ``cookie`` / ``v`` form.
    """
    for i, line in enumerate(lines):
        m = re.match(r"(?i)^cookie\s*:?\s*(.*)$", line)
        if not m:
            continue
        if m.group(1).strip() and ":" in line[:7]:
            return m.group(1).strip(), lines[:i] + lines[i + 1:]
        if not m.group(1).strip() and i + 1 < len(lines):    # "cookie[:]" then the value
            return lines[i + 1], lines[:i] + lines[i + 2:]
    return "", lines


def _parse_cookie(cookie: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        name, sep, value = part.strip().partition("=")
        name = name.strip()
        if sep and name and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            pairs[name] = value.strip().strip('"')
    return pairs


def _netscape_cookies(pairs: dict[str, str]) -> str:
    """cookies.txt for yt-dlp: one line per cookie, .youtube.com, secure, far expiry."""
    expiry = int(time.time()) + COOKIE_LIFETIME_S
    lines = [
        "# Netscape HTTP Cookie File",
        "# Written by haslerd from the YouTube Music sign-in on the key page. This is a login: keep it 0600.",
    ]
    for name, value in pairs.items():
        lines.append("\t".join([".youtube.com", "TRUE", "/", "TRUE", str(expiry), name, value]))
    return "\n".join(lines) + "\n"


def _write_private(path: Path, text: str) -> None:
    """Write ``path`` atomically with mode 0600 (mkstemp creates files 0600)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _get_ci(d: dict, key: str) -> Any:
    for k, v in d.items():
        if k.lower() == key:
            return v
    return None


def _safe_message(exc: Exception) -> str:
    """An exception's message with anything cookie-shaped cut out, for the page."""
    msg = str(exc)[:300]
    return re.sub(r"[A-Za-z0-9_.-]+=[^;\s]+", "…", msg) or type(exc).__name__
