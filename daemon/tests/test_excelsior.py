"""The Excelsior as a whole: its keys on the key page, what they do, what the
page sees of key 0, SP and the speaker selector - with the bank latching (as
built) and not (a modified unit, set per radio in variant.d)."""

import copy

import pytest
import yaml
from conftest import VARIANTS

from haslerd import boxconfig
from haslerd.actions import Actions
from haslerd.panel.controls import InterlockedBank, Momentary, bank_keys, build_controls, release_key
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.server import WebApp

DT = 0.02
BANK_IDS = ["k1", "k2", "k3", "k4", "k5", "k6", "pu", "tape"]
PINS = {"k0": "d0", "k1": "d8", "k2": "d9", "k3": "d10", "k4": "d11", "k5": "d12", "k6": "d13",
        "pu": "d14", "tape": "d1"}


def non_latching(variant):
    """The same descriptor with the per-radio overlay of a modified unit."""
    return boxconfig.effective(variant, {"controls": {"presets": {"latching": False}}})


class Radio:
    def __init__(self, tmp_path, latching=True):
        variant = load_variant(VARIANTS / "excelsior.yaml")
        if not latching:
            variant = non_latching(variant)
        self.variant = variant
        self.player = FakePlayer()
        self.actions = Actions(variant, self.player)
        self.panel = Panel.from_variant(variant, fake=True)
        self.keys_file = tmp_path / "keys.yaml"
        self.app = WebApp(self.actions, self.panel, self.keys_file, {"radio": True, "spotify": True, "ytm": True},
                          ha=False)
        self.app.load_saved()
        self.t = 0.0

    def run(self, seconds):
        end = self.t + seconds
        while self.t <= end + 1e-9:
            self.actions.dispatch_all(self.panel.poll(self.t))
            self.t = round(self.t + DT, 4)

    def latch(self, kid):
        """Press a latching key: the old one pops up first (the changeover gap)."""
        self.panel.digital.clear()
        self.run(0.02)
        self.panel.digital.set(PINS[kid], True)
        self.run(0.2)

    def press(self, kid, seconds=0.1):
        self.panel.digital.set(PINS[kid], True)
        self.run(seconds)
        self.panel.digital.set(PINS[kid], False)
        self.run(0.1)


@pytest.fixture
def radio(tmp_path):
    return Radio(tmp_path)


@pytest.fixture
def modified(tmp_path):
    return Radio(tmp_path, latching=False)


def page_keys(cfg, **changes):
    keys = []
    for key in cfg["keys"]:
        k = {f: v for f, v in key.items() if f in ("id", "tap", "hold")}
        k.update(changes.get(key["id"], {}))
        keys.append(k)
    return keys


class TestDescriptor:
    def test_bank_keys_and_key_0_from_the_descriptor(self):
        spec = next(c for c in load_variant(VARIANTS / "excelsior.yaml").controls if c["id"] == "presets")
        assert [k["id"] for k in bank_keys(spec)] == BANK_IDS
        assert [k["label"] for k in bank_keys(spec)] == ["1", "2", "3", "4", "5", "6", "PU", "Tape"]
        assert release_key(spec) == {"id": "k0", "label": "0", "pin": "d0"}

    def test_a_bank_without_keys_names_its_positions(self):
        spec = {"id": "bank", "kind": "interlocked_bank", "pins": ["d1", "d2"]}
        assert bank_keys(spec) == [{"id": "bank_1", "label": "1"}, {"id": "bank_2", "label": "2"}]
        assert release_key(spec) is None

    def test_latching_builds_one_bank_and_key_0(self):
        controls = build_controls(load_variant(VARIANTS / "excelsior.yaml").controls)
        assert isinstance(controls[0], InterlockedBank) and controls[0].id == "presets"
        assert isinstance(controls[1], Momentary) and controls[1].id == "k0"

    def test_not_latching_builds_momentary_keys_key_0_first(self):
        controls = build_controls(non_latching(load_variant(VARIANTS / "excelsior.yaml")).controls)
        keys = [c for c in controls if isinstance(c, Momentary)]
        assert [c.id for c in keys] == ["k0", *BANK_IDS]
        assert [c.pin for c in keys] == [PINS[k] for k in ["k0", *BANK_IDS]]
        assert not any(isinstance(c, InterlockedBank) for c in controls)

    def test_overlay_file_switches_latching_off(self, tmp_path):
        paths = boxconfig.Paths(release=VARIANTS.parent.parent, etc=tmp_path / "etc", state=tmp_path / "state")
        assert boxconfig.load_effective_variant(paths, "excelsior").controls[0].get("latching") is True
        overlay = paths.overlay_file("excelsior")
        overlay.parent.mkdir(parents=True)
        overlay.write_text("schema: 1\ncontrols:\n  presets: {latching: false}\n")
        variant = boxconfig.load_effective_variant(paths, "excelsior")
        panel = Panel.from_variant(variant, fake=True)
        assert "presets" not in [c.id for c in panel.controls]
        assert keymap.key_ids(Actions(variant, FakePlayer())) == ["k0", *BANK_IDS]

    def test_overlay_writer_accepts_it(self, tmp_path):
        paths = boxconfig.Paths(release=VARIANTS.parent.parent, etc=tmp_path / "etc", state=tmp_path / "state")
        boxconfig.write_overlay(paths, "excelsior", {"controls": {"presets": {"latching": False}}})
        assert boxconfig.read_overlay(paths, "excelsior")["controls"] == {"presets": {"latching": False}}


class TestLatchingConfig:
    def test_keys_are_the_bank_with_no_hold(self, radio):
        cfg = radio.app.config()
        assert [k["id"] for k in cfg["keys"]] == BANK_IDS
        assert [k["label"] for k in cfg["keys"]] == ["1", "2", "3", "4", "5", "6", "PU", "Tape"]
        assert all(k["latching"] and k["hold"] is None for k in cfg["keys"])
        assert cfg["keys"][2]["tap"] == {"do": "preset", "n": 3}
        assert cfg["keys"][6]["tap"] == {"do": "bluetooth"}
        assert cfg["keys"][7]["tap"] == {"do": "preset", "n": 7}
        assert len(cfg["presets"]) == 7

    def test_key_0_is_shown_fixed(self, radio):
        cfg = radio.app.config()
        assert [k["id"] for k in cfg["fixed_keys"]] == ["k0"]
        k0 = cfg["fixed_keys"][0]
        assert k0["label"] == "0" and k0["release"] is True
        assert k0["tap"]["spec"] == {"do": "standby"} and k0["hold"]["spec"] == {"do": "shutdown"}

    def test_no_combinations(self, radio):
        cfg = radio.app.config()
        assert cfg["combo_keys"] == [] and cfg["combos"] == []

    def test_title_from_the_descriptor(self, radio):
        assert radio.app.config()["title"] == "Excelsior"
        hm = load_variant(VARIANTS / "hasler-hm.yaml")
        assert keymap.config_doc(Actions(hm, FakePlayer()), {}, False)["title"] == "Hasler HM · Bern 1964"


class TestLatchingDispatch:
    def test_selecting_a_key_plays_its_preset(self, radio):
        radio.latch("k3")
        assert radio.player.playing.n == 3

    def test_tape_and_pu(self, radio):
        radio.latch("tape")
        assert radio.player.playing.n == 7
        radio.latch("pu")
        assert radio.player.source == "bluetooth"

    def test_a_latched_key_staying_down_fires_no_hold(self, radio):
        radio.latch("pu")
        calls = list(radio.player.calls)
        radio.run(2.0)
        assert radio.player.calls == calls
        assert not radio.actions.pairing

    def test_assignment_from_the_page_is_what_the_key_does(self, radio):
        cfg = radio.app.config()
        radio.app.save({"presets": cfg["presets"],
                        "keys": page_keys(cfg, k3={"tap": {"do": "preset", "n": 5}, "hold": {"do": "standby_toggle"}})})
        radio.latch("k3")
        assert radio.player.playing.n == 5

    def test_selecting_the_preset_already_playing_keeps_it(self, radio):
        radio.app.press({"key": "k3"})              # the phone started it
        radio.actions.clock = lambda: 1e9           # well past the bounce guard
        radio.latch("k3")
        assert radio.player.playing.n == 3 and not radio.actions.standby

    def test_key_0_pops_the_bank_into_standby(self, radio):
        radio.latch("k2")
        radio.panel.digital.clear()
        radio.press("k0", 0.1)
        assert radio.actions.standby is True

    def test_bank_without_keys_still_plays_by_index(self):
        variant = load_variant(VARIANTS / "excelsior.yaml")
        bank = next(c for c in variant.controls if c["id"] == "presets")
        del bank["keys"]
        variant.actions = {k: v for k, v in variant.actions.items() if not k.endswith(".tap")}
        variant.actions["presets.select"] = {"do": "preset_by_index"}
        player = FakePlayer()
        from haslerd.panel.controls import Event
        Actions(variant, player).dispatch(Event("presets", "select", 1))
        assert player.playing.n == 2


class TestLatchingSave:
    def test_save_round_trips_and_holds_are_ignored(self, radio, tmp_path):
        cfg = radio.app.config()
        out = radio.app.save({"presets": cfg["presets"], "keys": page_keys(
            cfg, tape={"tap": {"do": "cycle_source"}, "hold": {"do": "bt_pair", "seconds": 60}})})
        tape = next(k for k in out["keys"] if k["id"] == "tape")
        assert tape["tap"] == {"do": "cycle_source"} and tape["hold"] is None
        saved = yaml.safe_load(radio.keys_file.read_text())
        assert saved["actions"]["tape.tap"] == {"do": "cycle_source"}
        assert "tape.hold" not in saved["actions"]
        # A restart reads it back.
        again = Radio(tmp_path)
        assert next(k for k in again.app.config()["keys"] if k["id"] == "tape")["tap"] == {"do": "cycle_source"}

    def test_key_0_is_not_a_key_to_save(self, radio):
        cfg = radio.app.config()
        with pytest.raises(keymap.Invalid):
            radio.app.save({"presets": cfg["presets"], "keys": [{"id": "k0", "tap": {"do": "standby_toggle"}}]})

    def test_combinations_are_refused(self, radio):
        cfg = radio.app.config()
        with pytest.raises(keymap.Invalid):
            radio.app.save({"presets": cfg["presets"], "keys": page_keys(cfg),
                            "combos": [{"keys": ["k1", "k2"], "do": "standby_toggle"}]})

    def test_reset_brings_back_the_descriptor(self, radio):
        cfg = radio.app.config()
        radio.app.save({"presets": cfg["presets"], "keys": page_keys(cfg, k1={"tap": None})})
        assert radio.app.config()["keys"][0]["tap"] is None
        assert radio.app.reset()["keys"][0]["tap"] == {"do": "preset", "n": 1}


class TestLatchingPress:
    def test_phone_taps_a_latching_key(self, radio):
        radio.app.press({"key": "k4"})
        assert radio.player.playing.n == 4

    def test_phone_taps_key_0(self, radio):
        radio.app.press({"key": "k1"})
        radio.app.press({"key": "k0"})
        assert radio.actions.standby is True

    @pytest.mark.parametrize("key", ["k0", "k1", "pu"])
    def test_no_hold_from_the_phone(self, radio, key):
        with pytest.raises(ValueError, match="no hold"):
            radio.app.press({"key": key, "event": "hold"})


class TestLatchingStatus:
    def test_latched_key_sp_and_speakers(self, radio):
        radio.panel.digital.set("d2", True)     # SP down
        radio.panel.digital.set("d3", True)     # speaker selector: "10" = Both
        radio.latch("k5")
        radio.panel.digital.set("d2", True)
        radio.panel.digital.set("d3", True)
        radio.run(0.2)
        st = radio.app.status()
        assert st["latched"] == "k5"
        assert st["switches"] == [{"id": "sp", "label": "SP", "on": True}]
        assert st["selectors"][0] == {"id": "speaker_sel", "label": "Speakers", "position": 0,
                                      "positions": ["Both", "Left", "Right"], "text": "Both", "wired": True}
        assert [k["id"] for k in st["keys"]] == BANK_IDS
        # Tape takes the starter set's seventh entry (config/presets/dial.yaml).
        assert st["keys"][7] == {"id": "tape", "label": "Tape", "n": 7, "name": "KEXP", "short": "KEXP",
                                 "source": "radio"}

    def test_nothing_latched_after_key_0(self, radio):
        radio.latch("k5")
        radio.panel.digital.clear()
        radio.run(0.2)
        assert radio.app.status()["latched"] is None

    def test_treble_and_bass(self, radio):
        radio.panel.analog.set(0, 26400)
        radio.run(0.3)
        knobs = radio.app.status()["tone_knobs"]
        assert [k["id"] for k in knobs] == ["treble", "bass"]
        assert [k["label"] for k in knobs] == ["Treble", "Bass"]
        assert knobs[0]["value"] == 100

    def test_hm_has_none_of_it(self, tmp_path):
        hm = load_variant(VARIANTS / "hasler-hm.yaml")
        app = WebApp(Actions(hm, FakePlayer()), Panel.from_variant(hm, fake=True), None, {"radio": True}, ha=False)
        st, cfg = app.status(), app.config()
        assert (st["latched"], st["switches"], st["selectors"], st["tone_knobs"]) == (None, [], [], [])
        assert cfg["fixed_keys"] == []
        assert [k["id"] for k in cfg["keys"]] == ["k1", "k2", "k3", "k4", "k5", "k6", "pu"]
        assert not any(k["latching"] for k in cfg["keys"])
        assert cfg["keys"][6]["hold"] == {"do": "bt_pair", "seconds": 120}


class TestNotLatching:
    def test_keys_have_tap_and_hold_key_0_first(self, modified):
        cfg = modified.app.config()
        assert [k["id"] for k in cfg["keys"]] == ["k0", *BANK_IDS]
        assert [k["label"] for k in cfg["keys"]][:2] == ["0", "1"]
        assert not any(k["latching"] for k in cfg["keys"])
        assert cfg["keys"][0]["hold"] == {"do": "custom", "label": "shutdown", "spec": {"do": "shutdown"}}
        assert next(k for k in cfg["keys"] if k["id"] == "pu")["hold"] == {"do": "bt_pair", "seconds": 120}
        assert cfg["fixed_keys"] == []

    def test_combinations_are_offered(self, modified):
        assert modified.app.config()["combo_keys"] == ["k0", *BANK_IDS]

    def test_taps_and_holds_from_the_contacts(self, modified):
        modified.press("k2")                    # key 2: a station (key 3 holds a list)
        assert modified.player.playing.n == 2
        modified.press("pu", 1.0)
        assert modified.actions.pairing
        modified.press("k2")                    # the same key again: the station stops
        assert modified.actions.standby

    def test_key_0_is_assignable(self, modified):
        cfg = modified.app.config()
        modified.app.save({"presets": cfg["presets"], "keys": page_keys(cfg, k0={"tap": {"do": "preset", "n": 2}})})
        modified.press("k0")
        assert modified.player.playing.n == 2

    def test_a_volume_hold_ramps(self, modified):
        cfg = modified.app.config()
        modified.app.save({"presets": cfg["presets"], "keys": page_keys(cfg, k6={"hold": {"do": "volume_up"}})})
        k6 = next(c for c in modified.panel.controls if c.id == "k6")
        assert k6.repeat_s == keymap.VOLUME_REPEAT_S

    def test_a_combination_saves_and_fires(self, modified):
        cfg = modified.app.config()
        out = modified.app.save({"presets": cfg["presets"], "keys": page_keys(cfg),
                                 "combos": [{"keys": ["tape", "k1"], "hold_s": 1.0, "do": "standby_toggle"}]})
        assert out["combos"] == [{"keys": ["k1", "tape"], "hold_s": 1.0, "do": "standby_toggle"}]
        assert [c.id for c in modified.panel.combos.combos] == ["k1+tape"]

    def test_status_has_no_latched_key(self, modified):
        modified.panel.digital.set(PINS["k2"], True)
        modified.run(0.1)
        st = modified.app.status()
        assert st["latched"] is None and st["pressed"] == ["k2"]

    def test_the_same_keys_yaml_serves_both(self, tmp_path):
        latching = Radio(tmp_path)
        cfg = latching.app.config()
        latching.app.save({"presets": cfg["presets"], "keys": page_keys(cfg, k2={"tap": {"do": "preset", "n": 6}})})
        modified = Radio(tmp_path, latching=False)
        assert next(k for k in modified.app.config()["keys"] if k["id"] == "k2")["tap"] == {"do": "preset", "n": 6}
        modified.press("k2")
        assert modified.player.playing.n == 6


def test_descriptor_is_not_mutated_by_the_overlay():
    variant = load_variant(VARIANTS / "excelsior.yaml")
    before = copy.deepcopy(variant.controls)
    non_latching(variant)
    assert variant.controls == before
