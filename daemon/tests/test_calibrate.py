"""Knob calibration: fitting measured positions, live respec, raw history."""

import threading

import pytest

from haslerd.panel.calibrate import check_curve, fit_pot, fit_selector, summarize
from haslerd.panel.controls import AnalogSelector, Pot, _piecewise, build_controls

TICK = 0.02

# 19 Oct 2026, the Hasler's volume pot, measured by hand from its RIGHT (loud,
# low-count) end: down 25, 1/8 1879, 1/4 10882, 3/8 21106, 1/2 23783,
# 3/4 25616, up 26190. In the API's convention - position 0 = the quiet end -
# the same readings are:
HASLER_VOLUME = [
    (0.0, 26190), (0.25, 25616), (0.5, 23783), (0.625, 21106),
    (0.75, 10882), (0.875, 1879), (1.0, 25),
]


def feed(control, counts, n=10, start=0.0):
    events, now = [], start
    for _ in range(n):
        events.extend(control.update(now, {}, {control.channel: counts}))
        now += TICK
    return events


class TestFitPot:
    def test_todays_hasler_volume(self):
        fit = fit_pot(HASLER_VOLUME)
        assert fit.ok and fit.redo == []
        assert fit.direction == "reversed"
        # Only measured points; both ends pulled 30 counts inside the extremes.
        assert fit.curve == [[55, 100.0], [1879, 87.5], [10882, 75.0], [21106, 62.5],
                             [23783, 50.0], [25616, 25.0], [26160, 0.0]]
        # Flattest segment 25616..26190 = 23 counts per 1 %; floor 3 x 10 noise.
        assert fit.hysteresis_counts == 30

    def test_the_measured_points_land_on_their_positions(self):
        curve = [tuple(p) for p in fit_pot(HASLER_VOLUME).curve]
        for pos, raw in HASLER_VOLUME[1:-1]:
            assert _piecewise(curve, raw) == pos * 100
        assert _piecewise(curve, 25) == 100.0 and _piecewise(curve, 26190) == 0.0

    def test_points_may_be_dicts_in_any_order(self):
        pts = [{"position": p, "raw": r} for p, r in reversed(HASLER_VOLUME)]
        assert fit_pot(pts).curve == fit_pot(HASLER_VOLUME).curve

    def test_a_normal_pot_rises(self):
        fit = fit_pot([(0, 100), (0.25, 3000), (0.5, 9000), (0.75, 17000), (1, 26300)])
        assert fit.ok and fit.direction == "normal"
        assert fit.curve[0] == [216, 0.0]   # 100 + hysteresis (116/1 % at the bottom) and fit.curve[-1][1] == 100.0
        assert fit.curve[-1][0] < 26300
        values = [v for _, v in fit.curve]
        assert values == sorted(values)

    def test_a_misplaced_point_is_flagged_by_its_index(self):
        pts = list(HASLER_VOLUME)
        pts[2] = (0.5, 26185)            # "half way", but the knob never left the bottom
        fit = fit_pot(pts)
        assert fit.redo == [2]
        assert "position 0.5" in fit.warnings[0]
        assert not fit.ok
        assert 26185 not in [r for r, _ in fit.curve]   # the preview skips it

    def test_two_points_that_disagree_are_both_flagged(self):
        # 0.25 and 0.5 swapped: either could be the wrong one.
        fit = fit_pot([(0, 100), (0.25, 9000), (0.5, 3000), (0.75, 17000), (1, 26300)])
        assert fit.redo == [1, 2]
        assert fit.curve is None          # too few trusted interior points left

    def test_points_within_the_noise_count_as_out_of_order(self):
        fit = fit_pot([(0, 100), (0.25, 5000), (0.5, 5005), (0.75, 17000), (1, 26300)], noise=10)
        assert fit.redo == [1, 2]

    def test_stated_direction_that_contradicts_the_ends_redoes_the_ends(self):
        fit = fit_pot(HASLER_VOLUME, direction="normal")
        assert fit.curve is None and fit.redo == [0, 6]

    def test_ends_that_read_the_same_mean_no_wiper(self):
        fit = fit_pot([(0, 20), (0.3, 22), (0.6, 25), (1, 30)])
        assert fit.curve is None and fit.redo == [0, 3]

    def test_hysteresis_follows_the_flattest_segment(self):
        # Flattest: 20000 -> 26000 over 50 % = 120 counts per 1 %.
        fit = fit_pot([(0, 0), (0.25, 10000), (0.5, 20000), (1, 26000)], noise=5)
        assert fit.hysteresis_counts == 120

    def test_hysteresis_never_below_three_times_the_noise(self):
        fit = fit_pot([(0, 0), (0.25, 10000), (0.5, 20000), (1, 26000)], noise=60)
        assert fit.hysteresis_counts == 180

    def test_noise_coarser_than_the_track_warns(self):
        fit = fit_pot([(0, 0), (0.25, 10000), (0.5, 25000), (1, 25500)], noise=30)
        assert fit.ok and any("step" in w for w in fit.warnings)

    @pytest.mark.parametrize("pts, msg", [
        ([(0, 1), (0.5, 2), (1, 3)], "between the ends"),
        ([(0.1, 1), (0.5, 2), (0.7, 5), (1, 3)], "both ends"),
        ([(0, 1), (0.5, 2), (0.5, 4), (1, 3)], "same position"),
        ([(0, 1), (1.5, 2), (0.5, 4), (1, 3)], "position must"),
        ([(0, -5), (0.3, 2), (0.5, 4), (1, 3)], "raw must"),
    ])
    def test_not_a_measurement_at_all(self, pts, msg):
        with pytest.raises(ValueError, match=msg):
            fit_pot(pts)

    def test_bad_direction(self):
        with pytest.raises(ValueError, match="direction"):
            fit_pot(HASLER_VOLUME, direction="sideways")


class TestCheckCurve:
    def test_sorted_and_direction(self):
        assert check_curve([[26000, 0], [100, 100], [5000, 50]]) == (
            [[100, 100.0], [5000, 50.0], [26000, 0.0]], "reversed")
        assert check_curve([[0, 0], [100, 100]])[1] == "normal"

    @pytest.mark.parametrize("curve", [[], [[1, 2]], [[0, 0], [5, 50], [9, 20]], [[0, 0], [0, 50]],
                                       [[0, 0], [10, 150]], [[0, 0], ["a", 1]]])
    def test_rejects(self, curve):
        with pytest.raises(ValueError):
            check_curve(curve)


class TestFitSelector:
    def test_thresholds_midway(self):
        assert fit_selector([1000, 13000, 25000]) == [7000, 19000]

    def test_falling_ladder(self):
        assert fit_selector([25000, 13000, 1000]) == [19000, 7000]

    def test_out_of_order(self):
        with pytest.raises(ValueError, match="in order"):
            fit_selector([1000, 25000, 13000])

    def test_too_close(self):
        with pytest.raises(ValueError, match="too close"):
            fit_selector([1000, 1030, 25000])


class TestSummarize:
    def test_median_noise_and_stability(self):
        doc = summarize([100, 104, 96, 101, 99, 100, 103, 97, 100, 300])
        assert doc["raw"] == 100 and doc["min"] == 96 and doc["max"] == 300
        assert doc["noise"] == 3.5          # the 300 scratch is trimmed
        assert doc["stable"] is False       # but the spread says "still moving"
        assert summarize([]) is None


class TestPotLive:
    def pot(self):
        return Pot("volume", 1, curve=[(0, 0.0), (26000, 100.0)], hysteresis_counts=30)

    def test_raw_history_comes_from_normal_polling(self):
        p = self.pot()
        feed(p, 13000, n=20)
        s = p.sample(15)
        assert s["raw"] == 13000 and s["count"] == 15 and s["stable"] is True
        assert p.raw == 13000
        assert p.sample(100)["stable"] is False    # only 20 readings to judge by

    def test_respec_is_heard_at_the_next_poll(self):
        p = self.pot()
        feed(p, 13000)
        assert p.state() == 50.0
        p.respec(curve=[[0, 100], [26000, 0]], hysteresis_counts=10)
        assert p.direction == "reversed" and p.hysteresis == 10
        assert feed(p, 13000, n=1, start=1.0) == []        # mid-point: same value either way
        assert p.state() == 50.0
        p.respec(curve=[[0, 0], [13000, 80], [26000, 100]])
        assert [e.value for e in feed(p, 13000, n=1, start=2.0)] == [80.0]

    def test_unchanged_respec_emits_nothing(self):
        p = self.pot()
        feed(p, 13000)
        p.respec(curve=[[0, 0], [26000, 100]], hysteresis_counts=30)
        assert feed(p, 13000, start=1.0) == []

    def test_respec_from_spec_resets_what_it_leaves_out(self):
        p = self.pot()
        p.respec_from_spec({"taper": "log", "range": [0, 20000]})
        assert p.curve is None and p.counts_range == (0, 20000) and p.taper == "log"
        assert p.hysteresis == 250 and p.direction == "normal"

    def test_respec_rejects_unknown_fields(self):
        with pytest.raises(ValueError):
            self.pot().respec(channel=3)

    def test_respec_from_another_thread_while_polling(self):
        p = self.pot()
        stop = threading.Event()

        def loop():
            t = 0.0
            while not stop.is_set():
                p.update(t, {}, {1: 13000})
                t += TICK
        th = threading.Thread(target=loop)
        th.start()
        try:
            for i in range(200):
                p.respec(curve=[[0, 0], [13000, i % 100], [26000, 100]])
                p.sample(15)
        finally:
            stop.set()
            th.join()
        feed(p, 13000, n=2, start=100.0)
        assert p.state() == 99.0

    def test_build_controls_links_the_end_switch_and_direction(self):
        pot = build_controls([{"id": "tone", "kind": "pot", "channel": 0, "label": "Tone",
                               "direction": "reversed", "curve": [[0, 100], [9, 0]],
                               "end_switch": {"id": "power", "pin": "d8"}}])[0]
        assert pot.end_switch_id == "power" and pot.label == "Tone" and pot.direction == "reversed"

    def test_reconfigure_takes_the_new_spec(self):
        p = self.pot()
        new = Pot("volume", 1, curve=[(0, 100.0), (26000, 0.0)], hysteresis_counts=12)
        p.reconfigure(new)
        assert p.hysteresis == 12 and p.direction == "reversed"


class TestAnalogSelector:
    def test_decodes_by_thresholds_with_a_band(self):
        sel = AnalogSelector("speaker_sel", 3, positions=3, thresholds=[7000, 19000], band=200)
        events = feed(sel, 1000)
        assert [(e.kind, e.value) for e in events] == [("position", 0)]
        assert feed(sel, 7100, start=1.0) == []           # inside the band: stays put
        assert [e.value for e in feed(sel, 13000, start=2.0)] == [1]
        assert [e.value for e in feed(sel, 25000, start=3.0)] == [2]
        assert sel.sample(5)["raw"] == 25000

    def test_falling_thresholds(self):
        sel = AnalogSelector("s", 3, positions=3, thresholds=[19000, 7000])
        assert sel.decode(25000) == 0 and sel.decode(13000) == 1 and sel.decode(1000) == 2

    def test_built_from_the_descriptor(self):
        sel = build_controls([{"id": "sel", "kind": "analog_selector", "channel": 3, "positions": 3}])[0]
        assert isinstance(sel, AnalogSelector) and len(sel.thresholds) == 2
        sel.respec([1000, 2000])
        assert sel.thresholds == [1000, 2000]
        with pytest.raises(ValueError):
            sel.respec([1000])
