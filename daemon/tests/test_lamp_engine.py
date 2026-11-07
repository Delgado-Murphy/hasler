"""The lamp engine and its outputs - fake clock, fake sysfs, no pins touched.

The engine's half follows docs/lamp.md's light language: three colours, six
behaviours (steady, breath, kick, refuse, beckon, swell), two transition
rules, night, the owner's brightness, identify, calibration and the watchdog.
"""

import math
import threading
import time

import pytest

from haslerd.lamp import engine as eng
from haslerd.lamp.contract import Look, beckoning, breathing, parse_reply, signature, steady
from haslerd.lamp.engine import LampEngine, open_lamp
from haslerd.lamp.outputs import (
    FakeOutput, GpiozeroOutput, NullOutput, SysfsPwmOutput, discover_chips, open_output,
)
from haslerd.lamp.policy import CASTING, LOADING, PLAYING, READY, STANDBY

AMBER = Look("amber", 1.0)
NO_FLOOR = (0.0, 0.0, 0.0)
G = eng.GAMMA


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    out = FakeOutput()
    kw.setdefault("min_duty", NO_FLOOR)
    lamp = LampEngine(out, clock=clock, threaded=False, **kw)
    return lamp, out, clock


def frame(lamp, t):
    """The Frame the engine drew at ``t`` (perceptual level, colour mix)."""
    lamp.render(t)
    return lamp._last_frame


def lvl(lamp, t):
    return frame(lamp, t).level


def red(lamp, t):
    return lamp.render(t)[0][0]


def samples(lamp, t0, t1, dt=0.01):
    n = int(round((t1 - t0) / dt))
    return [lvl(lamp, t0 + i * dt) for i in range(n + 1)]


def perceptual(duty):
    return tuple(max(0.0, c) ** (1 / G) for c in duty)


# -- writes: nothing before the first show, seeding --------------------------


def test_nothing_is_written_before_the_first_show():
    lamp, out, clock = make()
    lamp.step(clock.t)
    lamp.reply("kick")
    lamp.countdown(0.5)
    lamp.countdown(None)
    lamp.step(clock.t + 0.05)
    assert out.writes == [] and not lamp.shown
    lamp.show(READY)
    lamp.step(clock.t + 0.1)
    assert out.writes and lamp.shown


def test_the_first_show_without_a_seed_cuts():
    lamp, _, clock = make()
    lamp.show(READY, fade_s=5.0)
    assert lvl(lamp, clock.t) == pytest.approx(0.30)
    assert lamp.render(clock.t)[1] is False


def test_a_seed_writes_nothing_and_the_first_show_glides_from_it():
    lamp, out, clock = make()
    glow = lamp.duty_for("amber", 0.45)
    lamp.seed(glow)
    lamp.step(clock.t)
    assert out.writes == []
    lamp.show(READY)                           # down to 30 %: the fall rule
    lamp.step(clock.t)
    assert out.writes == []                    # the first frame IS the glow: no write, no jump
    assert lvl(lamp, clock.t) == pytest.approx(0.45, abs=1e-3)
    assert 0.30 < lvl(lamp, clock.t + 0.6) < 0.45
    assert lvl(lamp, clock.t + eng.FALL_S) == pytest.approx(0.30)


def test_seed_level_and_an_ember_glow():
    lamp, _, clock = make()
    lamp.seed_level("ember", 0.30)
    lamp.show(STANDBY)
    f = frame(lamp, clock.t)
    assert f.w == eng.ONEHOT["ember"] and f.level == pytest.approx(0.30)
    lamp2, _, clock2 = make()
    lamp2.seed(lamp2.duty_for("ember", 0.3))
    lamp2.show(STANDBY)
    assert frame(lamp2, clock2.t).w == eng.ONEHOT["ember"]


# -- colours, gamma, palette ---------------------------------------------------


def test_gamma_and_palette_ratio():
    lamp, _, clock = make()
    lamp.show(READY)
    (r, g, b), moving = lamp.render(clock.t)
    assert not moving
    assert r == pytest.approx(0.30 ** G)
    assert g == pytest.approx(0.17 * 0.30 ** G)     # amber 1.0 : 0.17 : 0
    assert b == 0.0


def test_the_three_colours():
    assert eng.DEFAULT_PALETTE == {
        "amber": (1.0, 0.17, 0.0), "ember": (1.0, 0.06, 0.0), "blue": (0.10, 0.0, 1.0),
    }
    lamp, _, clock = make()
    lamp.show(CASTING)
    r, g, b = lamp.render(clock.t)[0]
    assert b == pytest.approx(0.55 ** G) and r == pytest.approx(0.10 * 0.55 ** G) and g == 0.0


def test_old_colour_names_are_aliases():
    lamp, _, clock = make()
    for old, new in (("amber_pale", "amber"), ("white", "amber"), ("red", "ember"), ("magenta", "blue")):
        lamp.show(Look(old, 0.5), cut=True)
        assert frame(lamp, clock.t).w == eng.ONEHOT[new]
    assert parse_reply("signature:magenta") == ("signature", "magenta", None)
    assert parse_reply(signature("amber_pale", 0.3)) == ("signature", "amber_pale", 0.3)


def test_balance_applies_after_gamma():
    lamp, _, clock = make(balance={"green": 0.5})
    lamp.show(AMBER)
    r, g, _ = lamp.render(clock.t)[0]
    assert (r, g) == pytest.approx((1.0, 0.085))


def test_palette_override_from_the_descriptor_and_old_names_ignored():
    lamp, _, clock = make(palette={"amber": [1.0, 0.2, 0.0], "amber_pale": [1, 1, 1],
                                   "white": [1, 1, 1], "red": {"red": 1.0}})
    lamp.show(AMBER)
    assert lamp.render(clock.t)[0][1] == pytest.approx(0.2)
    assert set(lamp.palette) == {"amber", "ember", "blue"}


# -- transition rules ---------------------------------------------------------


def test_brightness_rises_in_0_6s_ease_out():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.show(PLAYING)
    t0 = clock.t
    assert lvl(lamp, t0) == pytest.approx(0.30)
    assert lvl(lamp, t0 + 0.02) > 0.30 + 0.01            # under way on the first frame
    assert lvl(lamp, t0 + 0.3) > (0.30 + 0.80) / 2       # ease-out: past half at half time
    assert lvl(lamp, t0 + eng.RISE_S) == pytest.approx(0.80)
    assert lamp.render(t0 + eng.RISE_S + 0.01)[1] is False


def test_brightness_falls_in_1_2s_with_a_long_tail_never_symmetric():
    lamp, _, clock = make()
    lamp.show(PLAYING)
    lamp.show(READY)
    t0 = clock.t
    assert lvl(lamp, t0 + 0.02) < 0.80 - 0.01            # starts at once
    assert lvl(lamp, t0 + 0.6) < (0.30 + 0.80) / 2       # most of the way at half time ...
    assert lvl(lamp, t0 + 1.1) > 0.30 + 1e-3             # ... a tail still to go
    assert lvl(lamp, t0 + eng.FALL_S) == pytest.approx(0.30)
    assert eng.FALL_S == 2 * eng.RISE_S


def test_a_hue_change_dips_to_10_percent_and_swaps_at_the_bottom():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.show(CASTING)
    t0 = clock.t
    assert lvl(lamp, t0 + 0.3) > eng.HUE_TROUGH
    assert lvl(lamp, t0 + 0.6) == pytest.approx(eng.HUE_TROUGH)
    assert lvl(lamp, t0 + 0.6 + 0.45) > 0.3                     # rising, ease-out
    lamp.show(READY, cut=True)
    lamp.show(CASTING)
    for i in range(0, 151):
        t = t0 + i * 0.01
        f = frame(lamp, t)
        assert f.w in (eng.ONEHOT["amber"], eng.ONEHOT["blue"]), "never a mix of the two"
        if i < 60:
            assert f.w == eng.ONEHOT["amber"]
        elif i > 60:
            assert f.w == eng.ONEHOT["blue"]
    assert lvl(lamp, t0 + 1.5) == pytest.approx(0.55)
    assert lamp.render(t0 + 1.51)[1] is False


def test_amber_and_ember_fade_directly():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.show(STANDBY)
    t0 = clock.t
    mid = frame(lamp, t0 + 0.3).w
    levels = samples(lamp, t0 + 0.3, t0 + eng.FALL_S)
    assert min(levels) >= 0.22 - 1e-6                        # no trough: one family
    assert 0 < mid[0] < 1 and 0 < mid[1] < 1 and mid[2] == 0


def test_a_new_look_mid_transition_starts_from_the_frame_on_show():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.show(PLAYING)
    clock.t += 0.2
    here = lvl(lamp, clock.t)
    lamp.show(READY)
    assert lvl(lamp, clock.t) == pytest.approx(here)


def test_an_explicit_glide_is_slow():
    lamp, _, clock = make()
    lamp.show(PLAYING)
    lamp.show(Look("amber", 0.25), fade_s=20.0)
    t0 = clock.t
    assert lvl(lamp, t0 + 10) > 0.25 + 0.01
    assert lvl(lamp, t0 + 20) == pytest.approx(0.25)


def test_same_look_again_is_a_no_op():
    lamp, _, clock = make()
    lamp.show(LOADING)
    clock.t += 0.3
    before = lvl(lamp, clock.t)
    lamp.show(LOADING)
    assert lvl(lamp, clock.t) == pytest.approx(before) and lamp._trans is None


def test_off_falls_dark_without_a_hue_change():
    lamp, _, clock = make()
    lamp.show(CASTING)
    lamp.show(Look())
    t0 = clock.t
    assert frame(lamp, t0 + 0.5).w == eng.ONEHOT["blue"]
    assert lamp.render(t0 + eng.FALL_S)[0] == (0.0, 0.0, 0.0)


# -- breath ------------------------------------------------------------------


def test_breath_is_asymmetric_rise_1_3_fall_1_7_rest_0_4():
    lamp, _, clock = make()
    look = breathing("amber", 0.60)
    lamp.show(look)
    t0 = clock.t
    floor = 0.60 * 0.45
    assert lvl(lamp, t0) == pytest.approx(floor)                     # starts at the trough
    assert lvl(lamp, t0 + 0.65) > (floor + 0.60) / 2                 # rise: ease-out
    assert lvl(lamp, t0 + 1.3) == pytest.approx(0.60)                # peak
    assert lvl(lamp, t0 + 1.3 + 0.85) == pytest.approx((floor + 0.60) / 2, abs=1e-6)  # fall: ease-in-out
    assert lvl(lamp, t0 + 3.0) == pytest.approx(floor)
    assert lvl(lamp, t0 + 3.2) == pytest.approx(floor)               # rest
    assert lvl(lamp, t0 + 3.4 + 1.3) == pytest.approx(0.60)          # period 3.4 s
    levels = samples(lamp, t0, t0 + 6.8)
    assert min(levels) == pytest.approx(floor) and max(levels) == pytest.approx(0.60)
    assert lamp.render(t0 + 10)[1]                                   # a breath never settles


def test_the_slow_tempo_is_6s_with_a_40_percent_floor():
    lamp, _, clock = make()
    lamp.show(breathing("ember", 0.35, slow=True))
    t0 = clock.t
    assert lvl(lamp, t0) == pytest.approx(0.35 * 0.40)
    assert lvl(lamp, t0 + 1.3 * 6 / 3.4) == pytest.approx(0.35)
    assert lvl(lamp, t0 + 6.0) == pytest.approx(0.35 * 0.40)


def test_breath_keeps_its_phase_when_only_the_floor_moves():
    lamp, _, clock = make()
    lamp.show(breathing("amber", 0.45, floor=0.12))
    clock.t += 1.0
    before = lvl(lamp, clock.t)
    lamp.show(breathing("amber", 0.45, floor=0.18))
    assert lvl(lamp, clock.t) == pytest.approx(before, abs=1e-6)     # no restart at the trough


# -- kick ---------------------------------------------------------------------


def test_kick_is_written_within_one_frame():
    lamp, out, clock = make()
    lamp.show(READY)
    lamp.step(clock.t)
    n = len(out.writes)
    lamp.reply("kick")
    lamp.step(clock.t)                         # the frame the thread renders on waking
    assert len(out.writes) == n + 1
    assert perceptual(out.last)[0] > 0.30 + 0.05      # no raised-cosine slow start


def test_kick_is_written_within_25ms_by_the_thread():
    class Timed(FakeOutput):
        def __init__(self):
            super().__init__()
            self.times = []

        def write(self, r, g, b):
            self.times.append(time.monotonic())
            super().write(r, g, b)

    out = Timed()
    lamp = LampEngine(out, min_duty=NO_FLOOR)
    try:
        lamp.show(READY)
        time.sleep(0.1)
        n = len(out.writes)
        t = time.monotonic()
        lamp.reply("kick")
        deadline = t + 1.0
        while len(out.writes) == n and time.monotonic() < deadline:
            time.sleep(0.001)
        assert out.times[n] - t < 0.025
        assert perceptual(out.writes[n])[0] > 0.30
    finally:
        lamp.close()


def test_kick_shape_attack_30ms_then_350ms_cool_down():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.reply("kick")
    t0 = clock.t
    lead = eng.KICK_LEAD_S
    assert lvl(lamp, t0 + 0.03 - lead) == pytest.approx(0.5)        # +40 % of 30 % < 50 %: 50 %
    half = lvl(lamp, t0 + 0.03 - lead + 0.175)
    assert half < 0.30 + 0.2 * 0.5 * 0.5                            # exponential: well under half
    assert half > 0.30
    end = t0 + 0.03 + 0.35 - lead
    assert lvl(lamp, end + 0.001) == pytest.approx(0.30)
    assert lamp._reply is None and lamp._blend_from is None          # ends exactly on the Look


def test_kick_is_plus_40_percent_capped_at_full():
    lamp, _, clock = make()
    lamp.show(Look("amber", 0.5))
    lamp.reply("kick")
    assert lvl(lamp, clock.t + 0.02) == pytest.approx(0.7)
    lamp.show(PLAYING, cut=True)
    lamp.reply("kick")
    assert lvl(lamp, clock.t + 0.02) == pytest.approx(1.0)


def test_a_kick_rides_a_breath():
    lamp, _, clock = make()
    lamp.show(LOADING)
    clock.t += 1.0
    lamp.reply("kick")
    t0 = clock.t
    assert lvl(lamp, t0 + 0.02) > lvl(lamp, t0 + 0.5)
    lamp2, _, c2 = make()
    lamp2.show(LOADING)
    assert lvl(lamp, t0 + 0.5) == pytest.approx(lvl(lamp2, t0 + 0.5), abs=1e-6)


def test_old_key_words_kick():
    for word in ("ack", "next", "ha", "signature:blue@0.3", "signature:amber_pale"):
        lamp, _, clock = make()
        lamp.show(READY)
        lamp.reply(word)
        assert lamp._reply == "kick", word
        assert lvl(lamp, clock.t + 0.02) == pytest.approx(0.5)


def test_key_replies_off_drops_kicks_but_not_refusals():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.set_key_replies(False)
    lamp.reply("ack")
    assert lamp._reply is None
    lamp.reply("fail")
    assert lamp._reply == "refuse"
    lamp.set_key_replies(True)
    lamp.reply("kick")
    assert lamp._reply == "kick"


def test_no_kick_on_a_dark_lamp():
    lamp, _, clock = make()
    lamp.show(Look())
    lamp.reply("kick")
    assert lamp.render(clock.t + 0.02)[0] == (0.0, 0.0, 0.0)


# -- refuse ---------------------------------------------------------------------


def test_refuse_sags_to_25_percent_twice():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.reply("refuse")
    t0 = clock.t
    assert lvl(lamp, t0 + 0.15) == pytest.approx(0.075)
    assert lvl(lamp, t0 + 0.30) == pytest.approx(0.075)              # hold 200 ms
    assert 0.075 < lvl(lamp, t0 + 0.65) < 0.30                       # recovering, 600 ms
    assert lvl(lamp, t0 + 0.95) == pytest.approx(0.30)
    assert lvl(lamp, t0 + 0.95 + 0.15) == pytest.approx(0.075)       # twice
    assert lvl(lamp, t0 + 1.9) == pytest.approx(0.30)
    assert lamp._reply is None


def test_fail_refuses_and_nudge_is_one_sag():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.reply("fail")
    assert lamp._reply == "refuse"
    lamp.reply("nudge")
    assert lamp._reply == "refuse1"
    t0 = clock.t
    assert lvl(lamp, t0 + 0.2) == pytest.approx(0.075, abs=0.02)
    assert lvl(lamp, t0 + 1.1) == pytest.approx(0.30)                # once only
    lamp.reply("refuse3")
    assert lvl(lamp, clock.t + 0.95 * 2 + 0.2) == pytest.approx(0.075)
    assert lvl(lamp, clock.t + 0.95 * 3 + 0.01) == pytest.approx(0.30)


# -- swell ----------------------------------------------------------------------


def test_swell_is_plus_30_percent_raised_cosine_over_1_2s():
    lamp, _, clock = make()
    lamp.show(Look("amber", 0.5))
    lamp.reply("swell")
    t0 = clock.t
    assert lvl(lamp, t0 + 0.6) == pytest.approx(0.65)
    assert lvl(lamp, t0 + 0.3) == pytest.approx(lvl(lamp, t0 + 0.9))
    assert lvl(lamp, t0 + 0.05) < 0.5 * 1.01                         # gentle start
    assert lvl(lamp, t0 + 1.2) == pytest.approx(0.5)


def test_stop_glides_down_to_ready():
    lamp, _, clock = make()
    lamp.show(PLAYING)
    lamp.reply("stop")
    t0 = clock.t
    assert lvl(lamp, t0 + 0.02) < 0.80
    assert lvl(lamp, t0 + eng.FALL_S - 0.01) == pytest.approx(0.30, abs=0.01)


# -- beckon, identify -----------------------------------------------------------


def test_beckon_is_two_kicks_250ms_apart_then_rest_until_resolved():
    lamp, _, clock = make()
    lamp.show(beckoning("amber", 0.30, rest_s=4.0))
    t0 = clock.t
    assert lvl(lamp, t0 + 0.03) == pytest.approx(0.5)
    assert lvl(lamp, t0 + 0.28) == pytest.approx(0.5)
    assert lvl(lamp, t0 + 1.0) == pytest.approx(0.30)                # resting
    cycle = 0.25 + 0.38 + 4.0
    assert lvl(lamp, t0 + cycle + 0.03) == pytest.approx(0.5)        # and again
    assert lvl(lamp, t0 + 5 * cycle + 0.28) == pytest.approx(0.5)    # and again
    assert lamp.render(t0 + 100)[1]


def test_beckon_is_half_as_tall_at_night():
    lamp, _, clock = make()
    lamp.set_night(True)
    lamp.show(beckoning("amber", 0.30, 0.20))
    t0 = clock.t
    assert lvl(lamp, t0 + 0.03) == pytest.approx(0.20 + (0.5 - 0.20) / 2)


def test_identify_beckons_in_the_colour_on_show_then_stops():
    lamp, _, clock = make()
    lamp.show(CASTING)
    lamp.identify(10)
    t0 = clock.t
    assert lamp.identifying
    f = frame(lamp, t0 + 0.03)
    assert f.w == eng.ONEHOT["blue"] and f.level == pytest.approx(0.77)
    cycle = 0.25 + 0.38 + eng.IDENTIFY_REST_S
    assert lvl(lamp, t0 + cycle + 0.28) == pytest.approx(0.77)
    lamp.render(t0 + 10.0)
    assert not lamp.identifying
    assert lvl(lamp, t0 + 10.5) == pytest.approx(0.55)


def test_identify_on_a_dark_lamp_uses_amber_and_can_be_cancelled():
    lamp, _, clock = make()
    lamp.show(Look())
    lamp.identify(5)
    f = frame(lamp, clock.t + 0.03)
    assert f.w == eng.ONEHOT["amber"] and f.level == pytest.approx(0.5)
    lamp.identify(0)
    assert not lamp.identifying
    assert lamp.render(clock.t + 1)[0] == (0.0, 0.0, 0.0)


# -- blending and continuity ----------------------------------------------------


def test_a_reply_interrupting_a_reply_blends_from_the_frame_on_show():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.reply("kick")
    t0 = clock.t
    for i in range(3):
        lamp.render(t0 + i * 0.01)
    before = perceptual(lamp.render(t0 + 0.02)[0])[0]           # at the kick's peak
    clock.t = t0 + 0.02
    lamp.reply("refuse")
    steps = [before] + [perceptual(lamp.render(clock.t + i * 0.02)[0])[0] for i in range(6)]
    jumps = [abs(a - b) for a, b in zip(steps, steps[1:])]
    assert jumps[0] < 0.05                                       # unblended: 0.2 in one frame
    assert max(jumps) < 0.15
    assert lvl(lamp, clock.t + 0.15) == pytest.approx(0.075)     # then the refuse itself


def test_frames_stay_continuous_through_a_busy_minute():
    """Frame-to-frame change is bounded, except where a hue change swaps at its trough."""
    lamp, _, clock = make(min_duty=eng.DEFAULT_MIN_DUTY)
    script = {
        0.0: lambda: lamp.show(READY),
        0.5: lambda: lamp.show(PLAYING),
        0.7: lambda: lamp.reply("kick"),
        0.75: lambda: lamp.reply("kick"),
        1.0: lambda: lamp.reply("refuse"),
        1.3: lambda: lamp.reply("swell"),
        2.0: lambda: lamp.show(LOADING),
        3.0: lambda: lamp.show(CASTING),
        3.4: lambda: lamp.show(READY),       # interrupts the hue change mid-dip
        5.0: lambda: lamp.set_night(True),
        6.0: lambda: lamp.countdown(0.3),
        6.2: lambda: lamp.countdown(0.6),
        6.4: lambda: lamp.countdown(None),
        7.0: lambda: lamp.show(STANDBY),
        8.0: lambda: lamp.reply("stop"),
        8.1: lambda: lamp.identify(2),
        8.3: lambda: lamp.reply("nudge"),
        11.0: lambda: lamp.show(beckoning("amber", 0.3)),
        14.0: lambda: lamp.set_brightness("low"),
    }
    t0 = clock.t
    last = None
    worst = 0.0
    for i in range(0, 1600):
        t = round(i * 0.01, 2)
        clock.t = t0 + t
        if t in script:
            script[t]()
        duty = perceptual(lamp.render(clock.t)[0])
        f = lamp._last_frame
        if last is not None:
            hue_swap = family(f.w) != family(last[1].w)
            countdown_step = t in (6.0, 6.2)            # driven by the knob, ~20 Hz steps
            if not hue_swap and not countdown_step:
                worst = max(worst, max(abs(a - b) for a, b in zip(duty, last[0])))
        last = (duty, f)
    assert worst < 0.2          # a 70 ms blend of the biggest jump: ~0.17 a frame


def family(w):
    return eng.family(w)


def test_hue_swaps_happen_only_at_the_trough():
    lamp, _, clock = make()
    lamp.show(PLAYING)
    lamp.show(CASTING)
    t0 = clock.t
    prev = frame(lamp, t0)
    for i in range(1, 200):
        f = frame(lamp, t0 + i * 0.01)
        if eng.family(f.w) != eng.family(prev.w):
            assert min(f.level, prev.level) <= eng.HUE_TROUGH + 1e-6
            assert max(f.level, prev.level) <= eng.HUE_TROUGH + 0.05
        prev = f


# -- night ----------------------------------------------------------------------


def test_night_levels():
    lamp, _, clock = make()
    lamp.set_night(True)
    for look, level in ((PLAYING, 0.35), (READY, 0.20), (STANDBY, 0.15), (CASTING, 0.30),
                        (Look("amber", 0.9), eng.NIGHT_CAP)):
        lamp.show(look, cut=True)
        assert lvl(lamp, clock.t) == pytest.approx(level), look


def test_night_breath_keeps_its_shape():
    lamp, _, clock = make()
    lamp.set_night(True)
    lamp.show(LOADING, cut=True)
    t0 = clock.t
    assert lvl(lamp, t0) == pytest.approx(0.35 * 0.45)
    assert lvl(lamp, t0 + 1.3) == pytest.approx(0.35)


def test_night_switch_glides_and_falls_and_swells_are_slower():
    lamp, _, clock = make()
    lamp.show(PLAYING)
    lamp.set_night(True)
    t0 = clock.t
    assert 0.35 < lvl(lamp, t0 + 0.75) < 0.80
    assert lvl(lamp, t0 + eng.NIGHT_SWITCH_S) == pytest.approx(0.35)
    clock.t += 2
    lamp.show(READY)                                     # 35 % -> 20 % at night: 1.8 s
    t1 = clock.t
    assert lvl(lamp, t1 + 1.3) > 0.20 + 1e-4
    assert lvl(lamp, t1 + 1.8) == pytest.approx(0.20)
    clock.t += 2
    lamp.reply("swell")
    t2 = clock.t
    assert lvl(lamp, t2 + 0.9) == pytest.approx(0.20 * 1.3)
    assert lvl(lamp, t2 + 1.8) == pytest.approx(0.20)
    lamp.reply("kick")
    assert lamp._reply_len() == pytest.approx(0.03 + 0.35 * 1.5 - eng.KICK_LEAD_S)
    lamp.show(PLAYING)                                   # rises stay quick
    t3 = clock.t
    assert lvl(lamp, t3 + eng.RISE_S) == pytest.approx(0.35)


def test_night_hue_change_falls_slower_rises_as_by_day():
    lamp, _, clock = make()
    lamp.set_night(True)
    lamp.show(READY, cut=True)
    lamp.show(CASTING)
    t0 = clock.t
    assert frame(lamp, t0 + 0.85).w == eng.ONEHOT["amber"]
    assert lvl(lamp, t0 + 0.9) == pytest.approx(eng.HUE_TROUGH)
    assert lvl(lamp, t0 + 0.9 + 0.9) == pytest.approx(0.30)


# -- brightness ---------------------------------------------------------------


def test_brightness_scales_every_level_capped_at_full():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.set_brightness("low")
    assert lvl(lamp, clock.t + eng.FALL_S) == pytest.approx(0.18)
    lamp.set_brightness("bright")
    lamp.show(PLAYING, cut=True)
    assert lvl(lamp, clock.t) == pytest.approx(1.0)          # 0.8 x 1.25, capped
    lamp.set_brightness("normal")
    assert lamp.brightness == 1.0
    lamp.set_brightness("dazzling")                          # unknown: ignored
    assert lamp.brightness == 1.0


def test_low_brightness_never_takes_ember_below_its_visible_floor_or_off_on():
    lamp, _, clock = make()
    lamp.set_brightness("low")
    lamp.show(STANDBY, cut=True)
    assert lvl(lamp, clock.t) == pytest.approx(0.15)          # 0.22 x 0.6 = 0.13 < night ember
    lamp.set_night(True)
    assert lvl(lamp, clock.t + 2) == pytest.approx(0.15)
    lamp.show(Look(), cut=True)
    assert lamp.render(clock.t)[0] == (0.0, 0.0, 0.0)
    lamp.show(Look("amber", 0.12), cut=True)
    assert lvl(lamp, clock.t) == pytest.approx(eng.VISIBLE_MIN)


# -- countdown -----------------------------------------------------------------


def test_countdown_glows_amber_down_to_the_night_ember():
    lamp, _, clock = make()
    lamp.show(PLAYING)
    lamp.countdown(0.5)
    f = frame(lamp, clock.t)
    assert f.level == pytest.approx((0.80 + 0.15) / 2)
    assert f.w == pytest.approx((0.5, 0.5, 0.0))
    lamp.countdown(1.0)
    f = frame(lamp, clock.t)
    assert f.w == eng.ONEHOT["ember"] and f.level == pytest.approx(0.15)


def test_countdown_from_blue_never_goes_purple():
    lamp, _, clock = make()
    lamp.show(CASTING)
    for p in (0.25, 0.49):
        lamp.countdown(p)
        assert frame(lamp, clock.t).w == eng.ONEHOT["blue"]
    for p in (0.5, 0.75, 1.0):
        lamp.countdown(p)
        assert frame(lamp, clock.t).w == eng.ONEHOT["ember"]


def test_countdown_cancel_snaps_back_and_outranks_replies():
    lamp, _, clock = make()
    lamp.show(PLAYING)
    lamp.countdown(0.8)
    lamp.render(clock.t)
    lamp.reply("kick")
    assert lamp._reply is None
    lamp.countdown(None)
    t0 = clock.t
    mid = red(lamp, t0 + 0.1)
    assert 0.0 < mid < 0.8 ** G
    assert red(lamp, t0 + eng.COUNTDOWN_RESTORE_S) == pytest.approx(0.8 ** G)


# -- calibration ---------------------------------------------------------------


def test_default_min_duty_is_realistic():
    assert eng.DEFAULT_MIN_DUTY == (0.006, 0.006, 0.006)
    lamp, _, _ = make(min_duty=None)
    assert lamp.min_duty == (0.006, 0.006, 0.006)


def test_min_duty_keeps_the_main_channel_lit_to_the_end():
    lamp, _, clock = make(min_duty={"red": 0.01})
    lamp.show(Look("ember", 0.05), cut=True)
    r, g, b = lamp.render(clock.t)[0]
    assert r == pytest.approx(0.01) and g == 0.0 and b == 0.0


def test_standby_ember_is_pure_red_with_realistic_floors():
    lamp, _, clock = make(min_duty=None)
    for night in (False, True):
        lamp.set_night(night)
        lamp.show(STANDBY, cut=True)
        r, g, b = lamp.render(clock.t + 3)[0]
        assert r > 0.006 and g == 0.0 and b == 0.0


def test_night_ready_stays_amber_its_green_held_at_the_floor():
    lamp, _, clock = make(min_duty=None)
    lamp.set_night(True)
    lamp.show(READY, cut=True)
    r, g, _ = lamp.render(clock.t)[0]
    assert r == pytest.approx(0.20 ** G)
    assert g == pytest.approx(0.006)                 # wanted 0.0049: held, not dropped


def test_no_breath_trough_flicks_hue():
    for look in (breathing("amber", 0.45, floor=0.12), breathing("amber", 0.30),
                 breathing("ember", 0.35, slow=True), LOADING, breathing("blue", 0.30)):
        for night in (False, True):
            lamp, _, clock = make(min_duty=None)
            lamp.set_night(night)
            lamp.show(look, cut=True)
            lit = set()
            for i in range(0, 1300):
                r, g, b = lamp.render(clock.t + i * 0.01)[0]
                lit.add((r > 0, g > 0, b > 0))
            assert len(lit) == 1, (look, night, lit)


def test_amber_blends_into_ember_below_the_band_instead_of_flicking():
    lamp, _, _ = make()
    lo, hi = eng.AMBER_BAND
    share = [lamp.duty_for("amber", x)[1] / lamp.duty_for("amber", x)[0] for x in (hi, (lo + hi) / 2, lo)]
    assert share[0] == pytest.approx(0.17)
    assert 0.06 < share[1] < 0.17
    assert share[2] == pytest.approx(0.06)
    lamp2, _, _ = make(amber_band=[0.20, 0.30])
    assert lamp2.duty_for("amber", 0.20)[1] / lamp2.duty_for("amber", 0.20)[0] == pytest.approx(0.06)


def test_a_fall_through_the_band_drops_green_once():
    lamp, _, clock = make(min_duty=None)
    lamp.show(READY, cut=True)
    lamp.show(Look("amber", 0.05), fade_s=4.0)
    t0 = clock.t
    greens = [lamp.render(t0 + i * 0.01)[0][1] > 0 for i in range(401)]
    flips = sum(1 for a, b in zip(greens, greens[1:]) if a != b)
    assert flips == 1


def test_day_and_night_palette_interpolate_over_log_level():
    lamp, _, _ = make(palette={"amber": {"day": [1.0, 0.2, 0.0], "night": [1.0, 0.1, 0.0]}})
    share = lambda x: lamp.duty_for("amber", x)[1] / lamp.duty_for("amber", x)[0]  # noqa: E731
    assert share(1.0) == pytest.approx(0.2)
    assert share(0.35) == pytest.approx(0.1)
    assert share(0.25) == pytest.approx(0.1)              # below the anchor: night
    assert share(math.sqrt(0.35)) == pytest.approx(0.15)  # halfway in log(level)
    lamp2, _, _ = make(palette={"blue": [0.0, 0.0, 1.0]})
    assert lamp2.palette_night["blue"] == (0.0, 0.0, 1.0)


def test_level_gain_matches_brightness_and_is_capped():
    lamp, _, clock = make(level_gain={"blue": 1.5})
    lamp.show(CASTING, cut=True)
    assert lamp.render(clock.t)[0][2] == pytest.approx((0.55 * 1.5) ** G)
    lamp.show(Look("blue", 0.9), cut=True)
    assert lamp.render(clock.t)[0][2] == pytest.approx(1.0)
    assert lvl(lamp, clock.t) == pytest.approx(0.9)       # the Frame keeps its level


def test_ember_levels_come_from_calibration():
    lamp, _, clock = make(levels={"ember_day": 0.30, "ember_night": 0.10})
    lamp.show(STANDBY, cut=True)
    assert lvl(lamp, clock.t) == pytest.approx(0.30)
    lamp.set_night(True)
    assert lvl(lamp, clock.t + 2) == pytest.approx(0.10)
    lamp.countdown(1.0)
    assert lvl(lamp, clock.t + 2) == pytest.approx(0.10)


def test_calibrate_live_and_read_back():
    lamp, _, clock = make()
    lamp.show(AMBER)
    spec = {"palette": {"amber": [1.0, 0.25, 0.0]}, "balance": {"green": 0.8},
            "min_duty": {"red": 0.008, "green": 0.007, "blue": 0.009},
            "level_gain": {"blue": 1.2, "magenta": 3}, "levels": {"ember_day": 0.25},
            "amber_band": [0.1, 0.18]}
    lamp.calibrate(spec)
    assert lamp.render(clock.t)[0][1] == pytest.approx(0.25 * 0.8)
    cal = lamp.calibration()
    assert cal["palette"]["amber"] == {"day": [1.0, 0.25, 0.0], "night": [1.0, 0.25, 0.0]}
    assert cal["min_duty"] == {"red": 0.008, "green": 0.007, "blue": 0.009}
    assert cal["level_gain"] == {"amber": 1.0, "ember": 1.0, "blue": 1.2}
    assert cal["levels"] == {"ember_day": 0.25, "ember_night": 0.15}
    assert cal["amber_band"] == [0.1, 0.18]
    assert lamp.base_palette["amber"] == (1.0, 0.25, 0.0)
    lamp.calibrate(None)
    assert lamp.calibration()["min_duty"]["red"] == 0.006


def test_bad_calibration_falls_back_to_defaults():
    lamp, _, _ = make(palette={"amber": "orange"}, level_gain={"blue": "x"},
                      levels={"ember_day": None}, amber_band="wide")
    assert lamp.palette["amber"] == eng.DEFAULT_PALETTE["amber"]
    assert lamp.level_gain["blue"] == 1.0 and lamp.ember_day == 0.22
    assert lamp.amber_band == eng.AMBER_BAND


def test_preview_colour_goes_through_the_calibration():
    lamp, _, clock = make(min_duty=None)
    lamp.show(READY)
    lamp.preview_colour("ember", 0.22)
    assert lamp.render(clock.t)[0] == pytest.approx(lamp.duty_for("ember", 0.22))
    assert lamp.previewing


# -- watchdog --------------------------------------------------------------------


def test_watchdog_breathes_slow_ember_when_the_keeper_stops():
    lamp, _, clock = make()
    lamp.watchdog(5.0)
    lamp.show(READY)
    clock.t += 4.0
    lamp.feed()
    clock.t += 4.0
    lamp.render(clock.t)
    assert not lamp.tripped
    clock.t += 1.5
    lamp.render(clock.t)
    assert lamp.tripped
    f = frame(lamp, clock.t + eng.FALL_S * 2)
    assert f.w == eng.ONEHOT["ember"]
    assert lamp.render(clock.t + 5)[1]                       # breathing
    lamp.feed()                                              # the keeper is back
    assert not lamp.tripped
    t0 = clock.t
    f = frame(lamp, t0 + 3)
    assert f.w == eng.ONEHOT["amber"] and f.level == pytest.approx(0.30)


def test_watchdog_is_lifted_by_a_show_too_and_can_be_disarmed():
    lamp, _, clock = make()
    lamp.watchdog(1.0)
    lamp.show(READY)
    clock.t += 2
    lamp.render(clock.t)
    assert lamp.tripped
    lamp.show(PLAYING)
    assert not lamp.tripped
    assert lvl(lamp, clock.t + 0.9) == pytest.approx(0.80)
    lamp.watchdog(None)
    clock.t += 100
    lamp.render(clock.t)
    assert not lamp.tripped


def test_watchdog_wakes_the_sleeping_thread():
    out = FakeOutput()
    lamp = LampEngine(out, min_duty=NO_FLOOR)
    try:
        lamp.watchdog(0.1)
        lamp.show(READY)
        deadline = time.monotonic() + 2.0
        while not lamp.tripped and time.monotonic() < deadline:
            time.sleep(0.01)
        assert lamp.tripped
    finally:
        lamp.close()


# -- writes, thread, close --------------------------------------------------------


def test_step_writes_only_when_it_changes():
    lamp, out, clock = make()
    lamp.show(READY)
    for _ in range(10):
        lamp.step(clock.t)
    assert len(out.writes) == 1
    lamp.show(AMBER)
    t0 = clock.t
    for i in range(60):
        lamp.step(t0 + i * 0.02)
    assert out.last == pytest.approx(eng.DEFAULT_PALETTE["amber"])     # exact once settled
    assert 10 < len(out.writes) < 62


def test_unknown_or_malformed_replies_are_ignored():
    lamp, _, _ = make()
    lamp.show(READY)
    for bad in ("wave", "signature:chartreuse", "signature:off", "signature:blue@2", ""):
        lamp.reply(bad)
        assert lamp._reply is None, bad


def test_thread_sleeps_when_steady_and_renders_fades():
    out = FakeOutput()
    lamp = LampEngine(out, min_duty=NO_FLOOR)
    try:
        assert lamp._thread.name == "lamp" and lamp._thread.daemon
        lamp.show(READY)
        time.sleep(0.1)
        n = len(out.writes)
        assert out.last == pytest.approx((0.30 ** G, 0.17 * 0.30 ** G, 0.0))
        time.sleep(0.3)
        assert len(out.writes) == n                        # steady: no writes
        lamp.show(AMBER)
        time.sleep(0.8)
        assert len(out.writes) > n + 5                     # ~50 Hz during the rise
        assert out.last == pytest.approx(eng.DEFAULT_PALETTE["amber"])
    finally:
        lamp.close()


def test_callers_never_wait_on_a_slow_output():
    gate = threading.Event()

    class Slow(FakeOutput):
        def write(self, r, g, b):
            gate.wait(2)
            super().write(r, g, b)

    lamp = LampEngine(Slow())
    try:
        lamp.show(READY)
        lamp.show(AMBER)
        time.sleep(0.05)                                   # the thread is stuck writing
        t = time.monotonic()
        lamp.show(READY)
        lamp.reply("ack")
        lamp.countdown(0.5)
        lamp.identify(3)
        lamp.feed()
        assert time.monotonic() - t < 0.05
    finally:
        gate.set()
        lamp.close()


def test_close_leaves_it_dark_and_stops_the_thread():
    out = FakeOutput()
    lamp = LampEngine(out)
    lamp.show(AMBER)
    time.sleep(0.05)
    lamp.close()
    assert out.last == (0.0, 0.0, 0.0) and out.closed
    assert not lamp._thread.is_alive()
    lamp.close()                                           # twice is fine


def test_close_without_halt_dark_leaves_the_last_frame():
    out = FakeOutput()
    lamp = LampEngine(out, threaded=False)
    lamp.show(AMBER)
    lamp.step(0)
    lamp.close(halt_dark=False)
    assert out.last == pytest.approx(eng.DEFAULT_PALETTE["amber"]) and out.closed


def test_steady_and_beckoning_helpers():
    assert steady("amber", 0.3, 0.2) == Look("amber", 0.3, "solid", 0.0, 0.0, 0.2)
    assert beckoning("amber", 0.3).period_s == 2.2


# -- sysfs pwm ---------------------------------------------------------------


def fake_sysfs(tmp_path, gpios=(17, 22, 27), with_gpios_prop=True, extra_hw_chip=True):
    """pwmchipN dirs as the pwm-gpio overlay makes them; export creates pwm0."""
    root = tmp_path / "pwm"
    n = 0
    if extra_hw_chip:   # the SoC's own PWM, which must be skipped
        chip = root / f"pwmchip{n}"
        (chip / "device" / "of_node").mkdir(parents=True)
        (chip / "device" / "of_node" / "compatible").write_bytes(b"brcm,bcm2835-pwm\0")
        n += 1
    for gpio in gpios:
        chip = root / f"pwmchip{n}"
        node = chip / "device" / "of_node"
        node.mkdir(parents=True)
        (node / "compatible").write_bytes(b"pwm-gpio\0")
        if with_gpios_prop:
            (node / "gpios").write_bytes(
                (0x7F).to_bytes(4, "big") + gpio.to_bytes(4, "big") + (0).to_bytes(4, "big")
            )
        n += 1
    return root


def export_all(root):
    for chip in root.glob("pwmchip*"):
        pwm = chip / "pwm0"
        pwm.mkdir()
        for name in ("period", "duty_cycle", "enable"):
            (pwm / name).write_text("0")
        (pwm / "polarity").write_text("normal")


def duty(root, n):
    # The fake is a plain file: a shorter write leaves the old tail behind.
    return int((root / f"pwmchip{n}" / "pwm0" / "duty_cycle").read_text().split()[0])


def test_discovery_by_device_tree_gpio(tmp_path):
    root = fake_sysfs(tmp_path, gpios=(27, 17, 22))   # declared in any order
    chips = discover_chips({"red": 17, "green": 22, "blue": 27}, root)
    assert {c: p.name for c, p in chips.items()} == {
        "red": "pwmchip2", "green": "pwmchip3", "blue": "pwmchip1",
    }


def test_discovery_falls_back_to_declaration_order(tmp_path, caplog):
    root = fake_sysfs(tmp_path, with_gpios_prop=False)
    chips = discover_chips({"red": 17, "green": 22, "blue": 27}, root)
    assert [chips[c].name for c in ("red", "green", "blue")] == ["pwmchip1", "pwmchip2", "pwmchip3"]
    assert "can't tell" in caplog.text


def test_discovery_without_chips(tmp_path):
    assert discover_chips({"red": 17, "green": 22, "blue": 27}, tmp_path / "nothing") is None


def test_sysfs_anode_uses_inversed_polarity_at_800hz(tmp_path, monkeypatch):
    root = fake_sysfs(tmp_path, extra_hw_chip=False)
    export_all(root)
    pwm = root / "pwmchip0" / "pwm0"
    (pwm / "enable").write_text("1")            # running with the wrong polarity
    order = []
    real_write = type(root).write_text

    def write_text(self, data, *a, **kw):
        if self.parent == pwm:
            order.append((self.name, data))
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(type(root), "write_text", write_text)
    out = SysfsPwmOutput(common="anode", frequency_hz=800, root=root)
    # pwm-gpio idles a disabled channel at the polarity's idle level: switch
    # polarity only while disabled, and with duty 0 first, then period, enable.
    assert order == [
        ("duty_cycle", "0"), ("enable", "0"), ("polarity", "inversed"),
        ("duty_cycle", "0"), ("period", "1250000"), ("duty_cycle", "0"), ("enable", "1"),
    ]
    assert (pwm / "polarity").read_text() == "inversed"
    out.write(1.0, 0.25, 0.0)
    assert (duty(root, 0), duty(root, 1), duty(root, 2)) == (1250000, 312500, 0)   # lit time
    out.close()


def test_sysfs_cathode_is_direct_and_exports(tmp_path, monkeypatch):
    root = fake_sysfs(tmp_path, extra_hw_chip=False)
    for chip in root.glob("pwmchip*"):    # export makes pwm0, as the kernel would
        (chip / "export").write_text("")
    real_write = type(root).write_text

    def write_text(self, data, *a, **kw):
        if self.name == "export":
            pwm = self.parent / "pwm0"
            pwm.mkdir(exist_ok=True)
            for name in ("period", "duty_cycle", "enable"):
                (pwm / name).write_text("0")
            (pwm / "polarity").write_text("normal")
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(type(root), "write_text", write_text)
    out = SysfsPwmOutput(common="cathode", frequency_hz=1000, root=root)
    out.write(0.5, 0.0, 1.0)
    assert (duty(root, 0), duty(root, 1), duty(root, 2)) == (500000, 0, 1000000)
    out.close()


def test_sysfs_keeps_the_boot_services_glow(tmp_path):
    root = fake_sysfs(tmp_path, extra_hw_chip=False)
    export_all(root)
    for chip in root.glob("pwmchip*"):
        (chip / "pwm0" / "period").write_text("1250000")
        (chip / "pwm0" / "duty_cycle").write_text("90000")
        (chip / "pwm0" / "enable").write_text("1")
        (chip / "pwm0" / "polarity").write_text("inversed")
    SysfsPwmOutput(root=root).close()
    assert duty(root, 0) == 90000


def test_open_output_degrades_to_null(tmp_path, monkeypatch):
    import haslerd.lamp.outputs as outputs

    monkeypatch.setattr(outputs, "SYSFS_PWM", tmp_path / "none")
    monkeypatch.setattr(outputs.SysfsPwmOutput.__init__, "__defaults__",
                        (None, "anode", 800, tmp_path / "none"))
    assert isinstance(open_output({"backend": "pwm-sysfs"}), NullOutput)
    assert isinstance(open_output({"backend": "none"}), NullOutput)
    assert isinstance(open_output({"backend": "neon"}), NullOutput)
    assert isinstance(open_output({"common": "annode"}), NullOutput)


def test_gpiozero_output_raises_frequency_and_inverts_anode():
    made = {}

    class Led:
        frequency = 100

    class RGBLED:
        def __init__(self, red, green, blue, active_high, pwm):
            made.update(pins=(red, green, blue), active_high=active_high, pwm=pwm)
            self._leds = (Led(), Led(), Led())
            self.value = (0, 0, 0)

        def off(self):
            self.value = (0, 0, 0)

        def close(self):
            made["closed"] = True

    out = GpiozeroOutput(common="anode", factory=RGBLED)
    assert made == {"pins": (17, 22, 27), "active_high": False, "pwm": True}
    assert all(led.frequency == 400 for led in out._led._leds)
    out.write(1.5, 0.2, -1)
    assert out._led.value == (1.0, 0.2, 0.0)
    out.close()
    assert made["closed"]


def test_open_lamp(monkeypatch):
    import haslerd.lamp.engine as engine

    assert open_lamp({"backend": "none"}) is None
    monkeypatch.setattr(engine, "open_output", lambda spec: NullOutput())
    assert open_lamp({"backend": "pwm-sysfs"}) is None     # nothing works -> no lamp
    seen = []

    def fake(spec):
        seen.append(spec["backend"])
        return FakeOutput() if spec["backend"] == "gpio" else NullOutput()

    monkeypatch.setattr(engine, "open_output", fake)
    lamp = open_lamp({"backend": "pwm-sysfs", "palette": {"amber": [1, 0.2, 0]}})
    try:
        assert seen == ["pwm-sysfs", "gpio"]                  # falls back to gpiozero
        assert isinstance(lamp, LampEngine) and lamp.palette["amber"] == (1.0, 0.2, 0.0)
    finally:
        lamp.close()


def test_hasler_descriptor_lamp_block():
    from conftest import VARIANTS
    from haslerd.variant import load_variant

    spec = load_variant(VARIANTS / "hasler-hm.yaml").io["lamp"]
    assert spec["backend"] == "pwm-sysfs" and spec["common"] == "anode"
    assert spec["pins"] == {"red": 17, "green": 22, "blue": 27}
    assert spec["frequency_hz"] == 800


# -- the lamp page's previews ----------------------------------------------------


def test_preview_look_shows_a_meaning_then_the_live_look():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.preview_look("phone", 4.0)
    t0 = clock.t
    f = frame(lamp, t0 + 2.0)
    assert f.w == eng.ONEHOT["blue"] and f.level == pytest.approx(0.55)
    lamp.show(PLAYING)                       # the live state moves on underneath
    assert frame(lamp, t0 + 3.0).w == eng.ONEHOT["blue"]
    lamp.render(t0 + 4.0)
    f = frame(lamp, t0 + 6.0)
    assert f.w == eng.ONEHOT["amber"] and f.level == pytest.approx(0.80)


@pytest.mark.parametrize("name", sorted(eng.PREVIEW_LOOKS))
def test_every_meaning_previews(name):
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.preview_look(name, 3)
    assert lamp.render(clock.t + 1.0)[1]
    assert lamp._target_look() == eng.PREVIEW_LOOKS[name]


def test_preview_reply_and_stop_preview():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.set_key_replies(False)
    lamp.preview_reply("heard")
    assert lamp._reply == "kick"                 # the page shows it even with key replies off
    lamp.preview_reply("no")
    assert lamp._reply == "refuse"
    lamp.preview_reply("wave")
    assert lamp._reply == "refuse"
    lamp.preview_look("busy", 30)
    lamp.preview([0, 0, 1])
    lamp.identify(10)
    lamp.stop_preview()
    assert not lamp.previewing and not lamp.identifying and lamp._preview_look is None
    assert frame(lamp, clock.t + 3).w == eng.ONEHOT["amber"]


def test_confirmed_kicks_even_with_key_replies_off():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.set_key_replies(False)
    lamp.reply("ack")
    assert lamp._reply is None
    lamp.reply("confirmed")
    assert lamp._reply == "kick"


def test_preview_duty_is_raw():
    lamp, _, clock = make(min_duty=None, balance={"green": 0.5})
    lamp.show(READY)
    lamp.preview_duty((0.004, 0.003, 0.0), hold_s=1.0)
    assert lamp.render(clock.t)[0] == (0.004, 0.003, 0.0)    # no floor, no balance, no gamma
    assert lamp.render(clock.t + 2)[0] != (0.004, 0.003, 0.0)
