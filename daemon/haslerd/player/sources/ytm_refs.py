"""What a YouTube Music key can hold, beyond a link: the preset ``kind``.

A ytm preset used to be one music.youtube.com link. Some things worth a key
are not one fixed link: "this week's Spain chart", "Rosalía's top songs",
"the Calma mood list YouTube keeps re-issuing". So a ytm preset may also carry
a reference, resolved to a link when it is pressed (ytm.py) and kept working
when ytmusicapi is down by the link it was last resolved to (``uri``):

    kind      fields                          plays
    playlist  id (PL…/RDCLAK…/VL…) or uri     the playlist        (no kind = this)
    album     id (OLAK5uy_… or MPREb_…)       the album
    artist    id (UC…), mode top|radio|shuffle  top songs / artist radio / shuffle
    radio     seed song:<vid> | album:<OLAK>  an endless radio from that song/album
    chart     country (ES, ZZ = global), list daily|top100|trending
    mood      title + params (a Moods & genres page)  that page's list of that title
    liked     -                               the owner's Liked songs (signed in)

Pure functions and patterns only: keymap.py validates with them, ytm.py and
ytm_browse.py build and resolve with them.
"""

from __future__ import annotations

import re

BASE = "https://music.youtube.com"
SONG_RADIO = BASE + "/watch?v={vid}&list=RDAMVM{vid}"
WATCH_LIST = BASE + "/watch?list={id}"
PLAYLIST = BASE + "/playlist?list={id}"
LIKED = "LM"

KINDS = ("playlist", "album", "artist", "radio", "chart", "mood", "liked")
ARTIST_MODES = ("top", "radio", "shuffle")
CHART_LISTS = ("daily", "top100", "trending")
# Every field a reference may carry, besides the link (``uri``) and shuffle.
REF_KEYS = ("kind", "id", "mode", "seed", "country", "list", "title", "params")

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
LIST_ID = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
OLAK_ID = re.compile(r"^OLAK5uy_[A-Za-z0-9_-]{20,60}$")
MPRE_ID = re.compile(r"^MPREb_[A-Za-z0-9_-]{5,40}$")
CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
COUNTRY = re.compile(r"^[A-Z]{2}$")
# Moods & genres page tokens are base64-ish protobuf; nothing else gets in.
PARAMS = re.compile(r"^[A-Za-z0-9_%=+/-]{4,400}$")
SEED = re.compile(r"^(song|album):([A-Za-z0-9_-]{2,64})$")


def strip_vl(pid: str) -> str:
    return pid[2:] if pid.startswith("VL") else pid


def norm(title: str) -> str:
    return " ".join(str(title or "").split()).casefold()


def link_for(ref: dict) -> str:
    """The link a reference always means, or "" when it has to be looked up
    (artist, chart, mood, an album known only by MPREb_ id)."""
    kind = ref.get("kind") or "playlist"
    rid = str(ref.get("id") or "")
    if kind == "playlist":
        pid = strip_vl(rid)
        return PLAYLIST.format(id=pid) if LIST_ID.match(pid) else ""
    if kind == "liked":
        return PLAYLIST.format(id=LIKED)
    if kind == "album":
        return PLAYLIST.format(id=rid) if OLAK_ID.match(rid) else ""
    if kind == "radio":
        m = SEED.match(str(ref.get("seed") or ""))
        if not m:
            return ""
        what, sid = m.groups()
        if what == "song" and VIDEO_ID.match(sid):
            return SONG_RADIO.format(vid=sid)
        if what == "album" and OLAK_ID.match(sid):
            return WATCH_LIST.format(id="RDAMPL" + sid)
        return ""
    return ""


def needs_lookup(ref: dict) -> bool:
    """True for references whose link comes from YouTube Music at press time."""
    kind = ref.get("kind")
    if kind in ("artist", "chart", "mood"):
        return True
    return kind == "album" and bool(MPRE_ID.match(str(ref.get("id") or "")))


def ref_key(ref: dict) -> str:
    """A stable name for what a reference points at (cache and state keys)."""
    kind = ref.get("kind") or "playlist"
    if kind == "artist":
        return f"artist:{ref.get('id', '')}:{ref.get('mode') or 'top'}"
    if kind == "chart":
        return f"chart:{ref.get('country') or 'ZZ'}:{ref.get('list') or 'daily'}"
    if kind == "mood":
        return f"mood:{ref.get('params', '')}:{norm(ref.get('title', ''))}"
    if kind == "radio":
        return f"radio:{ref.get('seed', '')}"
    return f"{kind}:{ref.get('id', '')}"


def chart_list(title: str, playlist_id: str = "") -> str | None:
    """Which of our chart lists a get_charts video playlist is (English titles:
    the client runs without a language), or None for the others."""
    t = norm(title)
    if t.startswith("trending 20"):
        return "trending"
    if t.startswith("daily top music videos"):
        return "daily"
    if t.startswith("top 100 music videos"):
        return "top100"
    return None
