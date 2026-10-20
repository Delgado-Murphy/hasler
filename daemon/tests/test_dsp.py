"""The knobs' sound effects: effects math, hasler-dsp (anti-clipping, the file,
the JSON it takes), knob assignment and its persistence, and the API."""

import importlib.machinery
import importlib.util
import json
import random
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.dsp import effects as fx
from haslerd.dsp.knobs import TRY_SECONDS, KnobFunctions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, Variant, load_variant
from haslerd.web.knobs_api import KnobFunctionsApi
from haslerd.web.server import WebApp, WebServer

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "hasler-dsp"
SHIPPED = REPO / "config" / "camilladsp" / "Hasler Tone.yml"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("hasler_dsp", str(HELPER))
    spec = importlib.util.spec_from_loader("hasler_dsp", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


hd = load_helper()


def chain_peak(payload: dict) -> tuple[float, float]:
    values = hd.merge(hd.neutral(), payload)
    return hd.master_for(values)


# -- effects ------------------------------------------------------------------

class TestEffects:
    def test_every_effect_documents_itself(self):
        for e in fx.EFFECTS.values():
            d = e.doc()
            assert set(d) == {"id", "label", "description", "params", "ends"}
            assert len(d["ends"]) == 2
            for p in d["params"]:
                assert {"id", "label", "unit", "min", "max", "step", "default"} <= set(p)
                assert p["min"] <= p["default"] <= p["max"]
        assert fx.EFFECTS["tilt"].doc()["params"][0]["plusminus"] is True
        assert fx.EFFECTS["tilt"].ends == ("Warmer", "Brighter")

    @pytest.mark.parametrize("eid", list(fx.EFFECTS))
    def test_flat_at_rest(self, eid):
        e = fx.EFFECTS[eid]
        s = fx.render(eid, e.rest, e.defaults())
        payload = fx.combine([s])
        assert chain_peak(payload) == (0.0, 0.0)
        values = hd.merge(hd.neutral(), payload)
        assert max(abs(x) for x in hd.response_db(values, hd.check_freqs())) == 0.0   # exactly flat
        assert values["width"] == 1.0

    def test_no_position_is_no_effect(self):
        assert fx.render("tilt", None, {}) == fx.Sound()

    def test_centre_deadband(self):
        assert fx.bipolar(0.5) == fx.bipolar(0.51) == fx.bipolar(0.49) == 0.0
        assert fx.bipolar(0.0) == -1.0 and fx.bipolar(1.0) == 1.0

    def test_tilt_ends(self):
        p = fx.EFFECTS["tilt"].defaults()
        bright = fx.render("tilt", 1.0, p).filters
        assert bright == {"Tilt low": {"gain": -6.0, "freq": 800.0}, "Tilt high": {"gain": 6.0, "freq": 800.0}}
        dark = fx.render("tilt", 0.0, {**p, "range_db": 10, "pivot": 600}).filters
        assert dark["Tilt low"] == {"gain": 10.0, "freq": 600}

    def test_treble_cuts_and_boosts_its_own_way(self):
        p = fx.params_from("treble", {"cut_db": 18, "boost_db": 4})
        assert fx.render("treble", 0.0, p).filters["Treble"]["gain"] == -18.0
        assert fx.render("treble", 1.0, p).filters["Treble"]["gain"] == 4.0

    def test_loudness_follows_volume(self):
        p = fx.EFFECTS["loudness"].defaults()
        assert fx.render("loudness", 1.0, p, volume=90).filters["Loudness low"]["gain"] == 0.0
        assert fx.render("loudness", 1.0, p, volume=10).filters["Loudness low"]["gain"] == 9.0
        half = fx.render("loudness", 0.5, p, volume=10).filters
        assert half["Loudness low"]["gain"] == 4.5 and half["Loudness high"]["gain"] == 1.5
        mid = fx.render("loudness", 1.0, p, volume=40).filters["Loudness low"]["gain"]
        assert 0 < mid < 9

    def test_vintage_blend(self):
        p = fx.EFFECTS["vintage"].defaults()
        full = fx.render("vintage", 1.0, p).filters
        assert full["Vintage low"] == {"gain": -24.0, "freq": 120}
        assert full["Vintage high"] == {"gain": -18.0, "freq": 6000}
        assert full["Vintage mid"]["gain"] == 3.0
        assert fx.render("vintage", 0.5, p).filters["Vintage low"]["gain"] == -12.0

    def test_width(self):
        assert fx.render("width", 0.0, {}).width == 0.0
        assert fx.render("width", 1.0, {}).width == 1.0
        assert fx.combine([fx.render("width", 0.3, {})])["width"] == 0.3

    def test_text(self):
        assert fx.EFFECTS["tilt"].text(0.5) == "Flat"
        assert fx.EFFECTS["tilt"].text(0.0) == "Warmer · 100%"
        assert fx.EFFECTS["vintage"].text(0.0) == "Today"
        assert fx.EFFECTS["vintage"].text(0.4) == "1960s radio · 40%"
        assert fx.EFFECTS["width"].text(1.0) == "Stereo"
        assert fx.EFFECTS["width"].text(0.0) == "Mono"
        assert fx.EFFECTS["none"].text(0.3) == "No effect"

    def test_params_validation(self):
        with pytest.raises(ValueError, match="no parameter"):
            fx.params_from("tilt", {"q": 1})
        with pytest.raises(ValueError, match="2 to 12"):
            fx.params_from("tilt", {"range_db": 20})
        with pytest.raises(ValueError, match="number"):
            fx.params_from("tilt", {"range_db": True})
        with pytest.raises(ValueError, match="no effect"):
            fx.params_from("reverb", {})
        assert fx.params_from("tilt", {"range_db": 20}, clamp=True)["range_db"] == 12
        assert fx.params_from("tilt", None) == {"range_db": 6, "pivot": 800}

    def test_effects_only_use_helper_blocks_and_params(self):
        for e in fx.EFFECTS.values():
            for pos in (0.0, 0.3, 1.0):
                maxed = {p.id: p.max for p in e.params}
                minned = {p.id: p.min for p in e.params}
                for params in (maxed, minned, e.defaults()):
                    s = fx.render(e.id, pos, params, volume=0)
                    for block, values in s.filters.items():
                        assert block in hd.BLOCKS
                        assert set(values) <= set(hd.BLOCKS[block][1])
                    hd.merge(hd.neutral(), fx.combine([s]))   # within the helper's limits


# -- the anti-clipping rule ---------------------------------------------------

class TestNeverClips:
    def test_random_knobs_never_rise_above_flat(self):
        rnd = random.Random(7)
        ids = [e for e in fx.EFFECTS if e != "none"]
        for _ in range(150):
            chosen = rnd.sample(ids, rnd.randint(1, 4))
            sounds = []
            for eid in chosen:
                e = fx.EFFECTS[eid]
                params = {p.id: rnd.uniform(p.min, p.max) for p in e.params}
                sounds.append(fx.render(eid, rnd.random(), params, volume=rnd.uniform(0, 100)))
            values = hd.merge(hd.neutral(), fx.combine(sounds))
            peak, master = hd.master_for(values)
            fine = [10 ** (1.3 + 3 * i / 2999) for i in range(3000)]
            assert max(hd.response_db(values, fine)) + master <= 0.06   # grid vs. fine sweep

    def test_boost_is_cancelled_with_room_for_phase(self):
        # 6 dB peak + 1 dB for the phase.
        assert chain_peak({"filters": {"Presence": {"gain": 6}}}) == (6.0, -7.0)
        # +6/-6 tilt: 6 dB peak + 1 + 6/8 for the cut.
        assert chain_peak({"filters": {"Tilt low": {"gain": -6}, "Tilt high": {"gain": 6}}})[1] == -7.8
        # A cut raises no frequency, but a deep one can raise peaks: 1 + 12/8 dB.
        assert chain_peak({"filters": {"Treble": {"gain": -12}}}) == (0.0, -2.5)
        assert chain_peak({"filters": {"Bass": {"gain": -30}}})[1] == -3.0     # at most 3 dB of room
        assert chain_peak({"width": 0.2}) == (0.0, 0.0)                        # width is no shaping

    def test_hasler_tone_compat(self):
        v = hd.tone_values(hd.neutral(), 12.0)
        assert v["filters"]["Treble"]["gain"] == 6.0
        assert hd.master_for(v)[1] == -7.0
        assert hd.tone_values(hd.neutral(), -40)["filters"]["Treble"]["gain"] == -12.0


# -- hasler-dsp: JSON, file -----------------------------------------------------

class TestHelper:
    def test_refuses_what_it_does_not_own(self):
        for bad in ({"filters": {"Volume": {"gain": 1}}}, {"filters": {"Treble": {"type": "Peaking"}}},
                    {"filters": {"Treble": {"gain": 40}}}, {"filters": {"Treble": {"gain": "3"}}},
                    {"width": 2}, {"master_db": 3}, [1]):
            with pytest.raises(hd.Refused):
                hd.merge(hd.neutral(), bad)

    def test_replace_starts_from_neutral(self):
        base = hd.merge(hd.neutral(), {"filters": {"Bass": {"gain": -5}}, "width": 0.2})
        v = hd.merge(base, {"replace": True, "filters": {"Treble": {"gain": -3}}})
        assert v["filters"]["Bass"]["gain"] == 0.0 and v["width"] == 1.0
        kept = hd.merge(base, {"filters": {"Treble": {"gain": -3}}})
        assert kept["filters"]["Bass"]["gain"] == -5.0 and kept["width"] == 0.2

    def test_shipped_config_is_what_the_helper_writes(self):
        text = SHIPPED.read_text()
        assert hd.render_file(text, hd.neutral(), 0.0) == text
        assert "&id" not in text and "*id" not in text      # no YAML anchors for CamillaDSP
        assert "device: plughw:CARD=sndrpihifiberry,DEV=0   # the Amp4 Pro, by name" in text

    def test_old_file_is_upgraded_keeping_devices(self):
        old = ("description: old\ndevices:\n  samplerate: 44100\n  playback:\n    device: hw:X   # mine\n"
               "filters:\n  Treble:\n    parameters:\n      freq: 2500\n      gain: -4.0\n      q: 0.7\n"
               "      type: Highshelf\n    type: Biquad\npipeline:\n- names: [Treble]\n  type: Filter\n"
               "title: Old\n")
        values = hd.values_from_file(old)
        assert values["filters"]["Treble"]["gain"] == -4.0
        new = hd.render_file(old, values, 0.0)
        assert "    device: hw:X   # mine\n" in new and "title: Old\n" in new
        again = hd.values_from_file(new)
        assert again == values
        assert "Vintage low" in new

    def test_width_roundtrips_through_the_file(self):
        v = hd.merge(hd.neutral(), {"width": 0.25})
        assert hd.values_from_file(hd.render_file(SHIPPED.read_text(), v, 0.0))["width"] == 0.25
        m = hd.mixer_mapping(0.0)
        assert [s["gain"] for s in m[0]["sources"]] == [0.5, 0.5]

    def test_command_line(self, tmp_path):
        cfg = tmp_path / "Hasler Tone.yml"
        cfg.write_text(SHIPPED.read_text())
        env = {"HASLER_DSP_CONFIG": str(cfg), "HASLER_DSP_WS": "ws://127.0.0.1:9", "PATH": "/usr/bin:/bin"}

        def run(*args):
            done = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True, env=env)
            return done.returncode, json.loads(done.stdout)

        code, doc = run("set", json.dumps({"filters": {"Presence": {"gain": 4}}}))
        assert code == 0 and doc["ok"] and doc["master_db"] == -5.0 and doc["saved"]
        assert doc["live"] in ("not running", "no websocket module")
        assert "gain: -5.0" in cfg.read_text()
        code, doc = run("status")
        assert doc["file"]["filters"]["Presence"]["gain"] == 4.0 and doc["live"]["running"] is False
        code, doc = run("tone", "-6")
        assert doc["treble_db"] == -6.0 and -5.0 <= doc["master_db"] < 0
        assert run("status")[1]["file"]["filters"]["Presence"]["gain"] == 4.0      # kept
        code, doc = run("set", '{"filters": {"Nope": {}}}')
        assert code == 2 and doc["ok"] is False and "Nope" in doc["error"]
        code, doc = run("reset")
        assert doc["master_db"] == 0.0 and cfg.read_text() == SHIPPED.read_text()


# -- assignment ---------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        return {"ok": True, "master_db": 0.0}


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def hm():
    return load_variant(VARIANTS / "hasler-hm.yaml")


def manager(variant=None, volume=lambda: 50.0):
    variant = variant or hm()
    rec, clock = Recorder(), Clock()
    k = KnobFunctions(lambda: variant, rec, volume=volume, clock=clock, threaded=False)
    return k, rec, clock


def spare_pot_variant():
    return Variant(name="test", io={}, controls=[
        {"id": "volume", "kind": "pot", "channel": 0},
        {"id": "tone", "kind": "pot", "channel": 1},
        {"id": "extra", "kind": "pot", "channel": 2, "effect": "presence", "effect_params": {"range_db": 4}},
    ], actions={"volume.change": {"do": "set_volume"}, "tone.change": {"do": "set_tone"}})


class TestAssignment:
    def test_hasler_defaults(self):
        k, _, _ = manager()
        assert k.knob_ids() == ["volume", "tone"]
        assert not k.assignable("volume") and k.assignable("tone")
        d = k.default("tone")
        assert d.effect == "tilt" and d.params == {"range_db": 6.0, "pivot": 800.0}

    def test_excelsior_pots_from_the_descriptor(self):
        k, _, _ = manager(load_variant(VARIANTS / "excelsior.yaml"))
        assert [k.default(c).effect for c in ("treble", "bass")] == ["treble", "bass"]
        assert not k.assignable("volume")

    def test_unbound_pot_with_an_effect_plays_it(self):
        variant = spare_pot_variant()
        k, rec, _ = manager(variant)
        actions = Actions(variant, FakePlayer())
        actions.dsp = k
        actions.dispatch(Event("extra", "change", 100.0))
        assert rec.calls[-1]["filters"]["Presence"]["gain"] == 4.0
        actions.dispatch(Event("tone", "change", 0.0))
        assert rec.calls[-1]["filters"]["Tilt low"]["gain"] == 6.0
        assert rec.calls[-1]["filters"]["Presence"]["gain"] == 4.0    # both knobs, one chain
        actions.dispatch(Event("volume", "change", 30.0))
        assert len(rec.calls) == 2

    def test_legacy_without_helper(self):
        player = FakePlayer()
        variant = hm()
        k = KnobFunctions(lambda: variant, None, legacy=player.set_tone, threaded=False)
        actions = Actions(variant, player)
        actions.dsp = k
        actions.dispatch(Event("tone", "change", 80.0))
        assert ("set_tone", "tone", 80.0) in player.calls

    def test_put_parse_and_unique(self):
        k, rec, _ = manager(spare_pot_variant())
        with pytest.raises(ValueError, match="volume knob"):
            k.parse({"volume": {"effect": "tilt"}})
        with pytest.raises(ValueError, match="no knob"):
            k.parse({"bogus": None})
        with pytest.raises(ValueError, match="both the tone and the extra knob"):
            k.parse({"tone": {"effect": "presence"}})
        chosen = k.parse({"tone": {"effect": "presence"}, "extra": {"effect": "width"}})
        k.set_saved(chosen)
        assert k.current("tone").effect == "presence"
        k.set_saved(k.parse({"tone": None}))
        assert k.current("tone").effect == "tilt"

    def test_saved_settings(self):
        k, _, _ = manager()
        k.apply_saved({"knob_functions": {"tone": {"effect": "vintage", "params": {"low_cut": 500}},
                                          "volume": {"effect": "tilt"}, "nope": {}}})
        a = k.current("tone")
        assert a.effect == "vintage" and a.params["low_cut"] == 300      # clamped; bad ones skipped
        assert "volume" not in k.saved

    def test_try_expires_and_cancels(self):
        k, rec, clock = manager()
        k.knob_moved("tone", 100.0)
        assert rec.calls[-1]["filters"]["Tilt high"]["gain"] == 6.0
        assert k.start_try({"knob": "tone", "effect": "vintage"}) == TRY_SECONDS
        assert rec.calls[-1]["filters"]["Vintage high"]["gain"] == -18.0 and "Tilt high" not in rec.calls[-1]["filters"]
        assert k.try_left()["seconds_left"] == 30
        clock.t += TRY_SECONDS + 1
        k.tick()
        assert rec.calls[-1]["filters"]["Tilt high"]["gain"] == 6.0
        k.start_try({"knob": "tone", "effect": "width", "position": 0.0})
        assert rec.calls[-1]["width"] == 0.0
        assert k.cancel_try() is True
        assert rec.calls[-1]["width"] == 1.0
        with pytest.raises(ValueError):
            k.start_try({"knob": "tone", "effect": "tilt", "position": 2})

    def test_loudness_follows_the_volume(self):
        vol = {"v": 80.0}
        k, rec, _ = manager(volume=lambda: vol["v"])
        k.set_saved(k.parse({"tone": {"effect": "loudness"}}))
        k.knob_moved("tone", 100.0)
        assert rec.calls[-1]["filters"]["Loudness low"]["gain"] == 0.0
        vol["v"] = 10.0
        k.tick()
        assert rec.calls[-1]["filters"]["Loudness low"]["gain"] == 9.0

    def test_same_state_is_not_resent(self):
        k, rec, _ = manager()
        k.knob_moved("tone", 50.0)
        k.knob_moved("tone", 50.5)        # still in the deadband
        assert len(rec.calls) == 1

    def test_worker_thread_applies(self):
        import time
        variant = hm()
        rec = Recorder()
        k = KnobFunctions(lambda: variant, rec)
        k.knob_moved("tone", 0.0)
        for _ in range(100):
            if rec.calls:
                break
            time.sleep(0.01)
        k.close()
        assert rec.calls and rec.calls[-1]["filters"]["Tilt low"]["gain"] == 6.0


# -- the API ------------------------------------------------------------------

@pytest.fixture
def api(tmp_path):
    variant = hm()
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    k, rec, clock = manager(variant)
    actions.dsp = k
    app = WebApp(actions, panel, None, {"radio": True}, ha=False, settings_file=tmp_path / "settings.json")
    app.knob_functions = k
    app.extra_routes.append(KnobFunctionsApi(app, k))
    return app, k, rec, panel


class TestApi:
    def test_get(self, api):
        app, k, _, _ = api
        status, doc = app.route_extra("GET", "/api/knobs/functions", None)
        assert status == 200
        assert [e["id"] for e in doc["effects"]][:2] == ["tilt", "treble"]
        knobs = {d["id"]: d for d in doc["knobs"]}
        assert knobs["volume"]["assignable"] is False and knobs["volume"]["note"]
        tone = knobs["tone"]
        assert tone["effect"] == tone["default_effect"] == "tilt" and tone["assignable"] is True
        assert tone["params"] == {"range_db": 6.0, "pivot": 800.0}
        assert "end-stop" in tone["note"]
        assert doc["try"] is None

    def test_put_saves_and_null_resets(self, api):
        app, k, _, _ = api
        status, doc = app.route_extra("PUT", "/api/knobs/functions",
                                      {"tone": {"effect": "presence", "params": {"range_db": 9}}})
        assert status == 200 and doc["ok"]
        tone = next(d for d in doc["knobs"] if d["id"] == "tone")
        assert tone["effect"] == "presence" and tone["params"]["range_db"] == 9
        saved = json.loads(app.settings_file.read_text())
        assert saved["knob_functions"] == {"tone": {"effect": "presence", "params": {"range_db": 9.0, "freq": 3000.0}}}
        # A restart reads it back.
        k2, _, _ = manager()
        k2.apply_saved(saved)
        assert k2.current("tone").effect == "presence"
        app.route_extra("PUT", "/api/knobs/functions", {"tone": None})
        assert json.loads(app.settings_file.read_text())["knob_functions"] == {}
        assert k.current("tone").effect == "tilt"

    def test_put_refuses(self, api):
        app, _, _, _ = api
        for body in ({"volume": {"effect": "tilt"}}, {"tone": {"effect": "x"}}, {},
                     {"tone": {"effect": "tilt", "params": {"range_db": 99}}}):
            with pytest.raises(ValueError):
                app.route_extra("PUT", "/api/knobs/functions", body)
        assert not app.settings_file.exists()

    def test_try_and_cancel(self, api):
        app, k, rec, _ = api
        status, doc = app.route_extra("POST", "/api/knobs/try", {"knob": "tone", "effect": "width", "position": 0})
        assert status == 200 and doc["seconds"] == 30 and doc["try"]["knob"] == "tone"
        assert rec.calls[-1]["width"] == 0.0
        assert not app.settings_file.exists()           # a try is never saved
        _, doc = app.route_extra("DELETE", "/api/knobs/try", None)
        assert doc["cancelled"] is True and doc["try"] is None
        app.route_extra("POST", "/api/knobs/try", {"knob": "tone", "effect": "width", "position": 0})
        _, doc = app.route_extra("POST", "/api/knobs/try", {"knob": "tone", "cancel": True})
        assert doc["cancelled"] is True

    def test_status_names_the_effect(self, api):
        app, k, _, panel = api
        tone = next(c for c in app.actions.variant.controls if c["id"] == "tone")
        panel.analog.set(tone["channel"], 0)               # fully left
        for t in range(20):
            panel.poll(t * 0.01)
        st = app.status()["tone"]
        assert st["effect"] == "tilt" and st["label"] == "Tone (tilt)"
        assert st["text"] == "Warmer · 100%" and st["treble_db"] == -6.0

    def test_over_http_with_csrf(self, api):
        app, _, _, _ = api
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"

            def call(method, path, body=None, headers=None):
                req = urllib.request.Request(base + path, method=method,
                                             data=None if body is None else json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json", "Host": "localhost",
                                                      **(headers or {})})
                try:
                    with urllib.request.urlopen(req) as resp:
                        return resp.status, json.loads(resp.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            assert call("GET", "/api/knobs/functions")[0] == 200
            assert call("PUT", "/api/knobs/functions", {"tone": {"effect": "vintage"}},
                        {"Origin": "http://evil.example"})[0] == 403
            status, doc = call("PUT", "/api/knobs/functions", {"tone": {"effect": "nope"}})
            assert status == 400 and "no effect" in doc["error"]
            status, doc = call("PUT", "/api/knobs/functions", {"tone": {"effect": "vintage"}})
            assert status == 200 and doc["ok"]
            assert call("DELETE", "/api/knobs/try")[0] == 200
        finally:
            server.close()
