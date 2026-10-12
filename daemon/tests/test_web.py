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
        panel.digital.set("d2", True)
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
    assert [s["name"] for s in stations.search("swit", str(db))] == ["Radio SRF 1"]
    assert len(stations.search("", str(db))) == 2
    assert stations.search("x", str(tmp_path / "missing.db")) == []


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
