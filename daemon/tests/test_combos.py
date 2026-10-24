"""Key combinations (panel/combos.py): two keys held together, and the key
page's config, persistence and "Try it" for them (web/keymap.py, web/server.py)."""

import json
import urllib.error
import urllib.request

import pytest
import yaml
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.panel.combos import RELEASE_S, ComboWatch
from haslerd.panel.controls import EndSwitch, Event, build_controls
from haslerd.panel.io_base import FakeDigital, NullAnalog
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.server import WebApp, WebServer

DT = 0.02           # the panel loop's 50 Hz

KEYS = [
    {"id": "k1", "kind": "momentary", "pin": "d0"},
    {"id": "k2", "kind": "momentary", "pin": "d1"},
    {"id": "k3", "kind": "momentary", "pin": "d2"},
    {"id": "k5", "kind": "momentary", "pin": "d3", "repeat_ms": 150},
    {"id": "k6", "kind": "momentary", "pin": "d4", "repeat_ms": 150},
    {"id": "pu", "kind": "momentary", "pin": "d5"},
]
PIN = {k["id"]: k["pin"] for k in KEYS}


class Rig:
    """A panel of momentary keys driven by a script of (time, key, down)."""

    def __init__(self, combos=None):
        self.digital = FakeDigital([k["pin"] for k in KEYS])
        self.panel = Panel(build_controls(KEYS), self.digital, NullAnalog())
        if combos is not None:
            self.panel.set_combos(combos)
        self.t = 0.0

    def run(self, script, until):
        """Poll at 50 Hz to ``until``, applying ``script`` edges on the way.
        Returns [(time, str(event))]."""
        script = sorted(script)
        out = []
        while self.t <= until + 1e-9:
            while script and script[0][0] <= self.t + 1e-9:
                _, cid, down = script.pop(0)
                self.digital.set(PIN[cid], down)
            for e in self.panel.poll(self.t):
                out.append((round(self.t, 2), str(e)))
            self.t = round(self.t + DT, 4)
        return out


def names(events):
    return [e for _, e in events]


PAIR = [{"keys": ["k1", "k2"], "hold_s": 1.0, "do": "bt_pair", "seconds": 120}]


class TestDetection:
    def test_combo_fires_after_hold_s_and_swallows_both_keys(self):
        rig = Rig(PAIR)
        events = rig.run([(0.0, "k1", True), (0.2, "k2", True), (2.0, "k1", False), (2.05, "k2", False)], 3.0)
        assert names(events) == ["k1+k2.combo"]
        t, _ = events[0]
        assert 1.2 <= t <= 1.3            # 1.0 s after the second key, plus debounce

    def test_either_order_same_id(self):
        rig = Rig(PAIR)
        events = rig.run([(0.0, "k2", True), (0.1, "k1", True), (1.5, "k1", False), (1.5, "k2", False)], 2.5)
        assert names(events) == ["k1+k2.combo"]

    def test_fires_once_however_long_held(self):
        rig = Rig(PAIR)
        events = rig.run([(0.0, "k1", True), (0.1, "k2", True), (6.0, "k1", False), (6.0, "k2", False)], 7.0)
        assert names(events) == ["k1+k2.combo"]

    def test_early_release_does_nothing(self):
        rig = Rig(PAIR)
        events = rig.run([(0.0, "k1", True), (0.2, "k2", True), (0.7, "k2", False), (0.9, "k1", False)], 3.0)
        assert events == []

    def test_early_release_of_one_then_other_held_long_does_nothing(self):
        # k2 let go early; k1 stays down past its own hold: still nothing.
        rig = Rig(PAIR)
        events = rig.run([(0.0, "k1", True), (0.2, "k2", True), (0.5, "k2", False), (2.5, "k1", False)], 3.0)
        assert events == []

    def test_keys_work_normally_afterwards(self):
        rig = Rig(PAIR)
        rig.run([(0.0, "k1", True), (0.2, "k2", True), (1.5, "k1", False), (1.5, "k2", False)], 2.0)
        events = rig.run([(2.2, "k1", True), (2.3, "k1", False)], 3.0)
        assert names(events) == ["k1.tap"]

    def test_second_key_after_first_held_is_no_combo(self):
        # k1 fired its own hold before k2 went down: both behave as today.
        rig = Rig(PAIR)
        events = rig.run([(0.0, "k1", True), (1.0, "k2", True), (1.2, "k2", False), (2.5, "k1", False)], 3.0)
        assert names(events) == ["k1.hold", "k2.tap"]

    def test_third_key_ignored(self):
        # k3 is half of another combination: swallowed while k1+k2 is held.
        rig = Rig(PAIR + [{"keys": ["k3", "pu"], "do": "cycle_source"}])
        events = rig.run([
            (0.0, "k1", True), (0.1, "k2", True), (0.4, "k3", True), (0.6, "k3", False),
            (1.5, "k1", False), (1.5, "k2", False),
        ], 2.5)
        assert names(events) == ["k1+k2.combo"]

    def test_third_key_pressed_after_firing_is_swallowed_too(self):
        rig = Rig(PAIR + [{"keys": ["k2", "k3"], "do": "cycle_source"}])
        events = rig.run([
            (0.0, "k1", True), (0.1, "k2", True), (1.5, "k3", True), (1.6, "k3", False),
            (2.0, "k1", False), (2.0, "k2", False),
        ], 2.5)
        assert names(events) == ["k1+k2.combo"]

    def test_pair_that_is_not_a_combo_behaves_as_today(self):
        rig = Rig(PAIR)
        events = rig.run([(0.0, "k1", True), (0.1, "k3", True), (0.3, "k3", False), (0.4, "k1", False)], 1.0)
        assert names(events) == ["k3.tap", "k1.tap"]

    def test_key_outside_every_combo_untouched_while_one_forms(self):
        combos = [{"keys": ["k1", "k2"], "hold_s": 1.0, "do": "bt_pair"}]
        rig = Rig(combos)
        events = rig.run([
            (0.0, "k1", True), (0.1, "k2", True), (0.3, "pu", True), (0.4, "pu", False),
            (1.5, "k1", False), (1.5, "k2", False),
        ], 2.5)
        assert names(events) == ["pu.tap", "k1+k2.combo"]

    def test_bounce_flicker_while_forming_tolerated(self):
        rig = Rig(PAIR)
        events = rig.run([
            (0.0, "k1", True), (0.1, "k2", True),
            (0.5, "k1", False), (0.54, "k1", True),         # 40 ms up: commits up, then down
            (0.8, "k2", False), (0.82, "k2", True),         # a shorter one
            (1.6, "k1", False), (1.6, "k2", False),
        ], 2.5)
        assert names(events) == ["k1+k2.combo"]

    def test_release_longer_than_release_s_cancels(self):
        rig = Rig(PAIR)
        events = rig.run([
            (0.0, "k1", True), (0.1, "k2", True),
            (0.5, "k1", False), (0.5 + RELEASE_S + 0.1, "k1", True),
            (1.8, "k1", False), (1.8, "k2", False),
        ], 2.5)
        assert "k1+k2.combo" not in names(events)
        assert not any(e.endswith(".tap") or e.endswith(".hold") for e in names(events) if "k2" in e)

    def test_volume_ramp_does_not_run_while_forming(self):
        rig = Rig([{"keys": ["k5", "k6"], "hold_s": 1.5, "do": "standby_toggle"}])
        events = rig.run([(0.0, "k5", True), (0.1, "k6", True), (2.5, "k5", False), (2.5, "k6", False)], 3.0)
        assert names(events) == ["k5+k6.combo"]

    def test_volume_ramp_untouched_alone(self):
        with_combo = Rig([{"keys": ["k5", "k6"], "hold_s": 1.0, "do": "standby_toggle"}])
        without = Rig()
        script = [(0.0, "k5", True), (2.0, "k5", False)]
        assert with_combo.run(script, 3.0) == without.run(list(script), 3.0)

    def test_hold_s_per_combo(self):
        rig = Rig([{"keys": ["k1", "k2"], "hold_s": 2.5, "do": "bt_pair"}])
        events = rig.run([(0.0, "k1", True), (0.0, "k2", True), (3.0, "k1", False), (3.0, "k2", False)], 3.5)
        assert len(events) == 1 and 2.5 <= events[0][0] <= 2.6

    def test_stalled_loop_never_looks_like_a_release(self):
        # The combo's action blocks the loop for 2 s while both keys stay down:
        # their holds (due meanwhile) must still be swallowed.
        rig = Rig([{"keys": ["k1", "k2"], "hold_s": 0.5, "do": "bt_pair"}])
        events = rig.run([(0.0, "k1", True), (0.0, "k2", True)], 0.6)
        assert names(events) == ["k1+k2.combo"]
        later = [str(e) for e in rig.panel.poll(2.6)]
        later += [str(e) for e in rig.panel.poll(2.62)]
        rig.digital.set("d0", False)
        rig.digital.set("d1", False)
        for t in (2.7, 2.72, 2.74, 2.76, 2.9, 3.0):
            later += [str(e) for e in rig.panel.poll(t)]
        assert later == []

    def test_unknown_keys_in_config_are_skipped(self):
        watch = ComboWatch(build_controls(KEYS), [{"keys": ["k1", "zz"]}, {"keys": ["k1", "k1"]}, {"keys": ["k1"]}])
        assert not watch.active


SCRIPT = [
    (0.0, "k1", True), (0.1, "k1", False),                    # tap
    (0.5, "k2", True), (1.6, "k2", False),                    # hold
    (2.0, "k5", True), (3.2, "k5", False),                    # ramp
    (3.5, "k1", True), (3.6, "k2", True), (3.7, "k2", False), (3.8, "k1", False),   # two keys
    (4.0, "k1", True), (4.2, "k2", True), (5.5, "k1", False), (5.6, "k2", False),   # held together
    (6.0, "pu", True), (6.04, "pu", False), (6.05, "pu", True), (6.3, "pu", False),  # bounce
    (7.0, "k6", True), (7.1, "k5", True), (8.5, "k6", False), (8.6, "k5", False),
]

GOLDEN = [
    (0.14, "k1.tap"), (1.34, "k2.hold"), (2.86, "k5.hold"), (3.02, "k5.hold"), (3.18, "k5.hold"),
    (3.74, "k2.tap"), (3.84, "k1.tap"), (4.86, "k1.hold"), (5.06, "k2.hold"), (6.34, "pu.tap"),
    (7.86, "k6.hold"), (7.94, "k5.hold"), (8.02, "k6.hold"), (8.1, "k5.hold"), (8.18, "k6.hold"),
    (8.26, "k5.hold"), (8.34, "k6.hold"), (8.42, "k5.hold"), (8.5, "k6.hold"), (8.58, "k5.hold"),
]


class TestNoCombosNoChange:
    def test_golden_sequence_identical_with_no_combos(self):
        before = Rig().run(list(SCRIPT), 9.0)         # never configured
        assert Rig([]).run(list(SCRIPT), 9.0) == before
        assert before == GOLDEN                     # as the keys have always reported it

    def test_with_k1_k2_only_their_presses_together_change(self):
        events = Rig(PAIR).run(list(SCRIPT), 9.0)
        untouched = [e for e in GOLDEN if not 3.4 < e[0] < 5.7]
        assert [e for e in events if e[1] != "k1+k2.combo"] == untouched
        assert [e for e in events if e[1] == "k1+k2.combo"] == [(5.24, "k1+k2.combo")]

    def test_combo_for_other_keys_leaves_the_sequence_alone(self):
        script = [e for e in SCRIPT if e[1] in ("k1", "k3") and e[0] < 3.4]
        assert Rig().run(list(script), 4.0) == Rig([{"keys": ["k3", "pu"], "do": "bt_pair"}]).run(list(script), 4.0)


# -- the radio: descriptor default, dispatch, the key page --------------------


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    replies = []
    actions = Actions(variant, player, on_reply=replies.append)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True}, ha=False)
    app.load_saved()
    return app, actions, player, panel, replies


def page_doc(app, combos):
    cfg = app.config()
    return {"keys": [{k: v for k, v in key.items() if k != "label"} for key in cfg["keys"]],
            "presets": cfg["presets"], "combos": combos}


class TestDescriptor:
    def test_hasler_default_is_k1_k6_pairing(self):
        # 1 + 6, not neighbours: on the Hasler HM a held key blocks the one
        # beside it (with 1 down, 2 never closes - raw reads 2026-10-24).
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        assert variant.combos == [{"keys": ["k1", "k6"], "hold_s": 1.0, "do": "bt_pair", "seconds": 120}]

    def test_hat_variant_same_default(self):
        variant = load_variant(VARIANTS / "hasler-hm-hat.yaml")
        assert variant.combos == [{"keys": ["k1", "k6"], "hold_s": 1.0, "do": "bt_pair", "seconds": 120}]

    def test_excelsior_has_none(self):
        variant = load_variant(VARIANTS / "excelsior.yaml")
        assert variant.combos == []
        assert not Panel.from_variant(variant, fake=True).combos.active

    def test_panel_watches_the_descriptor_combo(self):
        panel = Panel.from_variant(load_variant(VARIANTS / "hasler-hm.yaml"), fake=True)
        assert [c.id for c in panel.combos.combos] == ["k1+k6"]


class TestDispatch:
    def test_combo_pairs_with_one_ack(self, rig):
        app, actions, player, panel, replies = rig
        actions.dispatch(Event("k1+k6", "combo"))
        assert ("bluetooth_pair", 120) in player.calls
        assert actions.pairing
        assert replies == ["ack"]

    def test_unconfigured_combo_is_quiet(self, rig):
        app, actions, player, panel, replies = rig
        actions.dispatch(Event("k3+k4", "combo"))
        assert player.calls == [] and replies == []

    def test_end_to_end_from_the_contacts(self, rig):
        app, actions, player, panel, replies = rig
        d = panel.digital
        pins = {c["id"]: c["pin"] for c in actions.variant.controls if c.get("kind") == "momentary"}
        t = 0.0
        d.set(pins["k1"], True)
        d.set(pins["k6"], True)
        while t < 1.5:
            actions.dispatch_all(panel.poll(t))
            t += DT
        d.set(pins["k1"], False)
        d.set(pins["k6"], False)
        while t < 2.0:
            actions.dispatch_all(panel.poll(t))
            t += DT
        assert ("bluetooth_pair", 120) in player.calls
        assert not any(c[0] == "play_preset" for c in player.calls)    # no k1/k6 tap
        # k6's own hold (volume up) is held back too: only the pot's first reading sets it.
        assert [c for c in player.calls if c[0] == "set_volume"] == [("set_volume", 100.0)]


class TestConfig:
    def test_get_lists_combos_and_eligible_keys(self, rig):
        cfg = rig[0].config()
        assert cfg["combos"] == [{"keys": ["k1", "k6"], "hold_s": 1.0, "do": "bt_pair", "seconds": 120}]
        assert cfg["combo_keys"] == ["k1", "k2", "k3", "k4", "k5", "k6", "pu"]
        assert cfg["combo_limits"] == {"hold_s": [0.5, 3.0], "default_hold_s": 1.0, "max": 8}

    def test_excelsior_has_no_combo_keys(self, tmp_path):
        variant = load_variant(VARIANTS / "excelsior.yaml")
        app = WebApp(Actions(variant, FakePlayer()), Panel.from_variant(variant, fake=True), None,
                     {"radio": True}, ha=False)
        cfg = app.config()
        assert cfg["combos"] == [] and cfg["combo_keys"] == []

    def test_put_replaces_saves_and_applies(self, rig, tmp_path):
        app, actions, player, panel, _ = rig
        cfg = app.save(page_doc(app, [
            {"keys": ["pu", "k3"], "hold_s": 2, "do": "standby_toggle"},
            {"keys": ["k5", "k6"], "do": "preset", "n": 2},
        ]))
        assert cfg["combos"] == [
            {"keys": ["k3", "pu"], "hold_s": 2.0, "do": "standby_toggle"},       # descriptor order
            {"keys": ["k5", "k6"], "hold_s": 1.0, "do": "preset", "n": 2},
        ]
        assert [c.id for c in panel.combos.combos] == ["k3+pu", "k5+k6"]
        saved = yaml.safe_load((tmp_path / "keys.yaml").read_text())
        assert saved["combos"][0] == {"keys": ["k3", "pu"], "hold_s": 2.0, "do": "standby_toggle"}

    def test_put_without_combos_keeps_them(self, rig, tmp_path):
        app, *_ = rig
        app.save(page_doc(app, [{"keys": ["k3", "k4"], "do": "cycle_source"}]))
        doc = page_doc(app, [])
        del doc["combos"]
        cfg = app.save(doc)
        assert [c["keys"] for c in cfg["combos"]] == [["k3", "k4"]]
        assert yaml.safe_load((tmp_path / "keys.yaml").read_text())["combos"][0]["keys"] == ["k3", "k4"]

    def test_empty_list_removes_all_and_survives_restart(self, rig, tmp_path):
        app, *_ = rig
        app.save(page_doc(app, []))
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        panel = Panel.from_variant(variant, fake=True)
        again = WebApp(Actions(variant, FakePlayer()), panel, tmp_path / "keys.yaml", {"radio": True}, ha=False)
        again.load_saved()
        assert again.config()["combos"] == [] and not panel.combos.active

    def test_saved_combos_survive_restart(self, rig, tmp_path):
        app, *_ = rig
        app.save(page_doc(app, [{"keys": ["k2", "k4"], "hold_s": 0.5, "do": "join_group"}]))
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        panel = Panel.from_variant(variant, fake=True)
        again = WebApp(Actions(variant, FakePlayer()), panel, tmp_path / "keys.yaml", {"radio": True}, ha=False)
        again.load_saved()
        assert again.config()["combos"] == [{"keys": ["k2", "k4"], "hold_s": 0.5, "do": "join_group"}]
        assert panel.combos.combos[0].hold_s == 0.5

    def test_old_keys_file_without_combos_gets_the_default(self, rig, tmp_path):
        app, *_ = rig
        keymap.save(tmp_path / "keys.yaml", [], {"k1.tap": None})
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        again = WebApp(Actions(variant, FakePlayer()), Panel.from_variant(variant, fake=True),
                       tmp_path / "keys.yaml", {"radio": True}, ha=False)
        again.load_saved()
        assert [c["keys"] for c in again.config()["combos"]] == [["k1", "k6"]]

    def test_broken_saved_combo_costs_only_itself(self, rig, tmp_path):
        (tmp_path / "keys.yaml").write_text(yaml.safe_dump({
            "schema": 1, "presets": [], "actions": {},
            "combos": [{"keys": ["k1", "nope"], "do": "bt_pair"}, {"keys": ["k3", "k4"], "do": "cycle_source"},
                       {"keys": ["k4", "k3"], "do": "standby_toggle"}],
        }))
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        again = WebApp(Actions(variant, FakePlayer()), Panel.from_variant(variant, fake=True),
                       tmp_path / "keys.yaml", {"radio": True}, ha=False)
        again.load_saved()
        assert again.config()["combos"] == [{"keys": ["k3", "k4"], "hold_s": 1.0, "do": "cycle_source"}]

    def test_reset_brings_back_the_default(self, rig):
        app, *_ = rig
        app.save(page_doc(app, []))
        assert app.reset()["combos"][0]["keys"] == ["k1", "k6"]

    @pytest.mark.parametrize("combo,where", [
        ({"keys": ["k1"], "do": "bt_pair"}, "combos[0].keys"),
        ({"keys": ["k1", "k1"], "do": "bt_pair"}, "combos[0].keys"),
        ({"keys": ["k1", "zz"], "do": "bt_pair"}, "combos[0].keys"),
        ({"keys": ["k1", "k2"], "hold_s": 0.2, "do": "bt_pair"}, "combos[0].hold_s"),
        ({"keys": ["k1", "k2"], "hold_s": 3.5, "do": "bt_pair"}, "combos[0].hold_s"),
        ({"keys": ["k1", "k2"], "hold_s": "1", "do": "bt_pair"}, "combos[0].hold_s"),
        ({"keys": ["k1", "k2"], "do": "bt_pair", "seconds": 5}, "combos[0].do"),
        ({"keys": ["k1", "k2"], "do": "preset", "n": 42}, "combos[0].do"),
        ({"keys": ["k1", "k2"], "do": "launch_rockets"}, "combos[0].do"),
        ({"keys": ["k1", "k2"], "do": "none"}, "combos[0].do"),
        ({"keys": ["k1", "k2"], "do": "ha_scene", "entity": "light.x"}, "combos[0].do"),
        ("k1+k2", "combos[0]"),
    ])
    def test_validation(self, rig, combo, where):
        app, actions, *_ = rig
        with pytest.raises(keymap.Invalid) as exc:
            app.save(page_doc(app, [combo]))
        assert exc.value.errors[0]["where"] == where
        assert actions.variant.combos[0]["keys"] == ["k1", "k6"]      # nothing applied

    def test_duplicate_pair(self, rig):
        app, *_ = rig
        with pytest.raises(keymap.Invalid) as exc:
            app.save(page_doc(app, [{"keys": ["k1", "k2"], "do": "bt_pair"}, {"keys": ["k2", "k1"], "do": "cycle_source"}]))
        assert exc.value.errors == [{"where": "combos[1].keys", "message": "1 + 2 is already a combination"}]

    def test_at_most_eight(self, rig):
        app, *_ = rig
        ids = ["k1", "k2", "k3", "k4", "k5", "k6", "pu"]
        pairs = [[a, b] for i, a in enumerate(ids) for b in ids[i + 1:]][:9]
        with pytest.raises(keymap.Invalid) as exc:
            app.save(page_doc(app, [{"keys": p, "do": "cycle_source"} for p in pairs]))
        assert exc.value.errors[0]["where"] == "combos"

    def test_ha_combo_listed_and_cleared_with_missing_entities(self, rig, tmp_path):
        app, actions, player, panel, _ = rig
        app.save(page_doc(app, [{"keys": ["k3", "k4"], "do": "ha_scene", "entity": "scene.relax"},
                                {"keys": ["k1", "k2"], "do": "bt_pair", "seconds": 60}]))
        entries = keymap.ha_entries(actions)
        assert ("k3+k4", "combo", {"do": "ha_scene", "entity": "scene.relax"}) in entries
        keymap.remove_entries(actions, panel, tmp_path / "keys.yaml", ["k3+k4.combo"])
        assert [c["keys"] for c in actions.variant.combos] == [["k1", "k2"]]
        assert [c.id for c in panel.combos.combos] == ["k1+k2"]
        assert yaml.safe_load((tmp_path / "keys.yaml").read_text())["combos"] == [
            {"keys": ["k1", "k2"], "hold_s": 1.0, "do": "bt_pair", "seconds": 60}]


class TestTryIt:
    def test_press_fires_a_combo(self, rig):
        app, actions, player, panel, replies = rig
        app.press({"combo": ["k6", "k1"]})
        assert ("bluetooth_pair", 120) in player.calls and replies == ["ack"]

    def test_press_unknown_combo(self, rig):
        with pytest.raises(ValueError):
            rig[0].press({"combo": ["k3", "k4"]})
        with pytest.raises(ValueError):
            rig[0].press({"combo": "k1+k2"})


class TestHttp:
    @pytest.fixture
    def base(self, rig):
        app = rig[0]
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        yield f"http://127.0.0.1:{server.port}", rig
        server.close()

    def call(self, url, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_round_trip(self, base):
        url, (app, actions, player, *_) = base
        status, body = self.call(url + "/api/config")
        assert status == 200 and body["combos"][0]["do"] == "bt_pair" and "pu" in body["combo_keys"]
        doc = page_doc(app, [{"keys": ["k3", "k6"], "hold_s": 1.5, "do": "standby_toggle"}])
        status, body = self.call(url + "/api/config", "PUT", doc)
        assert status == 200 and body["config"]["combos"] == [
            {"keys": ["k3", "k6"], "hold_s": 1.5, "do": "standby_toggle"}]
        doc["combos"][0]["hold_s"] = 9
        status, body = self.call(url + "/api/config", "PUT", doc)
        assert status == 400 and body["errors"][0]["where"] == "combos[0].hold_s"
        status, body = self.call(url + "/api/press", "POST", {"combo": ["k3", "k6"]})
        assert status == 200 and actions.standby
        status, body = self.call(url + "/api/press", "POST", {"combo": ["k1", "k2"]})
        assert status == 400

    def test_keys_state_over_http(self, base):
        url, (app, actions, player, panel, _) = base
        pins = {c["id"]: c["pin"] for c in actions.variant.controls if c.get("kind") == "momentary"}
        panel.digital.set(pins["k6"], True)
        panel.digital.set(pins["k1"], True)
        panel.poll(5.0)
        status, body = self.call(url + "/api/keys/state")
        assert status == 200 and body == {"down": ["k1", "k6"], "t": 5.0}


# -- "Check this pair": which keys read down right now ------------------------


class TestKeysState:
    def test_down_follows_the_last_poll_raw(self):
        rig = Rig()
        assert rig.panel.keys_down() == ([], None)            # never polled yet
        rig.digital.set(PIN["k6"], True)
        rig.digital.set(PIN["k1"], True)
        assert rig.panel.keys_down() == ([], None)            # the web never reads the board itself
        rig.panel.poll(1.0)
        assert rig.panel.keys_down() == (["k1", "k6"], 1.0)   # raw: no debounce wait, descriptor order
        rig.digital.set(PIN["k1"], False)
        rig.panel.poll(1.02)
        assert rig.panel.keys_down() == (["k6"], 1.02)

    def test_reading_fires_nothing(self):
        rig = Rig(PAIR)
        rig.digital.set(PIN["k1"], True)
        events = [e for t in (0.0, 0.02, 0.04) for e in rig.panel.poll(t)]
        for _ in range(5):
            rig.panel.keys_down()
        events += [e for t in (0.06, 0.08) for e in rig.panel.poll(t)]
        assert events == []

    def test_bus_glitch_keeps_the_last_good_reading(self):
        rig = Rig()
        rig.digital.set(PIN["k2"], True)
        rig.panel.poll(1.0)

        def broken():
            raise OSError("i2c")
        rig.digital.read = broken
        rig.panel.poll(1.02)
        assert rig.panel.keys_down() == (["k2"], 1.0)          # t says how old it is

    def test_only_keys_not_other_contacts(self):
        digital = FakeDigital([k["pin"] for k in KEYS] + ["d8"])
        panel = Panel(build_controls(KEYS) + [EndSwitch("power", "d8")], digital, NullAnalog())
        digital.set("d8", True)
        digital.set(PIN["pu"], True)
        panel.poll(0.0)
        assert panel.keys_down()[0] == ["pu"]                 # the end switch is not a key

    def test_endpoint_and_rate_limit(self, rig):
        app, actions, player, panel, _ = rig
        panel.poll(2.0)
        for i in range(10):
            status, doc = app.keys_state("10.0.0.5", now=100.0 + i * 0.05)
            assert status == 200 and doc == {"down": [], "t": 2.0}
        status, doc = app.keys_state("10.0.0.5", now=100.5)
        assert status == 429
        assert app.keys_state("10.0.0.6", now=100.5)[0] == 200    # per client
        assert app.keys_state("10.0.0.5", now=101.0)[0] == 200    # the window moves on
        # Polling every 200 ms (the page) never trips it.
        assert all(app.keys_state("10.0.0.7", now=200.0 + i * 0.2)[0] == 200 for i in range(60))

    def test_quiet_clients_are_forgotten(self, rig):
        app = rig[0]
        for n in range(50):
            app.keys_state(f"10.0.1.{n}", now=float(n) * 2)
        assert len(app._keys_state_reads) <= 1

    def test_absent_panel_is_409(self):
        from haslerd.panel.io_base import AbsentDigital
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        panel = Panel(build_controls(variant.controls), AbsentDigital("no board"), NullAnalog())
        app = WebApp(Actions(variant, FakePlayer()), panel, None, {"radio": True}, ha=False)
        status, doc = app.keys_state("10.0.0.5")
        assert status == 409 and doc["absent"] and "key board" in doc["error"]


class TestHush:
    """While the page checks a pair, holding keys must not play, ramp or pair."""

    def test_keys_do_nothing_while_checked_and_until_let_go(self):
        rig = Rig(PAIR)
        rig.panel.hush(1.0)
        events = rig.run([(0.1, "k1", True), (0.2, "k2", True), (0.3, "k5", True),
                          (2.5, "k1", False), (2.6, "k2", False), (2.7, "k5", False)], 3.5)
        assert events == []                 # no k1+k2.combo, no holds, no ramp, no taps after the window

    def test_keys_work_again_after(self):
        rig = Rig(PAIR)
        rig.panel.hush(0.5)
        events = rig.run([(1.0, "k1", True), (1.1, "k1", False)], 1.5)
        assert names(events) == ["k1.tap"]

    def test_knobs_and_switches_untouched(self):
        def run(hushed):
            digital = FakeDigital([k["pin"] for k in KEYS] + ["d8"])
            panel = Panel(build_controls(KEYS) + [EndSwitch("power", "d8", off_hold_s=0.5)], digital, NullAnalog())
            digital.set("d8", True)
            if hushed:
                panel.hush(10.0)
            out, t = [], 0.0
            while t < 4.0:
                if abs(t - 2.0) < 1e-9:
                    digital.set("d8", False)
                out += [str(e) for e in panel.poll(t)]
                t = round(t + DT, 4)
            return out
        assert run(True) == run(False)
        assert "power.off" in run(True) and "power.off_held" in run(True)

    def test_endpoint_hushes_the_panel(self, rig):
        app, actions, player, panel, _ = rig
        import time as _time
        app.keys_state("10.0.0.9")
        assert panel._hush_until > _time.monotonic()
