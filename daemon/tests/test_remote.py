"""The remote control: press, volume, next, standby, status keys, settings,
browse, the /api/ha extra-route plumbing and the fonts route. No network."""

import json
import os
import stat
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from conftest import VARIANTS

from haslerd import main
from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.state import StateStore
from haslerd.variant import Panel, load_variant
from haslerd.web import server as web_server
from haslerd.web.server import WebApp, WebServer, load_settings


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    player.config = SimpleNamespace(volume_limit=70)
    actions = Actions(variant, player)
    app = WebApp(
        actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True},
        ha=False, settings_file=tmp_path / "settings.json",
    )
    return app, actions, player


def call(url, method="GET", body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as err:
        return err.code, err.read(), err.headers


@pytest.fixture
def http(rig):
    app, *_ = rig
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", app
    server.close()


class TestPress:
    def test_tap_plays_the_keys_preset(self, rig):
        app, _, player = rig
        app.press({"key": "k1", "event": "tap"})
        assert player.playing.n == 1

    def test_tap_again_on_a_station_means_off(self, rig):
        app, actions, player = rig
        app.press({"key": "k2", "event": "tap"})
        app.press({"key": "k2", "event": "tap"})
        assert actions.standby is True and player.playing is None

    def test_tap_again_on_a_playlist_means_next_track(self, rig):
        app, _, player = rig
        app.press({"key": "k5", "event": "tap"})
        app.press({"key": "k5", "event": "tap"})
        assert player.verbs()[-1] == "next_track"

    def test_hold_dispatches_the_hold_action(self, rig):
        app, _, player = rig
        player.volume = 50.0
        app.press({"key": "k6", "event": "hold"})
        assert player.volume == 52.0
        app.press({"key": "pu", "event": "hold"})
        assert ("bluetooth_pair", 120) in player.calls

    def test_event_defaults_to_tap(self, rig):
        app, _, player = rig
        app.press({"key": "k3"})
        assert player.playing.n == 3

    @pytest.mark.parametrize("body", [{"key": "k9"}, {"key": "k1", "event": "double"}, {}, None])
    def test_unknown_key_or_event_is_rejected(self, rig, body):
        app, *_ = rig
        with pytest.raises(ValueError):
            app.press(body)

    def test_http(self, http):
        url, app = http
        status, body, _ = call(url + "/api/press", "POST", {"key": "k1", "event": "tap"})
        assert status == 200 and json.loads(body) == {"ok": True}
        assert app.actions.player.playing.n == 1
        status, body, _ = call(url + "/api/press", "POST", {"key": "zz", "event": "tap"})
        assert status == 400 and "zz" in json.loads(body)["error"]


class TestVolume:
    def test_value_is_clamped(self, rig):
        app, _, player = rig
        assert app.volume({"value": 55}) == 55 and player.volume == 55.0
        assert app.volume({"value": 140}) == 100 and player.volume == 100.0
        assert app.volume({"value": -3}) == 0 and player.volume == 0.0

    def test_delta_is_relative_and_clamped(self, rig):
        app, _, player = rig
        player.volume = 50.0
        assert app.volume({"delta": 5}) == 55
        assert app.volume({"delta": -80}) == 0
        player.volume = 98.0
        assert app.volume({"delta": 5}) == 100

    def test_delta_with_no_reading_starts_from_a_quiet_guess(self, rig):
        app, _, player = rig
        player.volume = None
        assert app.volume({"delta": 2}) == 42

    @pytest.mark.parametrize("body", [{}, {"value": "loud"}, {"value": True}, {"delta": None}])
    def test_bad_bodies(self, rig, body):
        app, *_ = rig
        with pytest.raises(ValueError):
            app.volume(body)

    def test_http(self, http):
        url, _ = http
        status, body, _ = call(url + "/api/volume", "POST", {"value": 30})
        assert status == 200 and json.loads(body) == {"ok": True, "volume": 30}
        assert call(url + "/api/volume", "POST", {"value": "x"})[0] == 400


class TestNextAndStandby:
    def test_next(self, http):
        url, app = http
        status, body, _ = call(url + "/api/next", "POST", {})
        assert status == 200 and json.loads(body) == {"ok": True}
        assert app.actions.player.verbs()[-1] == "next_track"

    def test_toggle_sleeps_then_wakes_and_resumes(self, rig):
        app, actions, player = rig
        app.press({"key": "k1"})
        assert app.set_standby({}) is True
        assert player.muted is True and player.playing is None
        assert app.set_standby({}) is False
        assert player.muted is False and player.playing.n == 1   # resume_last

    def test_explicit_on_off_are_idempotent(self, rig):
        app, actions, player = rig
        assert app.set_standby({"on": False}) is False
        assert "stop" not in player.verbs()
        assert app.set_standby({"on": True}) is True
        stops = player.verbs().count("stop")
        assert app.set_standby({"on": True}) is True
        assert player.verbs().count("stop") == stops
        assert app.set_standby({"on": False}) is False

    def test_bad_on_is_rejected(self, rig):
        app, *_ = rig
        with pytest.raises(ValueError):
            app.set_standby({"on": "yes"})

    def test_http(self, http):
        url, _ = http
        assert json.loads(call(url + "/api/standby", "POST", {"on": True})[1]) == {"ok": True, "standby": True}
        assert json.loads(call(url + "/api/standby", "POST", {})[1]) == {"ok": True, "standby": False}


class TestStatus:
    def test_keys_in_physical_order_with_names_and_sources(self, rig):
        app, *_ = rig
        keys = app.status()["keys"]
        assert [k["id"] for k in keys] == ["k1", "k2", "k3", "k4", "k5", "k6", "pu"]
        assert keys[0] == {"id": "k1", "label": "1", "n": 1, "name": "Radio Swiss Classic", "source": "radio"}
        assert keys[3]["source"] == "spotify" and keys[4]["source"] == "ytm"
        assert keys[6] == {"id": "pu", "label": "PU", "n": None, "name": "Next source", "source": None}

    def test_keys_follow_the_live_layout(self, rig):
        app, actions, _ = rig
        actions.variant.actions["k2.tap"] = {"do": "standby_toggle"}
        del actions.variant.actions["k3.tap"]
        keys = {k["id"]: k for k in app.status()["keys"]}
        assert keys["k2"]["name"] == "Standby" and keys["k2"]["n"] is None
        assert keys["k3"]["name"] == ""

    def test_current_key(self, rig):
        app, actions, _ = rig
        assert app.status()["current_key"] is None
        actions.dispatch(Event("k4", "tap"))
        assert app.status()["current_key"] == "k4"
        app.set_standby({"on": True})
        assert app.status()["current_key"] is None

    def test_volume_limit_and_existing_fields(self, rig):
        app, *_ = rig
        st = app.status()
        assert st["volume_limit"] == 70
        assert {"pressed", "playing", "standby", "volume", "source", "waiting", "renderer", "track"} <= set(st)


class TestSettings:
    def test_get(self, rig, monkeypatch):
        app, *_ = rig
        monkeypatch.setattr(app.wifi, "current_ssid", lambda: "Casa")
        doc = app.settings("hasler.local:8080")
        assert doc["volume_limit"] == 70 and doc["wifi"] == {"ssid": "Casa"}
        assert doc["version"].startswith("haslerd ")
        assert doc["moode_url"] == "http://hasler.local/"

    def test_put_persists_atomically_world_readable(self, rig, tmp_path, monkeypatch):
        app, _, player = rig
        monkeypatch.setattr(app.wifi, "current_ssid", lambda: "")
        (tmp_path / "settings.json").write_text(json.dumps({"other": 1}))
        app.put_settings({"volume_limit": 55})
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert saved == {"schema": 1, "other": 1, "volume_limit": 55}
        assert stat.S_IMODE(os.stat(tmp_path / "settings.json").st_mode) == 0o644
        assert not [p for p in tmp_path.iterdir() if p.name.startswith(".settings-")]
        assert player.config.volume_limit == 55

    def test_put_reapplies_the_knob_unless_in_standby(self, rig, monkeypatch):
        app, actions, player = rig
        monkeypatch.setattr(app.wifi, "current_ssid", lambda: "")
        player.volume = 40.0
        app.put_settings({"volume_limit": 60})
        assert player.calls[-1] == ("set_volume", 40.0)
        actions._standby(None, {})
        n = len(player.calls)
        app.put_settings({"volume_limit": 80})
        assert "set_volume" not in [c[0] for c in player.calls[n:]]
        assert player.config.volume_limit == 80

    @pytest.mark.parametrize("body", [{}, {"volume_limit": 29}, {"volume_limit": 101},
                                      {"volume_limit": 50.5}, {"volume_limit": "70"}, {"volume_limit": True}])
    def test_validation(self, rig, body):
        app, _, player = rig
        with pytest.raises(ValueError):
            app.put_settings(body)
        assert player.config.volume_limit == 70

    def test_http(self, http, monkeypatch):
        url, app = http
        monkeypatch.setattr(app.wifi, "current_ssid", lambda: "Casa")
        status, body, _ = call(url + "/api/settings")
        assert status == 200 and json.loads(body)["moode_url"] == "http://127.0.0.1/"
        status, body, _ = call(url + "/api/settings", "PUT", {"volume_limit": 65})
        doc = json.loads(body)
        assert status == 200 and doc["ok"] is True and doc["volume_limit"] == 65
        status, body, _ = call(url + "/api/settings", "PUT", {"volume_limit": 5})
        assert status == 400 and "30" in json.loads(body)["error"]

    def test_startup_load_overrides_yaml(self, tmp_path):
        web_server.save_settings(tmp_path / "settings.json", {"volume_limit": 45})
        settings = load_settings(tmp_path / "settings.json")
        assert main.moode_config({"moode": {"volume_limit": 70}}, settings).volume_limit == 45
        assert main.moode_config({"moode": {"volume_limit": 70}}, {}).volume_limit == 70
        assert main.moode_config({}, {"volume_limit": 5}).volume_limit == 70   # bad: ignored

    def test_unreadable_settings_are_empty(self, tmp_path):
        (tmp_path / "settings.json").write_text("{nope")
        assert load_settings(tmp_path / "settings.json") == {}
        assert load_settings(None) == {}


class FakeBrowse:
    def __init__(self, fail=False):
        self.fail = fail

    def sections(self):
        if self.fail:
            raise RuntimeError("boom")
        return [{"id": "s", "title": "S", "subtitle": "", "items": []}]

    def more_radio(self, q):
        return [{"type": "radio", "title": q}]


class TestBrowse:
    def test_none(self, rig):
        app, *_ = rig
        assert app.browse_doc() == {"available": False, "sections": []}
        assert app.browse_radio("x") == {"items": []}

    def test_with_catalog(self, http):
        url, app = http
        app.browse = FakeBrowse()
        doc = json.loads(call(url + "/api/browse")[1])
        assert doc["available"] is True and doc["sections"][0]["id"] == "s"
        assert json.loads(call(url + "/api/browse/radio?q=jazz")[1]) == {"items": [{"type": "radio", "title": "jazz"}]}

    def test_a_failing_catalog_is_unavailable(self, rig):
        app, *_ = rig
        app.browse = FakeBrowse(fail=True)
        assert app.browse_doc() == {"available": False, "sections": []}

    def test_build_browse_without_module(self, rig, monkeypatch):
        _, actions, _ = rig
        monkeypatch.setattr(main, "Catalog", None)
        assert main.build_browse(actions, None) is None

    def test_build_browse_passes_the_ytm_catalog(self, rig, monkeypatch, tmp_path):
        _, actions, player = rig
        seen = {}
        monkeypatch.setattr(main, "Catalog", lambda **kw: seen.update(kw) or FakeBrowse())
        ytm_catalog = object()
        player.sources = {"ytm": SimpleNamespace(catalog=ytm_catalog)}
        assert isinstance(main.build_browse(actions, tmp_path), FakeBrowse)
        assert seen["ytm_catalog"] is ytm_catalog and seen["data_dir"] == tmp_path
        assert callable(seen["radio_search"])


class FakeHaApi:
    def __init__(self, actions=None, state_dir=None, yaml_conf=None):
        self.args = (actions, state_dir, yaml_conf)
        self.seen = []
        self.loaded = False
        self.published = []
        self._connected = False

    def load(self):
        self.loaded = True
        self._connected = True

    @property
    def connected(self):
        return self._connected

    def publish(self, status):
        self.published.append(status)

    def route(self, method, path, body):
        self.seen.append((method, path, body))
        if path.startswith("/api/ha/nothing"):
            return None
        if path.startswith("/api/ha/bad"):
            return 400, {"ok": False, "error": "bad"}
        return 200, {"ok": True, "method": method}


class TestExtraRoutes:
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_methods_with_a_body(self, http, method):
        url, app = http
        fake = FakeHaApi()
        app.extra_routes.append(fake)
        status, body, _ = call(url + "/api/ha/config", method, {"x": 1})
        assert status == 200 and json.loads(body) == {"ok": True, "method": method}
        assert fake.seen == [(method, "/api/ha/config", {"x": 1})]

    def test_get_keeps_the_query_string(self, http):
        url, app = http
        fake = FakeHaApi()
        app.extra_routes.append(fake)
        status, _, _ = call(url + "/api/ha/entities?domain=light")
        assert status == 200 and fake.seen == [("GET", "/api/ha/entities?domain=light", None)]

    def test_status_codes_fallthrough_and_404(self, http):
        url, app = http
        first, second = FakeHaApi(), FakeHaApi()
        app.extra_routes += [first, second]
        assert call(url + "/api/ha/bad", "POST", {})[0] == 400
        assert call(url + "/api/ha/nothing")[0] == 404
        assert len(second.seen) == 1        # offered on after the first declined

    def test_no_extra_routes_is_404_and_bad_json_400(self, http):
        url, app = http
        assert call(url + "/api/ha/status")[0] == 404
        app.extra_routes.append(FakeHaApi())
        assert call(url + "/api/ha/x", "POST", raw=b"{nope")[0] == 400

    def test_other_paths_are_not_offered(self, http):
        url, app = http
        fake = FakeHaApi()
        app.extra_routes.append(fake)
        call(url + "/api/habitat")
        call(url + "/api/status")
        assert fake.seen == []

    def test_delete_ytm_account_still_works(self, http):
        url, app = http
        app.extra_routes.append(FakeHaApi())
        status, body, _ = call(url + "/api/ytm/account", "DELETE")
        assert status == 200 and json.loads(body)["ok"] is True

    def test_ha_flag_can_be_live(self, rig):
        app, *_ = rig
        state = {"on": False}
        app.ha = lambda: state["on"]
        assert app.config()["ha"] is False
        state["on"] = True
        assert app.config()["ha"] is True


class TestBuildWeb:
    def test_wires_ha_api_catalog_and_settings(self, rig, tmp_path, monkeypatch):
        _, actions, _ = rig
        monkeypatch.setattr(main, "HaApi", FakeHaApi)
        monkeypatch.setattr(main, "Catalog", lambda **kw: FakeBrowse())
        state = StateStore.at(tmp_path / "state.json")
        conf = {"web": {"enabled": False}, "home_assistant": {"url": "http://ha:8123"}}
        server, app = main.build_web(conf, actions, None, state, False)
        assert server is None
        ha_api = app.ha_api
        assert ha_api.loaded and ha_api.args == (actions, tmp_path, {"url": "http://ha:8123"})
        assert app.extra_routes[0] is ha_api and type(app.extra_routes[1]).__name__ == "SourcesApi"
        assert app.config()["ha"] is True
        assert app.settings_file == tmp_path / "settings.json"
        assert isinstance(app.browse, FakeBrowse)

    def test_without_ha_api_module(self, rig, tmp_path, monkeypatch):
        _, actions, _ = rig
        monkeypatch.setattr(main, "HaApi", None)
        monkeypatch.setattr(main, "Catalog", None)
        server, app = main.build_web({"web": {"enabled": False}}, actions, None, StateStore(None), True)
        assert app.ha_api is None and [type(r).__name__ for r in app.extra_routes] == ["SourcesApi", "GroupApi"] and app.config()["ha"] is True
        assert app.browse is None and app.settings_file is None

    def test_publisher_runs_off_loop_and_throttles(self, rig):
        app, *_ = rig
        app.ha_api = FakeHaApi()
        pub = main.HaPublisher(app, period_s=5.0)
        pub.tick(100.0)
        pub._thread.join(2)
        pub.tick(102.0)                      # too soon
        assert len(app.ha_api.published) == 1 and "keys" in app.ha_api.published[0]
        pub.tick(105.0)
        pub._thread.join(2)
        assert len(app.ha_api.published) == 2

    def test_publisher_without_ha_api_is_inert(self, rig):
        app, *_ = rig
        main.HaPublisher(None).tick(1.0)
        app.ha_api = None
        main.HaPublisher(app).tick(1.0)


class TestFonts:
    def test_served_with_type_and_long_cache(self, http, tmp_path, monkeypatch):
        url, _ = http
        fonts = tmp_path / "fonts"
        fonts.mkdir()
        (fonts / "Dial-Regular.woff2").write_bytes(b"wOF2data")
        monkeypatch.setattr(web_server, "FONTS", fonts)
        status, body, headers = call(url + "/fonts/Dial-Regular.woff2")
        assert status == 200 and body == b"wOF2data"
        assert headers["Content-Type"] == "font/woff2" and "max-age=31536000" in headers["Cache-Control"]

    @pytest.mark.parametrize("name", ["missing.woff2", "x.ttf", "..%2Fsecret.woff2", "a/b.woff2"])
    def test_refused(self, http, tmp_path, monkeypatch, name):
        url, _ = http
        monkeypatch.setattr(web_server, "FONTS", tmp_path)
        (tmp_path / "x.ttf").write_bytes(b"x")
        assert call(url + "/fonts/" + name)[0] == 404

    def test_pages_still_served(self, http):
        url, _ = http
        assert call(url + "/")[0] == 200
