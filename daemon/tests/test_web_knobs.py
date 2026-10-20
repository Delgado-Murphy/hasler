"""The knob calibration API: list, sample, fit, save to the overlay, reset."""

import json
import shutil
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS

from haslerd import boxconfig
from haslerd.actions import Actions
from haslerd.panel.controls import AnalogSelector, Pot
from haslerd.panel.io_base import FakeAnalog, FakeDigital
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, Variant, load_variant
from haslerd.web.server import Unavailable, WebApp, WebServer

TICK = 0.02
# Position 0 = the quiet end (see panel/calibrate.py).
HASLER_VOLUME = [
    {"position": 0.0, "raw": 26190}, {"position": 0.25, "raw": 25616},
    {"position": 0.5, "raw": 23783}, {"position": 0.625, "raw": 21106},
    {"position": 0.75, "raw": 10882}, {"position": 0.875, "raw": 1879},
    {"position": 1.0, "raw": 25},
]


@pytest.fixture
def paths(tmp_path):
    release = tmp_path / "release"
    (release / "config" / "variants").mkdir(parents=True)
    shutil.copy(VARIANTS / "hasler-hm.yaml", release / "config" / "variants" / "hasler-hm.yaml")
    return boxconfig.Paths(release=release, etc=tmp_path / "etc", state=tmp_path / "state")


@pytest.fixture
def rig(paths):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    app = WebApp(Actions(variant, FakePlayer()), panel, None, {"radio": True}, ha=False)
    app.box_paths = paths
    return app, panel


def poll(panel, counts: dict, n=20, start=0.0):
    for ch, c in counts.items():
        panel.analog.set(ch, c)
    now = start
    for _ in range(n):
        panel.poll(now)
        now += TICK


def volume(panel) -> Pot:
    return next(c for c in panel.controls if c.id == "volume")


class TestList:
    def test_knobs_of_the_running_variant(self, rig):
        app, panel = rig
        poll(panel, {1: 13000, 0: 2000})
        doc = app.knobs()
        assert doc["variant"] == "hasler-hm" and doc["overlay"] is False
        by_id = {k["id"]: k for k in doc["knobs"]}
        assert list(by_id) == ["volume", "tone"]
        vol = by_id["volume"]
        assert vol["kind"] == "pot" and vol["channel"] == 1 and vol["raw"] == 13000
        assert vol["direction"] == "reversed" and vol["hysteresis_counts"] == 30
        assert vol["source"] == "shipped" and vol["end_switch"] is None
        assert vol["curve"][0] == [25, 100.0] and vol["label"] == "Volume"
        tone = by_id["tone"]
        assert tone["taper"] == "linear" and tone["range"] == [0, 25500] and tone["curve"] is None
        assert tone["end_switch"]["id"] == "power"

    def test_sample(self, rig):
        app, panel = rig
        poll(panel, {1: 23783})
        s = app.knob_sample("volume", "15")
        assert s["raw"] == 23783 and s["stable"] is True and s["noise"] == 0
        assert s["value"] == 50.0

    def test_sample_before_any_reading(self, rig):
        app, _ = rig
        assert app.knob_sample("volume")["raw"] is None

    @pytest.mark.parametrize("cid, n", [("k1", 15), ("nope", 15), ("volume", 0), ("volume", "x")])
    def test_bad_ids_and_counts(self, rig, cid, n):
        app, _ = rig
        with pytest.raises(ValueError):
            app.knob_sample(cid, n)


class TestFitAndSave:
    def test_fit_previews_without_saving(self, rig, paths):
        app, _ = rig
        doc = app.knob_fit("volume", {"points": HASLER_VOLUME, "direction": "auto"})
        assert doc["direction"] == "reversed" and doc["hysteresis_counts"] == 30 and doc["redo"] == []
        assert not paths.overlay_file("hasler-hm").exists()

    def test_put_saves_to_the_overlay_and_applies_live(self, rig, paths):
        app, panel = rig
        poll(panel, {1: 23783})
        fit = app.knob_fit("volume", {"points": HASLER_VOLUME})
        fit["hysteresis_counts"] = 40
        knob = app.put_calibration("volume", {k: fit[k] for k in ("curve", "direction", "hysteresis_counts")})
        assert knob["source"] == "box" and knob["hysteresis_counts"] == 40
        overlay = boxconfig.read_overlay(paths, "hasler-hm")
        assert overlay["controls"]["volume"] == {"curve": fit["curve"], "direction": "reversed",
                                                 "hysteresis_counts": 40}
        assert volume(panel).hysteresis == 40 and volume(panel).curve[0] == (55, 100.0)
        assert app.knobs()["overlay"] is True

    def test_put_points_fits_and_saves(self, rig, paths):
        app, panel = rig
        knob = app.put_calibration("volume", {"points": HASLER_VOLUME})
        assert knob["curve"][-1] == [26160, 0.0] and knob["hysteresis_counts"] == 30

    def test_put_misplaced_points_is_refused(self, rig, paths):
        app, _ = rig
        pts = [dict(p) for p in HASLER_VOLUME]
        pts[2]["raw"] = 26185
        with pytest.raises(ValueError, match="out of order"):
            app.put_calibration("volume", {"points": pts})
        assert not paths.overlay_file("hasler-hm").exists()

    @pytest.mark.parametrize("body", [
        {},
        {"curve": [[0, 0], [5, 50], [9, 20]]},
        {"curve": [[0, 0], [9, 100]], "direction": "reversed"},
        {"curve": [[0, 0], [9, 100]], "hysteresis_counts": 0},
        {"curve": [[0, 0], [9, 100]], "hysteresis_counts": 2.5},
    ])
    def test_bad_calibrations(self, rig, body):
        app, _ = rig
        with pytest.raises(ValueError):
            app.put_calibration("volume", body)

    def test_delete_goes_back_to_shipped_live(self, rig, paths):
        app, panel = rig
        app.put_calibration("volume", {"curve": [[0, 0], [26000, 100]], "hysteresis_counts": 12})
        assert volume(panel).direction == "normal"
        knob = app.delete_calibration("volume")
        assert knob["source"] == "shipped" and knob["hysteresis_counts"] == 30
        assert volume(panel).direction == "reversed" and volume(panel).curve[0] == (25, 100.0)
        assert "volume" not in boxconfig.read_overlay(paths, "hasler-hm")["controls"]

    def test_the_hook_reapplies_the_overlay_when_wired(self, rig):
        app, panel = rig
        calls = []
        app.on_overlay_saved = calls.append
        app.put_calibration("volume", {"curve": [[0, 0], [26000, 100]], "hysteresis_counts": 12})
        assert calls == ["hasler-hm"]
        assert volume(panel).hysteresis == 30      # the hook (main.apply_overlay) owns applying

    def test_mains_reload_variant_applies_it_for_real(self, rig, paths):
        from haslerd.main import apply_overlay
        app, panel = rig
        app.reload_variant = lambda: apply_overlay(panel, paths, "hasler-hm", actions=app.actions)
        app.put_calibration("volume", {"curve": [[0, 0], [26000, 100]], "hysteresis_counts": 12})
        assert volume(panel).hysteresis == 12 and volume(panel).direction == "normal"
        app.delete_calibration("volume")
        assert volume(panel).hysteresis == 30 and volume(panel).direction == "reversed"

    def test_delete_uses_the_release_descriptor_not_the_overlaid_variant(self, rig):
        app, panel = rig
        # main loads the variant with the overlay already in it
        app.actions.variant.controls = [dict(c, hysteresis_counts=99) if c["id"] == "volume" else c
                                        for c in app.actions.variant.controls]
        app.put_calibration("volume", {"curve": [[0, 0], [26000, 100]], "hysteresis_counts": 12})
        app.delete_calibration("volume")
        assert volume(panel).hysteresis == 30

    def test_no_paths_means_no_saving(self, rig):
        app, _ = rig
        app.box_paths = None
        with pytest.raises(Unavailable):
            app.put_calibration("volume", {"points": HASLER_VOLUME})


@pytest.fixture
def selector_rig(paths):
    specs = [{"id": "sel", "kind": "analog_selector", "channel": 3, "positions": 3}]
    variant = Variant(name="bench", io={}, controls=specs)
    sel = AnalogSelector("sel", 3, positions=3)
    panel = Panel([sel], FakeDigital([]), FakeAnalog({3: 0}))
    app = WebApp(Actions(variant, FakePlayer()), panel, None, {"radio": True}, ha=False)
    (paths.release / "config" / "variants" / "bench.yaml").write_text(
        "variant: bench\ncontrols:\n  - {id: sel, kind: analog_selector, channel: 3, positions: 3}\n")
    app.box_paths = paths
    return app, panel, sel


class TestSelector:
    POINTS = [{"position": 0.0, "raw": 1000}, {"position": 0.5, "raw": 13000}, {"position": 1.0, "raw": 25000}]

    def test_listed_with_its_positions(self, selector_rig):
        app, panel, _ = selector_rig
        poll(panel, {3: 13000})
        knob = app.knobs()["knobs"][0]
        assert knob["kind"] == "selector" and knob["positions"] == [0, 1, 2]
        assert knob["value"] == 1          # an index into positions
        assert app.knob_sample("sel")["value"] == 1

    def test_fit_takes_points_at_i_over_n_minus_1(self, selector_rig):
        app, *_ = selector_rig
        doc = app.knob_fit("sel", {"points": self.POINTS})
        assert doc["thresholds"] == [7000, 19000] and doc["direction"] == "normal"
        assert doc["curve"] == [[1000, 0.0], [13000, 50.0], [25000, 100.0]]

    def test_fit_out_of_order_asks_for_a_redo(self, selector_rig):
        app, *_ = selector_rig
        pts = [dict(p) for p in self.POINTS]
        pts[1]["raw"] = 30000
        doc = app.knob_fit("sel", {"points": pts})
        assert doc["curve"] is None and doc["redo"] == [0, 1, 2]

    def test_put_the_previewed_curve_saves_thresholds(self, selector_rig, paths):
        app, panel, sel = selector_rig
        doc = app.knob_fit("sel", {"points": list(reversed(self.POINTS))})
        knob = app.put_calibration("sel", {k: doc[k] for k in ("curve", "direction", "hysteresis_counts")})
        assert knob["thresholds"] == [7000, 19000] and sel.thresholds == [7000, 19000]
        assert boxconfig.read_overlay(paths, "bench")["controls"]["sel"] == {"thresholds": [7000, 19000]}
        app.delete_calibration("sel")
        assert sel.thresholds == [8800, 17600]      # even split again

    def test_bad_positions(self, selector_rig):
        app, *_ = selector_rig
        with pytest.raises(ValueError):
            app.knob_fit("sel", {"points": [{"position": 0.3, "raw": 1}] * 3})


class TestHttp:
    def test_round_trip(self, rig, paths):
        app, panel = rig
        poll(panel, {1: 23783})
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        base = f"http://127.0.0.1:{server.port}"

        def call(method, path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read() or b"{}")

        try:
            status, doc = call("GET", "/api/knobs")
            assert status == 200 and doc["knobs"][0]["id"] == "volume"
            status, doc = call("GET", "/api/knobs/volume/sample?n=10")
            assert status == 200 and doc["raw"] == 23783
            status, fit = call("POST", "/api/knobs/volume/fit", {"points": HASLER_VOLUME, "direction": "auto"})
            assert status == 200 and fit["direction"] == "reversed"
            status, knob = call("PUT", "/api/knobs/volume/calibration",
                                {k: fit[k] for k in ("curve", "direction", "hysteresis_counts")})
            assert status == 200 and knob["ok"] and knob["id"] == "volume" and knob["source"] == "box"
            status, knob = call("DELETE", "/api/knobs/volume/calibration")
            assert status == 200 and knob["source"] == "shipped"
            status, doc = call("PUT", "/api/knobs/k1/calibration", {"points": HASLER_VOLUME})
            assert status == 400 and "k1" in doc["error"]
            status, doc = call("POST", "/api/knobs/volume/fit", {"points": HASLER_VOLUME[:3]})
            assert status == 400
            status, doc = call("GET", "/api/health")
            assert status in (200, 503)
        finally:
            server.close()

    def test_health_is_503_without_the_module(self, rig, monkeypatch):
        import builtins
        app, _ = rig
        real = builtins.__import__

        def no_health(name, globals=None, locals=None, fromlist=(), level=0):
            if fromlist and "health" in fromlist and level == 2:
                raise ImportError("no health")
            return real(name, globals, locals, fromlist, level)
        monkeypatch.setattr(builtins, "__import__", no_health)
        assert app.health()[0] == 503
