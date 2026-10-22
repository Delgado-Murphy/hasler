"""Lamp colours: the engine's palette override and preview, the page's API, the keeper's hook."""

import json
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.lamp import engine as eng
from haslerd.lamp.contract import Look
from haslerd.lamp.engine import LampEngine
from haslerd.lamp.outputs import FakeOutput
from haslerd.main import LampKeeper
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, WebServer, load_settings

NO_FLOOR = (0.0, 0.0, 0.0)
AMBER = Look("amber", 1.0)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    kw.setdefault("min_duty", NO_FLOOR)
    lamp = LampEngine(FakeOutput(), clock=clock, threaded=False, **kw)
    return lamp, clock


def duty(lamp, t):
    return lamp.render(t)[0]


# -- engine: set_palette ----------------------------------------------------


class TestSetPalette:
    def test_overrides_a_colour_live(self):
        lamp, clock = make()
        lamp.show(AMBER)
        lamp.set_palette({"amber": [1.0, 0.3, 0.0]})
        assert duty(lamp, clock.t) == pytest.approx((1.0, 0.3, 0.0))

    def test_empty_goes_back_to_the_release(self):
        lamp, clock = make()
        lamp.show(AMBER)
        lamp.set_palette({"amber": [1.0, 0.3, 0.0]})
        lamp.set_palette({})
        assert duty(lamp, clock.t)[1] == pytest.approx(eng.DEFAULT_PALETTE["amber"][1])

    def test_lays_over_the_descriptors_palette_not_replacing_it(self):
        lamp, clock = make(palette={"blue": [0.0, 0.2, 1.0]})
        lamp.set_palette({"amber": [1.0, 0.3, 0.0]})
        assert lamp.palette["blue"] == (0.0, 0.2, 1.0)
        lamp.set_palette(None)
        assert lamp.palette["blue"] == (0.0, 0.2, 1.0)
        assert lamp.base_palette["amber"] == eng.DEFAULT_PALETTE["amber"]

    def test_unknown_names_and_off_are_ignored_values_clamped(self):
        lamp, _ = make()
        lamp.set_palette({"chartreuse": [0, 1, 0], "off": [1, 1, 1], "red": [2.0, -1.0, float("nan")]})
        assert "chartreuse" not in lamp.palette
        assert lamp.palette["off"] == (0.0, 0.0, 0.0)
        assert lamp.palette["red"] == (1.0, 0.0, 0.0)


# -- engine: preview --------------------------------------------------------


class TestPreview:
    def test_shows_the_raw_colour_over_the_look(self):
        lamp, clock = make()
        lamp.show(AMBER)
        lamp.preview([0.0, 0.04, 1.0], 1.0)
        (r, g, b), moving = lamp.render(clock.t)
        assert (r, g, b) == pytest.approx((0.0, 0.04, 1.0))
        assert moving       # the thread keeps an eye on it to see it expire

    def test_level_is_perceptual_and_not_capped_at_night(self):
        lamp, clock = make()
        lamp.set_night(True)
        lamp.preview([1.0, 0.1, 0.0], 0.5)
        clock.t += 5
        r, g, _ = duty(lamp, clock.t)
        assert r == pytest.approx(0.5 ** eng.GAMMA)
        assert g == pytest.approx(0.1 * 0.5 ** eng.GAMMA)

    def test_outranks_replies(self):
        lamp, clock = make()
        lamp.show(AMBER)
        lamp.preview([0.0, 0.0, 1.0], 1.0)
        lamp.reply("fail")
        clock.t += 0.3
        assert duty(lamp, clock.t) == pytest.approx((0.0, 0.0, 1.0))

    def test_expires_after_the_last_change_then_blends_back(self):
        lamp, clock = make()
        lamp.show(AMBER)
        lamp.preview([0.0, 0.0, 1.0], 1.0)
        clock.t += 20
        lamp.preview([0.0, 0.1, 1.0], 1.0)          # a change restarts the 30 s
        clock.t += eng.PREVIEW_S - 1
        assert duty(lamp, clock.t)[2] == pytest.approx(1.0)
        clock.t += 1.0 + eng.PREVIEW_RESTORE_S / 2
        r, _, b = duty(lamp, clock.t)
        assert 0.0 < r < 1.0 and 0.0 < b < 1.0      # mid-blend
        clock.t += eng.PREVIEW_RESTORE_S
        assert duty(lamp, clock.t) == pytest.approx((1.0, 0.15, 0.0))
        assert not lamp.previewing

    def test_none_ends_it_now(self):
        lamp, clock = make()
        lamp.show(AMBER)
        lamp.preview([0.0, 0.0, 1.0], 1.0)
        lamp.preview(None)
        clock.t += eng.PREVIEW_RESTORE_S + 0.01
        assert duty(lamp, clock.t) == pytest.approx((1.0, 0.15, 0.0))

    def test_the_countdown_ends_it_and_refuses_new_ones(self):
        lamp, clock = make()
        lamp.show(AMBER)
        lamp.preview([0.0, 0.0, 1.0], 1.0)
        lamp.countdown(1.0)
        assert not lamp.previewing
        lamp.preview([0.0, 0.0, 1.0], 1.0)
        assert not lamp.previewing
        r, _, b = duty(lamp, clock.t)
        assert b == 0.0 and r == pytest.approx(eng.COUNTDOWN_FLOOR ** eng.GAMMA)

    def test_threaded_engine_returns_to_the_look_on_its_own(self):
        out = FakeOutput()
        lamp = LampEngine(out, min_duty=NO_FLOOR)
        try:
            lamp.show(AMBER)
            lamp.preview([0.0, 0.0, 1.0], 1.0, hold_s=0.05)
            import time
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and out.last != pytest.approx((1.0, 0.15, 0.0)):
                time.sleep(0.02)
            assert out.last == pytest.approx((1.0, 0.15, 0.0))
        finally:
            lamp.close()


# -- the web API ------------------------------------------------------------


class FakeLamp:
    def __init__(self):
        self.base_palette = dict(eng.DEFAULT_PALETTE)
        self.previews, self.palettes = [], []

    def preview(self, rgb, level=1.0):
        self.previews.append((rgb, level))

    def set_palette(self, overrides):
        self.palettes.append(overrides)


@pytest.fixture
def app(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    app.lamp = FakeLamp()
    return app


class TestPaletteApi:
    def test_get_lists_the_engines_colours_without_off(self, app):
        doc = app.lamp_palette()
        assert set(doc["defaults"]) == set(eng.DEFAULT_PALETTE) - {"off"}
        assert doc["saved"] == {} and doc["effective"] == doc["defaults"]
        assert doc["levels"] == {"full": 1.0, "night": 0.35, "ember": 0.22}
        assert doc["lamp"] is True and doc["preview_s"] == eng.PREVIEW_S

    def test_names_follow_the_engine(self, app):
        app.lamp.base_palette["magenta"] = (1.0, 0.0, 0.6)
        assert app.lamp_palette()["defaults"]["magenta"] == [1.0, 0.0, 0.6]

    def test_without_a_lamp_the_release_palette_still_shows(self, app):
        app.lamp = None
        doc = app.lamp_palette()
        assert doc["lamp"] is False and "amber" in doc["defaults"]
        assert app.lamp_preview({"rgb": [1, 0, 0]})["lamp"] is False

    def test_save_persists_only_what_differs_and_tells_the_listeners(self, app):
        heard = []
        app.lamp_listeners.append(heard.append)
        doc = app.put_lamp_palette({"palette": {"amber": [1.0, 0.2, 0.0], "blue": list(eng.DEFAULT_PALETTE["blue"])}})
        assert doc["saved"] == {"amber": [1.0, 0.2, 0.0]}
        assert doc["effective"]["amber"] == [1.0, 0.2, 0.0]
        assert load_settings(app.settings_file)["lamp_palette"] == {"amber": [1.0, 0.2, 0.0]}
        assert heard[-1]["lamp_palette"] == {"amber": [1.0, 0.2, 0.0]}

    def test_saving_the_release_colours_removes_the_key(self, app):
        app.put_lamp_palette({"palette": {"amber": [1.0, 0.2, 0.0]}})
        app.put_lamp_palette({"palette": {}})
        assert "lamp_palette" not in load_settings(app.settings_file)

    def test_keeps_the_rest_of_settings(self, app):
        app.put_settings({"night": "off"})
        app.put_lamp_palette({"palette": {"red": [1.0, 0.05, 0.0]}})
        saved = load_settings(app.settings_file)
        assert saved["night"] == "off" and saved["lamp_palette"]["red"] == [1.0, 0.05, 0.0]

    @pytest.mark.parametrize("bad", [
        {},
        {"palette": []},
        {"palette": {"chartreuse": [0, 1, 0]}},
        {"palette": {"off": [0, 0, 0]}},
        {"palette": {"amber": [1, 0.1]}},
        {"palette": {"amber": [1, 0.1, 1.5]}},
        {"palette": {"amber": [1, True, 0]}},
        {"palette": {"amber": ["1", 0, 0]}},
        {"palette": {"amber": {"red": 1}}},
    ])
    def test_rejects_bad_palettes(self, app, bad):
        with pytest.raises(ValueError):
            app.put_lamp_palette(bad)
        assert not app.settings_file.exists()

    def test_a_bad_saved_entry_is_skipped(self, app):
        app.settings_file.write_text(json.dumps({"lamp_palette": {"amber": [1, 0.2, 0], "red": "x", "gone": [0, 0, 0]}}))
        assert app.lamp_palette()["saved"] == {"amber": [1.0, 0.2, 0.0]}

    def test_without_a_settings_file_it_lives_in_memory(self, app):
        app.settings_file = None
        heard = []
        app.lamp_listeners.append(heard.append)
        app.put_lamp_palette({"palette": {"amber": [1.0, 0.2, 0.0]}})
        assert app.lamp_palette()["saved"] == {"amber": [1.0, 0.2, 0.0]}
        app.put_settings({"night": "off"})      # a night change must not drop the colours
        assert heard[-1]["lamp_palette"] == {"amber": [1.0, 0.2, 0.0]}


class TestPreviewApi:
    def test_level_names_and_numbers(self, app):
        app.lamp_preview({"rgb": [0, 0.04, 1], "level": "night"})
        app.lamp_preview({"rgb": [0, 0.04, 1], "level": 0.5})
        app.lamp_preview({"rgb": [0, 0.04, 1]})
        assert app.lamp.previews == [([0, 0.04, 1], 0.35), ([0, 0.04, 1], 0.5), ([0, 0.04, 1], 1.0)]

    def test_null_ends_it(self, app):
        doc = app.lamp_preview({"rgb": None})
        assert app.lamp.previews == [(None, 1.0)] and doc["showing"] is False

    @pytest.mark.parametrize("bad", [
        {}, {"rgb": [1, 0]}, {"rgb": [1, 0, 2]}, {"rgb": [1, 0, 0], "level": "dazzle"},
        {"rgb": [1, 0, 0], "level": 1.5}, {"rgb": "red"},
    ])
    def test_rejects(self, app, bad):
        with pytest.raises(ValueError):
            app.lamp_preview(bad)
        assert app.lamp.previews == []


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
        status, doc = self.call(url + "/api/lamp/palette")
        assert status == 200 and "amber" in doc["defaults"]
        status, doc = self.call(url + "/api/lamp/palette", "PUT", {"palette": {"ember": [1.0, 0.08, 0.0]}})
        assert status == 200 and doc["ok"] and doc["saved"] == {"ember": [1.0, 0.08, 0.0]}
        status, doc = self.call(url + "/api/lamp/preview", "POST", {"rgb": [1, 0.08, 0], "level": "ember"})
        assert status == 200 and doc["showing"] and doc["expires_s"] == eng.PREVIEW_S
        assert app.lamp.previews[-1] == ([1.0, 0.08, 0.0], 0.22)
        status, doc = self.call(url + "/api/lamp/palette", "DELETE", {})
        assert status == 200 and doc["saved"] == {}

    def test_bad_input_is_a_400(self, base):
        url, _ = base
        status, doc = self.call(url + "/api/lamp/palette", "PUT", {"palette": {"amber": [2, 0, 0]}})
        assert status == 400 and not doc["ok"]

    def test_writes_follow_the_csrf_rules(self, base):
        url, app = base
        for method, path, body in (("PUT", "/api/lamp/palette", {"palette": {}}),
                                   ("POST", "/api/lamp/preview", {"rgb": [1, 0, 0]}),
                                   ("DELETE", "/api/lamp/palette", {})):
            status, _ = self.call(url + path, method, body, ctype="text/plain")
            assert status == 403, (method, path)
        assert app.lamp.previews == []


# -- the keeper applies saved colours -----------------------------------------


class TestKeeper:
    def test_settings_change_and_start_apply_the_palette(self, tmp_path):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        actions = Actions(variant, FakePlayer())
        lamp = FakeLamp()
        lamp.show = lamp.reply = lamp.countdown = lamp.set_night = lambda *a, **k: None
        keeper = LampKeeper(lamp, actions, None, None, None)
        keeper.apply_palette({"lamp_palette": {"amber": [1, 0.2, 0]}})
        keeper.settings_changed({"night": "off"})
        assert lamp.palettes == [{"amber": [1, 0.2, 0]}, {}]

    def test_a_driver_without_set_palette_is_fine(self):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        actions = Actions(variant, FakePlayer())

        class Old:
            def show(self, *a, **k): ...
            def reply(self, *a): ...
            def countdown(self, *a): ...
            def set_night(self, *a): ...

        keeper = LampKeeper(Old(), actions, None, None, None)
        keeper.settings_changed({"lamp_palette": {"amber": [1, 0.2, 0]}})   # no error
