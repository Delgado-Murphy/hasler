"""Panel primitives, exercised with no hardware and no sleeping.

Time is passed in rather than read, so a 5-second hold costs nothing to test.
"""

import pytest
from conftest import VARIANTS

from haslerd.panel.controls import (
    EndSwitch,
    InterlockedBank,
    Momentary,
    Pot,
    RotarySwitch,
    build_controls,
)
from haslerd.variant import Panel, load_variant

TICK = 0.01


def run(control, script, start=0.0, tick=TICK):
    """Feed a script of (duration, digital-dict) and collect every event."""
    now = start
    events = []
    for duration, digital in script:
        end = now + duration
        while now <= end + 1e-9:
            events.extend(control.update(now, digital, {}))
            now = round(now + tick, 6)
    return events


class TestMomentary:
    def test_tap_on_release(self):
        c = Momentary("k1", "d0", hold_s=0.8)
        events = run(c, [(0.1, {"d0": False}), (0.2, {"d0": True}), (0.1, {"d0": False})])
        assert [str(e) for e in events] == ["k1.tap"]

    def test_hold_fires_while_still_down_and_suppresses_tap(self):
        c = Momentary("k1", "d0", hold_s=0.8)
        events = run(c, [(0.1, {"d0": False}), (1.2, {"d0": True}), (0.1, {"d0": False})])
        assert [str(e) for e in events] == ["k1.hold"]

    def test_hold_fires_once_however_long_it_is_held(self):
        c = Momentary("k1", "d0", hold_s=0.5)
        events = run(c, [(0.1, {"d0": False}), (5.0, {"d0": True}), (0.1, {"d0": False})])
        assert [str(e) for e in events] == ["k1.hold"]

    def test_repeat_keeps_firing_hold_while_down(self):
        c = Momentary("k6", "d5", hold_s=0.5, repeat_s=0.2)
        # Down for 1.15 s (incl. debounce): hold at ~0.5 s, then every 0.2 s.
        events = run(c, [(0.1, {"d5": False}), (1.15, {"d5": True}), (0.3, {"d5": False})])
        assert [str(e) for e in events] == ["k6.hold"] * 4

    def test_repeat_stops_on_release_and_a_short_press_is_still_a_tap(self):
        c = Momentary("k6", "d5", hold_s=0.5, repeat_s=0.2)
        events = run(c, [(0.1, {"d5": False}), (0.2, {"d5": True}), (0.5, {"d5": False})])
        assert [str(e) for e in events] == ["k6.tap"]

    def test_repeat_ms_comes_from_the_descriptor(self):
        (c,) = build_controls([{"id": "k6", "kind": "momentary", "pin": "d5", "repeat_ms": 150}])
        assert c.repeat_s == 0.15
        (c,) = build_controls([{"id": "k1", "kind": "momentary", "pin": "d0"}])
        assert c.repeat_s is None

    def test_contact_bounce_does_not_produce_phantom_taps(self):
        c = Momentary("k1", "d0", hold_s=0.8, bounce_s=0.03)
        script = [(0.1, {"d0": False})]
        for _ in range(6):  # 10 ms of chatter, well under the 30 ms debounce
            script += [(0.005, {"d0": True}), (0.005, {"d0": False})]
        script += [(0.2, {"d0": True}), (0.1, {"d0": False})]
        assert [str(e) for e in run(c, script, tick=0.005)] == ["k1.tap"]


class TestInterlockedBank:
    """The trap this whole design exists to avoid."""

    def bank(self):
        return InterlockedBank("presets", ["d1", "d2", "d3"], settle_s=0.06, hold_s=0.8)

    def test_changeover_never_emits_a_spurious_clear(self):
        # The outgoing key opens ~30 ms before the incoming one closes. A naive
        # per-key edge handler fires "clear" here on EVERY station change.
        c = self.bank()
        events = run(
            c,
            [
                (0.2, {"d1": True, "d2": False, "d3": False}),
                (0.03, {"d1": False, "d2": False, "d3": False}),   # the gap
                (0.2, {"d1": False, "d2": True, "d3": False}),
            ],
        )
        kinds = [e.kind for e in events]
        assert "clear" not in kinds, f"spurious clear during changeover: {kinds}"
        assert [str(e) for e in events if e.kind == "select"] == [
            "presets.select=0",
            "presets.select=1",
        ]

    def test_deliberate_release_does_emit_clear(self):
        # Key 0 pops the bank: all-open PERSISTS, so it must commit.
        c = self.bank()
        events = run(
            c,
            [
                (0.2, {"d1": True, "d2": False, "d3": False}),
                (0.5, {"d1": False, "d2": False, "d3": False}),
            ],
        )
        assert [str(e) for e in events] == ["presets.select=0", "presets.clear"]

    def test_rapid_station_cycling_stays_clean(self):
        c = self.bank()
        script = [(0.15, {"d1": True, "d2": False, "d3": False})]
        for pin in ("d2", "d3", "d1", "d2"):
            script.append((0.02, {"d1": False, "d2": False, "d3": False}))
            script.append((0.15, {p: p == pin for p in ("d1", "d2", "d3")}))
        events = run(c, script)
        assert [e.kind for e in events] == ["select"] * 5

    def test_latched_key_held_emits_hold_after_threshold(self):
        c = self.bank()
        events = run(c, [(2.0, {"d1": True, "d2": False, "d3": False})])
        assert [str(e) for e in events] == ["presets.select=0", "presets.hold=0"]

    def test_two_keys_stuck_down_keeps_previous_selection(self):
        c = self.bank()
        events = run(
            c,
            [
                (0.2, {"d1": True, "d2": False, "d3": False}),
                (0.3, {"d1": True, "d2": True, "d3": False}),
            ],
        )
        assert [e.kind for e in events] == ["select"]
        assert c.state() == 0


class TestEndSwitch:
    def test_off_then_off_held(self):
        c = EndSwitch("power", "d7", off_hold_s=5.0)
        events = run(c, [(0.2, {"d7": True}), (6.0, {"d7": False})], tick=0.05)
        assert [str(e) for e in events] == ["power.off", "power.off_held"]

    def test_brief_off_is_standby_only_not_shutdown(self):
        c = EndSwitch("power", "d7", off_hold_s=5.0)
        events = run(c, [(0.2, {"d7": True}), (1.0, {"d7": False}), (0.5, {"d7": True})])
        assert [str(e) for e in events] == ["power.off", "power.on"]


class TestRotarySwitch:
    def test_pattern_map_from_metering(self):
        c = RotarySwitch("spk", ["d9", "d10"], pattern_map={"10": 0, "00": 1, "01": 2})
        events = run(
            c,
            [
                (0.2, {"d9": True, "d10": False}),
                (0.2, {"d9": False, "d10": False}),
                (0.2, {"d9": False, "d10": True}),
            ],
        )
        assert [str(e) for e in events] == ["spk.position=0", "spk.position=1", "spk.position=2"]


class TestPot:
    def feed(self, pot, counts, n=10, start=0.0):
        events = []
        now = start
        for _ in range(n):
            events.extend(pot.update(now, {}, {0: counts}))
            now += TICK
        return events

    def test_resting_knob_emits_nothing_after_settling(self):
        p = Pot("volume", 0, counts_range=(0, 26400), hysteresis_counts=250)
        self.feed(p, 13000)
        assert self.feed(p, 13000, n=50) == []

    def test_scratchy_track_spikes_are_filtered_out(self):
        # A worn carbon track throws single-sample fliers. Median-of-5 eats them.
        p = Pot("volume", 0, counts_range=(0, 26400), hysteresis_counts=250, median_n=5)
        self.feed(p, 13000)
        before = p.state()
        events = []
        for i, counts in enumerate([13000, 26000, 13000, 13000, 100, 13000, 13000, 13000]):
            events.extend(p.update(1.0 + i * TICK, {}, {0: counts}))
        assert events == []
        assert p.state() == before

    def test_real_movement_emits_a_change(self):
        p = Pot("volume", 0, counts_range=(0, 26400), hysteresis_counts=250)
        self.feed(p, 5000)
        events = self.feed(p, 20000, start=1.0)
        assert [e.kind for e in events] == ["change"]
        assert events[0].value > 0

    def test_measured_curve_beats_the_taper_guess(self):
        p = Pot("tone", 0, curve=[(2000, 0.0), (10000, 50.0), (26000, 100.0)], hysteresis_counts=10)
        self.feed(p, 10000)
        assert p.state() == 50.0

    def test_no_adc_fitted_means_silence_not_a_crash(self):
        p = Pot("volume", 0, counts_range=(0, 26400))
        assert [p.update(t * TICK, {}, {}) for t in range(20)] == [[]] * 20
        assert p.state() is None


class TestBuildControls:
    def test_pot_end_switch_becomes_its_own_control(self):
        controls = build_controls(
            [
                {
                    "id": "tone",
                    "kind": "pot",
                    "channel": 1,
                    "end_switch": {"id": "power", "pin": "d7", "off_hold_ms": 5000},
                }
            ]
        )
        assert [c.id for c in controls] == ["tone", "power"]
        assert isinstance(controls[1], EndSwitch)
        assert controls[1].off_hold_s == 5.0

    def test_bank_release_key_becomes_its_own_control(self):
        controls = build_controls(
            [
                {
                    "id": "presets",
                    "kind": "interlocked_bank",
                    "pins": ["d1", "d2"],
                    "release": {"pin": "d0"},
                }
            ]
        )
        assert [c.id for c in controls] == ["presets", "presets_release"]

    def test_unknown_kind_is_rejected_loudly(self):
        with pytest.raises(ValueError, match="unknown control kind"):
            build_controls([{"id": "x", "kind": "telepathy"}])


class TestVariants:
    """Both cabinets must load and build - the Excelsior before it is wired."""

    @pytest.mark.parametrize("name", ["hasler-hm", "excelsior"])
    def test_descriptor_builds_a_panel(self, name):
        variant = load_variant(VARIANTS / f"{name}.yaml")
        panel = Panel.from_variant(variant, fake=True)
        assert panel.controls
        assert len(set(c.id for c in panel.controls)) == len(panel.controls)

    def test_hasler_references_seven_keys_and_the_end_switch(self):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        # d7 is skipped on purpose - it is GPA7 on the expander, output-only.
        assert variant.pin_names() == ["d0", "d1", "d2", "d3", "d4", "d5", "d6", "d8"]

    def test_hasler_keys_drive_presets_end_to_end(self):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        panel = Panel.from_variant(variant, fake=True)
        now = 0.0
        for _ in range(5):
            panel.poll(now)
            now += TICK
        k3 = next(c["pin"] for c in variant.controls if c["id"] == "k3")
        panel.digital.set(k3, True)
        events = []
        for _ in range(10):
            events.extend(panel.poll(now))
            now += TICK
        panel.digital.set(k3, False)
        for _ in range(10):
            events.extend(panel.poll(now))
            now += TICK
        assert [str(e) for e in events] == ["k3.tap"]
        assert variant.actions["k3.tap"] == {"do": "preset", "n": 3}

    def test_excelsior_changeover_is_clean_through_the_full_stack(self):
        variant = load_variant(VARIANTS / "excelsior.yaml")
        panel = Panel.from_variant(variant, fake=True)
        now, events = 0.0, []

        def settle(seconds):
            nonlocal now
            end = now + seconds
            while now <= end:
                events.extend(panel.poll(now))
                now = round(now + TICK, 6)

        panel.digital.press_only("d1")
        settle(0.2)
        panel.digital.clear()      # the mechanical changeover gap
        settle(0.03)
        panel.digital.press_only("d2")
        settle(0.2)

        assert [e.kind for e in events if e.control == "presets"] == ["select", "select"]
