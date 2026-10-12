"""The key-layout web page: translation, validation, live apply, persistence, HTTP."""

import json
import sqlite3
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap, stations
from haslerd.web.server import WebApp, WebServer

SPOTIFY_ID = "37i9dQZF1DXcBWIGoYBM5M"
YTM = "https://music.youtube.com/playlist?list=PL68AC80CBF3649BBB"


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    actions = Actions(variant, player)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True}, ha=False)
    return app, actions, player, panel


def doc_from(app):
    """The page's round trip: GET, edit, PUT."""
    cfg = app.config()
    return {"keys": [{k: v for k, v in key.items() if k != "label"} for key in cfg["keys"]], "presets": cfg["presets"]}


def key(doc, cid):
    return next(k for k in doc["keys"] if k["id"] == cid)


class TestConfigDoc:
    def test_keys_are_the_momentary_controls_left_to_right(self, rig):
        app, *_ = rig
        cfg = app.config()
        assert [k["id"] for k in cfg["keys"]] == ["k1", "k2", "k3", "k4", "k5", "k6", "pu"]
        assert [k["label"] for k in cfg["keys"]][-2:] == ["6", "PU"]

    def test_actions_come_out_in_the_page_vocabulary(self, rig):
        app, *_ = rig
        cfg = app.config()
        assert key(cfg, "k1")["tap"] == {"do": "preset", "n": 1}
        assert key(cfg, "k5")["hold"] == {"do": "volume_down"}
        assert key(cfg, "k6")["hold"] == {"do": "volume_up"}
        assert key(cfg, "pu")["hold"] == {"do": "bt_pair", "seconds": 120}

    def test_presets_listed_with_their_source(self, rig):
        app, *_ = rig
        by_n = {p["n"]: p for p in app.config()["presets"]}
        assert by_n[3]["name"] == "RNE Radio 3" and by_n[3]["source"] == "radio"
        assert by_n[5]["source"] == "ytm"

    def test_an_action_the_page_cannot_edit_is_shown_and_kept(self, rig):
        app, actions, *_ = rig
        actions.variant.actions["k2.hold"] = {"do": "mute", "state": True}
        doc = doc_from(app)
        assert key(doc, "k2")["hold"]["do"] == "custom"
        app.save(doc)
        assert actions.variant.actions["k2.hold"] == {"do": "mute", "state": True}


class TestSave:
    def test_a_changed_key_takes_effect_at_once(self, rig):
        app, actions, player, _ = rig
        doc = doc_from(app)
        key(doc, "k2")["tap"] = {"do": "standby_toggle"}
        app.save(doc)
        actions.dispatch(Event("k2", "tap"))
        assert actions.standby is True
        actions.dispatch(Event("k2", "tap"))
        assert actions.standby is False

    def test_spotify_web_links_become_uris(self, rig):
        app, actions, *_ = rig
        doc = doc_from(app)
        doc["presets"][0].update(source="spotify", uri=f"https://open.spotify.com/intl-es/playlist/{SPOTIFY_ID}?si=abc", shuffle=True)
        app.save(doc)
        p = actions.presets[1]
        assert (p.source, p.uri, p.shuffle) == ("spotify", f"spotify:playlist:{SPOTIFY_ID}", True)

    def test_none_removes_the_action(self, rig):
        app, actions, *_ = rig
        doc = doc_from(app)
        key(doc, "k1")["hold"] = {"do": "none"}
        key(doc, "k2")["hold"] = None
        app.save(doc)
        assert "k1.hold" not in actions.variant.actions and "k2.hold" not in actions.variant.actions

    def test_volume_hold_ramps_and_other_holds_do_not(self, rig):
        app, _, _, panel = rig
        doc = doc_from(app)
        key(doc, "k1")["hold"] = {"do": "volume_up"}
        key(doc, "k6")["hold"] = {"do": "none"}
        app.save(doc)
        controls = {c.id: c for c in panel.controls}
        assert controls["k1"].repeat_s == keymap.VOLUME_REPEAT_S
        assert controls["k6"].repeat_s is None

    def test_a_new_preset_can_be_added_and_bound(self, rig):
        app, actions, player, _ = rig
        doc = doc_from(app)
        doc["presets"].append({"n": 7, "name": "", "source": "ytm", "uri": YTM, "shuffle": False})
        key(doc, "pu")["hold"] = {"do": "preset", "n": 7}
        app.save(doc)
        assert actions.presets[7].name == "Preset 7"
        actions.dispatch(Event("pu", "hold"))
        assert player.playing.n == 7

    def test_saving_rewarms_the_player(self, rig):
        app, _, player, _ = rig
        app.save(doc_from(app))
        assert player.calls[-1][0] == "prefetch"


class TestValidation:
    @pytest.mark.parametrize("source, uri, message", [
        ("radio", "", "add a link"),
        ("radio", "stream.example/x", "http"),
        ("spotify", "https://example.com/playlist/x", "Spotify"),
        ("ytm", "https://music.youtube.com/watch?x=1", "YouTube Music"),
    ])
    def test_bad_links_are_named_by_field(self, rig, source, uri, message):
        app, *_ = rig
        doc = doc_from(app)
        doc["presets"][1].update(source=source, uri=uri)
        with pytest.raises(keymap.Invalid) as exc:
            app.save(doc)
        (err,) = exc.value.errors
        assert err["where"] == "presets[1].uri" and message in err["message"]

    def test_nothing_is_applied_or_written_when_anything_is_wrong(self, rig):
        app, actions, *_ = rig
        before = dict(actions.variant.actions)
        doc = doc_from(app)
        key(doc, "k2")["tap"] = {"do": "cycle_source"}
        key(doc, "k3")["tap"] = {"do": "preset", "n": 99}
        with pytest.raises(keymap.Invalid) as exc:
            app.save(doc)
        assert exc.value.errors[0]["where"] == "keys[2].tap"
        assert actions.variant.actions == before
        assert not app.keys_file.exists()

    def test_unknown_keys_and_verbs_are_refused(self, rig):
        app, *_ = rig
        doc = doc_from(app)
        doc["keys"].append({"id": "k9", "tap": None, "hold": None})
        key(doc, "k1")["tap"] = {"do": "shutdown"}
        with pytest.raises(keymap.Invalid) as exc:
            app.save(doc)
        assert {e["where"] for e in exc.value.errors} == {"keys[0].tap", "keys[7]"}

    def test_duplicate_preset_numbers_are_refused(self, rig):
        app, *_ = rig
        doc = doc_from(app)
        doc["presets"][1]["n"] = 1
        with pytest.raises(keymap.Invalid):
            app.save(doc)


class TestPersistence:
    def test_saved_layout_survives_a_restart(self, rig, tmp_path):
        app, *_ = rig
        doc = doc_from(app)
        key(doc, "k2")["tap"] = {"do": "cycle_source"}
        doc["presets"][2]["name"] = "Radio Tres"
        app.save(doc)

        # A fresh daemon: descriptor first, then the overlay on top.
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        actions = Actions(variant, FakePlayer())
        fresh = WebApp(actions, None, tmp_path / "keys.yaml", {}, ha=False)
        fresh.load_saved()
        assert actions.variant.actions["k2.tap"] == {"do": "cycle_source"}
        assert actions.presets[3].name == "Radio Tres"

    def test_reset_restores_the_descriptor_and_removes_the_file(self, rig):
        app, actions, *_ = rig
        doc = doc_from(app)
        key(doc, "k1")["tap"] = {"do": "cycle_source"}
        doc["presets"][0]["name"] = "Changed"
        app.save(doc)
        cfg = app.reset()
        assert not app.keys_file.exists()
        assert key(cfg, "k1")["tap"] == {"do": "preset", "n": 1}
        assert actions.presets[1].name == "Radio Swiss Classic"

    def test_an_unreadable_overlay_falls_back_to_the_descriptor(self, rig):
        app, actions, *_ = rig
        app.keys_file.write_text("presets: [unclosed")
        app.load_saved()
        assert actions.variant.actions["k1.tap"] == {"do": "preset", "n": 1}


class TestPlayAndStatus:
    def test_test_button_plays_the_preset_as_typed(self, rig):
        app, _, player, _ = rig
        app.play({"name": "Try", "source": "spotify", "uri": f"spotify:album:{SPOTIFY_ID}"})
        assert player.calls[-1] == ("play_preset", 0, "spotify")

    def test_test_button_refuses_a_bad_link(self, rig):
        app, *_ = rig
        with pytest.raises(keymap.Invalid):
            app.play({"source": "radio", "uri": "nope"})

    def test_status_reports_pressed_keys_and_what_plays(self, rig):
        app, actions, _, panel = rig
        k3 = next(c["pin"] for c in actions.variant.controls if c["id"] == "k3")
        panel.digital.set(k3, True)
        for t in range(10):
            panel.poll(t * 0.01)
        actions.dispatch(Event("k3", "tap"))
        st = app.status()
        assert st["pressed"] == ["k3"]
        assert st["playing"]["name"] == "RNE Radio 3"


def test_station_search_reads_moode_read_only(tmp_path):
    db = tmp_path / "moode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cfg_radio (station TEXT, name TEXT, genre TEXT, country TEXT)")
    con.executemany("INSERT INTO cfg_radio VALUES (?,?,?,?)", [
        ("http://srf/1", "Radio SRF 1", "News", "Switzerland"),
        ("http://bbc/3", "BBC Radio 3", "Classical", "United Kingdom"),
        ("local", "Not a stream", "", ""),
    ])
    con.commit()
    con.close()
    assert [s["name"] for s in stations.search_moode("swit", str(db))] == ["Radio SRF 1"]
    assert len(stations.search_moode("", str(db))) == 2
    assert stations.search_moode("x", str(tmp_path / "missing.db")) == []
    # Through the combined search too, with no curated list and no directory.
    offline = {"curated": tmp_path / "none.json", "directory": None}
    assert [s["name"] for s in stations.search("swit", str(db), **offline)] == ["Radio SRF 1"]
    assert stations.search("x", str(tmp_path / "missing.db"), **offline) == []


# -- the Spanish station search: curated list, moOde, Radio Browser ----------

@pytest.fixture(autouse=True)
def _no_directory(monkeypatch):
    """No test reaches radio-browser.info: the shared client is switched off."""
    monkeypatch.setattr(stations, "DIRECTORY", None)


@pytest.fixture
def curated(tmp_path):
    path = tmp_path / "es.json"
    path.write_text(json.dumps({"stations": [
        {"name": "RNE Radio Clásica", "url": "https://rne/r2.m3u8", "genre": "Classical", "region": "Spain · National"},
        {"name": "RAC1", "url": "https://rac/1.mp3", "genre": "News, talk", "region": "Spain · Catalonia",
         "keywords": "Catalunya"},
        {"name": "Catalunya Ràdio", "url": "https://ccma/cr.mp3", "genre": "News", "region": "Spain · Catalonia"},
    ]}), encoding="utf-8")
    return path


@pytest.fixture
def moode_db(tmp_path):
    db = tmp_path / "moode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cfg_radio (station TEXT, name TEXT, genre TEXT, country TEXT)")
    con.executemany("INSERT INTO cfg_radio VALUES (?,?,?,?)", [
        ("http://moode/clasica", "Radio Clasica Mundo", "Classical", "Peru"),
        ("https://rac/1.mp3", "RAC 1 (moOde copy)", "Talk", "Spain"),
    ])
    con.commit()
    con.close()
    return str(db)


def rb_station(name, url, country="Spain", **extra):
    return {"name": name, "url": url, "url_resolved": url, "country": country, "tags": "news,talk,live,more",
            "codec": "MP3", "bitrate": 128, "favicon": "", **extra}


class FakeDirectory:
    """A fetch for RadioBrowser: answers by countrycode, counts calls, can fail mirrors."""

    def __init__(self, spanish=(), everyone=(), dead=()):
        self.spanish, self.everyone, self.dead = list(spanish), list(everyone), set(dead)
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        if any(url.startswith(d) for d in self.dead):
            raise urllib.error.URLError("connection refused")
        return self.spanish if "countrycode=ES" in url else self.everyone


def directory(fake, **kw):
    return stations.RadioBrowser(fetch=fake, mirrors=("https://m1", "https://m2"), **kw)


def test_curated_matching_folds_accents_and_case(curated):
    names = lambda q: [s["name"] for s in stations.search_curated(q, curated)]
    assert names("rac") == ["RAC1"]
    assert names("catalunya") == ["RAC1", "Catalunya Ràdio"]  # RAC1 by region
    assert names("radio clasica") == ["RNE Radio Clásica"]
    assert names("rac 1") == ["RAC1"]  # spaces optional
    assert names("catalonia") == ["RAC1", "Catalunya Ràdio"]
    assert names("RÀDIO") == ["RNE Radio Clásica", "Catalunya Ràdio"]  # both fold to "radio"
    assert all(s["source"] == "curated" for s in stations.search_curated("", curated))


def test_moode_matching_folds_accents_too(moode_db):
    assert [s["name"] for s in stations.search_moode("clásica", moode_db)] == ["Radio Clasica Mundo"]


def test_search_orders_curated_moode_directory_and_dedupes(curated, moode_db):
    fake = FakeDirectory(
        spanish=[rb_station("RNE Clásica (directory)", "https://rne/r2.m3u8"),  # dup of curated
                 rb_station("Clásica FM Madrid", "https://cfm/stream")],
        everyone=[rb_station("Klassik Radio", "http://klassik/", country="Germany")],
    )
    found = stations.search("clasica", moode_db, curated=curated, directory=directory(fake))
    assert [(s["name"], s["source"]) for s in found] == [
        ("RNE Radio Clásica", "curated"),
        ("Radio Clasica Mundo", "moode"),
        ("Clásica FM Madrid", "radio-browser"),
        ("Klassik Radio", "radio-browser"),
    ]
    assert set(found[0]) >= {"name", "url", "genre", "country"}


def test_dedupe_ignores_scheme_and_trailing_slash(curated, moode_db):
    found = stations.search("rac", moode_db, curated=curated, directory=None)
    assert [s["name"] for s in found] == ["RAC1"]  # moOde's https://rac/1.mp3 is the same stream
    fake = FakeDirectory(spanish=[rb_station("RAC1", "http://rac/1.mp3/")])
    assert len(stations.search("rac", moode_db, curated=curated, directory=directory(fake))) == 1


def test_search_caps_results(curated, moode_db):
    fake = FakeDirectory(everyone=[rb_station(f"Clásica {i}", f"http://c/{i}") for i in range(80)])
    assert len(stations.search("clasica", moode_db, curated=curated, directory=directory(fake))) == stations.LIMIT


def test_directory_spanish_first_and_shape():
    fake = FakeDirectory(
        spanish=[rb_station("Cadena SER", "https://ser/live.mp3", favicon="https://ser/i.png")],
        everyone=[rb_station("SER World", "https://world/ser", country="Mexico"),
                  rb_station("Cadena SER", "https://ser/live.mp3"),      # already had it
                  rb_station("Bad", "rtmp://nope"),                       # not http(s)
                  {"name": "", "url_resolved": "http://anon/"}],          # no name
    )
    found = directory(fake).search("ser")
    assert [s["name"] for s in found] == ["Cadena SER", "SER World"]
    assert found[0] == {"name": "Cadena SER", "url": "https://ser/live.mp3", "genre": "news, talk, live",
                        "country": "Spain", "source": "radio-browser", "codec": "MP3", "bitrate": 128,
                        "favicon": "https://ser/i.png"}
    sent = fake.calls[0]
    assert "name=ser" in sent and "hidebroken=true" in sent and "order=clickcount" in sent and "reverse=true" in sent


def test_directory_uses_url_resolved():
    fake = FakeDirectory(spanish=[{"name": "X", "url": "http://x/playlist.pls", "url_resolved": "http://x/stream"}])
    assert directory(fake).search("xx")[0]["url"] == "http://x/stream"


def test_directory_caches_per_query():
    now = [1000.0]
    fake = FakeDirectory(spanish=[rb_station("COPE", "https://cope/net1.mp3")])
    rb = directory(fake, clock=lambda: now[0])
    rb.search("cope")
    rb.search("COPE ")  # same query, folded
    assert len(fake.calls) == 2  # one Spanish + one worldwide, once
    now[0] += stations.CACHE_TTL + 1
    rb.search("cope")
    assert len(fake.calls) == 4  # expired: asked again


def test_directory_falls_back_to_next_mirror_and_remembers_it():
    fake = FakeDirectory(spanish=[rb_station("COPE", "https://cope/net1.mp3")], dead=["https://m1"])
    rb = directory(fake)
    assert [s["name"] for s in rb.search("cope")] == ["COPE"]
    assert rb.server == "https://m2"
    fake.calls.clear()
    rb.search("onda cero")
    assert all(c.startswith("https://m2") for c in fake.calls)  # no time wasted on m1


def test_unreachable_directory_degrades_to_the_other_lists(curated, moode_db, caplog):
    caplog.set_level("INFO", logger="haslerd.web.stations")
    fake = FakeDirectory(dead=["https://m1", "https://m2"])
    rb = directory(fake)
    assert rb.search("rac") == []
    assert rb.cache == {}  # failures are not cached: the next keystroke retries
    found = stations.search("rac", moode_db, curated=curated, directory=rb)
    assert [s["name"] for s in found] == ["RAC1"]
    assert "radio-browser unreachable" in caplog.text


def test_half_an_answer_is_used_but_not_cached():
    def es_down(url, timeout):
        if "countrycode=ES" in url:
            raise urllib.error.URLError("reset")
        return [rb_station("COPE", "https://cope/net1.mp3")]

    rb = directory(es_down)
    assert [s["name"] for s in rb.search("cope")] == ["COPE"]
    assert rb.cache == {}


def test_slow_directory_is_cut_off_by_the_budget(curated, moode_db):
    import threading
    release = threading.Event()

    def slow(url, timeout):
        release.wait(5)
        return [rb_station("Late", "http://late/")]

    try:
        found = stations.search("rac", moode_db, curated=curated, directory=directory(slow), budget=0.05)
        assert [s["name"] for s in found] == ["RAC1"]
    finally:
        release.set()


def test_short_queries_skip_the_directory():
    fake = FakeDirectory(spanish=[rb_station("A", "http://a/")])
    assert directory(fake).search("a") == []
    assert fake.calls == []


def test_empty_query_is_the_curated_list(curated, moode_db):
    fake = FakeDirectory(spanish=[rb_station("Never", "http://never/")])
    found = stations.search("  ", moode_db, curated=curated, directory=directory(fake))
    assert [s["name"] for s in found] == ["RNE Radio Clásica", "RAC1", "Catalunya Ràdio"]
    assert fake.calls == []


def test_missing_curated_file_is_empty(tmp_path):
    assert stations.load_curated(tmp_path / "nope.json") == []


def test_shipped_curated_list_is_valid():
    doc = json.loads(stations.CURATED_FILE.read_text(encoding="utf-8"))
    entries = doc["stations"]
    assert len(entries) >= 30
    urls = [e["url"] for e in entries]
    for e in entries:
        assert e["name"].strip() and e["url"].startswith(("http://", "https://")), e
        assert e.get("genre") and e.get("region", "").startswith("Spain"), e
    assert len(urls) == len(set(urls)), "duplicate stream URL"
    assert len({e["name"] for e in entries}) == len(entries), "duplicate name"
    assert len(stations.load_curated()) == len(entries)  # nothing silently dropped


class TestHttp:
    @pytest.fixture
    def base(self, rig):
        app, *_ = rig
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        yield f"http://127.0.0.1:{server.port}", app
        server.close()

    def call(self, url, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()

    def test_page_and_config(self, base):
        url, _ = base
        status, body = self.call(url + "/api/config")
        assert status == 200 and json.loads(body)["variant"] == "hasler-hm"
        status, _ = self.call(url + "/nope")
        assert status == 404
        status, page = self.call(url + "/")
        assert status == 200 and b"/api/config" in page   # the page ships with the package

    def test_put_valid_and_invalid(self, base):
        url, app = base
        doc = doc_from(app)
        doc["presets"][0]["uri"] = ""
        status, body = self.call(url + "/api/config", "PUT", doc)
        assert status == 400 and json.loads(body)["errors"][0]["where"] == "presets[0].uri"
        doc["presets"][0]["uri"] = "https://stream.example/a"
        status, body = self.call(url + "/api/config", "PUT", doc)
        assert status == 200 and json.loads(body)["ok"] is True

    def test_play_stop_status(self, base):
        url, app = base
        status, _ = self.call(url + "/api/play", "POST", {"preset": {"source": "radio", "uri": "http://x/y"}})
        assert status == 200
        assert json.loads(self.call(url + "/api/status")[1])["playing"]["n"] == 0
        assert self.call(url + "/api/stop", "POST", {})[0] == 200

    def test_garbage_body_is_a_400_not_a_crash(self, base):
        url, _ = base
        req = urllib.request.Request(url + "/api/config", data=b"{not json", method="PUT")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 400


class TestSourceStatus:
    def test_pu_parked_on_bluetooth_reads_as_waiting(self, rig):
        app, actions, player, _ = rig
        actions.dispatch(Event("k1", "tap"))          # radio playing
        player.source = "radio"
        actions.dispatch(Event("pu", "tap"))          # -> bluetooth, MPD stopped
        player.source = "radio"     # like moOde: btactive stays 0 until a phone connects
        st = app.status()
        assert st["playing"] is None
        assert (st["source"], st["waiting"]) == ("bluetooth", True)

    def test_a_connected_phone_shows_its_name_and_track(self, rig):
        app, _, player, _ = rig
        player.source = "bluetooth"
        player.renderer_info = lambda: {"device": "Pixel 11", "title": "Song", "artist": "Band"}
        st = app.status()
        assert st["source"] == "bluetooth" and st["waiting"] is False
        assert st["renderer"]["device"] == "Pixel 11"

    def test_presets_report_their_own_source(self, rig):
        app, actions, *_ = rig
        actions.dispatch(Event("k4", "tap"))
        st = app.status()
        assert st["source"] == "spotify" and st["waiting"] is False


# -- YouTube Music --------------------------------------------------------


class FakeCatalog:
    available = True

    def __init__(self):
        self.signed_in = False
        self.pasted = None

    def search(self, q, kind):
        if kind not in ("songs", "artists", "albums", "playlists"):
            raise ValueError("search type must be one of: songs, artists, albums, playlists")
        if not q.strip():
            return []
        return [{"type": "song", "title": q, "subtitle": "Band", "thumb": "https://t/1",
                 "uri": "https://music.youtube.com/watch?v=aaaaaaaaaaa&list=RDAMVMaaaaaaaaaaa",
                 "name": f"{q} radio"}]

    def library(self):
        return [{"type": "playlist", "title": "Liked songs", "subtitle": "Your likes", "thumb": "",
                 "uri": "https://music.youtube.com/playlist?list=LM", "name": "Liked songs"}] if self.signed_in else []

    def account(self):
        return {"signed_in": self.signed_in, "name": "Owner" if self.signed_in else ""}

    def sign_in(self, headers):
        if "SAPISID" not in headers:
            raise ValueError("that cookie has no SAPISID (__Secure-3PAPISID)")
        self.pasted = headers
        self.signed_in = True
        return self.account()

    def sign_out(self):
        self.signed_in = False
        return self.account()


class FakeYtm:
    def __init__(self, catalog):
        self.catalog = catalog
        self.track = None

    def now_playing(self):
        return self.track


@pytest.fixture
def ytm_rig(rig):
    app, actions, player, panel = rig
    catalog = FakeCatalog()
    player.sources = {"ytm": FakeYtm(catalog)}
    app = WebApp(actions, panel, app.keys_file, {"radio": True, "spotify": True, "ytm": True}, ha=False)
    return app, catalog, player


class TestYtm:
    def test_catalog_comes_from_the_players_ytm_source(self, ytm_rig):
        app, catalog, _ = ytm_rig
        assert app.catalog is catalog

    def test_search_library_account(self, ytm_rig):
        app, catalog, _ = ytm_rig
        res = app.ytm_search("so what", "songs")
        assert res["available"] is True and res["results"][0]["name"] == "so what radio"
        assert app.ytm_search("", "songs") == {"available": True, "results": []}
        assert app.ytm_library() == {"available": True, "signed_in": False, "items": []}
        assert app.ytm_account() == {"available": True, "signed_in": False, "name": ""}
        assert app.ytm_sign_in({"headers": "Cookie: SAPISID=x"}) == {"signed_in": True, "name": "Owner"}
        assert app.ytm_library()["items"][0]["title"] == "Liked songs"
        assert app.ytm_sign_out() == {"signed_in": False, "name": ""}

    def test_without_a_catalog_everything_says_unavailable(self, rig):
        app, *_ = rig
        assert app.catalog is None
        assert app.ytm_search("x", "songs") == {"available": False, "results": []}
        assert app.ytm_library() == {"available": False, "signed_in": False, "items": []}
        assert app.ytm_account() == {"available": False, "signed_in": False, "name": ""}
        with pytest.raises(ValueError):
            app.ytm_sign_in({"headers": "x"})

    def test_status_names_the_ytm_track(self, ytm_rig):
        app, catalog, player = ytm_rig
        ytm = player.sources["ytm"]
        ytm.track = {"title": "So What", "artist": "Miles Davis", "album": "Kind Of Blue",
                     "thumb": "https://t/1", "videoId": "aaaaaaaaaaa"}
        player.source = "radio"
        assert app.status()["track"] is None
        player.source = "ytm"
        assert app.status()["track"] == {"title": "So What", "artist": "Miles Davis",
                                         "album": "Kind Of Blue", "thumb": "https://t/1"}
        ytm.track = None
        assert app.status()["track"] is None

    def test_http_endpoints(self, ytm_rig):
        app, catalog, _ = ytm_rig
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        base = f"http://127.0.0.1:{server.port}"
        call = TestHttp.call
        try:
            status, body = call(None, base + "/api/ytm/search?q=blue&type=albums")
            assert status == 200 and json.loads(body)["results"][0]["title"] == "blue"
            status, body = call(None, base + "/api/ytm/search?q=blue&type=videos")
            assert status == 400 and json.loads(body)["ok"] is False
            status, body = call(None, base + "/api/ytm/account", "POST", {"headers": "SID=1"})
            assert status == 400 and "SAPISID" in json.loads(body)["error"]
            secret = "Cookie: SAPISID=very-secret-value"
            status, body = call(None, base + "/api/ytm/account", "POST", {"headers": secret})
            assert status == 200
            assert json.loads(body) == {"ok": True, "account": {"signed_in": True, "name": "Owner"}}
            assert b"very-secret-value" not in body
            assert catalog.pasted == secret
            status, body = call(None, base + "/api/ytm/library")
            assert json.loads(body)["signed_in"] is True and len(json.loads(body)["items"]) == 1
            status, body = call(None, base + "/api/ytm/account", "DELETE")
            assert status == 200 and json.loads(body)["account"]["signed_in"] is False
            status, body = call(None, base + "/api/ytm/account")
            assert json.loads(body) == {"available": True, "signed_in": False, "name": ""}
            status, body = call(None, base + "/api/status")
            assert "track" in json.loads(body)
        finally:
            server.close()


class TestCoverArt:
    def test_art_redirects_to_the_sources_cover(self, rig):
        app, _, player, _ = rig
        player.sources = {"ytm": type("Y", (), {"cover": lambda self, vid: f"https://lh3.example/{vid}=w544"})()}
        assert app.cover("aaaaaaaaaaa") == "https://lh3.example/aaaaaaaaaaa=w544"

    def test_art_falls_back_to_youtubes_thumbnail_and_refuses_junk(self, rig):
        app, *_ = rig
        assert app.cover("aaaaaaaaaaa") == "https://i.ytimg.com/vi/aaaaaaaaaaa/hqdefault.jpg"
        assert app.cover("../etc/passwd") is None
        assert app.cover("") is None
