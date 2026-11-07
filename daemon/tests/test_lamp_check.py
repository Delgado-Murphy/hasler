"""The guided Lamp check: its save endpoint (the calibration contract in the
overlay's io.lamp), validation, Back to standard, and the raw duty preview."""

import copy
import json
import shutil
import urllib.error
import urllib.request

import pytest
import yaml
from conftest import VARIANTS

from haslerd import boxconfig
from haslerd.actions import Actions
from haslerd.lamp import engine as eng
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import lamp_check
from haslerd.web.server import Unavailable, WebApp, WebServer

CAL = {
    "min_duty": {"red": 0.0075, "green": 0.00375, "blue": 0.015},
    "balance": {"red": 1.0, "green": 1.142857, "blue": 0.666667},
    "palette": {"amber": {"day": [1.0, 0.17, 0.0], "night": [1.0, 0.13, 0.0]},
                "ember": [1.0, 0.1, 0.0], "blue": [0.04, 0.0, 1.0]},
    "level_gain": {"amber": 1.0, "ember": 1.0, "blue": 1.35},
    "levels": {"ember_day": 0.275, "ember_night": 0.15},
}
CONTRACT = {"min_duty", "balance", "palette", "level_gain", "levels", "calibrated"}


class FakeLamp:
    """An engine without the new calls: preview(rgb, level) only."""

    def __init__(self):
        self.base_palette = dict(eng.DEFAULT_PALETTE)
        self.previews = []

    def preview(self, rgb, level=1.0):
        self.previews.append((rgb, level))


class RawLamp(FakeLamp):
    """The engine the Lamp check wants: preview_duty and live calibration."""

    def __init__(self):
        super().__init__()
        self.raw, self.cals = [], []

    def preview_duty(self, duty, hold_s=30.0):
        self.raw.append((duty, hold_s))

    def set_calibration(self, cal):
        self.cals.append(cal)


class RawFlagLamp(FakeLamp):
    """An engine whose preview takes raw=True instead of preview_duty."""

    def __init__(self):
        super().__init__()
        self.raw = []

    def preview(self, rgb, level=1.0, hold_s=30.0, raw=False):
        (self.raw if raw else self.previews).append((rgb, level))


@pytest.fixture
def paths(tmp_path):
    release = tmp_path / "release"
    (release / "config" / "variants").mkdir(parents=True)
    shutil.copy(VARIANTS / "hasler-hm.yaml", release / "config" / "variants" / "hasler-hm.yaml")
    return boxconfig.Paths(release=release, etc=tmp_path / "etc", state=tmp_path / "state")


@pytest.fixture
def app(paths, tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    app = WebApp(Actions(variant, FakePlayer()), panel, None, {"radio": True}, ha=False)
    app.box_paths = paths
    app.lamp = FakeLamp()
    temp = tmp_path / "temp"
    temp.write_text("51234\n")
    app.cpu_temp_file = temp
    return app


def overlay_file(app):
    return app.box_paths.overlay_file("hasler-hm")


def overlay(app):
    return yaml.safe_load(overlay_file(app).read_text())


def write_overlay(app, doc):
    overlay_file(app).parent.mkdir(parents=True, exist_ok=True)
    overlay_file(app).write_text(yaml.safe_dump(doc))


# -- the save endpoint ------------------------------------------------------


class TestSave:
    def test_writes_exactly_the_contract_into_io_lamp(self, app):
        doc = app.put_lamp_calibration(copy.deepcopy(CAL))
        lamp = overlay(app)["io"]["lamp"]
        assert set(lamp) == CONTRACT
        for key in CAL:
            assert lamp[key] == CAL[key], key
        stamp = lamp["calibrated"]
        assert stamp["version"] == 1 and stamp["cpu_temp_c"] == 51.2
        assert stamp["at"][4] == "-" and stamp["at"][10] == "T" and stamp["at"][-3] == ":"
        assert doc["saved"] == lamp

    def test_keeps_other_keys(self, app):
        write_overlay(app, {
            "schema": 1,
            "controls": {"volume": {"hysteresis_counts": 40}},
            "io": {"mcp_address": 33,
                   "lamp": {"frequency_hz": 600, "palette": {"white": [1, 0.3, 0.05], "amber": [1, 0.2, 0]},
                            "min_duty": {"red": 0.01, "white": 0.5}}},
        })
        app.put_lamp_calibration(copy.deepcopy(CAL))
        doc = overlay(app)
        assert doc["controls"] == {"volume": {"hysteresis_counts": 40}}
        assert doc["io"]["mcp_address"] == 33
        lamp = doc["io"]["lamp"]
        assert lamp["frequency_hz"] == 600
        assert lamp["palette"]["white"] == [1, 0.3, 0.05]               # not the check's colour
        assert lamp["palette"]["amber"] == CAL["palette"]["amber"]      # a list replaced by day/night
        assert lamp["min_duty"] == CAL["min_duty"]                      # replaced whole, no stale keys

    def test_a_second_save_replaces_the_first(self, app):
        app.put_lamp_calibration(copy.deepcopy(CAL))
        again = copy.deepcopy(CAL)
        again["levels"] = {"ember_day": 0.2, "ember_night": 0.12}
        app.put_lamp_calibration(again)
        assert overlay(app)["io"]["lamp"]["levels"] == {"ember_day": 0.2, "ember_night": 0.12}

    def test_a_sent_calibrated_block_is_ignored(self, app):
        body = dict(copy.deepcopy(CAL), calibrated={"at": "1999", "cpu_temp_c": 1, "version": 9})
        app.put_lamp_calibration(body)
        assert overlay(app)["io"]["lamp"]["calibrated"]["version"] == 1

    def test_no_thermal_zone_is_null(self, app, tmp_path):
        app.cpu_temp_file = tmp_path / "missing"
        app.put_lamp_calibration(copy.deepcopy(CAL))
        assert overlay(app)["io"]["lamp"]["calibrated"]["cpu_temp_c"] is None

    def test_applies_live(self, app):
        app.lamp = RawLamp()
        heard = []
        app.on_overlay_saved = heard.append
        app.put_lamp_calibration(copy.deepcopy(CAL))
        assert heard == ["hasler-hm"]
        assert app.lamp.cals[-1]["levels"] == CAL["levels"] and "calibrated" in app.lamp.cals[-1]
        app.delete_lamp_calibration()
        # Back to the descriptor's io.lamp (hasler-hm.yaml has min_duty), not to nothing.
        assert app.lamp.cals[-1] == {"min_duty": {"red": 0.006, "green": 0.006, "blue": 0.006}}

    def test_a_live_save_keeps_a_custom_amber_band(self, app):
        write_overlay(app, {"schema": 1, "io": {"lamp": {"amber_band": [0.1, 0.25]}}})
        app.lamp = RawLamp()
        app.put_lamp_calibration(copy.deepcopy(CAL))
        assert app.lamp.cals[-1]["amber_band"] == [0.1, 0.25]
        assert overlay(app)["io"]["lamp"]["amber_band"] == [0.1, 0.25]
        app.delete_lamp_calibration()
        assert app.lamp.cals[-1]["amber_band"] == [0.1, 0.25]

    def test_the_check_supersedes_hand_set_colours(self, app, tmp_path):
        from haslerd.lamp.outputs import FakeOutput
        from haslerd.web.server import load_settings, save_settings
        app.settings_file = tmp_path / "settings.json"
        save_settings(app.settings_file, {"schema": 1, "lamp_palette": {"amber": [1.0, 0.4, 0.0]}, "room": "Den"})
        lamp = eng.LampEngine(FakeOutput(), threaded=False, clock=lambda: 1.0)
        lamp.set_palette({"amber": [1.0, 0.4, 0.0]})
        app.lamp = lamp
        heard = []
        app.lamp_listeners.append(lambda doc: (heard.append(doc), lamp.set_palette(doc.get("lamp_palette") or {})))
        app.put_lamp_calibration(copy.deepcopy(CAL))
        saved = load_settings(app.settings_file)
        assert "lamp_palette" not in saved and saved["room"] == "Den"
        assert heard and "lamp_palette" not in heard[-1]
        assert lamp.palette["amber"] == tuple(CAL["palette"]["amber"]["day"])        # the check's, live
        assert lamp.palette_night["amber"] == tuple(CAL["palette"]["amber"]["night"])
        assert lamp.balance == pytest.approx((1.0, 1.142857, 0.666667))

    def test_needs_the_box_config_directory(self, app):
        app.box_paths = None
        with pytest.raises(Unavailable):
            app.put_lamp_calibration(copy.deepcopy(CAL))

    def test_back_to_standard_removes_only_the_contract(self, app):
        write_overlay(app, {"schema": 1, "io": {"lamp": {"frequency_hz": 600, "palette": {"white": [1, 0.3, 0.05]}}}})
        app.put_lamp_calibration(copy.deepcopy(CAL))
        doc = app.delete_lamp_calibration()
        assert overlay(app)["io"]["lamp"] == {"frequency_hz": 600, "palette": {"white": [1, 0.3, 0.05]}}
        assert doc["saved"] == {}

    def test_back_to_standard_with_nothing_else_leaves_no_lamp_block(self, app):
        app.put_lamp_calibration(copy.deepcopy(CAL))
        app.delete_lamp_calibration()
        assert overlay(app)["io"] == {}


def _set(doc, path, value):
    *head, last = path
    for k in head:
        doc = doc[k]
    if value is KeyError:
        del doc[last]
    else:
        doc[last] = value


class TestValidation:
    @pytest.mark.parametrize("path,value", [
        (("min_duty", "red"), 0.06),
        (("min_duty", "red"), -0.001),
        (("min_duty", "green"), KeyError),
        (("min_duty", "white"), 0.01),
        (("balance", "blue"), 0.0),
        (("balance", "blue"), 5),
        (("balance", "red"), True),
        (("balance", "red"), "1"),
        (("balance", "red"), float("nan")),
        (("palette", "amber", "day"), [1, 0.2]),
        (("palette", "amber", "night"), [1, 0.2, 1.5]),
        (("palette", "amber", "dusk"), [1, 0.2, 0]),
        (("palette", "amber"), [1, 0.2, 0]),
        (("palette", "blue"), [0, 0, 0]),
        (("palette", "white"), [1, 1, 1]),
        (("palette", "ember"), KeyError),
        (("level_gain", "blue"), 0.05),
        (("level_gain", "blue"), 9),
        (("level_gain", "red"), 1),
        (("levels", "ember_day"), 0),
        (("levels", "ember_night"), 1.2),
        (("levels",), KeyError),
        (("pins",), {"red": 4}),
    ])
    def test_rejects(self, app, path, value):
        body = copy.deepcopy(CAL)
        _set(body, path, value)
        with pytest.raises(ValueError):
            app.put_lamp_calibration(body)
        assert not overlay_file(app).exists()

    @pytest.mark.parametrize("bad", [None, [], "x", {}])
    def test_rejects_non_documents(self, app, bad):
        with pytest.raises(ValueError):
            app.put_lamp_calibration(bad)


# -- GET -----------------------------------------------------------------------


class TestGet:
    def test_standard_follows_the_engine(self, app):
        doc = app.lamp_calibration()
        assert doc["variant"] == "hasler-hm" and doc["lamp"] and doc["can_save"]
        assert doc["saved"] == {} and doc["raw_preview"] is False
        std = doc["standard"]
        assert set(std) == CONTRACT - {"calibrated"}
        assert std["palette"]["amber"]["day"] == list(eng.DEFAULT_PALETTE["amber"])
        assert std["levels"] == {"ember_day": eng.EMBER_LEVEL, "ember_night": eng.EMBER_NIGHT}
        assert lamp_check.check(std) == std                     # a valid calibration itself
        assert doc["cpu_temp_c"] == 51.2

    def test_looks_come_from_the_policy(self, app):
        looks = {lk["id"]: lk for lk in app.lamp_calibration()["looks"]}
        assert looks["standby"]["colour"] == "ember" and looks["standby"]["level"] == 0.22
        assert looks["pairing"]["motion"] == "breath" and looks["pairing"]["period_s"] == pytest.approx(eng.BREATH_PERIOD)
        from haslerd.lamp import policy
        assert looks["playing"]["level"] == policy.PLAYING.level == 0.80
        assert looks["hotspot"]["motion"] == "beckon"                   # setup beckons now, never steady
        assert looks["busy"]["period_s"] == policy.SHUTTING_DOWN.period_s
        assert {"asking", "kick", "refuse", "swell", "hue"} <= set(looks)    # the behaviours too
        assert "offline" not in looks                                   # no red breath any more
        assert all(lk["watch"] for lk in looks.values())

    def test_no_model_of_the_engine_for_the_page(self, app):
        assert "model" not in app.lamp_calibration()

    def test_an_engine_with_the_new_calls_says_so(self, app):
        app.lamp = RawLamp()
        doc = app.lamp_calibration()
        assert doc["raw_preview"] is True
        app.lamp = RawFlagLamp()
        assert app.lamp_calibration()["raw_preview"] is True

    def test_without_a_lamp_or_paths(self, app):
        app.lamp, app.box_paths = None, None
        doc = app.lamp_calibration()
        assert doc["lamp"] is False and doc["can_save"] is False and doc["saved"] == {}


# -- the raw duty preview -------------------------------------------------------


class TestRawPreview:
    def test_goes_to_the_engines_preview_duty(self, app):
        app.lamp = RawLamp()
        doc = app.lamp_preview({"duty": [0.0025, 0, 0]})
        assert app.lamp.raw == [([0.0025, 0.0, 0.0], eng.PREVIEW_S)] and app.lamp.previews == []
        assert doc["raw"] is True and doc["showing"]

    def test_or_its_raw_flag(self, app):
        app.lamp = RawFlagLamp()
        doc = app.lamp_preview({"duty": [0.0025, 0, 0]})
        assert app.lamp.raw == [([0.0025, 0.0, 0.0], 1.0)] and app.lamp.previews == []
        assert doc["raw"] is True

    def test_duty_wins_over_other_keys(self, app):
        app.lamp = RawLamp()
        app.lamp_preview({"duty": [0.01, 0, 0], "rgb": [1, 1, 1], "look": "standby"})
        assert app.lamp.raw and app.lamp.previews == []

    def test_falls_back_to_the_nearest_colour(self, app):
        doc = app.lamp_preview({"duty": [0.5, 0.25, 0]})
        rgb, level = app.lamp.previews[-1]
        assert rgb == pytest.approx([1.0, 0.5, 0.0])
        assert level == pytest.approx(0.5 ** (1 / eng.GAMMA))
        assert doc["raw"] is False

    def test_black_is_dark(self, app):
        app.lamp_preview({"duty": [0, 0, 0]})
        assert app.lamp.previews[-1] == ([0.0, 0.0, 0.0], 0.0)

    def test_on_the_real_engine_the_fallback_lands_on_the_duty(self, app):
        from haslerd.lamp.outputs import FakeOutput
        lamp = eng.LampEngine(FakeOutput(), min_duty=(0.0, 0.0, 0.0), threaded=False, clock=lambda: 5.0)
        app.lamp = lamp
        app.lamp_preview({"duty": [0.4, 0.06, 0.0]})
        assert lamp.render(5.0)[0] == pytest.approx((0.4, 0.06, 0.0))

    @pytest.mark.parametrize("bad", [[1, 0], [1, 0, 2], [1, -0.1, 0], "red", [True, 0, 0], None])
    def test_rejects(self, app, bad):
        with pytest.raises(ValueError):
            app.lamp_preview({"duty": bad})
        assert app.lamp.previews == []

    def test_the_rgb_path_is_unchanged(self, app):
        app.lamp_preview({"rgb": [1, 0.15, 0], "level": "night"})
        assert app.lamp.previews == [([1, 0.15, 0], 0.35)]


# -- the draft, drawn by the engine ---------------------------------------------


def scratch(cal=CAL, night=False):
    from haslerd.lamp.outputs import FakeOutput
    lamp = eng.LampEngine(FakeOutput(), threaded=False, clock=lambda: 0.0)
    lamp.calibrate(copy.deepcopy(cal))
    if night:
        lamp.set_night(True)
    return lamp


def wait_for(fn, seconds=2.0):
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.02)
    return False


class TestDraftPreview:
    @pytest.fixture(autouse=True)
    def stop_stage(self, app):
        yield
        app._lamp_stage.stop()

    def test_a_colour_is_the_engines_duty_for_the_draft(self, app):
        app.lamp = RawLamp()
        doc = app.lamp_preview({"draft": copy.deepcopy(CAL), "colour": "amber", "level": 1.0})
        want = scratch().duty_for("amber", 1.0)
        assert app.lamp.raw[-1][0] == pytest.approx(list(want))
        assert doc["duty"] == pytest.approx(list(want), abs=1e-6) and doc["raw"] is True
        assert want != scratch({}).duty_for("amber", 1.0)              # the draft, not today's calibration

    def test_draft_level_gain_and_ember_levels_are_the_engines(self, app):
        app.lamp = RawLamp()
        cal = copy.deepcopy(CAL)
        cal["level_gain"]["blue"] = 1.25
        app.lamp_preview({"draft": cal, "colour": "blue", "level": 0.8})
        assert app.lamp.raw[-1][0] == pytest.approx(list(scratch(cal).duty_for("blue", 0.8)))
        assert app.lamp.raw[-1][0][2] == pytest.approx(min(1.0, 1.0 * 0.666667))    # 0.8 x 1.25 = full, x balance

    def test_on_a_real_live_engine_the_lamp_shows_it(self, app):
        from haslerd.lamp.outputs import FakeOutput
        live = eng.LampEngine(FakeOutput(), threaded=False, clock=lambda: 5.0)
        app.lamp = live
        app.lamp_preview({"draft": copy.deepcopy(CAL), "colour": "ember", "level": 0.275})
        assert live.render(5.0)[0] == pytest.approx(scratch().duty_for("ember", 0.275))

    def test_a_look_streams_the_engines_frames_and_names_the_channel_to_blame(self, app):
        app.lamp = RawLamp()
        doc = app.lamp_preview({"draft": copy.deepcopy(CAL), "look": "loading", "night": False})
        assert doc["look"] == "loading" and doc["jump"] in ("red", "green", "blue")
        assert wait_for(lambda: len(app.lamp.raw) > 5), "no frames streamed"
        assert all(hold == pytest.approx(1.0) for _, hold in app.lamp.raw)
        levels = {round(d[0], 4) for d, _ in app.lamp.raw}
        assert len(levels) > 2                                          # it breathes
        app.lamp_preview({"rgb": None})                                 # any other preview ends it
        n = len(app.lamp.raw)
        assert app._lamp_stage.playing is None
        assert wait_for(lambda: False, 0.1) is False and len(app.lamp.raw) == n

    def test_the_same_look_again_extends_it_without_a_restart(self, app):
        app.lamp = RawLamp()
        body = {"draft": copy.deepcopy(CAL), "look": "kick", "night": True}
        app.lamp_preview(body)
        key = app._lamp_stage.playing
        app.lamp_preview(copy.deepcopy(body))
        assert app._lamp_stage.playing is key
        body["night"] = False
        app.lamp_preview(body)
        assert app._lamp_stage.playing != key

    def test_night_draws_the_looks_own_night_level(self, app):
        from haslerd.lamp import policy, scratch as sc
        spec = lamp_check.check(copy.deepcopy(CAL))
        day = sc.SceneFrames(spec, sc.Scene(policy.PLAYING)).at(0.0)
        night = sc.SceneFrames(spec, sc.Scene(policy.PLAYING), night=True).at(0.0)
        assert day == pytest.approx(scratch().duty_for("amber", policy.PLAYING.level))
        assert night == pytest.approx(scratch().duty_for("amber", policy.PLAYING.night))

    def test_the_hue_change_never_shows_purple_and_the_kick_peaks(self, app):
        from haslerd.lamp import policy, scratch as sc
        cal = copy.deepcopy(CAL)
        cal["palette"]["blue"] = [0.0, 0.0, 1.0]                     # pure blue: any red is the mix
        spec = lamp_check.check(cal)
        scenes = lamp_check.scenes(policy)
        frames = sc.SceneFrames(spec, scenes["hue"]).sample()
        assert any(f[2] > 0.1 for f in frames) and any(f[0] > 0.05 and f[2] == 0 for f in frames)
        assert not any(f[0] > 0.02 and f[2] > 0.02 for f in frames)    # down, swap, up - never both
        kick = sc.SceneFrames(spec, scenes["kick"]).sample()
        assert max(f[0] for f in kick) > kick[0][0] * 1.3

    def test_every_sweep_item_renders_by_day_and_night(self, app):
        from haslerd.lamp import policy, scratch as sc
        spec = lamp_check.check(copy.deepcopy(CAL))
        for lid, scene in lamp_check.scenes(policy).items():
            for night in (False, True):
                frames = sc.SceneFrames(spec, scene, night=night).sample(fps=10)
                assert frames and max(max(f) for f in frames) > 0, (lid, night)
        assert set(lamp_check.scenes(policy)) == {item[0] for item in lamp_check.SWEEP}

    @pytest.mark.parametrize("body", [
        {"colour": "white", "level": 1},
        {"colour": "amber", "level": 1.5},
        {"colour": "amber", "look": "ready"},
        {"look": "offline"},
        {"look": "ready", "night": "yes"},
        {},
    ])
    def test_rejects(self, app, body):
        app.lamp = RawLamp()
        with pytest.raises(ValueError):
            app.lamp_preview({"draft": copy.deepcopy(CAL), **body})
        assert app.lamp.raw == []

    def test_a_bad_draft_is_rejected(self, app):
        bad = copy.deepcopy(CAL)
        bad["balance"]["blue"] = 0
        with pytest.raises(ValueError):
            app.lamp_preview({"draft": bad, "colour": "amber", "level": 1})


class TestScratch:
    def test_flicker_channel(self):
        from haslerd.lamp.scratch import flicker_channel
        assert flicker_channel([(0.5, 0.01, 0.0), (0.2, 0.0, 0.0), (0.4, 0.007, 0.0)]) == "green"
        assert flicker_channel([(0.5, 0.05, 0.0), (0.2, 0.02, 0.0)]) == "green"          # faintest lit minor
        assert flicker_channel([(0.0, 0.0, 0.5), (0.0, 0.0, 0.2)]) == "blue"
        assert flicker_channel([(0.0, 0.0, 0.0)]) is None

    def test_look_frames_are_the_engine_stepped(self):
        from haslerd.lamp import policy
        from haslerd.lamp.outputs import FakeOutput
        from haslerd.lamp.scratch import look_frames
        frames = look_frames({}, policy.LOADING, fps=25)
        assert len(frames) == round(policy.LOADING.period_s * 25)
        live = eng.LampEngine(FakeOutput(), threaded=False, clock=lambda: 0.0)
        live.show(policy.LOADING, cut=True)
        for i in (0, 10, 40):
            assert frames[i] == pytest.approx(live.render(i / 25)[0])


# -- over HTTP -------------------------------------------------------------------


class TestHttp:
    @pytest.fixture
    def base(self, app):
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        yield f"http://127.0.0.1:{server.port}", app
        server.close()

    def call(self, url, method="GET", body=None, ctype="application/json"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_round_trip(self, base):
        url, app = base
        status, doc = self.call(url + "/api/lamp/calibration")
        assert status == 200 and doc["saved"] == {}
        status, doc = self.call(url + "/api/lamp/calibration", "PUT", CAL)
        assert status == 200 and doc["ok"] and doc["saved"]["levels"] == CAL["levels"]
        status, doc = self.call(url + "/api/lamp/calibration", "POST", CAL)
        assert status == 200 and doc["ok"]
        status, doc = self.call(url + "/api/lamp/preview", "POST", {"duty": [0.01, 0, 0]})
        assert status == 200 and doc["ok"] and doc["raw"] is False
        status, doc = self.call(url + "/api/lamp/preview", "POST", {"draft": CAL, "colour": "blue", "level": 0.55})
        assert status == 200 and doc["ok"] and len(doc["duty"]) == 3
        status, doc = self.call(url + "/api/lamp/preview", "POST", {"draft": CAL, "look": "nope"})
        assert status == 400
        status, doc = self.call(url + "/api/lamp/calibration", "DELETE", {})
        assert status == 200 and doc["saved"] == {}

    def test_bad_input_is_a_400_and_no_paths_a_503(self, base):
        url, app = base
        status, doc = self.call(url + "/api/lamp/calibration", "PUT", {"min_duty": {}})
        assert status == 400 and not doc["ok"]
        app.box_paths = None
        status, doc = self.call(url + "/api/lamp/calibration", "PUT", CAL)
        assert status == 503

    def test_writes_follow_the_csrf_rules(self, base):
        url, app = base
        for method, body in (("PUT", CAL), ("POST", CAL), ("DELETE", {})):
            status, _ = self.call(url + "/api/lamp/calibration", method, body, ctype="text/plain")
            assert status == 403, method
        status, _ = self.call(url + "/api/lamp/preview", "POST", {"duty": [1, 0, 0]}, ctype="text/plain")
        assert status == 403
        assert not overlay_file(app).exists() and app.lamp.previews == []
