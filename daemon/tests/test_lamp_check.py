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
    """The engine the Lamp check wants: raw previews, look previews, live calibration."""

    def __init__(self):
        super().__init__()
        self.raw, self.looks, self.cals = [], [], []

    def preview(self, rgb, level=1.0, hold_s=30.0, raw=False):
        (self.raw if raw else self.previews).append((rgb, level))

    def preview_look(self, look, night, calibration):
        self.looks.append((look, night, calibration))

    def set_calibration(self, cal):
        self.cals.append(cal)


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
        assert app.lamp.cals[-1] is None

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
        assert doc["saved"] == {} and doc["raw_preview"] is False and doc["look_preview"] is False
        std = doc["standard"]
        assert set(std) == CONTRACT - {"calibrated"}
        assert std["palette"]["amber"]["day"] == list(eng.DEFAULT_PALETTE["amber"])
        assert std["levels"] == {"ember_day": eng.STANDBY_EMBER, "ember_night": eng.NIGHT_EMBER}
        assert lamp_check.check(std) == std                     # a valid calibration itself
        assert doc["cpu_temp_c"] == 51.2

    def test_looks_come_from_the_policy(self, app):
        looks = {lk["id"]: lk for lk in app.lamp_calibration()["looks"]}
        assert looks["standby"]["colour"] == "ember" and looks["standby"]["level"] == 0.22
        assert looks["pairing"]["motion"] == "breath" and looks["pairing"]["period_s"] == 2.0
        assert {"playing", "casting", "offline"} <= set(looks)

    def test_an_engine_with_the_new_calls_says_so(self, app):
        app.lamp = RawLamp()
        doc = app.lamp_calibration()
        assert doc["raw_preview"] and doc["look_preview"]

    def test_without_a_lamp_or_paths(self, app):
        app.lamp, app.box_paths = None, None
        doc = app.lamp_calibration()
        assert doc["lamp"] is False and doc["can_save"] is False and doc["saved"] == {}


# -- the raw duty preview -------------------------------------------------------


class TestRawPreview:
    def test_goes_to_the_engines_raw_preview(self, app):
        app.lamp = RawLamp()
        doc = app.lamp_preview({"duty": [0.0025, 0, 0]})
        assert app.lamp.raw == [([0.0025, 0.0, 0.0], 1.0)] and app.lamp.previews == []
        assert doc["raw"] is True and doc["showing"]

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

    def test_look_preview(self, app):
        app.lamp = RawLamp()
        doc = app.lamp_preview({"look": {"colour": "blue", "level": 0.6, "motion": "breath", "period_s": 2,
                                         "floor": 0.15}, "night": True, "calibration": copy.deepcopy(CAL)})
        look, night, cal = app.lamp.looks[-1]
        assert (look.colour, look.level, look.motion, look.period_s, look.floor) == ("blue", 0.6, "breath", 2, 0.15)
        assert night is True and cal["levels"] == CAL["levels"] and doc["look"] is True

    def test_look_preview_without_the_engine_call(self, app):
        doc = app.lamp_preview({"look": {"colour": "amber", "level": 1}, "night": False})
        assert doc["look"] is False and app.lamp.previews == []

    @pytest.mark.parametrize("bad", [
        {"look": "amber"}, {"look": {"colour": "chartreuse", "level": 1}},
        {"look": {"colour": "amber", "level": 2}}, {"look": {"colour": "amber", "level": 1, "motion": "spin"}},
        {"look": {"colour": "amber", "level": 1}, "night": "yes"},
    ])
    def test_look_rejects(self, app, bad):
        with pytest.raises(ValueError):
            app.lamp_preview(bad)


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
