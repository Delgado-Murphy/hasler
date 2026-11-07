"""Hardware guards and diagnostics (1.5.0): the descriptor's audio block and
the maker's volume cap, fault reasons and the internet probe on /api/status,
the owner's /api/diagnostics and the maker's support bundle, the knob drift
detector, the shared-overlay guard, Restart with a key press, the LED part id
in the Lamp check, metered / os_updates switches and the power-cut counter."""

from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from conftest import VARIANTS

from haslerd import audio, boxconfig, main
from haslerd.actions import Actions
from haslerd.bootresume import note_power_cut, power_cuts
from haslerd.lamp import faults
from haslerd.panel import controls
from haslerd.panel.controls import Event, Pot
from haslerd.player.base import FakePlayer
from haslerd.player.moode import MoodeConfig, MoodePlayer
from haslerd.variant import Panel, describe_backends, load_variant
from haslerd.web import diagnostics, lamp_check
from haslerd.web.confirm import ConfirmError
from haslerd.web.server import WebApp, load_settings, volume_limit_from, volume_limit_range
from haslerd.web.software import Software


@pytest.fixture
def paths(tmp_path):
    release = tmp_path / "release"
    (release / "config" / "variants").mkdir(parents=True)
    for name in ("hasler-hm.yaml", "hasler-hm-hat.yaml", "excelsior.yaml"):
        shutil.copy(VARIANTS / name, release / "config" / "variants" / name)
    (tmp_path / "state").mkdir()
    return boxconfig.Paths(release=release, etc=tmp_path / "etc", state=tmp_path / "state")


@pytest.fixture
def rig(tmp_path, paths):
    """A WebApp on the HM with a settings file, a state dir and box paths."""
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    player.config = main.moode_config({}, {}, variant.audio)
    actions = Actions(variant, player)
    app = WebApp(actions, panel, tmp_path / "state" / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "state" / "settings.json")
    app.box_paths = paths
    return app, actions, player, panel


# -- the audio block --------------------------------------------------------------


class TestAudioBlock:
    @pytest.mark.parametrize("name,speakers", [("hasler-hm", 1), ("hasler-hm-hat", 1), ("excelsior", 2)])
    def test_every_descriptor_states_its_audio_facts(self, name, speakers):
        v = load_variant(VARIANTS / f"{name}.yaml")
        assert v.audio["speakers"] == speakers
        assert v.audio["volume_limit_max"] == 90
        assert (v.audio["volume_limit_default"], v.audio["volume_floor_default"]) == (70, 28)
        assert v.audio["card"] == "sndrpihifiberry"
        assert v.io["lamp"]["led"] == "frosted-5mm-cA-220-100-100"

    def test_mono_follows_the_speaker_count(self):
        assert audio.mono_default({"speakers": 1}) is True
        assert audio.mono_default({"speakers": 2}) is False
        assert audio.mono_default({}) is audio.DEFAULT and audio.mono_default(None) is audio.DEFAULT
        assert audio.mono_default({"speakers": "two"}) is audio.DEFAULT

    def test_the_block_seeds_the_player_config(self):
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        cfg = main.moode_config({}, {}, v.audio)
        assert (cfg.volume_limit, cfg.volume_floor, cfg.volume_limit_max, cfg.card) == (70, 28, 90, "sndrpihifiberry")

    def test_an_older_descriptor_keeps_the_code_defaults(self):
        cfg = main.moode_config({}, {}, {})
        assert (cfg.volume_limit, cfg.volume_floor, cfg.volume_limit_max) == (70, 28, 100)
        assert main.audio_seed({"volume_limit_max": "loud", "card": ""}) == {}

    def test_box_yaml_wins_over_the_descriptor_and_the_page_over_box_yaml(self):
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        cfg = main.moode_config({"moode": {"volume_limit": 60}}, {"volume_limit": 55}, v.audio)
        assert cfg.volume_limit == 55
        assert main.moode_config({"moode": {"volume_limit": 60}}, {}, v.audio).volume_limit == 60

    def test_fake_player_takes_the_block_too(self):
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        assert main.fake_player({}, {}, v.audio).config.volume_limit_max == 90

    def test_build_mono_default_comes_from_the_block(self, tmp_path, monkeypatch):
        seen = []
        monkeypatch.setattr(audio.MonoSwitch, "available", property(lambda self: True))
        monkeypatch.setattr(audio.MonoSwitch, "set", lambda self, on, reopen=True: seen.append(on))
        monkeypatch.setattr(main.threading, "Thread",
                            lambda target, name, daemon: SimpleNamespace(start=target))
        main.build_mono(tmp_path, {"speakers": 2})
        main.build_mono(tmp_path, {"speakers": 1})
        assert seen == [False, True]


# -- the maker's cap -----------------------------------------------------------------


class TestVolumeCap:
    def test_range_is_bounded_by_the_cap(self):
        assert volume_limit_range(None) == (30, 100)
        assert volume_limit_range(90) == (30, 90)
        assert volume_limit_range(10) == (30, 30) and volume_limit_range(140) == (30, 100)
        assert volume_limit_from(90, 90) == 90
        with pytest.raises(ValueError, match="maker set the top at 90"):
            volume_limit_from(91, 90)
        with pytest.raises(ValueError):
            volume_limit_from(101, None)

    def test_settings_report_the_capped_range_and_whether_a_person_set_it(self, rig):
        app, *_ = rig
        doc = app.settings()
        assert doc["volume_limit_range"] == [30, 90]
        assert doc["volume_limit_set"] is False
        app.put_settings({"volume_limit": 85})
        doc = app.settings()
        assert doc["volume_limit"] == 85 and doc["volume_limit_set"] is True
        assert "volume_limit" in load_settings(app.settings_file)

    def test_put_above_the_cap_is_refused(self, rig):
        app, _, player, _ = rig
        with pytest.raises(ValueError, match="maker set the top at 90"):
            app.put_settings({"volume_limit": 95})
        assert player.config.volume_limit == 70

    def test_box_yaml_lowers_the_cap_per_radio(self):
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        cfg = main.moode_config({"moode": {"volume_limit_max": 80}}, {}, v.audio)
        assert cfg.volume_limit_max == 80 and volume_limit_range(cfg.volume_limit_max) == (30, 80)

    def test_a_saved_ceiling_above_the_cap_is_pulled_down_at_start(self, caplog):
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        with caplog.at_level(logging.WARNING, logger="haslerd"):
            cfg = main.moode_config({"moode": {"volume_limit_max": 80}}, {"volume_limit": 95}, v.audio)
        assert cfg.volume_limit == 80
        assert any("above the maker's cap" in r.getMessage() for r in caplog.records)
        # So is a default above it; garbage is still ignored.
        assert main.moode_config({"moode": {"volume_limit_max": 60, "volume_limit": 75}}, {}, {}).volume_limit == 60
        assert main.moode_config({}, {"volume_limit": "loud"}, v.audio).volume_limit == 70


# -- /api/status: fault reasons and the probe --------------------------------------------


class TestStatusHealth:
    def test_fault_reasons_online_and_cleared_at(self, rig):
        app, *_ = rig
        st = app.status()
        assert st["fault_reasons"] == [] and st["online"] is None and st["fault_cleared_at"] is None
        app.lamp_keeper = SimpleNamespace(fault_reasons=["storage", "heat"], fault_cleared_at=None)
        app.connectivity = SimpleNamespace(online=False, hotspot=False)
        st = app.status()
        assert st["fault_reasons"] == ["storage", "heat"] and st["online"] is False
        app.lamp_keeper = SimpleNamespace(fault_reasons=[], fault_cleared_at=time.time())
        at = app.status()["fault_cleared_at"]
        assert at and at[:4] == time.strftime("%Y") and at[-6] in "+-" and at[-3] == ":"

    def test_the_keeper_stamps_when_a_fault_clears(self):
        from test_lamp_policy import Clock, FakeDriver, FakeNet, FakePanel
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        clock = Clock()
        keeper = main.LampKeeper(FakeDriver(), Actions(variant, FakePlayer(), clock=clock), FakePanel(), FakeNet(), None,
                                 clock=clock, night=lambda when, settings: False)
        found = ["storage"]
        keeper.faults = lambda: list(found)
        keeper.tick(clock())
        assert keeper.fault_reasons == ["storage"] and keeper.fault_cleared_at is None
        found.clear()
        clock.t += faults.FAULT_POLL_S + 1
        keeper.tick(clock())
        assert keeper.fault_reasons == [] and isinstance(keeper.fault_cleared_at, float)

    def test_the_speaker_selector_says_whether_it_is_wired(self):
        variant = load_variant(VARIANTS / "excelsior.yaml")
        panel = Panel.from_variant(variant, fake=True)
        player = SimpleNamespace(current=lambda: None, get_volume=lambda: 30, active_source=lambda: "radio",
                                 speaker_route_wired=False, config=None)
        app = WebApp(Actions(variant, player), panel, None, {"radio": True}, ha=False)
        sel = app.status()["selectors"][0]
        assert sel["id"] == "speaker_sel" and sel["wired"] is False
        assert MoodePlayer.speaker_route_wired is False


# -- throttle bits, power cuts, backends -------------------------------------------------


class TestReadings:
    def test_throttled_decodes_the_firmware_word(self, tmp_path):
        f = tmp_path / "get_throttled"
        f.write_text("0x50005\n")
        doc = faults.throttled(f)
        assert doc["raw"] == "0x50005"
        assert doc["under_voltage_now"] and doc["throttled_now"] and not doc["freq_capped_now"]
        assert doc["under_voltage_seen"] and doc["throttled_seen"] and doc["any_now"] and doc["any_seen"]
        f.write_text("0\n")
        assert faults.throttled(f)["any_seen"] is False
        assert faults.throttled(tmp_path / "nope") is None
        f.write_text("fish\n")
        assert faults.throttled(f) is None

    def test_power_cuts_are_counted_atomically_and_per_month(self, tmp_path):
        assert power_cuts(tmp_path) == {"total": 0, "month": 0, "last_at": None, "hint": False}
        now = time.time()
        for i in range(3):
            note_power_cut(tmp_path, now + i)
        doc = power_cuts(tmp_path, now)
        assert doc["total"] == 3 and doc["month"] == 3 and doc["hint"] is True and doc["last_at"]
        kept = json.loads((tmp_path / "power-cuts.json").read_text())
        assert kept["total"] == 3 and len(kept["events"]) == 3
        assert not list(tmp_path.glob(".power-cuts.json-*"))     # the tmp file is gone
        # Last month's cuts don't count this month.
        last_month = power_cuts(tmp_path, now + 40 * 86400)
        assert last_month["total"] == 3 and last_month["month"] == 0 and last_month["hint"] is False
        assert note_power_cut(None) == {"total": 0, "events": []}

    def test_describe_backends_names_what_was_opened(self):
        from haslerd.panel.io_base import AbsentDigital, FakeAnalog, FakeDigital, NullAnalog
        doc = describe_backends(FakeDigital(["d0"]), FakeAnalog({0: 0}))
        assert doc == {"digital": {"backend": "fake"}, "analog": {"backend": "fake"}}
        doc = describe_backends(AbsentDigital("no board at 0x20"), NullAnalog())
        assert doc["digital"] == {"backend": "absent", "absent": True, "reason": "no board at 0x20"}
        mcp = type("Mcp23017Digital", (), {"addr": 0x27})()
        assert describe_backends(mcp, NullAnalog())["digital"] == {"backend": "mcp23017", "addr": "0x27"}
        panel = Panel.from_variant(load_variant(VARIANTS / "hasler-hm.yaml"), fake=True)
        assert panel.backends["digital"]["backend"] == "fake"


# -- /api/diagnostics -------------------------------------------------------------------


@pytest.fixture
def diag_rig(rig, tmp_path):
    app, actions, player, panel = rig
    (tmp_path / "temp").write_text("61234\n")
    (tmp_path / "throttled").write_text("0x80000\n")
    (tmp_path / "uptime").write_text("90061.5 123.4\n")
    app.cpu_temp_file = tmp_path / "temp"
    app.throttled_file = tmp_path / "throttled"
    app.uptime_file = tmp_path / "uptime"
    app.shutdown_kind = "power-cut"
    app.connectivity = SimpleNamespace(online=True, hotspot=False)
    app.lamp_keeper = SimpleNamespace(fault_reasons=["storage"], fault_cleared_at=None)
    app.wifi = SimpleNamespace(current_ssid=lambda: "Casa", signal_of=lambda ssid: 82 if ssid == "Casa" else None,
                               _hotspot=lambda: False)
    note_power_cut(tmp_path / "state")
    return app, actions, player, panel


class TestDiagnostics:
    def test_owner_doc_shape_and_words(self, diag_rig):
        app, *_ = diag_rig
        doc = app.diagnostics()
        assert set(doc) >= {"wifi", "online", "temperature", "storage", "last_shutdown", "power_cuts", "running",
                            "player", "lamp", "panel", "faults", "fault_cleared_at", "software", "variant", "version", "at"}
        assert doc["wifi"] == {"ssid": "Casa", "signal": 82, "hotspot": False, "words": "On Casa, good signal"}
        assert doc["online"] is True
        t = doc["temperature"]
        assert t["c"] == 61.2 and t["hot"] is False and t["throttled"]["soft_temp_limit_seen"] is True
        assert t["words"] == "warm (61 °C) · was slowed by heat once since switch-on"
        assert doc["storage"]["free_bytes"] > 0 and doc["storage"]["read_only"] is False and "free of" in doc["storage"]["words"]
        assert doc["last_shutdown"] == {"kind": "power-cut", "words": "the power was cut"}
        assert doc["power_cuts"]["total"] == 1 and doc["power_cuts"]["month"] == 1 and doc["power_cuts"]["hint"] is False
        assert doc["running"]["uptime_s"] == 90061 and doc["running"]["words"] == "1 d 1 h" and doc["running"]["since"]
        assert doc["player"]["dsp"] == {"installed": False, "running": None, "clipped_samples": None}
        assert doc["player"]["words"].startswith("player")
        assert doc["lamp"] == {"present": False, "words": "no lamp on this radio"}
        assert doc["faults"] == ["storage"] and doc["fault_cleared_at"] is None
        assert doc["software"] is None          # no updater on a bench box
        assert doc["variant"] == "hasler-hm" and doc["at"]

    def test_words_for_the_other_cases(self, tmp_path):
        assert diagnostics.shutdown_doc("clean")["words"] == "switched off properly"
        assert diagnostics.shutdown_doc("nope")["kind"] == "unknown"
        (tmp_path / "t").write_text("82000\n")
        hot = diagnostics.temperature_doc(tmp_path / "t", tmp_path / "none")
        assert hot["hot"] is True and hot["words"] == "too hot (82 °C)" and hot["throttled"] is None
        assert diagnostics.temperature_doc(tmp_path / "none", tmp_path / "none")["words"] == "not known"
        wifi = SimpleNamespace(current_ssid=lambda: "Casa", signal_of=lambda s: 20, _hotspot=lambda: False)
        assert diagnostics.wifi_doc(wifi)["words"] == "On Casa, weak signal"
        assert diagnostics.wifi_doc(wifi, hotspot=True)["words"] == "Setup mode: not on your Wi-Fi"
        assert diagnostics.wifi_doc(SimpleNamespace(current_ssid=lambda: ""))["words"] == "Not connected"
        assert diagnostics.running_doc(5.0, tmp_path / "none")["words"] == "1 min"

    def test_dsp_status_reads_the_helpers_json(self, tmp_path):
        cmd = tmp_path / "hasler-dsp"
        cmd.write_text("#!/bin/sh\n")

        def run(argv, **kw):
            assert argv == [str(cmd), "status"]
            return SimpleNamespace(stdout=json.dumps({"live": {"running": True, "clipped_samples": 0}}), stderr="")
        assert diagnostics.dsp_status(str(cmd), run) == {"installed": True, "running": True, "clipped_samples": 0}

        def broken(argv, **kw):
            raise OSError("no")
        assert diagnostics.dsp_status(str(cmd), broken)["running"] is None
        assert diagnostics.dsp_status(str(tmp_path / "absent"))["installed"] is False

    def test_bundle_has_the_makers_superset_and_no_secrets(self, diag_rig, tmp_path, monkeypatch):
        app, actions, *_ = diag_rig
        paths = app.box_paths
        paths.etc.mkdir(parents=True, exist_ok=True)
        paths.box_file.write_text("schema: 1\nvariant: hasler-hm-hat\nupdate_url: http://bench/secret\n"
                                  "metered: true\nweb:\n  hostnames: [kitchen]\n")
        boxconfig.write_overlay(paths, "hasler-hm", {"controls": {"volume": {"hysteresis_counts": 40}},
                                                     "io": {"digital": {"addr": 0x27}}})
        doc = load_settings(app.settings_file)
        doc.update({"volume_limit": 80, "room": {"name": "Kitchen"}, "ha_token": "secret-token",
                    "spotify": {"cookie": "abc", "password": "hunter2"}, "hotspot": {"psk": "x"}})
        app.settings_file.write_text(json.dumps(doc))

        def run(argv, **kw):
            if argv[0] == "journalctl":
                return SimpleNamespace(returncode=0, stdout="Nov 07 haslerd: token=abc login_url=http://x\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="Startup finished in 12s\n", stderr="")
        monkeypatch.setattr(diagnostics.subprocess, "run", run)
        bundle = diagnostics.bundle(app, run)
        assert set(bundle) >= {"generated_at", "diagnostics", "health", "info", "status", "knobs", "lamp_calibration",
                               "software", "descriptor", "backends", "overlay", "box", "settings", "pwm", "journal",
                               "bootcfg", "boot", "stream_events", "power_cuts"}
        assert bundle["descriptor"]["name"] == "hasler-hm" and bundle["descriptor"]["box_variant"] == "hasler-hm-hat"
        assert bundle["descriptor"]["path"].endswith("hasler-hm.yaml") and bundle["descriptor"]["audio"]["speakers"] == 1
        assert bundle["backends"]["digital"]["backend"] == "fake"
        assert bundle["overlay"]["controls"]["volume"]["hysteresis_counts"] == 40
        assert bundle["overlay"]["io"]["digital"]["addr"] == 0x27
        assert bundle["box"]["metered"] is True and "update_url" not in bundle["box"]
        assert bundle["settings"]["volume_limit"] == 80 and bundle["journal"]["exit"] == 0
        assert bundle["boot"]["timeline"]["text"].startswith("Startup")
        assert bundle["bootcfg"] is None and bundle["pwm"]["period_ns"] is None
        assert bundle["lamp_calibration"]["led"] == "frosted-5mm-cA-220-100-100" and "looks" not in bundle["lamp_calibration"]
        assert "/" not in json.dumps(bundle["backends"])
        bad = []

        def walk(node, path=""):
            if isinstance(node, dict):
                for k, v in node.items():
                    if diagnostics.SECRET_KEY.search(str(k)):
                        bad.append(path + "/" + str(k))
                    walk(v, path + "/" + str(k))
            elif isinstance(node, list):
                for v in node:
                    walk(v, path)
        walk(bundle)
        assert bad == [], bad
        assert "hunter2" not in json.dumps(bundle) and "secret-token" not in json.dumps(bundle)

    def test_strip_secrets(self):
        doc = {"a": {"token": 1, "Password": 2, "psk": 3, "fine": [{"cookie": 4, "ok": 5}]}, "login_url": "x", "name": "y"}
        assert diagnostics.strip_secrets(doc) == {"a": {"fine": [{"ok": 5}]}, "name": "y"}

    def test_http_routes(self, diag_rig):
        import urllib.request
        from haslerd.web.server import WebServer
        app, *_ = diag_rig
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            with urllib.request.urlopen(base + "/api/diagnostics", timeout=5) as r:
                assert json.loads(r.read())["last_shutdown"]["kind"] == "power-cut"
            with urllib.request.urlopen(base + "/api/diagnostics/bundle", timeout=20) as r:
                assert "descriptor" in json.loads(r.read())
        finally:
            server.close()


# -- Restart the radio --------------------------------------------------------------------


class TestRestart:
    def test_needs_a_confirmed_press_then_reboots(self, rig):
        app, actions, player, panel = rig
        with pytest.raises(ConfirmError):
            app.restart({})
        with pytest.raises(ConfirmError):
            app.restart({"confirm": "nope"})
        token = app.confirm.request("restart")["token"]
        assert "reboot" not in player.verbs()
        app.confirm.intercept([Event("k1", "tap")])
        assert app.restart({"confirm": token}) == {"restarting": True}
        assert player.verbs()[-1] == "reboot"
        with pytest.raises(ConfirmError):       # once
            app.restart({"confirm": token})

    def test_a_token_for_another_action_is_no_good(self, rig):
        app, *_ = rig
        token = app.confirm.request("reset")["token"]
        app.confirm.intercept([Event("k1", "tap")])
        with pytest.raises(ConfirmError):
            app.restart({"confirm": token})

    def test_sudoers_allows_exactly_reboot(self):
        text = (Path(__file__).resolve().parents[2] / "config" / "sudoers" / "haslerd").read_text()
        assert "/usr/bin/systemctl reboot" in text and "/bin/systemctl reboot" in text
        unit = (Path(__file__).resolve().parents[2] / "config" / "haslerd.service").read_text()
        assert "SupplementaryGroups=gpio i2c audio systemd-journal" in unit


# -- the knob drift detector ------------------------------------------------------------


class TestDrift:
    CURVE = [(740, 100.0), (26240, 0.0)]

    def pot(self, hysteresis=30):
        return Pot("tone", 0, curve=self.CURVE, hysteresis_counts=hysteresis, median_n=1)

    def feed(self, pot, raws, t0=0.0, dt=0.02):
        out = []
        for i, raw in enumerate(raws):
            out.extend(pot.update(t0 + i * dt, {}, {0: raw}))
        return out

    def test_a_knob_at_its_end_is_not_drift_but_past_the_margin_is(self):
        pot = self.pot()
        # Resting at the physical stop, 20 counts past the curve's end: fine (the ends sit inside the stops).
        self.feed(pot, [26260] * 200)
        assert pot.drift is None
        # 200 counts past the end (> 3 x hysteresis = 90) for over 3 s: past_end.
        pot = self.pot()
        self.feed(pot, [26440] * 100)
        assert pot.drift is None                         # not yet: 2 s
        self.feed(pot, [26440] * 60, t0=2.0)
        assert pot.drift == "past_end"
        # Back inside: cleared.
        self.feed(pot, [20000] * 5, t0=4.0)
        assert pot.drift is None

    def test_noise_widens_the_margin(self):
        pot = self.pot(hysteresis=10)
        # +-60 counts of noise around 150 past the end: 3 x noise (180) > 150, so no drift.
        raws = [26390 + (60 if i % 2 else -60) for i in range(300)]
        self.feed(pot, raws)
        assert pot.drift is None

    def test_a_wandering_wiper_while_the_knob_is_still(self):
        # One scratch in five: the median-of-5 never moves (nothing is committed),
        # but the raw readings spread 150 counts (> 3 x hysteresis) over the window.
        pot = Pot("tone", 0, curve=self.CURVE, hysteresis_counts=30, median_n=5)
        self.feed(pot, [13000] * 60)
        assert pot.drift is None
        scratchy = [13150 if i % 5 == 4 else 13000 for i in range(60)]
        self.feed(pot, scratchy, t0=2.0)
        assert pot.drift == "wander" and pot.state() == pot.scale(13000)
        # The knob turned (a change committed): not wander any more.
        self.feed(pot, [15000] * 5, t0=4.0)
        assert pot.drift is None

    def test_a_full_scale_jump_is_held_one_poll(self):
        pot = self.pot()
        events = self.feed(pot, [13000] * 5)
        assert events and pot.state() is not None
        before = pot.state()
        # A loose wiper: full scale in one poll - the mixer keeps its level this poll.
        assert pot.update(1.0, {}, {0: 740}) == []
        assert pot.state() == before
        # Back where it was next poll: nothing was ever committed.
        assert pot.update(1.02, {}, {0: 13000}) == []
        assert pot.state() == before
        # The next poll agreeing commits it: a real fast turn is 20 ms late, not lost.
        assert pot.update(1.04, {}, {0: 740}) == []
        out = pot.update(1.06, {}, {0: 745})
        assert out and out[0].kind == "change" and out[0].value == 100.0

    def test_small_moves_are_never_held(self):
        pot = self.pot()
        self.feed(pot, [13000] * 5)
        out = pot.update(1.0, {}, {0: 14000})            # 1000 counts: a hand, well under a quarter range
        assert out and out[0].kind == "change"

    def test_thresholds_are_documented_constants(self):
        assert controls.DRIFT_HOLD_S == 3.0 and controls.DRIFT_END_HYSTERESIS == 3
        assert controls.DRIFT_WANDER_POLLS == 50 and controls.EXCURSION_FRACTION == 0.25

    def test_api_knobs_carries_the_flag(self, rig):
        app, actions, player, panel = rig
        tone = next(c for c in panel.controls if c.id == "tone")
        tone.drift = "past_end"
        docs = {k["id"]: k for k in app.knobs()["knobs"]}
        assert docs["tone"]["drift"] == "past_end" and docs["volume"]["drift"] is None


# -- the shared-overlay guard ---------------------------------------------------------------


class TestOverlayGuard:
    def test_a_pinned_descriptor_ignores_the_overlays_address(self, caplog):
        hat = load_variant(VARIANTS / "hasler-hm-hat.yaml")
        with caplog.at_level(logging.WARNING, logger="haslerd.boxconfig"):
            out = boxconfig.effective(hat, {"controls": {}, "io": {"digital": {"addr": 0x27}, "lamp": {"pwm_hz": 400}}})
        assert out.io["digital"]["addr"] == 0x20 and out.io["lamp"]["pwm_hz"] == 400
        assert any("pins 32" in r.getMessage() or "pins" in r.getMessage() for r in caplog.records)

    def test_an_auto_descriptor_takes_it(self):
        hm = load_variant(VARIANTS / "hasler-hm.yaml")
        out = boxconfig.effective(hm, {"controls": {}, "io": {"digital": {"addr": 0x27}}})
        assert out.io["digital"]["addr"] == 0x27

    def test_guard_leaves_other_digital_keys_and_never_mutates(self):
        shipped = {"digital": {"backend": "mcp23017", "addr": 0x20}}
        overlay = {"digital": {"addr": 0x27, "int_gpio": 5}}
        assert boxconfig.guard_io_overlay(shipped, overlay) == {"digital": {"int_gpio": 5}}
        assert overlay == {"digital": {"addr": 0x27, "int_gpio": 5}}
        assert boxconfig.guard_io_overlay(shipped, {"digital": {"addr": 0x27}}) == {}
        assert boxconfig.guard_io_overlay(shipped, {"digital": {"addr": 0x20}}) == {"digital": {"addr": 0x20}}

    def test_through_the_box_loader(self, paths):
        paths.etc.mkdir(parents=True, exist_ok=True)
        paths.box_file.write_text("schema: 1\nvariant: hasler-hm-hat\n")
        boxconfig.write_overlay(paths, "hasler-hm", {"io": {"digital": {"addr": 0x27}}})
        v = boxconfig.load_effective_variant(paths, "hasler-hm-hat")
        assert v.io["digital"]["addr"] == 0x20 and v.overlay_path is not None


# -- the LED part id in the Lamp check ---------------------------------------------------------


class TestLampLed:
    CAL = {"min_duty": {"red": 0.01, "green": 0.005, "blue": 0.02}, "balance": {"red": 1, "green": 1, "blue": 1},
           "palette": {"amber": {"day": [1, 0.2, 0], "night": [1, 0.15, 0]}, "ember": [1, 0.1, 0], "blue": [0, 0, 1]},
           "level_gain": {"amber": 1, "ember": 1, "blue": 1}, "levels": {"ember_day": 0.25, "ember_night": 0.15}}

    def test_stamp_and_led_changed(self, rig):
        app, *_ = rig
        app.lamp = None
        doc = app.lamp_calibration()
        assert doc["led"] == "frosted-5mm-cA-220-100-100" and doc["led_changed"] is False
        app.put_lamp_calibration(dict(self.CAL))
        saved = yaml.safe_load(app.box_paths.overlay_file("hasler-hm").read_text())["io"]["lamp"]
        assert saved["calibrated"]["led"] == "frosted-5mm-cA-220-100-100"
        assert app.lamp_calibration()["led_changed"] is False
        # The descriptor names another part now: the old check is flagged.
        saved["calibrated"]["led"] = "clear-3mm"
        overlay = yaml.safe_load(app.box_paths.overlay_file("hasler-hm").read_text())
        overlay["io"]["lamp"] = saved
        app.box_paths.overlay_file("hasler-hm").write_text(yaml.safe_dump(overlay))
        assert app.lamp_calibration()["led_changed"] is True

    def test_stamp_without_a_part_id(self):
        assert "led" not in lamp_check.stamp(dict(self.CAL), 40.0, 0)["calibrated"]
        assert lamp_check.stamp(dict(self.CAL), 40.0, 0, led="x")["calibrated"]["led"] == "x"
        assert lamp_check.led_of({"led": " "}) is None and lamp_check.led_of(None) is None


# -- metered and os_updates ----------------------------------------------------------------------


class TestUpdateSwitches:
    def software(self, tmp_path, text="schema: 1\nvariant: hasler-hm\n"):
        etc = tmp_path / "etc"
        etc.mkdir(exist_ok=True)
        (etc / "box.yaml").write_text(text)
        paths = boxconfig.Paths(release=tmp_path, etc=etc, state=tmp_path / "state")
        return Software(root=tmp_path / "root", box_paths=paths, systemctl=lambda u: None), paths

    def test_defaults_and_words(self, tmp_path):
        sw, _ = self.software(tmp_path)
        assert sw.box_settings()["metered"] is False and sw.box_settings()["os_updates"] is True
        sw, _ = self.software(tmp_path, "schema: 1\nmetered: true\nos_updates: off\n")
        assert sw.box_settings()["metered"] is True and sw.box_settings()["os_updates"] is False
        sw, _ = self.software(tmp_path, "schema: 1\nos_updates: no\n")
        assert sw.box_settings()["os_updates"] is False

    def test_put_writes_words_the_line_readers_understand(self, tmp_path):
        sw, paths = self.software(tmp_path)
        assert sw.put_settings({"metered": True})["metered"] is True
        assert "metered: true" in paths.box_file.read_text()
        sw.put_settings({"os_updates": False})
        text = paths.box_file.read_text()
        assert "\nos_updates: false\n" in text and "\nmetered: true\n" in text
        # scripts/hasler-os-update and the updater read the line itself, without YAML:
        # one key per line, never PyYAML's {schema: 1, ...} fold of a flat file.
        assert not any(l.startswith("{") for l in text.splitlines())
        line = next(l for l in text.splitlines() if l.startswith("os_updates:"))
        assert line.partition(":")[2].strip() in ("false", "off")
        assert sw.box_settings()["os_updates"] is False
        sw.put_settings({"metered": False, "os_updates": True})
        text = paths.box_file.read_text()
        assert "metered" not in text and "os_updates" not in text    # the defaults: kept short

    def test_bad_values_are_400s(self, tmp_path):
        sw, _ = self.software(tmp_path)
        with pytest.raises(ValueError):
            sw.check_settings({"metered": "yes"})
        with pytest.raises(ValueError):
            sw.check_settings({})

    def test_status_carries_metered(self, tmp_path):
        sw, paths = self.software(tmp_path, "schema: 1\nmetered: true\n")
        root = tmp_path / "root"
        (root / "var/lib/hasler-update").mkdir(parents=True)
        (root / "var/lib/hasler-update/status.json").write_text("{}")
        assert sw.status()["metered"] is True
