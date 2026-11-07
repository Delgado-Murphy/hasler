"""Synced lyrics: YouTube Music's timed lines (both answer shapes), LRC (offsets,
several stamps a line, word times), the LRCLIB fallback over mocked HTTP, the
lyrics cache (memory, card, bounded, retried), and the song's position in
/api/status with the owner's offset."""

import json
import urllib.parse
from dataclasses import dataclass

import pytest
from conftest import VARIANTS
from ytm_fake import PERLA, FakeYTMusic, calls, fake_api

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.player.sources import lyrics as ly
from haslerd.player.sources import ytm_browse as yb
from haslerd.player.sources.ytm import position_from
from haslerd.player.sources.ytmusic import YtmCatalog
from haslerd.variant import Panel, load_variant
from haslerd.web.server import LYRICS_OFFSET_MS, WebApp


@dataclass
class LyricLine:            # ytmusicapi.models.lyrics.LyricLine (1.10+)
    text: str
    start_time: int
    end_time: int
    id: int


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeHttp:
    """LRCLIB over HTTP: answers by endpoint; records what left the radio."""

    def __init__(self, get=None, search=None, down=False):
        self.answers = {"get": get, "search": search}
        self.down = down
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.down:
            raise OSError("no route to host")
        return self.answers["get" if "/api/get?" in url else "search"]

    def params(self):
        return [dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(u).query)) for u in self.urls]


# -- YouTube Music's answer, both shapes -----------------------------------------


def test_timed_lines_from_dataclasses():
    raw = {"lyrics": [LyricLine("I was a liar", 9200, 10630, 1), LyricLine("♪", 10630, 12000, 2),
                      LyricLine("I gave in to the fire", 10680, 12540, 3)],
           "source": "Source: LyricFind", "hasTimestamps": True}
    lines, plain = ly.timed_from_ytm(raw)
    assert lines == [{"t": 9.2, "end": 10.63, "text": "I was a liar"},
                     {"t": 10.63, "end": 12.0, "text": ""},                 # an instrumental gap
                     {"t": 10.68, "end": 12.54, "text": "I gave in to the fire"}]
    assert plain == "I was a liar\n\nI gave in to the fire"


def test_timed_lines_from_dicts_and_the_raw_mobile_shape():
    dicts = {"lyrics": [{"text": "b", "start_time": 2000, "end_time": 3000, "id": 2},
                        {"text": "a", "start_time": 1000, "end_time": 2000, "id": 1}], "hasTimestamps": True}
    assert [d["text"] for d in ly.timed_from_ytm(dicts)[0]] == ["a", "b"]           # sorted by time
    raw = {"lyrics": [{"lyricLine": "Hola", "cueRange": {"startTimeMilliseconds": "1500",
                                                         "endTimeMilliseconds": "2500", "metadata": {"id": "1"}}}]}
    assert ly.timed_from_ytm(raw)[0] == [{"t": 1.5, "end": 2.5, "text": "Hola"}]


def test_plain_and_empty_answers():
    assert ly.timed_from_ytm({"lyrics": "Today is gonna be the day\n", "hasTimestamps": False}) == \
        (None, "Today is gonna be the day\n")
    assert ly.timed_from_ytm({"lyrics": "  "}) == (None, None)
    assert ly.timed_from_ytm(None) == (None, None)
    assert ly.timed_from_ytm({"lyrics": [{"text": "no time"}]}) == (None, None)


# -- LRC ---------------------------------------------------------------------------


def test_lrc_lines_offsets_and_several_stamps():
    lrc = ("[ar:ROSALÍA]\n[ti:La Perla]\n[offset:+500]\n"
           "[00:12.34]First line\n"
           "[00:20.00][01:05.5]Chorus line\n"
           "[00:15]Second, no fraction\n"
           "[1:02:03]mm:ss:xx, hundredths after a colon\n"
           "no stamp here\n"
           "[00:30.123]\n")
    lines = ly.parse_lrc(lrc)
    assert [(d["t"], d["text"]) for d in lines] == [
        (11.84, "First line"), (14.5, "Second, no fraction"), (19.5, "Chorus line"),
        (29.623, ""), (61.53, "mm:ss:xx, hundredths after a colon"), (65.0, "Chorus line")]
    assert all(d["end"] is None for d in lines)


def test_lrc_negative_offset_and_crlf():
    lines = ly.parse_lrc("[offset:-250]\r\n[00:01.00]a\r\n[00:00.10]b")
    assert [(d["t"], d["text"]) for d in lines] == [(0.35, "b"), (1.25, "a")]


def test_enhanced_lrc_word_times():
    lines = ly.parse_lrc("[00:10.00]<00:10.00>Hola <00:10.80>mundo <00:11.50>azul\n"
                         "[00:20.00][00:40.00]<00:20.00>Otra <00:20.50>vez")
    assert lines[0]["text"] == "Hola mundo azul"
    assert lines[0]["words"] == [{"t": 10.0, "w": "Hola "}, {"t": 10.8, "w": "mundo "}, {"t": 11.5, "w": "azul"}]
    assert [w["t"] for w in lines[2]["words"]] == [40.0, 40.5]                    # the repeat moves its words


def test_lrc_nothing_timed():
    assert ly.parse_lrc("just words\nno times") == []
    assert ly.parse_lrc(None) == []


def test_clean_title_and_facts():
    assert ly.clean_title("La Perla (Official Video)") == "La Perla"
    assert ly.clean_title("Song [Lyric Video] (feat. X)") == "Song (feat. X)"
    watch = {"tracks": [{"title": "La Perla", "length": "3:16", "artists": [{"name": "ROSALÍA"}, {"name": "Y"}],
                         "album": {"name": "LUX"}}]}
    assert ly.track_facts(watch) == {"artist": "ROSALÍA", "title": "La Perla", "album": "LUX", "duration": 196}
    assert ly.track_facts({}) == {"artist": "", "title": "", "album": "", "duration": None}


# -- LRCLIB ------------------------------------------------------------------------


def test_lrclib_get_sends_only_the_song_and_names_us():
    http = FakeHttp(get={"syncedLyrics": "[00:01.00]Hola\n[00:02.50]ladrón", "plainLyrics": "Hola\nladrón"})
    doc = ly.Lrclib(fetch=http).get("ROSALÍA", "La Perla (Official Video)", "LUX", 196)
    assert doc == {"lyrics": "Hola\nladrón", "source": "LRCLIB", "synced": True,
                   "lines": [{"t": 1.0, "end": None, "text": "Hola"}, {"t": 2.5, "end": None, "text": "ladrón"}]}
    assert http.params() == [{"artist_name": "ROSALÍA", "track_name": "La Perla", "album_name": "LUX", "duration": "196"}]
    assert http.urls[0].startswith("https://lrclib.net/api/get?")
    assert ly.USER_AGENT.startswith("Hasler-radio/")


def test_lrclib_search_when_get_misses_picks_the_closest_synced():
    found = [{"duration": 250, "syncedLyrics": "[00:01.00]too long"},
             {"duration": 197, "plainLyrics": "plain only"},
             {"duration": 195, "syncedLyrics": "[00:01.00]this one"},
             {"duration": 196, "instrumental": True}]
    http = FakeHttp(get=None, search=found)
    doc = ly.Lrclib(fetch=http).get("ROSALÍA", "La Perla", "LUX", 196)
    assert doc["lines"][0]["text"] == "this one" and len(http.urls) == 2
    assert set(http.params()[1]) == {"artist_name", "track_name"}


def test_lrclib_plain_only_unknown_and_down():
    http = FakeHttp(get={"syncedLyrics": None, "plainLyrics": "Solo texto"})
    assert ly.Lrclib(fetch=http).get("A", "B", "C", 100) == {"lyrics": "Solo texto", "source": "LRCLIB",
                                                              "synced": False, "lines": None}
    assert ly.Lrclib(fetch=FakeHttp(get=None, search=[])).get("A", "B", "C", 100) is None
    assert ly.Lrclib(fetch=FakeHttp()).get("", "B") is None                     # no artist: not asked
    with pytest.raises(OSError):
        ly.Lrclib(fetch=FakeHttp(down=True)).get("A", "B", "C", 100)


# -- YtmBrowse.lyrics: sources in order, the cache ----------------------------------


@pytest.fixture
def catalog(tmp_path):
    return YtmCatalog(tmp_path, api=fake_api())


def browse_with(catalog, http, clock=None):
    return yb.YtmBrowse(catalog, cache_dir=catalog.state_dir / "ytm-cache" / "browse",
                        clock=clock or Clock(), lrclib=ly.Lrclib(fetch=http))


def test_youtube_timed_lines_first_lrclib_not_asked(catalog):
    FakeYTMusic.timed = {"lyrics": [LyricLine("Hola", 1000, 2000, 1)], "source": "Source: LyricFind",
                         "hasTimestamps": True}
    http = FakeHttp()
    doc = browse_with(catalog, http).lyrics(PERLA)
    assert doc == {"lyrics": "Hola", "source": "Source: LyricFind", "synced": True,
                   "lines": [{"t": 1.0, "end": 2.0, "text": "Hola"}]}
    assert http.urls == []
    assert calls("get_lyrics") == [("get_lyrics", "MPLYt_G6BH2uQgVxU-7")]


def test_lrclib_when_youtube_has_no_times(catalog):
    http = FakeHttp(get={"syncedLyrics": "[00:03.00]Hola, ladrón de paz", "plainLyrics": "Hola, ladrón de paz"})
    doc = browse_with(catalog, http).lyrics(PERLA)
    assert doc["synced"] and doc["source"] == "LRCLIB" and doc["lines"][0]["t"] == 3.0
    # Only the song's facts (from YouTube's watch list) left the radio.
    assert http.params()[0] == {"artist_name": "ROSALÍA", "track_name": "La Perla", "album_name": "LUX",
                                "duration": "196"}


def test_plain_youtube_lyrics_when_nobody_has_times(catalog):
    doc = browse_with(catalog, FakeHttp(get={"plainLyrics": "otra letra"})).lyrics(PERLA)
    assert doc == {"lyrics": "Hola, ladrón de paz\r\n...", "source": "Source: Musixmatch", "synced": False, "lines": None}


def test_an_older_ytmusicapi_without_timestamps(catalog, monkeypatch):
    def old(self, browse_id):
        self._rec("get_lyrics", browse_id)
        return {"lyrics": "vieja", "source": "Source: X"}
    monkeypatch.setattr(FakeYTMusic, "get_lyrics", old)
    doc = browse_with(catalog, FakeHttp(get=None, search=[])).lyrics(PERLA)
    assert doc["lyrics"] == "vieja" and not doc["synced"]


def test_whole_answers_are_kept_on_the_card_and_read_back_offline(catalog):
    http = FakeHttp(get={"syncedLyrics": "[00:03.00]Hola"})
    clock = Clock()
    browse_with(catalog, http, clock).lyrics(PERLA)
    path = catalog.state_dir / "ytm-cache" / "browse" / f"lyrics-{PERLA}.json"
    saved = json.loads(path.read_text())
    assert saved["key"] == PERLA and saved["value"]["synced"] and "_whole" not in saved["value"]
    # After a reboot, offline: from the card, nobody asked.
    FakeYTMusic.fail = {"*"}
    again = browse_with(catalog, FakeHttp(down=True), clock)
    assert again.lyrics(PERLA)["lines"][0]["text"] == "Hola"
    # Older than LYRICS_TTL and YouTube unreachable: the old answer still shows.
    clock.t += yb.LYRICS_TTL + 1
    assert again.lyrics(PERLA)["lines"][0]["text"] == "Hola"


def test_lrclib_down_is_kept_briefly_and_not_on_the_card(catalog):
    clock = Clock()
    http = FakeHttp(down=True)
    b = browse_with(catalog, http, clock)
    assert b.lyrics(PERLA)["lyrics"].startswith("Hola")                          # plain YouTube meanwhile
    assert not (catalog.state_dir / "ytm-cache" / "browse" / f"lyrics-{PERLA}.json").exists()
    b.lyrics(PERLA)
    assert len(http.urls) == 1                                                   # kept for a while
    http.down = False
    http.answers["get"] = {"syncedLyrics": "[00:01.00]Ya"}
    clock.t += yb.LYRICS_RETRY_S + 1
    assert b.lyrics(PERLA)["synced"]


def test_the_card_keeps_only_the_newest(catalog, monkeypatch):
    monkeypatch.setattr(yb, "LYRICS_DISK_MAX", 3)
    b = browse_with(catalog, FakeHttp(get={"syncedLyrics": "[00:01.00]x"}))
    folder = catalog.state_dir / "ytm-cache" / "browse"
    folder.mkdir(parents=True)
    for i in range(5):
        (folder / f"lyrics-old{i:08d}.json").write_text("{}")
    b.lyrics(PERLA)
    left = sorted(p.name for p in folder.glob("lyrics-*.json"))
    assert len(left) == 3 and f"lyrics-{PERLA}.json" in left


# -- the position in /api/status ------------------------------------------------------


def test_position_from_mpd_status():
    assert position_from({"state": "play", "elapsed": "12.5", "duration": "200.25"}, 99.0) == \
        {"elapsed_s": 12.5, "duration_s": 200.25, "state": "play", "at": 99.0}
    assert position_from({"state": "pause", "time": "61:180"}, 1.0) == \
        {"elapsed_s": 61.0, "duration_s": 180.0, "state": "pause", "at": 1.0}
    assert position_from({"state": "play", "elapsed": "3.0"}, 1.0)["duration_s"] is None     # a live stream
    assert position_from({"state": "stop"}, 1.0) is None
    assert position_from({"state": "play"}, 1.0) is None
    assert position_from(None, 1.0) is None


class FakeYtm:
    def __init__(self):
        self.track = None
        self.on_preset_resolved = None
        self.catalog = YtmCatalog(None, api=fake_api())

    def now_playing(self):
        return self.track

    def cover(self, vid):
        return ""


@pytest.fixture
def app(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    player = FakePlayer()
    player.sources = {"ytm": FakeYtm()}
    a = WebApp(Actions(variant, player), Panel.from_variant(variant, fake=True), tmp_path / "keys.yaml",
               {"radio": True, "ytm": True}, ha=False, settings_file=tmp_path / "settings.json")
    return a, player


def test_status_has_the_position_and_the_offset(app):
    a, player = app
    assert a.status()["position"] is None                                        # nothing playing
    player.source = "ytm"
    player.sources["ytm"].track = {"title": "La Perla", "artist": "ROSALÍA", "album": "LUX", "thumb": "",
                                   "videoId": PERLA, "position": {"elapsed_s": 42.5, "duration_s": 196.0,
                                                                  "state": "play", "at": 123.0}}
    st = a.status()
    assert st["position"] == {"elapsed_s": 42.5, "duration_s": 196.0, "state": "play", "at": 123.0,
                              "offset_ms": LYRICS_OFFSET_MS}
    assert "position" not in st["track"]
    a.put_settings({"ytm": {"lyrics_offset_ms": -150}})
    assert a.status()["position"]["offset_ms"] == -150
    assert a.settings()["ytm"] == {"hide_explicit": False, "lyrics_offset_ms": -150}
    a.put_settings({"ytm": {"hide_explicit": True}})                             # one doesn't drop the other
    assert a.settings()["ytm"] == {"hide_explicit": True, "lyrics_offset_ms": -150}


@pytest.mark.parametrize("bad", [5000, -3001, 1.5, "100", True, None])
def test_the_offset_is_checked(app, bad):
    a, _ = app
    with pytest.raises(ValueError):
        a.put_settings({"ytm": {"lyrics_offset_ms": bad}})


def test_a_hand_edited_offset_out_of_range_falls_back(app):
    a, _ = app
    a.settings_file.write_text(json.dumps({"schema": 1, "ytm": {"lyrics_offset_ms": 99999}}))
    assert a.ytm_settings()["lyrics_offset_ms"] == LYRICS_OFFSET_MS
