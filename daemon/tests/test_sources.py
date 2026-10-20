"""The configurable PU cycle, resume after a cast, moOde's casting targets and
/api/sources + /api/casting. No Pi, no moOde: a temp sqlite db and fakes."""

import json
import sqlite3
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS

from haslerd.actions import CAST_END_GRACE_S, SOURCE_CYCLE, Actions, cycle_from
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.player.renderers import (MoodeRenderers, RendererError, parse_session,
                                      spotify_device_name, valid_name, worker_idle)
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, WebServer
from haslerd.web.sources_api import SourcesApi, apply_saved

MIXED = [
    {"n": 1, "name": "Radio", "radio": "http://example.invalid/r"},
    {"n": 2, "name": "Spotify", "spotify": "spotify:playlist:abc"},
    {"n": 3, "name": "YTM", "ytm": "https://music.youtube.com/playlist?list=xyz"},
    {"n": 4, "name": "Radio 2", "radio": "http://example.invalid/r2"},
]


class FakeRenderers:
    def __init__(self, **enabled):
        self.on = {"airplay": True, "bluetooth": True, "upnp": False, **enabled}
        self.calls = []
        self.fail = None

    def enabled(self):
        return dict(self.on)

    def describe(self):
        return [{"id": t, "label": t, "available": True, "on": v, "name": f"Hasler {t}",
                 "running": v, "managed_by": "moode"} for t, v in self.on.items()]

    def set(self, target, on=None, name=None):
        self.calls.append((target, on, name))
        if self.fail:
            raise self.fail
        if on is not None:
            self.on[target] = on


class CastPlayer(FakePlayer):
    def __init__(self, **enabled):
        super().__init__()
        self.renderers = FakeRenderers(**enabled)
        self.sources = {"radio": object(), "spotify": object(), "ytm": object()}


@pytest.fixture
def rig():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    variant.presets = [dict(p) for p in MIXED]
    player = CastPlayer()
    return variant, player, Actions(variant, player)


def pu(actions, player, times=1):
    out = []
    for _ in range(times):
        actions.dispatch(Event("pu", "tap"))
        out.append([c for c in player.calls if c[0] == "select_source"][-1][1])
    return out


# -- the cycle --------------------------------------------------------------


class TestCycleFrom:
    def test_known_distinct_stops(self):
        assert cycle_from(["radio", "ytm", "airplay"]) == ("radio", "ytm", "airplay")

    @pytest.mark.parametrize("raw", [[], "radio", ["radio", "radio"], ["chromecast"], None])
    def test_refused(self, raw):
        with pytest.raises(ValueError):
            cycle_from(raw)


class TestConfigurableCycle:
    def test_default_is_todays_order(self, rig):
        _, player, actions = rig
        assert actions.source_cycle == SOURCE_CYCLE
        assert pu(actions, player, 4) == ["bluetooth", "airplay", "spotify", "radio"]

    def test_custom_order_is_followed(self, rig):
        _, player, actions = rig
        actions.source_cycle = ("radio", "airplay")
        assert pu(actions, player, 3) == ["airplay", "radio", "airplay"]

    def test_a_switched_off_renderer_is_skipped(self, rig):
        _, player, actions = rig
        player.renderers.on["airplay"] = False
        assert pu(actions, player, 3) == ["bluetooth", "spotify", "radio"]
        assert actions.cycle_availability()["airplay"] == "AirPlay is switched off (Casting)"

    def test_nothing_usable_falls_back_to_radio(self, rig):
        _, player, actions = rig
        player.renderers.on["airplay"] = False
        actions.source_cycle = ("airplay",)
        assert pu(actions, player) == ["radio"]

    def test_ytm_as_its_own_stop(self, rig):
        _, player, actions = rig
        actions.source_cycle = ("radio", "ytm", "spotify")
        actions.dispatch(Event("k2", "tap"))      # spotify
        actions.dispatch(Event("k3", "tap"))      # ytm
        actions.dispatch(Event("k1", "tap"))      # radio
        pu(actions, player)
        assert player.current().n == 3, "the ytm stop resumes the last ytm preset"
        pu(actions, player)
        assert player.current().n == 2, "then spotify"
        pu(actions, player)
        assert player.current().n == 1, "radio no longer covers ytm"

    def test_radio_covers_ytm_when_ytm_is_not_a_stop(self, rig):
        _, player, actions = rig
        actions.source_cycle = ("radio", "spotify")
        actions.dispatch(Event("k3", "tap"))      # ytm
        pu(actions, player)                       # spotify
        pu(actions, player)                       # radio stop
        assert player.current().n == 3

    def test_ytm_without_presets_is_skipped(self, rig):
        variant, player, _ = rig
        variant.presets = [p for p in MIXED if "ytm" not in p]
        actions = Actions(variant, player)
        actions.source_cycle = ("radio", "ytm", "bluetooth")
        assert actions.cycle_availability()["ytm"] == "no YouTube Music presets"
        assert pu(actions, player) == ["bluetooth"]

    def test_a_missing_source_is_skipped(self, rig):
        _, player, actions = rig
        del player.sources["spotify"]
        actions.source_cycle = ("radio", "spotify", "bluetooth")
        assert pu(actions, player) == ["bluetooth"]

    def test_unreadable_moode_assumes_renderers_on(self, rig):
        _, player, actions = rig
        player.renderers.on = {}
        assert actions.cycle_availability()["airplay"] is None


# -- resume after a cast ------------------------------------------------------


class TestResumeAfterCast:
    def cast(self, actions, player, t0=100.0, renderer="airplay"):
        actions.dispatch(Event("k1", "tap"))
        player.source = renderer
        player.playing = None
        actions.watch_cast(renderer, t0)
        player.source = "radio"
        actions.watch_cast("radio", t0 + 1)

    def test_on_by_default_but_can_be_switched_off(self, rig):
        _, player, actions = rig
        assert actions.resume_after_cast is True     # owner's default
        actions.resume_after_cast = False
        self.cast(actions, player)
        assert actions.watch_cast("radio", 101 + CAST_END_GRACE_S) is False
        assert player.current() is None

    def test_resumes_the_last_preset_after_the_grace(self, rig):
        _, player, actions = rig
        actions.resume_after_cast = True
        self.cast(actions, player)
        assert actions.watch_cast("radio", 101 + CAST_END_GRACE_S - 1) is False
        assert actions.watch_cast("radio", 101 + CAST_END_GRACE_S) is True
        assert player.current().n == 1

    def test_a_short_gap_is_not_an_end(self, rig):
        _, player, actions = rig
        actions.resume_after_cast = True
        self.cast(actions, player)
        actions.watch_cast("airplay", 105)        # back again (a pause, a skip)
        assert actions.watch_cast("radio", 106) is False
        assert actions.watch_cast("radio", 106 + CAST_END_GRACE_S - 1) is False

    def test_a_key_pressed_during_the_cast_wins(self, rig):
        _, player, actions = rig
        actions.resume_after_cast = True
        actions.dispatch(Event("k1", "tap"))
        actions.watch_cast("bluetooth", 100)
        actions.dispatch(Event("k4", "tap"))      # takes the audio back
        actions.watch_cast("radio", 101)
        assert actions.watch_cast("radio", 101 + CAST_END_GRACE_S) is False
        assert player.current().n == 4

    def test_a_cast_that_woke_the_radio_leaves_it_quiet(self, rig):
        _, player, actions = rig
        actions.resume_after_cast = True
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("power", "off"))
        actions.watch_cast("airplay", 100)
        actions.wake_for_phone("airplay")
        actions.watch_cast("radio", 101)
        assert actions.watch_cast("radio", 101 + CAST_END_GRACE_S) is False
        assert player.current() is None

    def test_spotify_connect_is_left_alone(self, rig):
        _, player, actions = rig
        actions.resume_after_cast = True
        actions.watch_cast("spotify", 100)
        assert actions.watch_cast("radio", 101 + CAST_END_GRACE_S) is False


# -- moOde's renderers -----------------------------------------------------------


SID = "ho7vk67sqrjua8sme0pqhsjgdq"
IDLE = 'foo|s:1:"x";w_queue|s:0:"";w_queueargs|s:0:"";w_lock|i:0;w_active|i:0;'


@pytest.fixture
def moode(tmp_path):
    db = tmp_path / "moode.db"
    con = sqlite3.connect(db)
    con.execute("create table cfg_system (id integer, param text, value text)")
    rows = {"airplaysvc": "0", "airplayname": "Hasler AirPlay", "btsvc": "1",
            "btname": "Hasler Bluetooth", "upnpsvc": "0", "upnpname": "Hasler UPNP",
            "sessionid": SID, "spotifysvc": "0"}
    con.executemany("insert into cfg_system (param, value) values (?, ?)", rows.items())
    con.commit()
    con.close()
    (tmp_path / f"sess_{SID}").write_text(IDLE)
    posts = []

    def post(url, form, cookie, timeout):
        posts.append((url, form, cookie))
        # What ren-config.php does: write the flag and name to cfg_system.
        con = sqlite3.connect(db)
        for k, v in form.items():
            if not k.startswith("update_"):
                con.execute("update cfg_system set value=? where param=?", (v, k))
        con.commit()
        con.close()
        return 200

    clock = [0.0]

    def sleep(dt):
        clock[0] += dt

    r = MoodeRenderers(db=db, session_dir=tmp_path, post=post,
                       unit_active=lambda unit: unit == "bluetooth", clock=lambda: clock[0],
                       installed=lambda prog: True, sleep=sleep)
    return r, posts, tmp_path, db


class TestMoodeRenderers:
    def test_reads_flags_names_and_units(self, moode):
        r, _, _, _ = moode
        assert r.enabled() == {"airplay": False, "bluetooth": True, "upnp": False}
        bt = next(t for t in r.describe() if t["id"] == "bluetooth")
        assert bt == {"id": "bluetooth", "label": "Bluetooth", "available": True, "reason": None,
                      "on": True, "name": "Hasler Bluetooth", "running": True, "managed_by": "moode"}

    def test_switching_on_posts_moodes_own_form_with_its_session(self, moode):
        r, posts, _, _ = moode
        out = r.set("airplay", on=True)
        assert posts == [("http://127.0.0.1/ren-config.php",
                          {"update_airplay_settings": "1", "airplaysvc": "1",
                           "airplayname": "Hasler AirPlay"}, SID)]
        assert out["on"] is True

    def test_renaming_bluetooth_always_sends_both_fields(self, moode):
        r, posts, _, _ = moode
        r.set("bluetooth", name="Hasler")
        assert posts[-1][1] == {"update_bt_settings": "1", "btsvc": "1", "btname": "Hasler"}

    def test_no_change_no_post(self, moode):
        r, posts, _, _ = moode
        r.set("bluetooth", on=True, name="Hasler Bluetooth")
        assert posts == []

    def test_a_busy_worker_is_try_again_and_nothing_is_sent(self, moode):
        r, posts, tmp, _ = moode
        (tmp / f"sess_{SID}").write_text('w_queue|s:7:"airplay";w_lock|i:1;')
        with pytest.raises(RendererError, match="busy"):
            r.set("airplay", on=True)
        assert posts == []

    def test_moode_not_taking_it_is_an_error(self, moode):
        r, _, _, _ = moode
        r._post = lambda *a: 200            # answers, writes nothing
        with pytest.raises(RendererError, match="didn't take"):
            r.set("upnp", on=True)

    def test_a_missing_program_is_unavailable_and_never_switched_on(self, moode):
        # moOde's watchdog would restart a missing shairport-sync every 10 s.
        r, posts, _, _ = moode
        r._installed = lambda prog: prog != "shairport-sync"
        air = next(t for t in r.describe() if t["id"] == "airplay")
        assert air["available"] is False and "shairport-sync" in air["reason"]
        with pytest.raises(RendererError, match="isn't installed"):
            r.set("airplay", on=True)
        assert posts == []
        r.set("airplay", name="Hasler")          # renaming (it stays off) is fine

    def test_moode_slow_to_answer_but_applying_is_success(self, moode):
        r, posts, _, _ = moode
        post = r._post

        def slow(*a):
            post(*a)                         # moOde writes the flag, then the reply times out
            raise TimeoutError("timed out")
        r._post = slow
        assert r.set("upnp", on=True)["on"] is True

    @pytest.mark.parametrize("bad", ['Has"ler', "", " ", "x" * 33, "a;rm -rf /", 7])
    def test_bad_names_are_refused(self, moode, bad):
        r, posts, _, _ = moode
        with pytest.raises(ValueError):
            r.set("airplay", name=bad)
        assert posts == []

    def test_spotify_is_not_a_moode_target(self, moode):
        r, _, _, _ = moode
        with pytest.raises(ValueError):
            r.set("spotify", on=True)

    def test_unreadable_db(self, tmp_path):
        r = MoodeRenderers(db=tmp_path / "none.db", session_dir=tmp_path)
        assert r.enabled() == {}
        assert all(t["available"] is False for t in r.describe())


def test_parse_session_and_idle():
    assert worker_idle(parse_session(IDLE))
    assert not worker_idle(parse_session('w_queue|s:5:"btsvc";w_lock|i:0;'))
    assert not worker_idle(parse_session('w_queue|s:0:"";w_lock|i:1;'))


def test_valid_name():
    assert valid_name(" Hasler ") == "Hasler"
    assert valid_name("Hasler HM-1.2") == "Hasler HM-1.2"


def test_spotify_device_name(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text("# x\ndevice_name: Hasler  # the name\ndevice_type: speaker\n")
    assert spotify_device_name(cfg) == "Hasler"
    assert spotify_device_name(tmp_path / "missing.yml") is None


# -- the API ------------------------------------------------------------------


@pytest.fixture
def api(rig, tmp_path):
    variant, player, actions = rig
    panel = Panel.from_variant(variant, fake=True)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True},
                 ha=False, settings_file=tmp_path / "settings.json")
    sources = SourcesApi(app, aplmeta=tmp_path / "aplmeta.json", spotify_name=lambda: "Hasler")
    app.extra_routes.append(sources)
    return sources, app, actions, player, tmp_path


class TestSourcesApi:
    def test_get_lists_every_stop_cycle_first(self, api):
        sources, _, _, player, _ = api
        player.renderers.on["airplay"] = False
        status, doc = sources.route("GET", "/api/sources", None)
        assert status == 200
        assert doc["cycle"] == list(SOURCE_CYCLE) and doc["default"] == list(SOURCE_CYCLE)
        assert [s["id"] for s in doc["stops"]] == ["radio", "bluetooth", "airplay", "spotify", "ytm"]
        airplay = next(s for s in doc["stops"] if s["id"] == "airplay")
        assert airplay["available"] is False and "switched off" in airplay["reason"]
        assert doc["bound_to"] == ["pu.tap"]
        assert doc["resume_after_cast"] is True

    def test_put_applies_and_persists_and_survives_a_restart(self, api):
        sources, _, actions, _, tmp = api
        status, doc = sources.route("PUT", "/api/sources",
                                    {"cycle": ["radio", "ytm", "airplay"], "resume_after_cast": True})
        assert status == 200 and doc["ok"] and doc["cycle"] == ["radio", "ytm", "airplay"]
        assert actions.source_cycle == ("radio", "ytm", "airplay") and actions.resume_after_cast
        saved = json.loads((tmp / "settings.json").read_text())
        assert saved["sources"] == {"cycle": ["radio", "ytm", "airplay"], "resume_after_cast": True}
        fresh = Actions(actions.variant, FakePlayer())
        apply_saved(fresh, saved)
        assert fresh.source_cycle == ("radio", "ytm", "airplay") and fresh.resume_after_cast

    def test_put_keeps_other_settings(self, api):
        sources, _, _, _, tmp = api
        (tmp / "settings.json").write_text(json.dumps({"volume_limit": 80}))
        sources.route("PUT", "/api/sources", {"cycle": ["radio"]})
        saved = json.loads((tmp / "settings.json").read_text())
        assert saved["volume_limit"] == 80 and saved["sources"]["cycle"] == ["radio"]

    @pytest.mark.parametrize("body", [{}, {"cycle": ["tv"]}, {"cycle": []}, {"resume_after_cast": "yes"}])
    def test_put_refuses_bad_bodies(self, api, body):
        sources, _, actions, _, _ = api
        with pytest.raises(ValueError):
            sources.route("PUT", "/api/sources", body)
        assert actions.source_cycle == SOURCE_CYCLE

    def test_a_broken_saved_cycle_is_ignored(self, rig):
        _, _, actions = rig
        apply_saved(actions, {"sources": {"cycle": ["radio", "tv"], "resume_after_cast": 1}})
        assert actions.source_cycle == SOURCE_CYCLE and actions.resume_after_cast is True   # a bad value keeps the default

    def test_casting_lists_targets_and_who_casts(self, api):
        sources, _, actions, player, tmp = api
        (tmp / "aplmeta.json").write_text(json.dumps({"title": "Song", "artist": "Band"}))
        player.source = "airplay"
        status, doc = sources.route("GET", "/api/casting", None)
        ids = [t["id"] for t in doc["targets"]]
        assert ids == ["airplay", "bluetooth", "upnp", "spotify"]
        spotify = doc["targets"][-1]
        assert spotify["read_only"] and spotify["name"] == "Hasler" and spotify["on"]
        assert doc["now"] == {"source": "airplay", "casting": True, "renderer": "airplay",
                              "device": "", "title": "Song", "artist": "Band"}
        assert doc["pairing"]["open"] is False

    def test_put_casting_switches_one_target(self, api):
        sources, _, _, player, _ = api
        status, doc = sources.route("PUT", "/api/casting", {"airplay": {"on": False, "name": "Hasler"}})
        assert status == 200 and player.renderers.calls == [("airplay", False, "Hasler")]

    @pytest.mark.parametrize("body", [{}, {"spotify": {"on": False}}, {"chromecast": {"on": True}},
                                      {"airplay": {"on": True}, "upnp": {"on": True}}, {"airplay": {}}])
    def test_put_casting_refuses(self, api, body):
        sources, _, _, player, _ = api
        with pytest.raises(ValueError):
            sources.route("PUT", "/api/casting", body)
        assert player.renderers.calls == []

    def test_moode_busy_is_a_503(self, api):
        from haslerd.web.server import Unavailable
        sources, _, _, player, _ = api
        player.renderers.fail = RendererError("moOde is busy")
        with pytest.raises(Unavailable):
            sources.route("PUT", "/api/casting", {"upnp": {"on": True}})

    def test_pairing_window(self, api):
        sources, _, actions, player, _ = api
        status, doc = sources.route("POST", "/api/casting/pair", {"seconds": 60})
        assert status == 200 and ("bluetooth_pair", 60) in player.calls
        assert doc["pairing"]["open"] is True and 55 <= doc["pairing"]["seconds_left"] <= 60
        with pytest.raises(ValueError):
            sources.route("POST", "/api/casting/pair", {"seconds": 5})

    def test_no_pairing_with_bluetooth_off(self, api):
        sources, _, _, player, _ = api
        player.renderers.on["bluetooth"] = False
        status, doc = sources.route("POST", "/api/casting/pair", {})
        assert status == 409 and "switched off" in doc["error"]
        assert ("bluetooth_pair", 120) not in player.calls

    def test_other_paths_are_not_ours(self, api):
        sources, *_ = api
        assert sources.route("GET", "/api/sourcesx", None) is None
        assert sources.route("DELETE", "/api/sources", None)[0] == 405

    def test_over_http(self, api):
        _, app, actions, _, _ = api
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        base = f"http://127.0.0.1:{server.port}"

        def call(path, method="GET", body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as err:
                return err.code, json.loads(err.read())
        try:
            status, doc = call("/api/sources")
            assert status == 200 and doc["cycle"] == list(SOURCE_CYCLE)
            status, doc = call("/api/sources", "PUT", {"cycle": ["radio", "bluetooth"]})
            assert status == 200 and actions.source_cycle == ("radio", "bluetooth")
            status, doc = call("/api/sources", "PUT", {"cycle": ["tv"]})
            assert status == 400 and "unknown source" in doc["error"]
            status, doc = call("/api/casting")
            assert status == 200 and doc["targets"][0]["id"] == "airplay"
        finally:
            server.close()
