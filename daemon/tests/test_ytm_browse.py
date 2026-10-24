"""The YouTube Music browser (ytm_browse.py) and preset references (ytm_refs.py,
ytm.py's press-time lookup, keymap validation), against a fake ytmusicapi.
"""

import json
import threading
import time

import pytest
from conftest import VARIANTS
from ytm_fake import (
    AFRICANA, CALMA, COFFEE, COFFEE_NEW, CUTE, DAILY_ES, FEATURED, LUX_MPRE, LUX_OLAK, OMEGA_MPRE, PERLA,
    RADIO_ID, ROSALIA, SHUFFLE_ID, TOP100_ES, TOP_SONGS, TRENDING_ES, FakeYTMusic, calls, fake_api, sign_in,
)

from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer, Preset
from haslerd.player.sources import ytm_browse as yb
from haslerd.player.sources import ytm_refs
from haslerd.player.sources.ytm import YtmError, YtmSource
from haslerd.player.sources.ytmusic import YtmCatalog
from haslerd.variant import load_variant
from haslerd.web import keymap

PL = "https://music.youtube.com/playlist?list={}"
WL = "https://music.youtube.com/watch?list={}"
ITEM_KEYS = {"type", "title", "subtitle", "thumb", "id", "preset", "radio", "explicit", "artists", "album",
             "uri", "name"}


@pytest.fixture
def catalog(tmp_path):
    return YtmCatalog(tmp_path, api=fake_api())


@pytest.fixture
def browse(catalog):
    return catalog.browse


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# -- items -------------------------------------------------------------------


def test_song_search_items_are_radio_presets_with_the_explicit_badge(browse):
    items = browse.search("rosalia", "songs")
    assert [i["id"] for i in items] == [PERLA, CUTE]                    # the one with no id is dropped
    perla = items[0]
    assert set(perla) == ITEM_KEYS
    assert perla["type"] == "song" and perla["explicit"] is True and items[1]["explicit"] is False
    assert perla["subtitle"] == "ROSALÍA, Yahritza Y Su Esencia · LUX"
    assert perla["thumb"].endswith("=w226-h226-l90-rj")
    assert perla["preset"] == {"source": "ytm", "kind": "radio", "seed": f"song:{PERLA}",
                               "uri": f"https://music.youtube.com/watch?v={PERLA}&list=RDAMVM{PERLA}",
                               "shuffle": False, "name": "La Perla radio"}
    assert perla["radio"] == perla["preset"]
    assert perla["album"] == {"id": LUX_MPRE, "title": "LUX"}
    assert perla["artists"][0] == {"id": ROSALIA, "name": "ROSALÍA"}
    assert perla["uri"] == perla["preset"]["uri"] and perla["name"] == "La Perla radio"   # older pages


def test_album_items_play_the_album_and_offer_its_radio(browse):
    lux, omega = browse.search("rosalia", "albums")
    assert lux["preset"] == {"source": "ytm", "kind": "album", "id": LUX_OLAK, "uri": PL.format(LUX_OLAK),
                             "shuffle": False, "name": "LUX"}
    assert lux["radio"]["seed"] == f"album:{LUX_OLAK}" and lux["radio"]["uri"] == WL.format("RDAMPL" + LUX_OLAK)
    assert lux["subtitle"] == "Album · ROSALÍA · 2025" and lux["explicit"] is True and lux["id"] == LUX_MPRE
    # No audio playlist in the result: looked up at press time by its MPREb_ id.
    assert omega["preset"]["id"] == OMEGA_MPRE and omega["preset"]["uri"] == "" and omega["radio"] is None


def test_artist_items_hold_top_songs_and_offer_the_artist_radio(browse):
    items = browse.search("rosalia", "artists")
    assert [i["title"] for i in items] == ["ROSALÍA", "Bad Bunny"]      # no UC id: dropped
    ros = items[0]
    assert ros["preset"] == {"source": "ytm", "kind": "artist", "id": ROSALIA, "mode": "top", "uri": "",
                             "shuffle": False, "name": "ROSALÍA"}
    assert ros["radio"]["mode"] == "radio" and ros["radio"]["uri"] == WL.format(RADIO_ID)
    assert ros["uri"] == WL.format(RADIO_ID) and ros["name"] == "ROSALÍA radio"   # older pages: the radio
    assert ros["thumb"].startswith("https://lh3.googleusercontent.com/") and ros["thumb"].endswith("=w226-h226-l90-rj")


def test_playlist_search_puts_youtubes_own_lists_first_and_community_apart(browse):
    items = browse.search("rosalia", "playlists")
    assert [i["id"] for i in items][:1] == [FEATURED]
    assert len({i["id"] for i in items}) == len(items) == 2              # deduplicated
    assert items[0]["preset"] == {"source": "ytm", "kind": "playlist", "id": FEATURED,
                                  "uri": PL.format(FEATURED), "shuffle": False, "name": "Presenting Rosalía"}
    community = browse.search("rosalia", "community")
    assert [i["title"] for i in community] == ["Rosalia, Lollapalooza 2019"]
    assert community[0]["thumb"].startswith("https://i.ytimg.com/")          # not googleusercontent: as is


def test_search_all_mixes_kinds_and_skips_what_cannot_be_played(browse):
    items = browse.search("rosalia", "all")
    assert [i["type"] for i in items] == ["artist", "song", "song", "album", "playlist"]   # video = song, no episode
    assert calls("search") == [("search", "rosalia", None)]


def test_search_is_cached_per_query_and_type(browse):
    browse.search("Rosalia", "songs")
    browse.search("  rosalia ", "songs")
    browse.search("rosalia", "albums")
    assert len(calls("search")) == 2


def test_search_cache_expires_and_is_bounded(catalog):
    clock = Clock()
    browse = yb.YtmBrowse(catalog, clock=clock)
    browse.search("a", "songs")
    clock.t += yb.TTL["search"] + 1
    browse.search("a", "songs")
    assert len(calls("search")) == 2
    for i in range(yb.SIZE["search"] + 5):
        browse._put("search", f"k{i}", clock.t, [])
    assert len(browse._caches["search"]) == yb.SIZE["search"]


def test_a_failed_search_is_empty_and_not_cached(browse):
    FakeYTMusic.fail = {"search"}
    assert browse.search("rosalia", "songs") == []
    FakeYTMusic.fail = set()
    assert len(browse.search("rosalia", "songs")) == 2


def test_bad_search_type_is_refused(browse):
    with pytest.raises(ValueError):
        browse.search("x", "videos")


def test_suggestions(browse):
    assert browse.suggest("rosal") == ["rosalia", "rosalia la perla", "rosalía"]
    browse.suggest("ROSAL")
    assert len(calls("get_search_suggestions")) == 1
    assert browse.suggest("  ") == []


def test_art_rewrites_googleusercontent_only():
    assert yb.art("https://yt3.googleusercontent.com/abc=s576") == "https://yt3.googleusercontent.com/abc=w226-h226-l90-rj"
    assert yb.art("https://i.ytimg.com/vi/x/hqdefault.jpg?sqp=a=b") == "https://i.ytimg.com/vi/x/hqdefault.jpg?sqp=a=b"
    assert yb.art("") == ""


def test_browse_calls_use_a_short_timeout_and_dont_queue_for_ever(catalog, monkeypatch):
    seen = {}
    real = catalog._call

    def spy(what, fn, default, **kw):
        seen.update(kw)
        return real(what, fn, default, **kw)

    monkeypatch.setattr(catalog, "_call", spy)
    catalog.browse.search("x", "songs")
    assert seen["timeout"] == 3.0 and seen["wait"] == 5.0
    # The lock held elsewhere (a long top-up): a browse call gives up, empty.
    monkeypatch.setattr(yb, "LOCK_WAIT_S", 0.05)
    holder = threading.Event()
    release = threading.Event()

    def hold():
        with catalog._lock:
            holder.set()
            release.wait(2)

    t = threading.Thread(target=hold)
    t.start()
    holder.wait(1)
    try:
        assert catalog.browse.search("other", "songs") == []
    finally:
        release.set()
        t.join()


# -- moods, charts, artists, albums --------------------------------------------


def test_mood_categories_come_in_spanish_from_their_own_client(browse):
    sections = browse.moods()
    assert [s["title"] for s in sections] == ["Estados de ánimo y momentos", "Géneros"]
    calma = sections[0]["items"][0]
    assert calma == {"type": "mood", "title": "Calma", "subtitle": "", "thumb": "", "id": CALMA, "preset": None,
                     "radio": None, "explicit": False, "artists": [], "album": None, "uri": "", "name": "Calma"}
    assert calls("get_mood_categories") == [("get_mood_categories", "es")]
    # Searching still uses the default (no language) client.
    assert browse.search("rosalia", "songs")


def test_moods_fall_back_to_english_names(browse, monkeypatch):
    real = FakeYTMusic.get_mood_categories

    def es_breaks(self):
        if self.language == "es":
            raise KeyError("es")
        return real(self)

    monkeypatch.setattr(FakeYTMusic, "get_mood_categories", es_breaks)
    assert [s["title"] for s in browse.moods()] == ["Moods & moments", "Genres"]


def test_a_mood_page_lists_presets_that_follow_reissues(browse):
    items = browse.mood(CALMA)
    assert [i["title"] for i in items] == ["Coffee Shop Blend", "Cafecito & Chill"]
    assert items[0]["preset"] == {"source": "ytm", "kind": "mood", "id": COFFEE, "title": "Coffee Shop Blend",
                                  "params": CALMA, "uri": PL.format(COFFEE), "shuffle": False,
                                  "name": "Coffee Shop Blend"}
    assert calls("_send_request") == [("_send_request", "browse", yb.MOODS_BROWSE, CALMA)]


def test_genre_pages_parse_despite_the_shelf_without_a_link(browse):
    items = browse.mood(AFRICANA)
    assert [i["title"] for i in items] == ["The Hits: 80s & Beyond Nigeria"]


def test_charts_are_chart_presets_with_the_country_list(browse):
    doc = browse.charts("es")
    assert doc["country"] == "ES" and doc["countries"] == ["ES", "MX", "US", "ZZ"]
    kinds = [(i["title"], i["preset"]["kind"], i["preset"].get("list")) for i in doc["items"]]
    assert kinds == [("Trending 20 Spain", "chart", "trending"), ("Daily Top Music Videos - Spain", "chart", "daily"),
                     ("Top 100 Music Videos Spain", "chart", "top100")]
    assert doc["items"][1]["preset"] == {"source": "ytm", "kind": "chart", "country": "ES", "list": "daily",
                                         "uri": PL.format(DAILY_ES), "shuffle": False,
                                         "name": "Daily Top Music Videos - Spain"}
    assert doc["items"][0]["type"] == "chart" and doc["artists"][0]["title"] == "Shakira"
    with pytest.raises(ValueError):
        browse.charts("Spain")


def test_other_chart_playlists_are_plain_playlists(browse):
    doc = browse.charts("US")
    assert [(i["title"][:12], i["preset"]["kind"]) for i in doc["items"]] == [
        ("Top 100 Live", "playlist"), ("Daily Top Mu", "chart"), ("Top 50 Jazz ", "playlist")]


def test_charts_and_moods_open_from_the_card_after_a_boot(tmp_path):
    first = YtmCatalog(tmp_path, api=fake_api())
    first.browse.charts("ES")
    first.browse.moods()
    first.browse.mood(CALMA)
    files = sorted(p.name for p in (tmp_path / "ytm-cache" / "browse").iterdir())
    assert files == ["charts-ES.json", "countries-all.json", f"mood-{CALMA}.json", "moods-all.json"]
    # A cold boot with no network: everything still there.
    again = YtmCatalog(tmp_path, api=fake_api())
    FakeYTMusic.fail = {"*"}
    assert [i["preset"]["list"] for i in again.browse.charts("ES")["items"]] == ["trending", "daily", "top100"]
    assert again.browse.charts("ES")["countries"] == ["ES", "MX", "US", "ZZ"]
    assert again.browse.moods()[0]["items"][0]["title"] == "Calma"
    assert again.browse.mood(CALMA)[0]["id"] == COFFEE


def test_an_old_disk_cache_is_shown_at_once_and_refreshed_behind(tmp_path):
    clock = Clock()
    first = YtmCatalog(tmp_path, api=fake_api())
    first.browse.clock = clock
    first.browse.mood(CALMA)
    later = YtmCatalog(tmp_path, api=fake_api())
    later.browse.clock = Clock(clock.t + 7 * 3600)        # past the 6 h
    FakeYTMusic.coffee = COFFEE_NEW
    assert later.browse.mood(CALMA)[0]["id"] == COFFEE      # the old answer, now
    deadline = time.time() + 3
    while time.time() < deadline and later.browse.mood(CALMA)[0]["id"] != COFFEE_NEW:
        time.sleep(0.02)
    assert later.browse.mood(CALMA)[0]["id"] == COFFEE_NEW  # and the new one once fetched
    assert json.loads((tmp_path / "ytm-cache" / "browse" / f"mood-{CALMA}.json").read_text())["value"][0]["id"] == COFFEE_NEW


def test_a_failure_keeps_the_last_good_answer(catalog):
    clock = Clock()
    browse = yb.YtmBrowse(catalog, clock=clock)
    assert browse.artist(ROSALIA)["title"] == "ROSALÍA"
    clock.t += 2 * 86400
    FakeYTMusic.fail = {"get_artist"}
    assert browse.artist(ROSALIA)["title"] == "ROSALÍA"


def test_artist_page(browse):
    doc = browse.artist(ROSALIA)
    assert doc["title"] == "ROSALÍA" and doc["subtitle"] == "36.2M monthly listeners"
    assert doc["preset"] == {"source": "ytm", "kind": "artist", "id": ROSALIA, "mode": "top",
                             "uri": PL.format(TOP_SONGS), "shuffle": False, "name": "ROSALÍA"}
    assert doc["radio"]["uri"] == WL.format(RADIO_ID) and doc["radio"]["mode"] == "radio"
    assert doc["shuffle"]["uri"] == WL.format(SHUFFLE_ID) and doc["shuffle"]["mode"] == "shuffle"
    assert [s["title"] for s in doc["songs"]] == ["La Perla", "DESPECHÁ"]
    assert doc["songs"][1]["artists"] == [{"id": ROSALIA, "name": "ROSALÍA"}]     # filled in from the page
    assert doc["albums"][0]["preset"]["id"] == LUX_OLAK and doc["singles"][0]["id"] == OMEGA_MPRE
    assert doc["related"][0]["title"] == "Aitana"
    browse.artist(ROSALIA)
    assert len(calls("get_artist")) == 1                                # cached a day
    with pytest.raises(ValueError):
        browse.artist("rosalia")


def test_album_page_by_either_id(browse):
    doc = browse.album(LUX_MPRE)
    assert doc["title"] == "LUX" and doc["year"] == "2025" and doc["duration"] == "49 minutes"
    assert doc["preset"]["id"] == LUX_OLAK and doc["radio"]["seed"] == f"album:{LUX_OLAK}"
    assert [t["title"] for t in doc["tracks"]] == ["Sexo, Violencia y Llantas", "Reliquia"]
    assert doc["tracks"][0]["album"] == {"id": LUX_MPRE, "title": "LUX"}
    assert doc["tracks"][1]["artists"] == [{"id": ROSALIA, "name": "ROSALÍA"}]
    assert doc["tracks"][0]["thumb"] == doc["thumb"]                    # tracks carry the album's art
    assert browse.album(LUX_OLAK)["title"] == "LUX"
    assert calls("get_album_browse_id") == [("get_album_browse_id", LUX_OLAK)]
    assert len(calls("get_album")) == 1                                 # same album, cached
    with pytest.raises(ValueError):
        browse.album("PLxyz")


def test_home_shelves(browse):
    shelves = browse.home()
    assert [s["title"] for s in shelves] == ["Quick picks", "New releases", "Featured playlists for you",
                                             "Mixed for you"]
    assert [s["items"][0]["type"] for s in shelves] == ["song", "album", "playlist", "playlist"]
    assert calls("get_home") == [("get_home", False)]


def test_home_is_personal_when_signed_in(catalog):
    catalog.browse.home()
    sign_in(catalog)
    catalog.browse.home()
    assert calls("get_home") == [("get_home", False), ("get_home", True)]


def test_library_needs_the_sign_in(browse):
    assert browse.library("liked") == []
    assert calls("get_liked_songs") == []
    with pytest.raises(ValueError):
        browse.library("everything")


def test_library_sections(catalog):
    sign_in(catalog)
    b = catalog.browse
    assert [i["id"] for i in b.library("liked")] == [PERLA, CUTE]
    playlists = b.library("playlists")
    assert [i["title"] for i in playlists] == ["Liked songs", "Kitchen"]
    assert playlists[0]["preset"] == {"source": "ytm", "kind": "liked", "uri": PL.format("LM"),
                                      "shuffle": False, "name": "Liked songs"}
    assert [i["id"] for i in b.library("history")] == [CUTE]
    assert [i["id"] for i in b.library("artists")] == [ROSALIA]
    assert [i["title"] for i in b.library("mixes")] == ["My Supermix"]


def test_related_and_lyrics(browse):
    shelves = browse.related(PERLA)
    assert [s["title"] for s in shelves] == ["You might also like", "Similar artists"]   # text shelves skipped
    assert calls("get_song_related") == [("get_song_related", "MPTRt_G6BH2uQgVxU-7")]
    lyr = browse.lyrics(PERLA)
    assert lyr["lyrics"].startswith("Hola, ladrón de paz") and lyr["source"] == "Source: Musixmatch"
    browse.lyrics(PERLA)
    assert len(calls("get_lyrics")) == 1
    with pytest.raises(ValueError):
        browse.lyrics("nope")


def test_strip_explicit():
    assert yb.strip_explicit([{"explicit": True}, {"explicit": False}, {}]) == [{"explicit": False}, {}]


# -- rating --------------------------------------------------------------------


def test_like_needs_the_sign_in(catalog):
    with pytest.raises(PermissionError):
        catalog.rate(PERLA, "LIKE")
    assert calls("rate_song") == []
    sign_in(catalog)
    assert catalog.rate(PERLA, "LIKE") is True
    assert catalog.rate(PERLA, "INDIFFERENT") is True
    assert calls("rate_song") == [("rate_song", PERLA, "LIKE"), ("rate_song", PERLA, "INDIFFERENT")]
    with pytest.raises(ValueError):
        catalog.rate(PERLA, "DISLIKE")
    FakeYTMusic.fail = {"rate_song"}
    assert catalog.rate(PERLA, "LIKE") is False


# -- references ----------------------------------------------------------------


def test_fixed_links():
    assert ytm_refs.link_for({"kind": "playlist", "id": "VLPLxyz12"}) == PL.format("PLxyz12")
    assert ytm_refs.link_for({"kind": "liked"}) == PL.format("LM")
    assert ytm_refs.link_for({"kind": "album", "id": LUX_OLAK}) == PL.format(LUX_OLAK)
    assert ytm_refs.link_for({"kind": "album", "id": LUX_MPRE}) == ""
    assert ytm_refs.link_for({"kind": "radio", "seed": f"song:{PERLA}"}) == \
        f"https://music.youtube.com/watch?v={PERLA}&list=RDAMVM{PERLA}"
    assert ytm_refs.link_for({"kind": "radio", "seed": f"album:{LUX_OLAK}"}) == WL.format("RDAMPL" + LUX_OLAK)
    for kind in ("artist", "chart", "mood"):
        assert ytm_refs.needs_lookup({"kind": kind})


def test_resolve_each_looked_up_kind(browse):
    assert browse.resolve({"kind": "artist", "id": ROSALIA, "mode": "top"}) == PL.format(TOP_SONGS)
    assert browse.resolve({"kind": "artist", "id": ROSALIA, "mode": "radio"}) == WL.format(RADIO_ID)
    assert browse.resolve({"kind": "artist", "id": ROSALIA, "mode": "shuffle"}) == WL.format(SHUFFLE_ID)
    assert len(calls("get_artist")) == 1                                # one artist page for all three
    assert browse.resolve({"kind": "chart", "country": "ES", "list": "daily"}) == PL.format(DAILY_ES)
    assert browse.resolve({"kind": "chart", "country": "ES", "list": "top100"}) == PL.format(TOP100_ES)
    assert browse.resolve({"kind": "chart", "country": "ES", "list": "trending"}) == PL.format(TRENDING_ES)
    assert browse.resolve({"kind": "chart", "country": "US", "list": "trending"}) is None
    assert browse.resolve({"kind": "mood", "params": CALMA, "title": "coffee  shop blend"}) == PL.format(COFFEE)
    assert browse.resolve({"kind": "mood", "params": CALMA, "title": "Gone"}) is None
    assert browse.resolve({"kind": "album", "id": LUX_MPRE}) == PL.format(LUX_OLAK)


def test_resolution_follows_a_reissued_mood_list_after_its_cache(catalog):
    clock = Clock()
    browse = yb.YtmBrowse(catalog, clock=clock)
    ref = {"kind": "mood", "params": CALMA, "title": "Coffee Shop Blend"}
    assert browse.resolve(ref) == PL.format(COFFEE)
    FakeYTMusic.coffee = COFFEE_NEW
    assert browse.resolve(ref) == PL.format(COFFEE)                     # 6 h cache
    clock.t += 6 * 3600 + 1
    # Moods are kept on the card: an old one answers at once (it nearly
    # always still plays) while the new page is fetched behind.
    assert browse.resolve(ref) == PL.format(COFFEE)
    deadline = time.time() + 3
    while time.time() < deadline and browse.resolve(ref) != PL.format(COFFEE_NEW):
        time.sleep(0.02)
    assert browse.resolve(ref) == PL.format(COFFEE_NEW)


# -- the ytm source: press-time lookup -------------------------------------------


class FakeMPD:
    def __init__(self):
        self.queue, self.calls = [], []

    def clear(self):
        self.queue = []

    def random(self, on):
        self.calls.append(("random", on))

    def add(self, uri):
        self.queue.append(uri)

    def addid(self, uri):
        self.queue.append(uri)
        return str(len(self.queue))

    def addtagid(self, *a):
        pass

    def play(self, *a):
        self.calls.append(("play", *a))

    def next(self):
        self.calls.append(("next",))

    def status(self):
        return {"state": "play", "playlistlength": str(len(self.queue)), "song": "0"}

    def currentsong(self):
        return {}


def runner(cmd, **kw):
    import subprocess
    if "-J" in cmd:
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"entries": [{"id": "zzzzzzzzzzz"}]}), "")
    return subprocess.CompletedProcess(cmd, 0, "https://rr1.googlevideo.com/x?expire=9999999999\n", "")


@pytest.fixture
def src(tmp_path):
    cat = YtmCatalog(tmp_path, api=fake_api())
    mpd = FakeMPD()
    s = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat, state_dir=tmp_path)
    s.mpd = mpd
    yield s
    s.close()


def chart_preset(**kw):
    spec = {"n": 4, "name": "Spain today", "ytm": "", "kind": "chart", "country": "ES", "list": "daily", **kw}
    return Preset.from_spec(spec)


def test_a_preset_without_a_link_parses_as_a_reference():
    p = chart_preset()
    assert p.source == "ytm" and p.uri == "" and p.kind == "chart"
    assert p.ytm_ref == {"kind": "chart", "country": "ES", "list": "daily"}
    hash(p)                                                             # still frozen and hashable
    old = Preset.from_spec({"n": 1, "name": "x", "ytm": PL.format("PLabc")})
    assert old.ref == () and old.kind == ""


def test_a_chart_preset_is_looked_up_when_pressed(src):
    seen = []
    src.on_preset_resolved = lambda p, link: seen.append((p.n, link))
    src.play(chart_preset())
    assert ("get_playlist", DAILY_ES) in FakeYTMusic.calls              # expanded from the looked-up list
    assert src.mpd.queue == [src.prefix + c * 11 for c in "abc"]
    assert seen == [(4, PL.format(DAILY_ES))]                           # written back to the preset's uri
    assert src._endless is None                                         # a chart is a playlist, not a radio
    src.save_state()
    state = json.loads((src._state_file).read_text())
    assert state["resolved"] == {"chart:ES:daily": PL.format(DAILY_ES)}


def test_the_link_is_not_written_back_when_the_preset_already_has_it(src):
    seen = []
    src.on_preset_resolved = lambda p, link: seen.append(link)
    src.play(chart_preset(ytm=PL.format(DAILY_ES)))
    assert seen == []


def test_with_youtube_music_down_the_fallback_link_plays_through_ytdlp(src):
    FakeYTMusic.fail = {"*"}
    src.play(chart_preset(ytm=PL.format(DAILY_ES)))
    assert src.mpd.queue == [src.prefix + "zzzzzzzzzzz"]                # yt-dlp expanded the fallback


def test_a_reference_remembers_its_link_across_a_reboot(tmp_path, src):
    src.play(chart_preset())
    src.save_state()
    src.close()
    FakeYTMusic.fail = {"*"}
    mpd = FakeMPD()
    again = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=YtmCatalog(tmp_path, api=None),
                      state_dir=tmp_path)
    again.catalog._api, again.catalog._import_failed = None, True       # no ytmusicapi at all now
    try:
        again.play(chart_preset())                                     # no uri in the preset itself
        assert mpd.queue                                               # the stale list, or yt-dlp's
        assert again._link(chart_preset(), lookup=False) == PL.format(DAILY_ES)
    finally:
        again.close()


def test_nothing_known_and_no_network_is_an_error_not_silence(src):
    FakeYTMusic.fail = {"*"}
    with pytest.raises(YtmError):
        src.play(chart_preset())


def test_artist_radio_and_shuffle_are_endless(src):
    FakeYTMusic.fail = set()
    p = Preset.from_spec({"n": 2, "name": "Rosalía radio", "ytm": "", "kind": "artist", "id": ROSALIA, "mode": "radio"})
    src.play(p)
    assert src._endless == WL.format(RADIO_ID)
    assert ("get_watch_playlist", None, RADIO_ID, True) in FakeYTMusic.calls
    p = Preset.from_spec({"n": 2, "name": "Rosalía shuffle", "ytm": "", "kind": "artist", "id": ROSALIA,
                          "mode": "shuffle"})
    src.play(p)
    assert src._endless == WL.format(SHUFFLE_ID)


def test_album_radio_is_endless_and_an_album_by_mpre_is_looked_up(src):
    src.play(Preset.from_spec({"n": 3, "name": "LUX radio", "ytm": "", "kind": "radio", "seed": f"album:{LUX_OLAK}"}))
    assert src._endless == WL.format("RDAMPL" + LUX_OLAK)
    src.play(Preset.from_spec({"n": 3, "name": "Omega", "ytm": "", "kind": "album", "id": LUX_MPRE}))
    assert ("get_playlist", LUX_OLAK) in FakeYTMusic.calls and src._endless is None


def test_warm_looks_references_up_in_the_background(src):
    p = chart_preset(list="trending")
    assert src.warm_once(p) is True
    assert src._link(p, lookup=False) == PL.format(TRENDING_ES)
    # warm()'s bookkeeping (and the moOde playlists) use the known link without asking.
    before = len(FakeYTMusic.calls)
    src._forget_files({src._link(p, lookup=False)})
    assert len(FakeYTMusic.calls) == before


def test_the_boot_cache_prune_leaves_the_browser_cache_alone(src, tmp_path):
    src.catalog.browse.charts("ES")
    src._prune()
    assert (tmp_path / "ytm-cache" / "browse" / "charts-ES.json").exists()


def test_again_is_next_track_for_every_reference_kind():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    variant.presets = [{"n": 1, "name": "Chart", "ytm": "", "kind": "chart", "country": "ES", "list": "daily"},
                       {"n": 2, "name": "Radio", "ytm": "", "kind": "artist", "id": ROSALIA, "mode": "radio"}]
    variant.actions = {"k1.tap": {"do": "preset", "n": 1}, "k2.tap": {"do": "preset", "n": 2}}
    player = FakePlayer()
    actions = Actions(variant, player)
    for key in ("k1", "k2"):
        player.calls.clear()
        actions.dispatch(Event(key, "tap"))
        actions.dispatch(Event(key, "tap"))
        assert player.verbs() == ["play_preset", "next_track"]


# -- keymap validation -------------------------------------------------------------


def check(raw):
    errors = []
    spec = keymap.preset_from_page({"n": 5, "name": "K", "source": "ytm", **raw}, "presets[0]", errors)
    return spec, errors


@pytest.mark.parametrize("raw, link", [
    ({"kind": "playlist", "id": "VLPLabcdef"}, PL.format("PLabcdef")),
    ({"kind": "playlist", "uri": PL.format("PLabcdef")}, PL.format("PLabcdef")),
    ({"kind": "album", "id": LUX_OLAK}, PL.format(LUX_OLAK)),
    ({"kind": "album", "id": LUX_MPRE}, ""),
    ({"kind": "artist", "id": ROSALIA}, ""),
    ({"kind": "artist", "id": ROSALIA, "mode": "radio", "uri": WL.format(RADIO_ID)}, WL.format(RADIO_ID)),
    ({"kind": "radio", "seed": f"song:{PERLA}"}, f"https://music.youtube.com/watch?v={PERLA}&list=RDAMVM{PERLA}"),
    ({"kind": "radio", "seed": f"album:{LUX_OLAK}"}, WL.format("RDAMPL" + LUX_OLAK)),
    ({"kind": "chart", "country": "es", "list": "top100"}, ""),
    ({"kind": "mood", "params": CALMA, "title": "Coffee Shop Blend", "uri": PL.format(COFFEE)}, PL.format(COFFEE)),
    ({"kind": "liked"}, PL.format("LM")),
])
def test_each_kind_validates_and_keeps_its_reference(raw, link):
    spec, errors = check(raw)
    assert errors == [] and spec["ytm"] == link and spec["kind"] == raw["kind"]
    preset = Preset.from_spec(spec)
    page = keymap.preset_to_page(preset)
    assert page["kind"] == raw["kind"] and page["uri"] == link
    again, errors = check({k: v for k, v in page.items() if k not in ("n", "name", "source")})
    assert errors == [] and again == spec                               # the page's round trip is lossless


def test_reference_defaults():
    spec, _ = check({"kind": "artist", "id": ROSALIA})
    assert spec["mode"] == "top"
    spec, _ = check({"kind": "chart"})
    assert spec["country"] == "ZZ" and spec["list"] == "daily"
    spec, _ = check({"kind": "chart", "country": "es"})
    assert spec["country"] == "ES"


@pytest.mark.parametrize("raw, field", [
    ({"kind": "jukebox"}, "kind"),
    ({"kind": "playlist"}, "uri"),
    ({"kind": "playlist", "id": "bad id!"}, "id"),
    ({"kind": "album", "id": "PLnotanalbum"}, "id"),
    ({"kind": "artist", "id": "rosalia"}, "id"),
    ({"kind": "artist", "id": ROSALIA, "mode": "loud"}, "mode"),
    ({"kind": "radio", "seed": "song:short"}, "seed"),
    ({"kind": "radio", "seed": f"artist:{ROSALIA}"}, "seed"),
    ({"kind": "chart", "country": "Spain"}, "country"),
    ({"kind": "chart", "list": "weekly"}, "list"),
    ({"kind": "mood", "params": CALMA}, "title"),
    ({"kind": "mood", "params": "<script>", "title": "x"}, "params"),
    ({"kind": "chart", "uri": "https://evil.example/x"}, "uri"),
])
def test_bad_references_are_refused_by_field(raw, field):
    spec, errors = check(raw)
    assert spec is None and errors and errors[0]["where"] == f"presets[0].{field}"


def test_shuffle_is_kept_for_lists_and_dropped_for_endless_radios():
    spec, _ = check({"kind": "chart", "shuffle": True})
    assert spec["shuffle"] is True
    spec, _ = check({"kind": "artist", "id": ROSALIA, "mode": "radio", "shuffle": True})
    assert "shuffle" not in spec


def test_old_link_presets_are_unchanged():
    spec, errors = check({"uri": PL.format("PLabc"), "shuffle": True})
    assert errors == [] and spec == {"n": 5, "name": "K", "ytm": PL.format("PLabc"), "shuffle": True}
