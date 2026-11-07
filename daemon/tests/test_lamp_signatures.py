"""Every signal unmistakable on the real lamp - docs/lamp.md "Telling them apart".

Each meaning is rendered through the whole pipeline (palette, amber band,
gamma, minimum duty, gate) on a fake clock, at day and night, and reduced to
a few numbers a person would notice: how bright on average, how far it swings
(in light, after gamma), how fast it repeats, how many pulses a cycle, how
much of the time it is up, and its colour. Every pair of meanings must differ
by at least one clear margin - and the pairs the owner found too alike must
differ by *motion*, not just by a shade.

The second half pins the engine fixes from the rendered review: amber never
turns ember-red and ember never turns amber at any brightness, no minor
channel snaps on or off in one frame, brightness keeps the replies' headroom,
the countdown never cuts a reply, and the levels near the floors stay
readable.
"""

from __future__ import annotations

import itertools
import math

import pytest

from haslerd.lamp import engine as eng
from haslerd.lamp import policy
from haslerd.lamp.contract import BoxState, Look
from haslerd.lamp.engine import LampEngine
from haslerd.lamp.outputs import FakeOutput

FPS = 50
TRACE_S = 14.0          # long enough for two of busy's 6 s breaths
SETTLE_S = 3.0
LUMA = (0.30, 0.59, 0.11)   # rough photopic weights; the blue die is dim anyway


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def engine(night=False, brightness="normal"):
    clock = Clock()
    lamp = LampEngine(FakeOutput(), clock=clock, threaded=False)    # real minimum duties
    lamp.set_night(night)
    lamp.set_brightness(brightness)
    return lamp, clock


def trace(meaning, night=False, brightness="normal", seconds=TRACE_S):
    """Rendered duty (r, g, b) at 50 Hz for ``seconds``, after settling."""
    lamp, clock = engine(night, brightness)
    if meaning == "identify":
        lamp.show(policy.READY, cut=True)
        lamp.render(clock.t)
        lamp.identify(60)
    else:
        lamp.show(LOOKS[meaning], cut=True)
        clock.t += SETTLE_S
        for i in range(int(SETTLE_S * FPS)):         # let the state machine run up to now
            lamp.render(clock.t - SETTLE_S + i / FPS)
    t0 = clock.t
    return [lamp.render(t0 + i / FPS)[0] for i in range(int(seconds * FPS))]


LOOKS: dict[str, Look] = {
    "playing": policy.PLAYING,
    "ready": policy.READY,
    "asleep": policy.STANDBY,
    "phone": policy.CASTING,
    "phone paused": policy.CAST_PAUSED,
    "busy": policy.SHUTTING_DOWN,
    "asking": policy.confirm_look((policy.READY, 0.0)),
    "asking (phone)": policy.confirm_look((policy.CASTING, 0.0)),
    "setup": policy.HOTSPOT,
    "starting": policy.LOADING,
    "warming up": policy.BOOT["network"],
    "pairing": policy.PAIRING,
    "asleep, starting": policy.BOOT_STANDBY,
}
MEANINGS = tuple(LOOKS) + ("identify",)
# The same meaning drawn two ways (the page says "starting" for both).
SAME = {("starting", "warming up")}


def luma(d):
    return sum(w * c for w, c in zip(LUMA, d))


def period_of(ys):
    """The dominant period (s) by autocorrelation, or None for a steady trace."""
    n = len(ys)
    mean = sum(ys) / n
    xs = [y - mean for y in ys]
    var = sum(x * x for x in xs)
    if var <= 1e-12 * n:
        return None
    corr = {}
    for lag in range(int(0.3 * FPS), int(7.5 * FPS)):
        corr[lag] = sum(xs[i] * xs[i + lag] for i in range(n - lag)) / var * n / (n - lag)
    peaks = [lag for lag in list(corr)[1:-1]
             if corr[lag] >= corr[lag - 1] and corr[lag] >= corr[lag + 1] and corr[lag] > 0.5]
    if not peaks:
        return None
    top = max(corr[lag] for lag in peaks)
    return min(lag for lag in peaks if corr[lag] >= 0.9 * top) / FPS


def features(duties):
    ys = [luma(d) for d in duties]
    lo, hi = min(ys), max(ys)
    mean = sum(ys) / len(ys)
    swing = hi / lo if lo > 0 else math.inf
    moving = swing >= 1.2
    period = period_of(ys) if moving else None
    mid = math.sqrt(lo * hi) if lo > 0 else hi / 2
    up = [y > mid for y in ys]
    high = sum(up) / len(up)
    pulses = None
    if period:
        n = int(round(period * FPS))
        window = up[:n] + up[:1]
        pulses = sum(1 for a, b in zip(window, window[1:]) if b and not a)
    r = sum(d[0] for d in duties) / len(duties)
    g = sum(d[1] for d in duties) / len(duties)
    b = sum(d[2] for d in duties) / len(duties)
    family = "blue" if b > r else "warm"
    green = g / r if r > 0 else 0.0
    return {"mean": mean, "lo": lo, "hi": hi, "swing": swing, "moving": moving,
            "period": period, "pulses": pulses, "high": high, "family": family, "green": green}


def ratio(a, b):
    a, b = max(a, 1e-12), max(b, 1e-12)
    return max(a, b) / min(a, b)


def motion_margins(a, b):
    """What tells a and b apart by movement alone."""
    out = []
    if a["moving"] != b["moving"]:
        mover = a if a["moving"] else b
        if mover["swing"] >= 3.0:
            out.append(f"steady vs moving {mover['swing']:.0f}:1")
    elif a["moving"] and b["moving"]:
        if a["period"] and b["period"] and ratio(a["period"], b["period"]) >= 1.9:
            out.append(f"tempo {a['period']:.1f} vs {b['period']:.1f} s")
        if a["pulses"] and b["pulses"] and a["pulses"] != b["pulses"]:
            out.append(f"pulses {a['pulses']} vs {b['pulses']}")
        if ratio(a["swing"], b["swing"]) >= 3.0:
            out.append(f"depth {a['swing']:.0f}:1 vs {b['swing']:.0f}:1")
        if ratio(a["high"], b["high"]) >= 2.0:
            out.append(f"time up {a['high']:.0%} vs {b['high']:.0%}")
        if ratio(a["hi"], b["hi"]) >= 2.0:
            out.append(f"peak x{ratio(a['hi'], b['hi']):.1f}")
    return out


def margins(a, b):
    out = motion_margins(a, b)
    if a["family"] != b["family"]:
        out.append("colour family")
    elif a["family"] == "warm" and abs(a["green"] - b["green"]) >= 0.08:
        out.append(f"amber/ember (g/r {a['green']:.2f} vs {b['green']:.2f})")
    if ratio(a["mean"], b["mean"]) >= 2.0:
        out.append(f"level x{ratio(a['mean'], b['mean']):.1f}")
    return out


@pytest.fixture(scope="module", params=[False, True], ids=["day", "night"])
def table(request):
    night = request.param
    return night, {m: features(trace(m, night)) for m in MEANINGS}


def test_every_pair_of_meanings_differs_by_a_clear_margin(table):
    night, feats = table
    alike = []
    for a, b in itertools.combinations(MEANINGS, 2):
        if (a, b) not in SAME and not margins(feats[a], feats[b]):
            alike.append((a, b))
    assert not alike, ("night" if night else "day", alike)


# The owner's three, and the pairs like them: told apart by motion, not a shade.
BY_MOTION = [
    ("asleep", "busy"),
    ("asleep", "asleep, starting"),
    ("asking", "setup"),
    ("setup", "ready"),
    ("setup", "starting"),
    ("busy", "starting"),
    ("busy", "asleep, starting"),
    ("phone", "pairing"),
    ("phone paused", "pairing"),
    ("phone", "asking (phone)"),
] + [("identify", m) for m in LOOKS]


@pytest.mark.parametrize("a, b", BY_MOTION, ids=[f"{a}|{b}" for a, b in BY_MOTION])
def test_look_alikes_differ_by_motion(table, a, b):
    _, feats = table
    assert motion_margins(feats[a], feats[b]), (a, b, feats[a], feats[b])


def test_every_moving_signal_swings_at_least_3_to_1_in_light(table):
    night, feats = table
    for m, f in feats.items():
        if f["moving"]:
            assert f["swing"] >= 3.0, (m, night, f["swing"])


def test_calm_signals_are_truly_steady(table):
    _, feats = table
    for m in ("playing", "ready", "asleep", "phone", "phone paused"):
        assert not feats[m]["moving"], (m, feats[m])


def test_the_owner_s_three_signatures(table):
    night, f = table
    # Busy: the slow tempo, twice the standard breath, and deep.
    assert f["busy"]["period"] == pytest.approx(6.0, abs=0.1)
    assert f["starting"]["period"] == pytest.approx(3.0, abs=0.1)
    assert f["busy"]["swing"] >= (8.0 if night else 15.0)
    # Setup: long-short - two pulses a 3 s cycle, up a third of the time.
    decay = eng.KICK_DECAY_S * (eng.NIGHT_SLOW if night else 1.0)
    assert f["setup"]["period"] == pytest.approx(eng.long_short_len(1.0, decay), abs=0.1)
    assert f["setup"]["pulses"] == 2 and f["setup"]["high"] >= 2 * f["asking"]["high"]
    # Identify: three flashes a 1.7 s cycle, at full - brighter than anything else.
    assert f["identify"]["period"] == pytest.approx(1.7, abs=0.05)
    assert f["identify"]["pulses"] == 3
    assert all(f["identify"]["hi"] >= 1.5 * f[m]["hi"] for m in LOOKS)


def distinctness_table(night):
    """The table for docs/lamp.md (python -m tests.test_lamp_signatures)."""
    feats = {m: features(trace(m, night)) for m in MEANINGS}
    rows = []
    for m in MEANINGS:
        f = feats[m]
        rows.append(f"| {m} | {f['mean']:.4f} | {f['swing']:.1f}:1 | "
                    f"{(str(round(f['period'], 2)) + ' s') if f['period'] else '-'} | "
                    f"{f['pulses'] or '-'} | {f['high']:.0%} | {f['family']} g/r {f['green']:.2f} |")
    pairs = [f"{a} / {b}: {', '.join(margins(feats[a], feats[b]))}"
             for a, b in itertools.combinations(MEANINGS, 2)]
    return rows, pairs


# -- the rendered review's fixes -------------------------------------------------

AMBER_LOOKS = {"ready": policy.READY, "playing": policy.PLAYING, "loading": policy.LOADING,
               "boot": policy.BOOT["early"], "boot net": policy.BOOT["network"],
               "sleep glow": Look("amber", policy.SLEEP_LEVEL, night=policy.SLEEP_LEVEL),
               "setup": policy.HOTSPOT, "asking": policy.confirm_look((policy.READY, 0.0))}
EMBER_LOOKS = {"asleep": policy.STANDBY, "busy": policy.SHUTTING_DOWN,
               "asleep, starting": policy.BOOT_STANDBY}
GRID = [(n, b) for n in (False, True) for b in ("low", "normal", "bright")]


def look_trace(look, night, brightness, seconds=7.0):
    lamp, clock = engine(night, brightness)
    lamp.show(look, cut=True)
    return [lamp.render(clock.t + i / FPS)[0] for i in range(int(seconds * FPS))]


@pytest.mark.parametrize("night, brightness", GRID)
def test_amber_is_never_drawn_ember_red(night, brightness):
    for name, look in AMBER_LOOKS.items():
        for r, g, _ in look_trace(look, night, brightness):
            assert g > 0 and g / r >= 0.08, (name, night, brightness, r, g)


@pytest.mark.parametrize("night, brightness", GRID)
def test_ember_never_lights_its_green(night, brightness):
    for name, look in EMBER_LOOKS.items():
        for r, g, b in look_trace(look, night, brightness):
            assert r > 0 and g == 0.0 and b == 0.0, (name, night, brightness, g)


def snaps(duties, floor=0.006):
    """Minor-channel switches bigger than a floor's step and a half, in one frame.

    (The main channel changing - a hue change swapping at its 10 % trough - is
    the hue rule's own business.)"""
    out = 0
    for a, b in zip(duties, duties[1:]):
        main_a = max(range(3), key=lambda i: a[i])
        main_b = max(range(3), key=lambda i: b[i])
        if main_a != main_b:
            continue
        for i in range(3):
            if i == main_a:
                continue
            if (a[i] == 0.0) != (b[i] == 0.0) and max(a[i], b[i]) > floor * eng.GATE_SWITCH:
                out += 1
    return out


@pytest.mark.parametrize("brightness", ["low", "normal", "bright"])
def test_no_minor_channel_snaps_at_look_changes_or_the_night_switch(brightness):
    for night in (False, True):
        lamp, clock = engine(night, brightness)
        script = {0.0: lambda: lamp.show(policy.READY, cut=True),
                  1.0: lambda: lamp.show(policy.LOADING),
                  4.3: lambda: lamp.set_night(not night),           # mid-breath
                  6.0: lambda: lamp.show(policy.PLAYING),
                  8.0: lambda: lamp.show(policy.READY),
                  10.0: lambda: lamp.show(policy.LOADING),
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
        assert snaps(duties) == 0, (night, brightness)


def perceptual(d):
    return tuple(max(0.0, c) ** (1 / eng.GAMMA) for c in d)


@pytest.mark.parametrize("night", [False, True])
def test_bright_keeps_the_headroom_for_replies(night):
    lamp, clock = engine(night, "bright")
    lamp.show(policy.PLAYING, cut=True)
    base = luma(lamp.render(clock.t)[0])
    lamp.reply("kick")
    peak = max(luma(lamp.render(clock.t + i / FPS)[0]) for i in range(20))
    assert peak / base >= 1.6, (night, peak / base)
    lamp.identify(5)
    flash = max(luma(lamp.render(clock.t + 1 + i / FPS)[0]) for i in range(100))
    assert flash / base >= 1.6


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


@pytest.mark.parametrize("look", ["ready", "phone paused"])
def test_a_refuse_still_reads_at_night_on_low(look):
    lamp, clock = engine(True, "low")
    lamp.show(LOOKS[look], cut=True)
    base = luma(lamp.render(clock.t)[0])
    lamp.reply("refuse")
    low = min(luma(lamp.render(clock.t + i / FPS)[0]) for i in range(int(eng.SAG_S * FPS)))
    assert base / low >= 3.0, (look, base / low)


def test_phone_paused_on_low_stays_clearly_lit_and_its_red_trace_well_off():
    for night in (False, True):
        r, g, b = look_trace(policy.CAST_PAUSED, night, "low", 0.1)[0]
        assert b >= 0.015 and r == 0.0
    lamp, _ = engine()
    want = eng.DEFAULT_PALETTE["blue"][0] * policy.CAST_PAUSED.level ** eng.GAMMA
    assert want <= 0.8 * eng.DEFAULT_MIN_DUTY[0] * eng.GATE_OFF      # not on the gate's edge


@pytest.mark.parametrize("night", [False, True])
def test_a_kick_from_asleep_is_seen_but_not_harsh(night):
    lamp, clock = engine(night)
    lamp.show(policy.STANDBY, cut=True)
    base = luma(lamp.render(clock.t)[0])
    lamp.reply("kick")
    peak = max(luma(lamp.render(clock.t + i / FPS)[0]) for i in range(20))
    assert 2.5 <= peak / base <= 4.0, (night, peak / base)


def test_halted_reads_off_not_no_internet():
    assert policy.meaning_for(BoxState(halted=True)) == "off"


if __name__ == "__main__":
    for night in (False, True):
        rows, pairs = distinctness_table(night)
        print("night" if night else "day")
        print("| meaning | mean light | swing | period | pulses | time up | colour |")
        print("\n".join(rows))
        print("\n".join(pairs))
