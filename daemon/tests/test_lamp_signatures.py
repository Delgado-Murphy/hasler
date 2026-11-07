"""Every signal unmistakable on the real lamp - docs/lamp.md "Telling them apart".

Each meaning of the light language v2 is rendered through the whole pipeline
(palette, amber band, gamma, minimum duty, gate) on a fake clock - by day and
by night, with the default floors and with the floors the owner measured on
the real frosted LED (red 0.03, green 0.045, blue 0.03 duty) - and reduced to
what a person notices: its hue family (from the rendered duty), its motion
class (still, smooth, sharp, coded), its tempo, its level and how far it
swings in light (after gamma).

**The contrast rule:** any two meanings differ in at least two of {hue
family, motion class, tempo >= 2x, level or swing >= 3:1}, at least one of
them not hue. A few pairs where brightness *is* the message are listed in
LEVEL_ONLY and must still differ by 3:1 in light where the owner's levels
allow; NIGHT_TIGHT names the pairs the night levels squeeze (reported, and
pinned so a change is seen).

The second half pins the engine fixes from the rendered review: amber never
turns ember-red and ember never turns amber at any brightness, no minor
channel snaps on or off in one frame, brightness keeps the replies'
headroom, the countdown never cuts a reply, the levels near the floors stay
readable - with both sets of floors.
"""

from __future__ import annotations

import itertools
import math

import pytest

from haslerd.lamp import engine as eng
from haslerd.lamp import policy, signals
from haslerd.lamp.contract import BoxState, Look
from haslerd.lamp.engine import LampEngine
from haslerd.lamp.outputs import FakeOutput

FPS = 50
TRACE_S = 26.0          # two red-alert cycles at night (12 s), two SOS cycles by day (11 s)
LUMA = (0.30, 0.59, 0.11)   # rough photopic weights; the blue die is dim anyway
MEASURED = {"red": 0.03, "green": 0.045, "blue": 0.03}
FLOORS = {"default": None, "measured": MEASURED}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def engine(night=False, brightness="normal", floors=None):
    clock = Clock()
    # A fixed seed: the filament's shimmer is random, and a test must not be.
    lamp = LampEngine(FakeOutput(), clock=clock, threaded=False, min_duty=floors, seed=1964)
    lamp.set_night(night)
    lamp.set_brightness(brightness)
    return lamp, clock


LOOKS: dict[str, Look] = {
    "playing": policy.PLAYING,
    "ready": policy.READY,
    "asleep": policy.STANDBY,
    "phone": policy.CASTING,
    "phone paused": policy.CAST_PAUSED,
    "busy": policy.BUSY,
    "asking": policy.confirm_look((policy.READY, 0.0)),
    "setup": policy.SETUP,
    "no internet": policy.NO_INTERNET,
    "fault": policy.FAULT,
    "pairing": policy.PAIRING,
}
MEANINGS = tuple(LOOKS) + ("identify",)
MOTION = {"playing": "filament", "ready": "still", "asleep": "still", "phone": "still",
          "phone paused": "still", "busy": "smooth", "pairing": "smooth", "asking": "sharp",
          "no internet": "sharp", "setup": "coded", "fault": "coded", "identify": "coded"}
# Morse codes are told apart by their rhythm.
CODES = {"setup": "cq", "fault": "sos", "identify": "hi"}
# Brightness is the message: a phone playing / paused.
LEVEL_ONLY = {("phone", "phone paused")}
# Colour is the message: two still states of different hue families at
# similar levels (the radio, a phone, asleep). They differ by hue alone -
# reported to the owner (docs/lamp.md "Telling them apart").
HUE_IS_MESSAGE = {("ready", "phone"), ("playing", "phone"), ("asleep", "phone"),
                  ("asleep", "phone paused"), ("ready", "phone paused")}
# Pairs the owner's night levels squeeze (playing 35 / ready 30, casting 30 /
# paused 20, ember 18 near ready 30): the rule is relaxed to one dimension,
# and the light ratio is pinned below so a change shows.
NIGHT_TIGHT = {("playing", "ready"), ("phone", "phone paused"), ("ready", "asleep")}
# The spec's own timings put these on one dimension (motion class): the busy
# breath (2.8 s) and the beckon (two kicks + 2.2 s = 2.83 s) share a tempo and
# both are amber; by day the breath's 13:1 swing against the beckon's 3.5:1
# adds "level", at night it doesn't. Reported to the owner.
SPEC_CONFLICTS = {("busy", "asking")}
# With the measured floors (a 4.5 % green floor) amber can't be dim: CQ's 25 %
# base turns red like the red alert's flashes, and at night amber's green held
# at its floor flattens every amber motion's swing in light. Reported.
MEASURED_LIMITS = {("setup", "no internet"), ("playing", "asking")}
MEASURED_NIGHT_LIMITS = {("playing", "busy"), ("playing", "asking"), ("ready", "busy"), ("ready", "asking")}


def trace(meaning, night=False, floors=None, brightness="normal", seconds=TRACE_S):
    lamp, clock = engine(night, brightness, floors)
    if meaning == "identify":
        lamp.show(policy.READY, cut=True)
        lamp.render(clock.t)
        lamp.identify(60)
    else:
        lamp.show(LOOKS[meaning], cut=True)
    t0 = clock.t
    return [lamp.render(t0 + i / FPS)[0] for i in range(int(seconds * FPS))]


def luma(d):
    return sum(w * c for w, c in zip(LUMA, d))


def period_of(ys):
    n = len(ys)
    mean = sum(ys) / n
    xs = [y - mean for y in ys]
    var = sum(x * x for x in xs)
    if var <= 1e-12 * n:
        return None
    corr = {}
    for lag in range(int(0.3 * FPS), int(13 * FPS), 2):
        idx = range(0, n - lag, 2)
        corr[lag] = sum(xs[i] * xs[i + lag] for i in idx) / len(idx) / (var / n)
    lags = sorted(corr)
    peaks = [lag for a, lag, b in zip(lags, lags[1:], lags[2:])
             if corr[lag] >= corr[a] and corr[lag] >= corr[b] and corr[lag] > 0.5]
    if not peaks:
        return None
    top = max(corr[lag] for lag in peaks)
    return min(lag for lag in peaks if corr[lag] >= 0.85 * top) / FPS


def hue_of(d):
    r, g, b = d
    top = max(r, g, b)
    if top <= 0.0:
        return "dark"
    if top == b:
        return "blue"
    if top == g:
        return "green"
    return "amber" if g / r >= 0.08 else "red"


def hues_of(duties):
    """The hues shown at least 2 % of the time (a red flash over amber counts)."""
    count: dict[str, int] = {}
    for d in duties:
        h = hue_of(d)
        count[h] = count.get(h, 0) + 1
    return frozenset(h for h, c in count.items() if c >= 0.02 * len(duties) and h != "dark")


def main_hue(duties):
    return hue_of((sum(d[0] for d in duties), sum(d[1] for d in duties), sum(d[2] for d in duties)))


def features(meaning, duties):
    ys = [luma(d) for d in duties]
    lo, hi = min(ys), max(ys)
    swing = hi / lo if lo > 0 else math.inf
    return {"mean": sum(ys) / len(ys), "lo": lo, "hi": hi, "swing": swing,
            "period": period_of(ys) if swing >= 1.2 else None,
            "hue": main_hue(duties), "hues": hues_of(duties), "motion": MOTION[meaning],
            "code": CODES.get(meaning)}


def ratio(a, b):
    a, b = max(a, 1e-12), max(b, 1e-12)
    return max(a, b) / min(a, b)


def dims(a, b):
    """The dimensions a and b differ in (contrast rule)."""
    out = []
    if a["hue"] != b["hue"] or a["hues"] != b["hues"]:
        out.append("hue")
    if a["motion"] != b["motion"] or (a["code"] and b["code"] and a["code"] != b["code"]):
        out.append("motion")        # another class, or another code (CQ is not SOS)
    if a["period"] and b["period"] and ratio(a["period"], b["period"]) >= 1.9:
        out.append("tempo")
    if ratio(a["mean"], b["mean"]) >= 3.0 or ratio(a["swing"], b["swing"]) >= 3.0:
        out.append("level")
    return out


_CACHE: dict = {}


def table(night, floors):
    key = (night, floors)
    if key not in _CACHE:
        _CACHE[key] = {m: features(m, trace(m, night, FLOORS[floors])) for m in MEANINGS}
    return _CACHE[key]


GRID = [(n, f) for f in FLOORS for n in (False, True)]
IDS = [f"{'night' if n else 'day'}-{f}" for n, f in GRID]


@pytest.mark.parametrize("night, floors", GRID, ids=IDS)
def test_playing_is_told_from_ready_and_busy(night, floors):
    feats = table(night, floors)
    p, r, b = feats["playing"], feats["ready"], feats["busy"]
    assert p["motion"] != r["motion"] and p["motion"] != b["motion"]
    assert ratio(p["swing"], b["swing"]) >= (1.5 if night and floors == "measured" else 2.5)   # a shimmer, not a breath
    if not night:
        assert ratio(p["mean"], r["mean"]) >= 3.0                   # 80 % vs 45 %


@pytest.mark.parametrize("night, floors", GRID, ids=IDS)
def test_the_contrast_rule(night, floors):
    feats = table(night, floors)
    bad = []
    for a, b in itertools.combinations(MEANINGS, 2):
        d = dims(feats[a], feats[b])
        pair = (a, b)
        if pair in LEVEL_ONLY or (night and pair in NIGHT_TIGHT):
            continue
        if pair in HUE_IS_MESSAGE:
            assert "hue" in d, (a, b, d)
            continue
        if pair in SPEC_CONFLICTS:
            assert "motion" in d, (a, b, d)
            continue
        if floors == "measured" and (pair in MEASURED_LIMITS or (night and pair in MEASURED_NIGHT_LIMITS)):
            assert d, (a, b, d)
            continue
        if len(d) < 2 or d == ["hue"] or set(d) == {"hue"}:
            bad.append((a, b, d))
    assert not bad, bad


@pytest.mark.parametrize("night, floors", GRID, ids=IDS)
def test_level_only_pairs_differ_3_to_1_by_day(night, floors):
    feats = table(night, floors)
    for a, b in LEVEL_ONLY:
        r = ratio(feats[a]["mean"], feats[b]["mean"])
        if not night:
            assert r >= 3.0, (a, b, r)


def test_night_tight_pairs_are_what_the_levels_allow():
    """Pinned: the owner's night levels leave these close (see docs/lamp.md)."""
    feats = table(True, "default")
    got = {pair: round(ratio(feats[pair[0]]["mean"], feats[pair[1]]["mean"]), 1) for pair in NIGHT_TIGHT}
    assert got[("playing", "ready")] < 2.0          # 35 % vs 30 %: by level alone, near
    assert got[("ready", "asleep")] >= 2.5          # amber vs ember: hue, and level
    assert feats["ready"]["hue"] == "amber" and feats["asleep"]["hue"] == "red"


@pytest.mark.parametrize("night, floors", GRID, ids=IDS)
def test_every_moving_look_swings_at_least_3_to_1_in_light(night, floors):
    feats = table(night, floors)
    for m, f in feats.items():
        if m == "playing":
            # The one calm motion: the filament's shimmer - small, and no rhythm.
            assert 1.02 < f["swing"] < (1.30 if not night else 1.20), (m, f["swing"])
            assert f["period"] is None, (m, f["period"])
        elif night and floors == "measured" and m in ("busy", "asking"):
            assert f["swing"] >= 1.7, (m, f["swing"])      # MEASURED_NIGHT_LIMITS: reported
        elif f["motion"] != "still":
            assert f["swing"] >= 3.0, (m, f["swing"])
        else:
            assert f["swing"] < 1.05, (m, f["swing"])        # calm signals are truly still


@pytest.mark.parametrize("night, floors", GRID, ids=IDS)
def test_hues_render_as_designed(night, floors):
    feats = table(night, floors)
    want = {"playing": "amber", "ready": "amber", "asleep": "red", "phone": "blue",
            "phone paused": "blue", "busy": "amber", "setup": "amber", "fault": "red",
            "pairing": "blue", "no internet": "amber"}
    for m, h in want.items():
        assert feats[m]["hue"] == h, (m, feats[m]["hue"])


@pytest.mark.parametrize("night", [False, True], ids=["day", "night"])
def test_the_tempos(night):
    f = table(night, "default")
    k = 1.5 if night else 1.0
    assert f["busy"]["period"] == pytest.approx(2.8 * k, abs=0.1)
    assert f["pairing"]["period"] == pytest.approx(2.8 * k, abs=0.1)
    assert f["no internet"]["period"] == pytest.approx(12.0 if night else 6.0, abs=0.1)
    assert f["asking"]["period"] == pytest.approx(eng.BECKON_GAP_S + eng.kick_len(eng.KICK_DECAY_S * k)
                                                  + 2.2, abs=0.1)
    fault = signals.get("fault")
    sos = eng.pattern_len(fault.night_steps if night else fault.steps)
    assert sos == pytest.approx(4.05 * k + 7.0 * k, abs=0.01)     # 27 units of 150 ms, then 7 s


def distinctness_table(night, floors="default"):
    feats = table(night, floors)
    rows = [f"| {m} | {f['hue']} | {f['motion']} | "
            f"{(str(round(f['period'], 2)) + ' s') if f['period'] else '-'} | "
            f"{f['mean']:.4f} | {f['swing']:.1f}:1 |" for m, f in feats.items()]
    pairs = [f"{a} / {b}: {', '.join(dims(feats[a], feats[b])) or '-'}"
             for a, b in itertools.combinations(MEANINGS, 2)]
    return rows, pairs


# -- the replies and overlays -------------------------------------------------------


def reply_trace(word, night=False, base=None, seconds=6.0, floors=None, lamp_setup=None):
    lamp, clock = engine(night, floors=floors)
    lamp.show(base or policy.READY, cut=True)
    lamp.render(clock.t)
    if lamp_setup:
        lamp_setup(lamp)
    lamp.reply(word)
    t0 = clock.t
    return lamp, [lamp.render(t0 + i / FPS)[0] for i in range(int(seconds * FPS))]


def test_the_hal_eye_is_red_65_for_1_9_s():
    lamp, ds = reply_trace("refuse")
    reds = [d for d in ds if d[1] == 0.0 and d[0] > 0.2]
    peak = max(d[0] for d in ds)
    assert peak == pytest.approx(0.65 ** eng.GAMMA, rel=0.01)
    hold = [i for i, d in enumerate(ds) if d[0] >= 0.99 * peak]
    assert (hold[-1] - hold[0]) / FPS == pytest.approx(0.7, abs=0.1)
    assert reds and ds[int(1.95 * FPS)] == pytest.approx(ds[-1], rel=0.02)    # back at 1.9 s


def test_the_hal_eye_at_night_is_35_and_slower():
    lamp, ds = reply_trace("fail", night=True)
    peak = max(d[0] for d in ds)
    assert peak == pytest.approx(0.35 ** eng.GAMMA, rel=0.01)
    hold = [i for i, d in enumerate(ds) if d[0] >= 0.99 * peak]
    assert (hold[-1] - hold[0]) / FPS == pytest.approx(1.05, abs=0.15)


def test_the_volume_sag_is_the_only_plain_sag_and_never_red():
    lamp, ds = reply_trace("nudge")
    assert max(d[0] for d in ds) <= ds[0][0] * 1.001   # it only sags: no rise to red
    assert min(d[0] for d in ds) < 0.3 * ds[0][0]
    assert lamp._reply != "pattern"


def test_the_green_bloom_cross_fades_to_green_and_back():
    lamp, ds = reply_trace("bloom")
    greens = [i for i, d in enumerate(ds) if d[1] > d[0]]
    assert greens and greens[0] / FPS <= 0.2 and (greens[-1] - greens[0]) / FPS == pytest.approx(0.55, abs=0.15)
    assert ds[int(1.1 * FPS)] == pytest.approx(ds[-1], rel=0.02)      # back by ~1 s


def test_confirmed_blooms_even_with_key_replies_off():
    lamp, ds = reply_trace("confirmed", lamp_setup=lambda lp: lp.set_key_replies(False))
    assert any(d[1] > d[0] for d in ds)


def test_ok_is_the_bloom_then_green_morse_ok():
    lamp, ds = reply_trace("ok", seconds=7.0)
    marks = 0
    for a, b in zip(ds, ds[1:]):
        if b[1] > b[0] and not a[1] > a[0]:
            marks += 1
    assert marks == 1 + 6                    # the bloom, then O (---) and K (-.-)


def test_update_ok_in_geek_mode_is_close_encounters():
    lamp, ds = reply_trace("update_ok", seconds=7.0, lamp_setup=lambda lp: lp.set_geek(True))
    amber_notes = sum(1 for d in ds if d[1] > 0 and d[0] > d[1] * 2 and d[0] > 0.25)
    assert amber_notes > FPS                 # the five notes in amber, ~2 s of them


def test_a_bloom_over_moon_blue_dips_through_the_trough():
    lamp, ds = reply_trace("bloom", base=policy.CASTING, seconds=3.0)
    for a, b in zip(ds, ds[1:]):
        if (a[2] > max(a[:2])) != (b[2] > max(b[:2])):          # blue <-> not blue
            assert min(max(a), max(b)) <= eng.HUE_TROUGH ** eng.GAMMA * 1.6


def identify_trace(night=False, base=None, lamp_setup=None, seconds=12.0):
    lamp, clock = engine(night)
    lamp.show(base or policy.READY, cut=True)
    lamp.render(clock.t)
    if lamp_setup:
        lamp_setup(lamp)
    lamp.identify(9)
    t0 = clock.t
    return lamp, clock, [lamp.render(t0 + i / FPS)[0] for i in range(int(seconds * FPS))]


def marks_in(ds):
    starts = []
    for i, (a, b) in enumerate(zip(ds, ds[1:])):
        if b[1] > b[0] and not a[1] > a[0]:
            starts.append(i)
    return starts


def test_identify_is_hi_in_green_three_times_then_back():
    lamp, clock, ds = identify_trace()
    starts = marks_in(ds)
    assert len(starts) == 3 * 6                           # H (....) I (..), three times
    first, last = starts[0], starts[-1]
    assert (last - first) / FPS == pytest.approx(6.0 + 1.69 - 0.13, abs=0.2)
    assert max(d[1] for d in ds) == pytest.approx((1.0 * 0.55) ** eng.GAMMA, rel=0.02)
    assert not lamp.identifying and ds[-1] == pytest.approx(lamp.render(clock.t + 20)[0])


def test_identify_is_bright_at_night_too():
    lamp, clock, ds = identify_trace(night=True, seconds=16.0)
    assert max(d[1] for d in ds) == pytest.approx((0.75 * 0.55) ** eng.GAMMA, rel=0.02)
    assert len(marks_in(ds)) == 3 * 6


def test_identify_sends_the_callsign_in_geek_mode():
    def geek(lamp):
        lamp.set_geek(True)
        lamp.set_callsign("K-1!")
    lamp, clock, ds = identify_trace(lamp_setup=geek)
    # K (-.-) 1 (.----): 8 marks a repeat
    assert len(marks_in(ds)) % 8 == 0 and len(marks_in(ds)) >= 8


def test_identify_over_moon_blue_dips_first():
    lamp, clock, ds = identify_trace(base=policy.CASTING)
    first_green = next(i for i, d in enumerate(ds) if d[1] > max(d[0], d[2]))
    assert max(ds[first_green - 1]) <= eng.HUE_TROUGH ** eng.GAMMA * 1.6


def test_seventy_three_before_the_shutdown_only_in_geek_mode():
    lamp, clock = engine()
    lamp.show(policy.READY, cut=True)
    assert lamp.farewell() is False
    lamp.set_geek(True)
    assert lamp.farewell() is True and lamp._reply == "pattern"


def test_tos_working_twinkles():
    lamp, clock = engine()
    lamp.show(policy.READY, cut=True)
    lamp._rng.seed(7)
    assert lamp.play_egg("tos_working")
    ds = [lamp.render(clock.t + i / FPS)[0] for i in range(150)]
    base = ds[-1][0]
    ups = sum(1 for a, b in zip(ds, ds[1:]) if b[0] > base * 1.5 and a[0] <= base * 1.5)
    assert 3 <= ups <= 6
    assert not lamp.play_egg("rickroll")


def test_morse_edges_are_crisp_and_timed_on_absolute_time():
    """A mark reaches its level within the 22 ms attack whatever the frame phase."""
    look = signals.get("fault")
    for phase in (0.0, 0.007, 0.013):
        lamp, clock = engine()
        lamp.show(look, cut=True)
        t0 = clock.t + phase
        first = look.steps[0][1]                       # the first dot, 150 ms
        assert lamp._look_frame(look, t0 + 0.022 + 1e-3).level == pytest.approx(0.80)
        assert lamp._look_frame(look, t0 + first + 0.12).level < 0.25


def test_asleep_never_moves():
    for word in ("swell", "bloom"):
        lamp, ds = reply_trace(word, base=policy.STANDBY)
    ds = trace("asleep")
    assert len({d for d in ds}) == 1


def test_the_signal_table_remaps_without_code():
    try:
        signals.remap({"setup": {"colour": "green", "motion": "pattern", "level": 0.6, "morse": "qrv"},
                       "identify": {"colour": "amber", "motion": "solid"},           # refused
                       "no_internet": {"colour": "amber", "motion": "wobble"}})     # refused
        assert policy.look_for(BoxState(hotspot=True))[0].colour == "green"
        assert signals.get("identify") == signals.DEFAULTS["identify"]
        assert signals.get("no_internet") == signals.DEFAULTS["no_internet"]
    finally:
        signals.remap(None)
    assert policy.look_for(BoxState(hotspot=True))[0] == signals.DEFAULTS["setup"]


def test_a_new_palette_colour_draws_from_config():
    lamp, clock = engine()
    lamp.calibrate({"palette": {"violet": {"rgb": [0.6, 0.0, 1.0], "family": "blue"}},
                    "level_gain": {"violet": 0.8}})
    assert "violet" in lamp.hues and lamp.families["violet"] == "blue"
    lamp.show(Look("violet", 0.5), cut=True)
    r, g, b = lamp.render(clock.t)[0]
    assert b == pytest.approx((0.5 * 0.8) ** eng.GAMMA) and r == pytest.approx(0.6 * b)
    assert lamp.play(signals.identify_look("v"))


# -- the rendered review's fixes ---------------------------------------------------

AMBER_LOOKS = {"ready": policy.READY, "playing": policy.PLAYING, "busy": policy.BUSY,
               "sleep glow": Look("amber", policy.SLEEP_LEVEL, night=policy.SLEEP_LEVEL),
               "asking": policy.confirm_look((policy.READY, 0.0))}
BRIGHT = [(n, b) for n in (False, True) for b in ("low", "normal", "bright")]


def look_trace(look, night, brightness, seconds=7.0, floors=None):
    lamp, clock = engine(night, brightness, floors)
    lamp.show(look, cut=True)
    return [lamp.render(clock.t + i / FPS)[0] for i in range(int(seconds * FPS))]


@pytest.mark.parametrize("night, brightness", BRIGHT)
def test_amber_is_never_drawn_ember_red(night, brightness):
    for name, look in AMBER_LOOKS.items():
        for r, g, _ in look_trace(look, night, brightness):
            assert g > 0 and g / r >= 0.08, (name, night, brightness, r, g)


@pytest.mark.parametrize("night, brightness", BRIGHT)
def test_amber_with_the_measured_floors_stays_amber_where_it_can(night, brightness):
    """With a 4.5 % green floor dim amber can't be amber AND dim: steady amber
    holds its green (a little yellow) down to night ready; what it never does
    is snap, or show green far above red (yellow-green)."""
    for name in ("ready", "playing"):
        for r, g, _ in look_trace(AMBER_LOOKS[name], night, brightness, 1.0, MEASURED):
            assert g > 0 and g / r <= 0.70, (name, r, g)


@pytest.mark.parametrize("floors", list(FLOORS))
@pytest.mark.parametrize("night, brightness", BRIGHT)
def test_ember_never_lights_its_green(night, brightness, floors):
    for r, g, b in look_trace(policy.STANDBY, night, brightness, 1.0, FLOORS[floors]):
        assert r > 0 and g == 0.0 and b == 0.0


def snaps(duties, floor=0.006):
    """Minor-channel switches bigger than a floor's step and a half, in one frame
    (the main channel changing - a hue swap at the trough - is the hue rule's)."""
    out = 0
    for a, b in zip(duties, duties[1:]):
        main_a = max(range(3), key=lambda i: a[i])
        main_b = max(range(3), key=lambda i: b[i])
        if main_a != main_b:
            continue
        for i in range(3):
            if i != main_a and (a[i] == 0.0) != (b[i] == 0.0) and max(a[i], b[i]) > floor * eng.GATE_SWITCH:
                out += 1
    return out


@pytest.mark.parametrize("floors", list(FLOORS))
@pytest.mark.parametrize("brightness", ["low", "normal", "bright"])
def test_no_minor_channel_snaps_at_look_changes_or_the_night_switch(brightness, floors):
    floor = 0.045 if floors == "measured" else 0.006
    for night in (False, True):
        lamp, clock = engine(night, brightness, FLOORS[floors])
        script = {0.0: lambda: lamp.show(policy.READY, cut=True),
                  1.0: lambda: lamp.show(policy.BUSY),
                  4.3: lambda: lamp.set_night(not night),           # mid-breath
                  6.0: lambda: lamp.show(policy.PLAYING),
                  8.0: lambda: lamp.show(policy.READY),
                  10.0: lambda: lamp.show(policy.BUSY),
                  11.5: lambda: lamp.show(policy.READY),
                  13.0: lambda: lamp.show(policy.PAIRING),
                  17.0: lambda: lamp.show(policy.CASTING),
                  19.0: lambda: lamp.show(policy.CAST_PAUSED),
                  21.0: lambda: lamp.show(policy.CASTING)}
        t0 = clock.t
        duties = []
        for i in range(int(24 * FPS)):
            t = round(i / FPS, 2)
            clock.t = t0 + t
            if t in script:
                script[t]()
            duties.append(lamp.render(clock.t)[0])
        assert snaps(duties, floor) == 0, (night, brightness)


def perceptual(d):
    return tuple(max(0.0, c) ** (1 / eng.GAMMA) for c in d)


@pytest.mark.parametrize("night", [False, True])
def test_bright_keeps_the_headroom_for_replies(night):
    lamp, clock = engine(night, "bright")
    lamp.show(policy.PLAYING, cut=True)
    base = sum(luma(lamp.render(clock.t + i / FPS)[0]) for i in range(150)) / 150   # the shimmer's mean
    clock.t += 3.0
    lamp.reply("kick")
    peak = max(luma(lamp.render(clock.t + i / FPS)[0]) for i in range(20))
    assert peak / base >= 1.6, (night, peak / base)


def test_the_countdown_never_cuts_a_kick_in_one_frame():
    lamp, clock = engine()
    lamp.show(policy.PLAYING, cut=True)
    lamp.reply("kick")
    t0 = clock.t
    for i in range(2):
        lamp.render(t0 + i * 0.01)
    before = perceptual(lamp.render(t0 + 0.02)[0])
    clock.t = t0 + 0.02
    lamp.countdown(0.05)
    steps = [before] + [perceptual(lamp.render(clock.t + i * 0.02)[0]) for i in range(1, 8)]
    jumps = [max(abs(x - y) for x, y in zip(a, b)) for a, b in zip(steps, steps[1:])]
    assert max(jumps) < 0.2, jumps


def test_the_volume_sag_still_reads_at_night_on_low():
    lamp, clock = engine(True, "low")
    lamp.show(policy.READY, cut=True)
    base = luma(lamp.render(clock.t)[0])
    lamp.reply("refuse1")
    low = min(luma(lamp.render(clock.t + i / FPS)[0]) for i in range(int(eng.SAG_S * FPS)))
    assert base / low >= 3.0, base / low


def test_phone_paused_on_low_stays_clearly_lit_and_its_red_trace_off():
    for night in (False, True):
        r, g, b = look_trace(policy.CAST_PAUSED, night, "low", 0.1)[0]
        assert b >= 0.015 and r == 0.0


@pytest.mark.parametrize("night", [False, True])
def test_a_kick_from_asleep_is_seen_but_not_harsh(night):
    lamp, ds = reply_trace("kick", night=night, base=policy.STANDBY, seconds=1.0)
    base = luma(ds[-1])
    peak = max(luma(d) for d in ds)
    assert 2.5 <= peak / base <= 4.5, (night, peak / base)


def test_halted_reads_off_and_faults_read_fault():
    assert policy.meaning_for(BoxState(halted=True)) == "off"
    assert policy.meaning_for(BoxState(fault=True, standby=True)) == "fault"


if __name__ == "__main__":
    for floors in FLOORS:
        for night in (False, True):
            rows, pairs = distinctness_table(night, floors)
            print(f"## {'night' if night else 'day'}, {floors} floors")
            print("| meaning | hue | motion | tempo | mean light | swing |")
            print("\n".join(rows))
            print("\n".join(p for p in pairs if len(p.split(": ")[1].split(", ")) < 2))
