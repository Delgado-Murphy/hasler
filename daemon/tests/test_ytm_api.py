"""``/api/ytm/*`` (web/ytm_api.py) over a real YtmCatalog and a fake ytmusicapi."""

import json
import time
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS
from ytm_fake import (
    CALMA, COFFEE, CUTE, DAILY_ES, LUX_MPRE, LUX_OLAK, PERLA, RADIO_ID, ROSALIA, TOP_SONGS, FakeYTMusic, calls,
    fake_api, sign_in,
)

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer, Preset
from haslerd.player.sources.ytmusic import YtmCatalog
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.server import WebApp, WebServer

PL = "https://music.youtube.com/playlist?list={}"
WL = "https://music.youtube.com/watch?list={}"


class FakeYtm:
    """The bits of YtmSource the web page reads."""

    def __init__(self, catalog):
        self.catalog = catalog
        self.track = None
        self.on_preset_resolved = None

    def now_playing(self):
        return self.track

    def cover(self, vid):
        return "https://yt3.googleusercontent.com/cover=w120-h120-l90-rj"


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    catalog = YtmCatalog(tmp_path, api=fake_api())
    player.sources = {"ytm": FakeYtm(catalog)}
    actions = Actions(variant, player)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    return app, catalog, player


def get(app, path, **query):
    return app.ytm_api.get(path, {k: [v] for k, v in query.items()})


def playing(player, vid=PERLA):
    player.source = "ytm"
    player.sources["ytm"].track = {"title": "La Perla", "artist": "ROSALÍA", "album": "LUX",
                                   "thumb": "https://t/1", "videoId": vid, "explicit": True}


# -- GET -------------------------------------------------------------------------


def test_search(rig):
    app, *_ = rig
    status, doc = get(app, "/api/ytm/search", q="rosalia", type="songs")
    assert status == 200 and doc["available"] is True and doc["type"] == "songs"
    assert [i["id"] for i in doc["items"]] == [PERLA, CUTE] and doc["results"] == doc["items"]
    status, doc = get(app, "/api/ytm/search", q="rosalia")
    assert doc["type"] == "all" and [i["type"] for i in doc["items"]][:2] == ["artist", "song"]
    with pytest.raises(ValueError):
        get(app, "/api/ytm/search", q="x", type="videos")


def test_suggest_moods_mood(rig):
    app, *_ = rig
    assert get(app, "/api/ytm/suggest", q="rosal")[1] == {"available": True, "q": "rosal",
                                                          "suggestions": ["rosalia", "rosalia la perla", "rosalía"]}
    moods = get(app, "/api/ytm/moods")[1]
    assert moods["sections"][0]["title"] == "Estados de ánimo y momentos"
    params = moods["sections"][0]["items"][0]["id"]
    doc = get(app, "/api/ytm/mood", params=params)[1]
    assert doc["params"] == CALMA and doc["items"][0]["preset"]["kind"] == "mood"
    with pytest.raises(ValueError):
        get(app, "/api/ytm/mood", params="<x>")


def test_charts(rig):
    app, *_ = rig
    doc = get(app, "/api/ytm/charts")[1]
    assert doc["available"] and doc["country"] == "ES" and "US" in doc["countries"]
    assert doc["items"][1]["preset"]["uri"] == PL.format(DAILY_ES) and doc["artists"]
    with pytest.raises(ValueError):
        get(app, "/api/ytm/charts", country="Spain")


def test_artist_and_album(rig):
    app, *_ = rig
    doc = get(app, f"/api/ytm/artist/{ROSALIA}")[1]
    assert doc["found"] and doc["preset"]["uri"] == PL.format(TOP_SONGS) and doc["radio"]["uri"] == WL.format(RADIO_ID)
    assert {"songs", "albums", "singles", "related", "shuffle"} <= set(doc)
    doc = get(app, f"/api/ytm/album/{LUX_MPRE}")[1]
    assert doc["found"] and doc["preset"]["id"] == LUX_OLAK and len(doc["tracks"]) == 2
    with pytest.raises(ValueError):
        get(app, "/api/ytm/artist/nope")
    with pytest.raises(ValueError):
        get(app, "/api/ytm/album/nope")


def test_home_related_lyrics(rig):
    app, *_ = rig
    doc = get(app, "/api/ytm/home")[1]
    assert doc["signed_in"] is False and doc["sections"][0]["title"] == "Quick picks"
    doc = get(app, f"/api/ytm/related/{PERLA}")[1]
    assert doc["vid"] == PERLA and doc["sections"][0]["items"][0]["id"] == CUTE
    doc = get(app, f"/api/ytm/lyrics/{PERLA}")[1]
    assert doc["lyrics"].startswith("Hola") and doc["vid"] == PERLA
    with pytest.raises(ValueError):
        get(app, "/api/ytm/related/x")
    assert get(app, "/api/ytm/nothing")[0] == 404


def test_library(rig):
    app, catalog, _ = rig
    assert get(app, "/api/ytm/library", section="liked")[1] == {
        "available": True, "signed_in": False, "section": "liked", "items": []}
    sign_in(catalog)
    doc = get(app, "/api/ytm/library", section="liked")[1]
    assert [i["id"] for i in doc["items"]] == [PERLA, CUTE]
    assert doc["preset"] == {"source": "ytm", "kind": "liked", "uri": PL.format("LM"), "shuffle": False,
                             "name": "Liked songs"}
    doc = get(app, "/api/ytm/library")[1]                               # older pages: no section
    assert doc["section"] == "playlists" and doc["items"][0]["title"] == "Liked songs"
    with pytest.raises(ValueError):
        get(app, "/api/ytm/library", section="nope")


def test_everything_is_graceful_when_youtube_breaks(rig):
    app, catalog, _ = rig
    FakeYTMusic.fail = {"*"}
    sign_in(catalog)
    for path, query in [("/api/ytm/search", {"q": "x"}), ("/api/ytm/suggest", {"q": "x"}), ("/api/ytm/moods", {}),
                        ("/api/ytm/mood", {"params": CALMA}), ("/api/ytm/charts", {}),
                        (f"/api/ytm/artist/{ROSALIA}", {}), (f"/api/ytm/album/{LUX_MPRE}", {}),
                        ("/api/ytm/home", {}), ("/api/ytm/library", {"section": "history"}),
                        (f"/api/ytm/related/{PERLA}", {}), (f"/api/ytm/lyrics/{PERLA}", {})]:
        status, doc = get(app, path, **query)
        assert status == 200 and doc["available"] is True, path
        lists = [v for k, v in doc.items() if isinstance(v, list) and k != "countries"]
        assert all(v == [] for v in lists), path
    assert get(app, f"/api/ytm/artist/{ROSALIA}")[1]["found"] is False
    assert get(app, f"/api/ytm/lyrics/{PERLA}")[1]["lyrics"] is None


def test_without_a_catalogue_everything_says_unavailable(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    app = WebApp(Actions(variant, FakePlayer()), Panel.from_variant(variant, fake=True), None, {}, ha=False)
    for path in ("/api/ytm/search", "/api/ytm/suggest", "/api/ytm/moods", "/api/ytm/charts", "/api/ytm/home",
                 "/api/ytm/library", f"/api/ytm/artist/{ROSALIA}", f"/api/ytm/album/{LUX_MPRE}",
                 f"/api/ytm/related/{PERLA}", f"/api/ytm/lyrics/{PERLA}", "/api/ytm/account"):
        status, doc = get(app, path, q="x")
        assert status == 200 and doc["available"] is False, path
    assert get(app, "/api/ytm/mood", params=CALMA)[1]["available"] is False


# -- explicit ----------------------------------------------------------------------


def test_explicit_is_a_badge_until_the_owner_hides_it(rig):
    app, *_ = rig
    assert app.settings()["ytm"] == {"hide_explicit": False}
    items = get(app, "/api/ytm/search", q="rosalia", type="songs")[1]["items"]
    assert [i["explicit"] for i in items] == [True, False]
    app.put_settings({"ytm": {"hide_explicit": True}})
    assert app.settings()["ytm"] == {"hide_explicit": True}
    assert json.loads(app.settings_file.read_text())["ytm"] == {"hide_explicit": True}
    doc = get(app, "/api/ytm/search", q="rosalia", type="songs")[1]
    assert [i["id"] for i in doc["items"]] == [CUTE] and doc["results"] == doc["items"]
    assert [a["title"] for a in get(app, "/api/ytm/search", q="rosalia", type="albums")[1]["items"]] == ["Omega (feat. Ralphie Choo)"]
    album = get(app, f"/api/ytm/album/{LUX_MPRE}")[1]
    assert [t["title"] for t in album["tracks"]] == ["Sexo, Violencia y Llantas"]
    artist = get(app, f"/api/ytm/artist/{ROSALIA}")[1]
    assert [s["title"] for s in artist["songs"]] == ["DESPECHÁ"] and artist["albums"] == []
    related = get(app, f"/api/ytm/related/{PERLA}")[1]
    assert related["sections"][0]["items"][0]["id"] == CUTE
    assert len(calls("search")) == 2                                    # filtered from the cache, not refetched
    with pytest.raises(ValueError):
        app.put_settings({"ytm": {"hide_explicit": "yes"}})
    with pytest.raises(ValueError):
        app.put_settings({"ytm": {"other": True}})
    app.put_settings({"ytm": {"hide_explicit": False}})
    assert len(get(app, "/api/ytm/search", q="rosalia", type="songs")[1]["items"]) == 2


# -- POST ----------------------------------------------------------------------------


def test_start_a_radio(rig):
    app, _, player = rig
    status, doc = app.ytm_api.post("/api/ytm/radio", {"seed": f"song:{PERLA}", "name": "La Perla radio"})
    assert status == 200 and doc["preset"]["kind"] == "radio"
    assert doc["preset"]["uri"] == f"https://music.youtube.com/watch?v={PERLA}&list=RDAMVM{PERLA}"
    assert player.playing.kind == "radio" and player.playing.name == "La Perla radio"
    status, doc = app.ytm_api.post("/api/ytm/radio", {"seed": f"album:{LUX_OLAK}"})
    assert player.playing.uri == WL.format("RDAMPL" + LUX_OLAK)
    status, doc = app.ytm_api.post("/api/ytm/radio", {"seed": f"artist:{ROSALIA}"})
    assert player.playing.ytm_ref == {"kind": "artist", "id": ROSALIA, "mode": "radio"}
    with pytest.raises(keymap.Invalid):
        app.ytm_api.post("/api/ytm/radio", {"seed": "song:nope"})


def test_like_is_signed_in_and_now_playing_only(rig):
    app, catalog, player = rig
    status, doc = app.ytm_api.post("/api/ytm/like", {"vid": PERLA, "rating": "LIKE"})
    assert status == 403 and doc["signed_in"] is False
    sign_in(catalog)
    status, doc = app.ytm_api.post("/api/ytm/like", {"vid": PERLA, "rating": "LIKE"})
    assert status == 409                                                # nothing playing
    playing(player)
    status, doc = app.ytm_api.post("/api/ytm/like", {"vid": PERLA, "rating": "LIKE"})
    assert status == 200 and doc == {"ok": True, "vid": PERLA, "rating": "LIKE"}
    assert calls("rate_song") == [("rate_song", PERLA, "LIKE")]
    assert app.ytm_api.post("/api/ytm/like", {"vid": CUTE})[0] == 409  # not the song playing
    with pytest.raises(ValueError):
        app.ytm_api.post("/api/ytm/like", {"vid": PERLA, "rating": "DISLIKE"})
    FakeYTMusic.fail = {"rate_song"}
    assert app.ytm_api.post("/api/ytm/like", {"vid": PERLA, "rating": "INDIFFERENT"})[0] == 502


def test_a_like_may_name_the_song_that_just_ended(rig):
    app, catalog, player = rig
    sign_in(catalog)
    playing(player, PERLA)
    app.status()                                                        # the page saw La Perla
    playing(player, CUTE)                                               # ... and then the track changed
    assert app.ytm_api.post("/api/ytm/like", {"vid": PERLA})[0] == 200
    app.ytm_api.clock = lambda: time.monotonic() + 600
    assert app.ytm_api.post("/api/ytm/like", {"vid": PERLA})[0] == 409


def test_status_names_the_track_for_like_and_start_radio(rig):
    app, _, player = rig
    playing(player)
    assert app.status()["track"] == {"title": "La Perla", "artist": "ROSALÍA", "album": "LUX",
                                     "thumb": "https://t/1", "vid": PERLA, "explicit": True,
                                     "art": "https://yt3.googleusercontent.com/cover=w544-h544-l90-rj"}


# -- the resolved link written back --------------------------------------------------


def test_a_resolved_link_is_written_back_to_the_saved_preset(rig):
    app, _, _ = rig
    doc = {"presets": [{"n": 1, "name": "Spain", "source": "ytm", "kind": "chart", "country": "ES", "list": "daily"}],
           "keys": []}
    app.save(doc)
    preset = app.actions.presets[1]
    assert preset.uri == ""
    app._write_link(preset, PL.format(DAILY_ES))
    assert app.actions.presets[1].uri == PL.format(DAILY_ES)
    assert app.actions.presets[1].ytm_ref == preset.ytm_ref
    saved = keymap.load_layout(app.keys_file)[0]
    assert saved[0]["ytm"] == PL.format(DAILY_ES) and saved[0]["kind"] == "chart"
    assert app.config()["presets"][0]["uri"] == PL.format(DAILY_ES)
    # A preset since changed to something else is left alone.
    other = Preset(n=1, name="x", uri="", source="ytm", ref=(("country", "US"), ("kind", "chart"), ("list", "daily")))
    app._write_link(other, PL.format("PLother"))
    assert app.actions.presets[1].uri == PL.format(DAILY_ES)


def test_the_source_hook_is_wired(rig):
    app, _, player = rig
    assert player.sources["ytm"].on_preset_resolved == app._ytm_resolved


# -- over HTTP -------------------------------------------------------------------------


def call(url, method="GET", body=None, ctype="application/json"):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def test_http(rig):
    app, catalog, player = rig
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    base = f"http://127.0.0.1:{server.port}"
    try:
        status, doc = call(base + "/api/ytm/search?q=rosalia&type=albums")
        assert status == 200 and doc["items"][0]["title"] == "LUX"
        assert call(base + "/api/ytm/search?q=x&type=videos")[0] == 400
        assert call(base + f"/api/ytm/artist/{ROSALIA}")[1]["title"] == "ROSALÍA"
        assert call(base + f"/api/ytm/mood?params={CALMA}")[1]["items"][0]["id"] == COFFEE
        assert call(base + "/api/ytm/account")[1] == {"available": True, "signed_in": False, "name": ""}
        sign_in(catalog)
        playing(player)
        # Writes follow the page's CSRF rules: JSON only.
        assert call(base + "/api/ytm/like", "POST", {"vid": PERLA}, ctype="text/plain")[0] == 403
        assert calls("rate_song") == []
        status, doc = call(base + "/api/ytm/like", "POST", {"vid": PERLA, "rating": "LIKE"})
        assert status == 200 and doc["ok"] is True
        status, doc = call(base + "/api/ytm/radio", "POST", {"seed": f"song:{CUTE}"})
        assert status == 200 and doc["preset"]["seed"] == f"song:{CUTE}"
        assert call(base + "/api/ytm/nope", "POST", {})[0] == 404
        status, doc = call(base + "/api/play", "POST", {"preset": {
            "source": "ytm", "kind": "chart", "country": "ES", "list": "daily", "name": "Spain"}})
        assert status == 200 and player.playing.kind == "chart"
    finally:
        server.close()


# -- curated picks -------------------------------------------------------------


def test_curated_shelves(rig, tmp_path):
    app, *_ = rig
    doc = {"curator": "ytm", "verified": "2026-10-24", "entries": [
        {"title": "Flamenco Hits", "kind": "playlist", "id": "RDCLAK5uy_flamenco00000000000000000000000", "region": "Spain",
         "genres": ["world"], "moods": [], "decade": None, "why": "Duende, start to finish", "owner": "YouTube Music",
         "thumbnail": "https://yt3.googleusercontent.com/x=w544-h544-l90-rj", "tracks": 58},
        {"title": "Kind of Blue", "kind": "album", "id": LUX_OLAK, "region": "USA", "genres": ["jazz"], "why": "The one"},
        {"title": "Swinging Jazz", "kind": "playlist", "id": "VLPLswing", "genres": ["jazz"], "why": "Lift"},
        {"title": "dup", "kind": "playlist", "id": "PLswing", "genres": ["jazz"]},
        {"title": "", "id": "PLnotitle"}, {"title": "bad id", "id": "no spaces allowed"},
    ]}
    path = tmp_path / "ytm-curated.json"
    path.write_text(json.dumps(doc))
    app.ytm_api.curated_file = path
    status, got = get(app, "/api/ytm/curated")
    assert status == 200 and got["available"] is True
    # Shelves of fewer than four join "More picks"; each entry shows once.
    assert [s["title"] for s in got["sections"]] == ["More picks"]
    items = got["sections"][0]["items"]
    assert [i["title"] for i in items] == ["Flamenco Hits", "Kind of Blue", "Swinging Jazz"]
    first = items[0]
    assert first["subtitle"] == first["why"] == "Duende, start to finish" and first["tracks"] == 58
    assert first["preset"] == {"source": "ytm", "kind": "playlist", "id": "RDCLAK5uy_flamenco00000000000000000000000",
                               "uri": PL.format("RDCLAK5uy_flamenco00000000000000000000000"), "shuffle": False,
                               "name": "Flamenco Hits"}
    assert items[1]["type"] == "album" and items[1]["preset"]["kind"] == "album" and items[1]["preset"]["id"] == LUX_OLAK
    assert items[2]["preset"]["id"] == "PLswing"
    # Every preset is one a key can hold.
    for it in items:
        assert keymap.preset_from_page({**it["preset"], "n": 1}, "p", []) is not None


def test_curated_grouping_and_shipped_file():
    from haslerd.web.ytm_api import CURATED_FILE, curated_sections
    entries = [{"title": f"S{i}", "kind": "playlist", "id": f"PLspain{i}", "region": "Spain"} for i in range(4)]
    entries += [{"title": f"D{i}", "kind": "playlist", "id": f"PLdec{i}", "decade": "1980s", "genres": ["rock"]} for i in range(4)]
    secs = curated_sections({"entries": entries})
    assert [(s["title"], len(s["items"])) for s in secs] == [("Spain", 4), ("Decades", 4)]
    assert curated_sections({"entries": "nope"}) == [] and curated_sections(None) == []
    # The shipped list: about ten shelves, every entry a playable preset.
    shipped = curated_sections(json.loads(CURATED_FILE.read_text("utf-8")))
    assert 8 <= len(shipped) <= 14 and sum(len(s["items"]) for s in shipped) >= 150
    for s in shipped:
        for it in s["items"]:
            assert keymap.preset_from_page({**it["preset"], "n": 1}, "p", []) is not None, it["title"]


def test_curated_missing_file(rig, tmp_path):
    app, *_ = rig
    app.ytm_api.curated_file = tmp_path / "none.json"
    assert get(app, "/api/ytm/curated")[1]["sections"] == []
