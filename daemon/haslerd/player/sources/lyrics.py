"""Timed lyrics for the key page: YouTube Music's own, else LRCLIB's, else plain.

One shape for the page (``/api/ytm/lyrics/<vid>``)::

    {"lyrics": str|None,          # the plain text (always, when any source has words)
     "source": str,               # "Source: LyricFind" / "LRCLIB" - shown under them
     "synced": bool,              # True: ``lines`` carry times the page can follow
     "lines": [{"t": 12.34,       # seconds into the song the line starts
                "end": 15.0|None, # when YouTube says it ends (LRC has no ends)
                "text": "…",
                "words": [{"t": 12.34, "w": "Hola, "}, …]  # only from enhanced LRC
               }] | None}

YouTube Music (ytmusicapi >= 1.8: ``get_lyrics(browseId, timestamps=True)``)
answers either ``{"lyrics": "plain", "hasTimestamps": False}`` or
``{"lyrics": [LyricLine(text, start_time, end_time, id)], "hasTimestamps": True}``
(milliseconds; dataclasses, or dicts in a test or an older build) -
``timed_from_ytm`` reads both. When it has no times, LRCLIB (lrclib.net, free,
no key) is asked with the song's artist, title, album and length - the same
four facts YouTube is already told, nothing about the owner or the radio. Its
``syncedLyrics`` is LRC: ``parse_lrc`` reads ``[mm:ss.xx]`` (several per
line), ``[offset:±ms]`` and enhanced ``<mm:ss.xx>`` word times.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, is_dataclass
from typing import Any

from ... import __version__

log = logging.getLogger(__name__)

LRCLIB = "https://lrclib.net/api"
LRCLIB_TIMEOUT_S = 4.0
# LRCLIB asks clients to name themselves and link to the project.
USER_AGENT = f"Hasler-radio/{__version__} (https://github.com/Delgado-Murphy/hasler)"
DURATION_SLACK_S = 2.0          # a hit this close in length is the same recording
# YouTube Music's timed lines belong to the song's album recording; the vid
# playing may be its music video (an intro, a skit, a longer outro). Lines that
# run on past the end, or stop this long (and this share of the song) before
# it, are for another version: the page shouldn't light them on this clock.
FIT_EARLY_END_S = 40.0
FIT_EARLY_END_SHARE = 0.2
MAX_LINES = 400

_STAMP = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_WORD = re.compile(r"<(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?>")
_TAG = re.compile(r"^\[([a-zA-Z#]+):(.*)\]\s*$")
# A music video's title on YouTube: "Song (Official Video)", "Song [Lyrics]".
_NOISE = re.compile(r"\s*[(\[](?:official|lyric|lyrics|audio|video|visuali[sz]er|hd|4k|remaster)[^)\]]*[)\]]",
                    re.I)


def _secs(mm: str, ss: str, frac: str | None) -> float:
    f = 0.0
    if frac:
        f = int(frac) / (10 ** len(frac))
    return int(mm) * 60 + int(ss) + f


def parse_lrc(text: str) -> list[dict]:
    """LRC to lines, sorted by time; [] when it has no time stamps.

    ``[00:12.34][01:02.00]Chorus`` is one line at each time. ``[offset:+500]``
    moves every line 0.5 s *earlier* (the LRC convention). Enhanced word times
    ``<00:12.34>Hola <00:12.90>mundo`` become ``words``; the line's text is
    then the words joined.
    """
    if not isinstance(text, str):
        return []
    offset = 0.0
    out: list[dict] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        tag = _TAG.match(line)
        if tag and not _STAMP.match(line):
            if tag.group(1).lower() == "offset":
                try:
                    offset = int(tag.group(2).strip()) / 1000.0
                except ValueError:
                    pass
            continue
        stamps = []
        pos = 0
        while (m := _STAMP.match(line, pos)) is not None:
            stamps.append(_secs(*m.groups()))
            pos = m.end()
        if not stamps:
            continue
        body = line[pos:]
        words = []
        marks = list(_WORD.finditer(body))
        if marks:
            for i, m in enumerate(marks):
                end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
                w = body[m.end():end]
                if w.strip():
                    words.append({"t": _secs(*m.groups()), "w": w})
            body = "".join(w["w"] for w in words) if words else _WORD.sub("", body)
        body = body.strip()
        for t in stamps:
            doc: dict[str, Any] = {"t": t, "end": None, "text": body}
            if words:
                shift = t - stamps[0]       # a repeated enhanced line: its words move with it
                doc["words"] = [{"t": w["t"] + shift, "w": w["w"]} for w in words]
            out.append(doc)
    for doc in out:
        doc["t"] = round(max(0.0, doc["t"] - offset), 3)
        for w in doc.get("words", ()):
            w["t"] = round(max(0.0, w["t"] - offset), 3)
    out.sort(key=lambda d: d["t"])
    return out[:MAX_LINES]


def _field(line: Any, *names: str) -> Any:
    if is_dataclass(line) and not isinstance(line, type):
        line = asdict(line)
    if isinstance(line, dict):
        for n in names:
            if n in line:
                return line[n]
        cue = line.get("cueRange")             # the raw mobile-API shape
        if isinstance(cue, dict):
            for n in names:
                key = {"start_time": "startTimeMilliseconds", "end_time": "endTimeMilliseconds"}.get(n)
                if key and key in cue:
                    return cue[key]
        return None
    for n in names:
        if hasattr(line, n):
            return getattr(line, n)
    return None


def timed_from_ytm(raw: Any) -> tuple[list[dict] | None, str | None]:
    """(timed lines or None, plain text or None) from ``get_lyrics``'s answer."""
    if not isinstance(raw, dict):
        return None, None
    lyr = raw.get("lyrics")
    if isinstance(lyr, str):
        return None, lyr if lyr.strip() else None
    if not isinstance(lyr, list) or not lyr:
        return None, None
    lines = []
    for item in lyr:
        text = _field(item, "text", "lyricLine")
        start = _field(item, "start_time", "startTimeMilliseconds")
        end = _field(item, "end_time", "endTimeMilliseconds")
        try:
            t = int(start) / 1000.0
        except (TypeError, ValueError):
            continue
        try:
            e = int(end) / 1000.0 if end is not None else None
        except (TypeError, ValueError):
            e = None
        text = str(text or "").strip()
        if text in ("♪", "♫"):
            text = ""
        lines.append({"t": round(t, 3), "end": round(e, 3) if e is not None else None, "text": text})
    lines.sort(key=lambda d: d["t"])
    lines = lines[:MAX_LINES]
    plain = "\n".join(d["text"] for d in lines).strip() or None
    return (lines or None), plain


def timing_misfit(lines: list[dict] | None, duration: float | None) -> str | None:
    """Why timed ``lines`` don't fit a recording ``duration`` s long, or None when
    they do (or the length isn't known). The last line's end (else its start) is
    compared with the song: past its end by more than DURATION_SLACK_S, or ending
    more than FIT_EARLY_END_S and FIT_EARLY_END_SHARE of it before the end."""
    if not lines or not duration or duration <= 0:
        return None
    last = max(float(d.get("end") or d.get("t") or 0.0) for d in lines)
    if last > duration + DURATION_SLACK_S:
        return f"the lines run to {last:.0f} s, the song is {duration:.0f} s"
    if duration - last > max(FIT_EARLY_END_S, FIT_EARLY_END_SHARE * duration):
        return f"the lines end at {last:.0f} s, the song runs to {duration:.0f} s"
    return None


def clean_title(title: str) -> str:
    """"La Perla (Official Video)" -> "La Perla", for LRCLIB's lookup."""
    return _NOISE.sub("", title or "").strip() or (title or "").strip()


def _length_s(text: Any) -> int | None:
    """"3:16" / "1:02:03" / 196 -> seconds."""
    if isinstance(text, (int, float)) and text > 0:
        return int(text)
    if not isinstance(text, str) or not re.fullmatch(r"\d+(:\d{1,2}){1,2}", text.strip()):
        return None
    total = 0
    for part in text.strip().split(":"):
        total = total * 60 + int(part)
    return total or None


def _get_json(url: str, timeout: float) -> Any:
    """GET ``url`` as JSON; None for a 404 ("not known"); raises OSError when unreachable."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                                               "Lrclib-Client": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:     # noqa: S310 - fixed https host
            return json.loads(resp.read(512 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise OSError(f"LRCLIB answered {exc.code}") from exc
    except ValueError as exc:
        raise OSError("LRCLIB sent something that isn't JSON") from exc


class Lrclib:
    """LRCLIB's lyrics for one song. ``get`` returns a page doc (see module), None
    when LRCLIB doesn't know the song, and raises OSError when it can't be reached."""

    def __init__(self, base: str = LRCLIB, timeout: float = LRCLIB_TIMEOUT_S, fetch: Any = None) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.fetch = fetch          # None: _get_json, looked up per call (tests swap it)

    def get(self, artist: str, title: str, album: str = "", duration: int | None = None) -> dict | None:
        title, artist = clean_title(title), (artist or "").strip()
        if not title or not artist:
            return None
        fetch = self.fetch or _get_json
        hit = None
        if album and duration:
            q = {"artist_name": artist, "track_name": title, "album_name": album, "duration": int(duration)}
            hit = fetch(f"{self.base}/get?{urllib.parse.urlencode(q)}", self.timeout)
            if not isinstance(hit, dict) or self._off_length(hit, duration):
                hit = None
        if hit is None or not (hit.get("syncedLyrics") or hit.get("plainLyrics")):
            q = {"artist_name": artist, "track_name": title}
            found = fetch(f"{self.base}/search?{urllib.parse.urlencode(q)}", self.timeout)
            hit = self._pick(found if isinstance(found, list) else [], duration) or hit
        if not isinstance(hit, dict):
            return None
        doc = self._doc(hit)
        if doc and doc["synced"] and timing_misfit(doc["lines"], duration):
            log.info("LRCLIB's timing for %s doesn't fit (%s): its words only", title,
                     timing_misfit(doc["lines"], duration))
            doc = {**doc, "synced": False, "lines": None}
        return doc

    @staticmethod
    def _off_length(hit: dict, duration: int | None) -> bool:
        """True when LRCLIB says ``hit`` is a recording of another length."""
        try:
            got = float(hit.get("duration") or 0)
        except (TypeError, ValueError):
            return False
        return bool(duration and got and abs(got - duration) > DURATION_SLACK_S)

    @staticmethod
    def _pick(found: list, duration: int | None) -> dict | None:
        """The closest-length hit, synced ones first; none further off than DURATION_SLACK_S."""
        best = None
        for item in found[:20]:
            if not isinstance(item, dict) or item.get("instrumental"):
                continue
            try:
                off = abs(float(item.get("duration") or 0) - duration) if duration else 0.0
            except (TypeError, ValueError):
                continue
            if duration and off > DURATION_SLACK_S:
                continue
            rank = (0 if item.get("syncedLyrics") else 1, off)
            if item.get("syncedLyrics") or item.get("plainLyrics"):
                if best is None or rank < best[0]:
                    best = (rank, item)
        return best[1] if best else None

    @staticmethod
    def _doc(hit: dict) -> dict | None:
        lines = parse_lrc(hit.get("syncedLyrics") or "")
        plain = hit.get("plainLyrics") if isinstance(hit.get("plainLyrics"), str) else None
        if not plain and lines:
            plain = "\n".join(d["text"] for d in lines)
        if not lines and not (plain and plain.strip()):
            return None
        return {"lyrics": plain.strip() if plain else None, "source": "LRCLIB",
                "synced": bool(lines), "lines": lines or None}


def track_facts(watch: Any) -> dict:
    """{artist, title, album, duration} of the first track of ``get_watch_playlist``."""
    tracks = watch.get("tracks") if isinstance(watch, dict) else None
    t = tracks[0] if isinstance(tracks, list) and tracks and isinstance(tracks[0], dict) else {}
    artists = [a.get("name") for a in t.get("artists") or [] if isinstance(a, dict) and a.get("name")]
    album = t.get("album") if isinstance(t.get("album"), dict) else {}
    return {"artist": ", ".join(artists[:1]), "title": str(t.get("title") or ""),
            "album": str(album.get("name") or ""), "duration": _length_s(t.get("length"))}
