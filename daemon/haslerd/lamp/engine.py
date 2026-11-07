"""The lamp engine: one thread that renders Looks onto an Output - docs/lamp.md.

The policy says *what* to show (a ``Look``); this decides how each frame
looks. It sleeps on a condition while the lamp is steady (zero CPU, zero
writes) and renders at ~50 Hz only while something moves, so a slow preset
start can never freeze it.

A frame is a ``Frame``: a mix of the palette's colours (amber, ember, blue,
green, red, and any io.lamp.palette adds), a perceptual level, and the
*envelope* - the level the colour is judged at (the trough of a breath, the
base under a kick). Everything is blended in that space, then resolved once
per frame:

    mix + envelope -> calibrated linear ratio (day/night palette over
    log(level), amber -> ember below the amber band)
    level x level gain -> gamma 2.2 -> balance -> minimum duty -> Output.write

Judging the colour at the envelope, not the instantaneous level, is what
keeps a breath's trough or a kick from flicking hue: a minor channel (amber's
green, blue's red) is lit or dark for the whole motion, decided with
hysteresis, and a lit one is held at its minimum duty rather than vanishing.

The two transition rules live here too: brightness rises in 0.6 s
(ease-out) and falls in 1.2 s with a long tail; amber, ember, green and red
cross-fade, but a change to or from moon blue never fades through RGB - it
falls to 10 %, swaps hue at the bottom and rises again.

Patterns (Morse, the red alert, the green bloom, the HAL eye) are steps
evaluated on absolute time with crisp edges; the designed ones come from
the signal table (lamp/signals.py), drawn as Looks or as overlays
(``play``, the replies, ``identify``).
"""

from __future__ import annotations

import dataclasses
import logging
import math
import random
import threading
import time
from typing import Any, Callable, NamedTuple

from .contract import (
    ALIASES, BECKON_REST_S, BREATH_FALL_S, BREATH_REST_S, BREATH_RISE_S, CASTING_LEVEL,
    CASTING_NIGHT, EMBER_LEVEL, EMBER_NIGHT, IDENTIFY_S, KEY_REPLIES, NIGHT_CAP, PLAYING_LEVEL,
    PLAYING_NIGHT, READY_LEVEL, READY_NIGHT, REPLY_ALIASES, SETUP_REST_S, SLOW_AT_NIGHT, Look,
    Output, canonical, parse_reply, steady,
)
from . import signals
from .outputs import CHANNELS, NullOutput, open_output

log = logging.getLogger(__name__)

RGB = tuple[float, float, float]

GAMMA = 2.2
FRAME_S = 0.02                  # 50 Hz while anything moves
LIT = 1e-4                      # perceptual level below which the lamp is dark

# The release's five colours, in Frame.w order; io.lamp.palette may add more,
# which follow in the order given.
HUES: tuple[str, ...] = ("amber", "ember", "blue", "green", "red", "gold")
ONEHOT: dict[str, tuple[float, ...]] = {
    name: tuple(1.0 if h == name else 0.0 for h in HUES) for name in HUES}
# Fade families: a change between them never fades through RGB (the hue rule:
# down to 10 %, swap, up). Amber, ember, green and red cross-fade directly;
# moon blue is the one that dips. A new colour cross-fades too unless its
# palette entry says otherwise ({"family": "blue"} or a name of its own).
FAMILIES: dict[str, str] = {"amber": "warm", "ember": "warm", "blue": "blue",
                            "green": "warm", "red": "warm", "gold": "warm"}
# The amber hues: amber, and gold - playing's slightly yellower tint of it
# (one hue family). Both keep their green near its floor and blend into ember
# below the amber band.
AMBERS: tuple[str, ...] = ("amber", "gold")

# -- transitions (docs/lamp.md "Transitions") ----------------------------------

RISE_S = 0.6                    # brightness up: ease-out
FALL_S = 1.2                    # brightness down: long tail - never symmetric
TAIL_K = 4.0                    # the tail's steepness (exponential, normalised)
HUE_TROUGH = 0.10               # a hue change dips to this ...
HUE_FALL_S, HUE_RISE_S = 0.6, 0.9   # ... in 0.6 s, swaps at the bottom, rises in 0.9 s
NIGHT_SLOW = 1.5                # falls, decays, swells and the breath's tempo at night
NIGHT_SWITCH_S = 1.5            # dusk/dawn glide
BLEND_S = 0.07                  # a reply interrupting a reply: from the frame on show
RESTORE_S = 0.3                 # an overlay that ends off its Look blends back this fast
COUNTDOWN_RESTORE_S = 0.4       # knob back on: snap back to the Look

# -- the behaviours (docs/lamp.md "Behaviours") ---------------------------------

KICK_GAIN, KICK_MIN = 1.4, 0.5
# At night the kick's absolute floor is lower: a 50 % flash is glaring in a
# dark bedroom - but still ~3:1 in light over night ready, or nobody sees it.
KICK_MIN_NIGHT = 0.36  # to +40 % of the level, at least 50 % absolute ...
# From the dim ember (asleep) a kick to 50 % is a 6-14x jump in light: harsh.
KICK_MIN_EMBER, KICK_MIN_EMBER_NIGHT = 0.35, 0.28
KICK_ATTACK_S = 0.03            # ... in 30 ms ...
KICK_DECAY_S = 0.35             # ... then the filament cools, exponentially
KICK_K = 4.0
KICK_LEAD_S = FRAME_S / 2       # the first frame after a key is already on its way up
SAG_TO = 0.25                   # refuse: sag to 25 % of the level ...
SAG_DOWN_S, SAG_HOLD_S, SAG_UP_S = 0.15, 0.20, 0.60   # ... hold, recover
SAG_S = SAG_DOWN_S + SAG_HOLD_S + SAG_UP_S
SWELL_GAIN, SWELL_S = 0.30, 1.2  # +30 % and back, raised cosine
BECKON_GAP_S = 0.25             # two kicks this far apart, then a rest
# A beckon asks for attention: its kicks reach at least this (day / night),
# 1.6x the level it beats on - 6:1 in light over ready, by day and by night.
BECKON_GAIN = 1.6
BECKON_TOP, BECKON_TOP_NIGHT = 0.80, 0.55
# Setup's long-short: a long glow (rise, hold, fall), a gap, then a kick.
LONG_RISE_S, LONG_HOLD_S, LONG_FALL_S, LONG_GAP_S = 0.35, 0.45, 0.45, 0.35
# A pattern's edges (Morse, the red alert): a crisp 22 ms attack and a ~25 ms
# exponential release (the tail's time constant is a quarter of EDGE_DOWN).
EDGE_UP, EDGE_DOWN = 0.022, 0.10
# An overlay in another hue family (a pattern reply, identify in a named
# colour) dips to the hue trough over this before it starts and as it ends.
OVERLAY_DIP_S = 0.3
IDENTIFY_BACK_S = 0.4           # identify ends by easing back to the Look
BREATH_PERIOD = BREATH_RISE_S + BREATH_FALL_S + BREATH_REST_S
# A breath never swings less than this (peak / trough, perceptual): ~3:1 in
# light, the least that reads as moving at low levels.
BREATH_MIN_SWING = 1.8
# Brightness never pushes a Look above this: the kick, swell and beckon need
# the headroom above (a Look defined higher keeps its own level).
LOOK_CAP = PLAYING_LEVEL

# Brightness the owner picked (Settings -> Pilot lamp), a factor on every level.
BRIGHTNESS: dict[str, float] = {"low": 0.6, "normal": 1.0, "bright": 1.25}
# "low" never takes a lit Look below this (ember: its night level): a refuse
# sag from here still reaches ~3:1 in light before the minimum duty stops it,
# and the dim blue die stays clearly lit.
VISIBLE_MIN = 0.17
# Amber is never drawn below the amber band's top times this, or it turns
# ember-red (its green under the floor) and reads as asleep.
AMBER_MARGIN = 1.1

# Shown when the keeper stops ticking (engine.watchdog): busy, don't unplug
# (the signal table's "busy"; this is its default).
BUSY = signals.DEFAULTS["busy"]
# With a high green floor (a frosted LED on 3.3 V can need 4-5 % duty) amber
# can't be dim and amber at once: below the band it honestly blends into
# ember-red rather than showing a green held far above its share. Amber is
# held above the band only up to this (day, night), so ready stays dim.
AMBER_MIN_CAP: tuple[float, float] = (0.50, 0.30)
# The standby ember the Lamp check's "standard" calibration starts from.
STANDBY_EMBER, NIGHT_EMBER = EMBER_LEVEL, EMBER_NIGHT

# The levels the Lamp colours page previews at (perceptual): full, the night
# cap, standby ember.
PREVIEW_S = 30.0                # a calibration preview lasts this long after its last change
PREVIEW_LEVELS: dict[str, float] = {"full": 1.0, "night": NIGHT_CAP, "ember": EMBER_LEVEL}
# The lamp page's "show me": a meaning's Look for a while (preview_look), and
# its reply words (preview_reply).
PREVIEW_LOOK_S = 8.0
PREVIEW_LOOKS: dict[str, Look] = {
    "playing": signals.DEFAULTS["playing"],
    "ready": steady("amber", READY_LEVEL, READY_NIGHT),
    "asleep": steady("ember", EMBER_LEVEL, EMBER_NIGHT),
    "phone": steady("blue", CASTING_LEVEL, CASTING_NIGHT),
    "busy": BUSY,
    "starting": signals.DEFAULTS["starting"],
    "pairing": signals.DEFAULTS["pairing"],
    "asking": signals.DEFAULTS["asking"],
    "setup": signals.DEFAULTS["setup"],
    "no_internet": signals.DEFAULTS["no_internet"],
    "fault": signals.DEFAULTS["fault"],
}
_SIGNAL_LOOKS = ("busy", "starting", "pairing", "asking", "setup", "no_internet", "fault")


def preview_looks() -> dict[str, Look]:
    """The looks "Show me" draws: PREVIEW_LOOKS with the signal table's remaps."""
    looks = dict(PREVIEW_LOOKS)
    for name in _SIGNAL_LOOKS:
        looks[name] = signals.get(name)
    return looks

PREVIEW_REPLIES: dict[str, str] = {"heard": "kick", "no": "refuse", "ok": "ok"}
# The Easter eggs play_egg knows.
EGGS: tuple[str, ...] = ("close_encounters", "tos_working")
# Replies drawn as the signal table's overlays (reply word -> signal name).
OVERLAY_REPLIES: dict[str, str] = {"refuse": "no", "refuse3": "no", "bloom": "bloom", "ok": "ok",
                                   "update_ok": "update_ok"}

# -- calibration (io.lamp; docs/lamp.md "Calibration") --------------------------

# Linear RGB ratios before calibration (the language's spec).
DEFAULT_PALETTE: dict[str, RGB] = {
    "amber": (1.0, 0.17, 0.0),      # filament: the radio
    "ember": (1.0, 0.06, 0.0),      # pure red when low: asleep (never moves)
    "blue":  (0.10, 0.0, 1.0),      # moon blue: a phone is in charge
    "green": (0.0, 1.0, 0.0),       # magic-eye green: OK (never steady, never dim)
    "red":   (1.0, 0.0, 0.0),       # signal red: a fault (never steady)
    "gold":  (1.0, 0.22, 0.0),      # playing: an incandescent filament, a little yellower than amber
}
NIGHT_ANCHOR = 0.35             # a palette's "night" colour is exact here; "day" at 1.0
# Below this band of envelope levels amber blends into ember, so its green
# fades out with the colour instead of flicking off at its floor. Night ready
# (20 %) sits on the band's top: still amber. io.lamp.amber_band overrides.
AMBER_BAND: tuple[float, float] = (0.12, 0.20)
# Smallest linear duty a lit channel gets: pwm-gpio can't make a pulse
# shorter than ~6-10 us at 800 Hz (0.006 = 7.5 us).
DEFAULT_MIN_DUTY: RGB = (0.006, 0.006, 0.006)
# A minor channel is lit while the envelope wants at least GATE_OFF of its
# floor and relit at GATE_ON (hysteresis): lit, it is held at the floor, so
# the colour is never more than ~2x off its ratio; dark, it is gone.
GATE_OFF, GATE_ON = 0.5, 0.7
# ... but only while the channel itself is within this of its floor, so a
# switch is never more than a floor's step. A channel that must come on while
# already well above it (a Look change jumped the envelope) fades in instead.
GATE_SWITCH = 1.5
GATE_FADE_S = 0.4
# The green die is far brighter to the eye: 0.55 matches it to amber (0.45-0.69
# by eye; io.lamp.level_gain.green tunes it).
DEFAULT_GAIN: dict[str, float] = {"amber": 1.0, "ember": 1.0, "blue": 1.0, "green": 0.55, "red": 1.0,
                                  "gold": 1.0}
# Amber's green is the hardest minor channel: with a high floor it is held
# (at its floor) down to a quarter of what it wants, so night amber stays
# amber (a little yellow) instead of turning ember-red.
AMBER_GATE_OFF, AMBER_GATE_ON = 0.22, 0.25


class Frame(NamedTuple):
    """One frame, before calibration: colour mix, level, envelope (all perceptual)."""

    w: tuple[float, ...]   # weights of the engine's hues (amber, ember, blue, extras; sum 1)
    level: float
    env: float      # the level the colour is judged at


DARK_FRAME = Frame(ONEHOT["amber"], 0.0, 0.0)


# -- curves --------------------------------------------------------------------


def ease(x: float) -> float:
    """Raised-cosine ease in/out, 0..1 -> 0..1."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return 0.5 - 0.5 * math.cos(math.pi * x)


def ease_out(x: float) -> float:
    """Cubic ease-out: steepest at the start, so a rise is under way at once."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return 1.0 - (1.0 - x) ** 3


def tail(x: float) -> float:
    """Normalised exponential: a fall that starts at once and lands with a long tail."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return (1.0 - math.exp(-TAIL_K * x)) / (1.0 - math.exp(-TAIL_K))


def breath_shape(t: float, period: float) -> float:
    """0 (trough) .. 1 (peak) of the asymmetric breath, starting at the trough.

    Rise 1.3 s ease-out, fall 1.7 s ease-in-out, rest 0.4 s at the trough
    (for the standard 3.4 s; other periods scale all three).
    """
    if period <= 0.0:
        return 1.0
    k = period / BREATH_PERIOD
    rise, fall = BREATH_RISE_S * k, BREATH_FALL_S * k
    u = t % period
    if u < rise:
        return ease_out(u / rise)
    if u < rise + fall:
        return 1.0 - ease((u - rise) / fall)
    return 0.0


def kick_shape(t: float, decay_s: float = KICK_DECAY_S) -> float:
    """0..1..0: a 30 ms linear attack, then the filament's exponential cool-down."""
    if t < 0.0:
        return 0.0
    if t < KICK_ATTACK_S:
        return t / KICK_ATTACK_S
    x = (t - KICK_ATTACK_S) / decay_s
    if x >= 1.0:
        return 0.0
    return (math.exp(-KICK_K * x) - math.exp(-KICK_K)) / (1.0 - math.exp(-KICK_K))


def kick_len(decay_s: float = KICK_DECAY_S) -> float:
    return KICK_ATTACK_S + decay_s


def kick_peak(level: float, night: bool = False, ember: bool = False) -> float:
    if ember:
        least = KICK_MIN_EMBER_NIGHT if night else KICK_MIN_EMBER
    else:
        least = KICK_MIN_NIGHT if night else KICK_MIN
    return min(1.0, max(level * KICK_GAIN, least))


def sag_shape(t: float, times: int = 1) -> float:
    """Level factor of ``times`` refuse sags: 1 -> 25 % -> 1."""
    if t < 0.0 or t >= times * SAG_S:
        return 1.0
    u = t % SAG_S
    if u < SAG_DOWN_S:
        return 1.0 - (1.0 - SAG_TO) * ease_out(u / SAG_DOWN_S)
    if u < SAG_DOWN_S + SAG_HOLD_S:
        return SAG_TO
    return SAG_TO + (1.0 - SAG_TO) * ease((u - SAG_DOWN_S - SAG_HOLD_S) / SAG_UP_S)


def pair_shape(t: float, rest_s: float, decay_s: float = KICK_DECAY_S) -> float:
    """A beckon: two kicks BECKON_GAP_S apart, then ``rest_s``, repeating."""
    cycle = BECKON_GAP_S + kick_len(decay_s) + max(0.0, rest_s)
    u = t % cycle
    return max(kick_shape(u, decay_s), kick_shape(u - BECKON_GAP_S, decay_s))


def long_short_len(rest_s: float, decay_s: float = KICK_DECAY_S) -> float:
    return LONG_RISE_S + LONG_HOLD_S + LONG_FALL_S + LONG_GAP_S + kick_len(decay_s) + max(0.0, rest_s)


def long_short_shape(t: float, rest_s: float, decay_s: float = KICK_DECAY_S) -> float:
    """Setup's beat, 0..1: a long glow (rise 0.35 s, hold 0.45 s, fall 0.45 s), a
    0.35 s gap, a kick, then ``rest_s`` - "daah-dit", repeating."""
    u = t % long_short_len(rest_s, decay_s)
    if u < LONG_RISE_S:
        return ease_out(u / LONG_RISE_S)
    u -= LONG_RISE_S
    if u < LONG_HOLD_S:
        return 1.0
    u -= LONG_HOLD_S
    if u < LONG_FALL_S:
        return 1.0 - ease(u / LONG_FALL_S)
    return kick_shape(u - LONG_FALL_S - LONG_GAP_S, decay_s)


# Playing's filament: a tiny, irregular shimmer like an old bulb or a valve
# heater - band-passed random noise (roughly 0.1-1 Hz) from a seeded table
# that repeats only every ~8.5 minutes, softly limited, so nobody can pick out
# a rhythm. +-4 % of the level by day, +-3 % at night.
FILAMENT_DEPTH, FILAMENT_DEPTH_NIGHT = 0.04, 0.03
FILAMENT_RATE = 8.0             # table samples a second
FILAMENT_N = 4096               # ~512 s before it repeats
FILAMENT_FAST_S, FILAMENT_SLOW_S = 0.5, 2.5   # the band: smooth fast wobble, no slow drift


def filament_table(rng: random.Random) -> tuple[float, ...]:
    """A loop of smooth noise, -1..1: white noise, low-passed (FILAMENT_FAST_S)
    minus its slower drift (FILAMENT_SLOW_S), scaled to its spread and soft-limited."""
    white = [rng.gauss(0.0, 1.0) for _ in range(FILAMENT_N)]
    a = 1.0 - math.exp(-1.0 / (FILAMENT_RATE * FILAMENT_FAST_S))
    b = 1.0 - math.exp(-1.0 / (FILAMENT_RATE * FILAMENT_SLOW_S))
    fast = slow = 0.0
    out = []
    for _ in range(3):                  # run round the loop to settle the filters
        out = []
        for x in white:
            fast += a * (x - fast)
            slow += b * (fast - slow)
            out.append(fast - slow)
    spread = math.sqrt(sum(v * v for v in out) / len(out)) or 1.0
    return tuple(math.tanh(v / (1.5 * spread)) for v in out)


def filament_noise(t: float, table: tuple[float, ...]) -> float:
    """The filament's shimmer at ``t`` (-1..1), eased between table samples."""
    x = (t * FILAMENT_RATE) % len(table)
    i = int(x)
    a, b = table[i], table[(i + 1) % len(table)]
    return _lerp(a, b, ease(x - i))


def pattern_len(steps) -> float:
    return sum(float(s[1]) for s in steps)


def identify_len() -> float:
    """One repeat of the identify code (HI: ~3 s by day)."""
    return pattern_len(signals.get("identify").steps)


def _lerp(a: float, b: float, w: float) -> float:
    return a + (b - a) * w


def _lerp3(a: RGB, b: RGB, w: float) -> RGB:
    return (a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w, a[2] + (b[2] - a[2]) * w)


def _pad(w: tuple[float, ...], n: int) -> tuple[float, ...]:
    return w + (0.0,) * (n - len(w)) if len(w) < n else w


def _lerpn(a: tuple[float, ...], b: tuple[float, ...], w: float) -> tuple[float, ...]:
    if len(a) == len(b) == 3:
        return _lerp3(a, b, w)  # type: ignore[arg-type]
    n = max(len(a), len(b))
    a, b = _pad(a, n), _pad(b, n)
    return tuple(x + (y - x) * w for x, y in zip(a, b))


def perceptual3(duty: RGB) -> RGB:
    return (max(0.0, duty[0]) ** (1 / GAMMA), max(0.0, duty[1]) ** (1 / GAMMA),
            max(0.0, duty[2]) ** (1 / GAMMA))


def linear3(p: RGB) -> RGB:
    return (p[0] ** GAMMA, p[1] ** GAMMA, p[2] ** GAMMA)


def _lerp_frame(a: Frame, b: Frame, w: float) -> Frame:
    """Blend two frames; a dark end takes the other end's colour (a fade, not a hue shift)."""
    wa, wb = a.w, b.w
    if a.level <= LIT:
        wa = wb
    elif b.level <= LIT:
        wb = wa
    return Frame(_lerpn(wa, wb, w), _lerp(a.level, b.level, w), _lerp(a.env, b.env, w))


def family(w: tuple[float, ...], hues: tuple[str, ...] = HUES,
           families: dict[str, str] | None = None) -> str:
    """The hue family of the colour that dominates ``w`` ("warm" for amber and ember,
    which may fade into each other; "blue"; a new colour's own)."""
    fam = families or FAMILIES
    i = max(range(len(w)), key=lambda k: w[k])
    name = hues[i] if i < len(hues) else "amber"
    return fam.get(name, name)


# -- calibration parsing -------------------------------------------------------


def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, v)) if v == v else 0.0     # NaN -> 0


def _rgb(value: Any, default: RGB) -> RGB:
    """A descriptor colour: [r, g, b] or {red, green, blue}; bad input -> default."""
    try:
        if isinstance(value, dict):
            return tuple(float(value.get(c, d)) for c, d in zip(CHANNELS, default))  # type: ignore[return-value]
        r, g, b = (float(v) for v in value)
        return (r, g, b)
    except (TypeError, ValueError):
        log.warning("lamp: ignoring bad colour %r", value)
        return default


def _ratio_rgb(value: Any, default: RGB) -> RGB:
    r = _rgb(value, default)
    return (_clamp01(r[0]), _clamp01(r[1]), _clamp01(r[2]))


def parse_palette_entry(value: Any, day: RGB, night: RGB) -> tuple[RGB, RGB]:
    """A palette entry: [r, g, b] (day and night alike), {red, green, blue}, {day, night},
    or {rgb} - the dict forms may also carry a "family" (read by the engine)."""
    if isinstance(value, dict) and "rgb" in value and "day" not in value:
        value = {"day": value["rgb"], **({"night": value["night"]} if "night" in value else {})}
    if isinstance(value, dict) and ("day" in value or "night" in value):
        d = _ratio_rgb(value["day"], day) if "day" in value else day
        n = _ratio_rgb(value["night"], d) if "night" in value else d
        return d, n
    d = _ratio_rgb(value, day)
    return d, d


def _float(value: Any, default: float, lo: float = 0.0, hi: float = float("inf")) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return default if v != v else min(hi, max(lo, v))


class LampEngine:
    """contract.LampDriver on its own daemon thread named "lamp".

    Nothing is written before the first ``show()`` - the boot glow stays
    until then (``seed()`` tells the engine what it is, so the first Look
    glides from it).
    """

    def __init__(
        self,
        output: Output,
        palette: dict[str, Any] | None = None,
        balance: Any = None,
        min_duty: Any = None,
        clock: Callable[[], float] = time.monotonic,
        threaded: bool = True,
        level_gain: Any = None,
        levels: Any = None,
        amber_band: Any = None,
        seed: int | None = None,
    ) -> None:
        self.output = output
        self._clock = clock
        self._cond = threading.Condition(threading.RLock())
        self._configure({"palette": palette, "balance": balance, "min_duty": min_duty,
                         "level_gain": level_gain, "levels": levels, "amber_band": amber_band})
        # The release's palette (with the descriptor's overrides): what the
        # page's saved colours lay on top of, and what "Reset" goes back to.
        self.base_palette: dict[str, RGB] = dict(self.palette)
        self.base_palette_night: dict[str, RGB] = dict(self.palette_night)
        now = clock()
        self._look: Look = Look()
        self._epoch = now               # breath / beckon phase origin
        self._trans: dict | None = None
        self._shown = False
        self._seed: Frame | None = None
        self._reply: str | None = None
        self._reply_t0 = now
        self._reply_from: Frame = DARK_FRAME   # the base when the reply started
        self._reply_slow = 1.0
        self._identify: dict | None = None       # the identify overlay while it runs
        self._pattern: dict = {"steps": [], "total": 0.0, "t0": now}   # the pattern reply
        self.geek = False
        self.callsign = ""
        self._rng = random.Random(seed)
        # The filament's own shimmer: each radio flickers its own way (a seed
        # makes it repeatable, for tests).
        self._phases = filament_table(random.Random(self._rng.random()))
        self._preview_look: Look | None = None
        self._preview_look_until = now
        self._countdown: float | None = None
        self._preview: RGB | None = None       # duty, while calibrating
        self._preview_until = 0.0
        self._blend_from: RGB | None = None    # duty
        self._blend_t0 = now
        self._blend_s = BLEND_S
        self._night = False
        self._scale = 1.0
        self.key_replies = True
        self._watchdog_s: float | None = None
        self._fed = now
        self._tripped = False
        self._gate = [True, True, True]
        self._gate_fade: list[float | None] = [None, None, None]   # fade-in start per channel
        self._now = now
        self._last_frame: Frame = DARK_FRAME
        self._last: RGB = (0.0, 0.0, 0.0)      # the last rendered duty
        self._written: RGB | None = None
        self._dirty = False
        self._closed = False
        self._thread: threading.Thread | None = None
        if threaded:
            self._thread = threading.Thread(target=self._run, name="lamp", daemon=True)
            self._thread.start()

    # -- calibration -------------------------------------------------------

    def _configure(self, spec: dict) -> None:
        palette = dict(DEFAULT_PALETTE)
        night = dict(DEFAULT_PALETTE)
        families = dict(FAMILIES)
        raw_palette = spec.get("palette") if isinstance(spec.get("palette"), dict) else {}
        for name, value in raw_palette.items():
            name = str(name)
            if name in ALIASES or name in ("off", "show"):
                continue    # an old colour's tuning: its colour is gone
            if name not in palette:
                # A new colour (a green "done", a warning red...): any name the
                # release doesn't use; Looks and signals may then draw it.
                first, _ = parse_palette_entry(value, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0))
                if max(first) <= 0.0:
                    log.warning("lamp: ignoring palette colour %r: black", name)
                    continue
                palette[name] = night[name] = first
                families[name] = name
            palette[name], night[name] = parse_palette_entry(value, palette[name], night[name])
            if isinstance(value, dict) and isinstance(value.get("family"), str):
                families[name] = value["family"]
        self.palette: dict[str, RGB] = palette
        self.palette_night: dict[str, RGB] = night
        self.hues: tuple[str, ...] = tuple(palette)
        self.families: dict[str, str] = families
        self._amber_idx = tuple(i for i, h in enumerate(self.hues) if h in AMBERS)
        balance = spec.get("balance")
        self.balance = _rgb(balance, (1.0, 1.0, 1.0)) if balance is not None else (1.0, 1.0, 1.0)
        floor = spec.get("min_duty")
        self.min_duty = _rgb(floor, DEFAULT_MIN_DUTY) if floor is not None else DEFAULT_MIN_DUTY
        gain = {n: DEFAULT_GAIN.get(n, 1.0) for n in self.hues}
        raw = spec.get("level_gain")
        if isinstance(raw, dict):
            for name, value in raw.items():
                if name in gain:        # old colour names: tuned for a colour that is gone
                    gain[name] = _float(value, 1.0, 0.0, 4.0)
        self.level_gain: dict[str, float] = gain
        levels = spec.get("levels") if isinstance(spec.get("levels"), dict) else {}
        self.ember_day = _float(levels.get("ember_day"), EMBER_LEVEL, 0.01, 1.0)
        self.ember_night = _float(levels.get("ember_night"), EMBER_NIGHT, 0.01, 1.0)
        band = spec.get("amber_band")
        try:
            lo, hi = (float(v) for v in band)  # type: ignore[union-attr]
            self.amber_band = (min(lo, hi), max(lo, hi))
        except (TypeError, ValueError):
            self.amber_band = AMBER_BAND

    def calibrate(self, spec: dict | None) -> None:
        """Apply an ``io.lamp`` block's calibration live (the calibration page).

        Reads ``palette``, ``balance``, ``min_duty``, ``level_gain``, ``levels``
        and ``amber_band``; anything missing is the default again. The page's
        saved colours (``set_palette``) are dropped - the new palette is the
        release's now.
        """
        with self._cond:
            self._configure(dict(spec or {}))
            self.base_palette = dict(self.palette)
            self.base_palette_night = dict(self.palette_night)
            self._wake()

    def set_calibration(self, cal: dict | None) -> None:
        """The Lamp check's name for ``calibrate`` (web/lamp_check.py)."""
        self.calibrate(cal)

    def calibration_defaults(self) -> dict:
        """The uncalibrated values in the Lamp check's contract shape."""
        pal = {n: list(DEFAULT_PALETTE[n]) for n in HUES}
        return {
            "min_duty": dict(zip(CHANNELS, DEFAULT_MIN_DUTY)),
            "balance": {c: 1.0 for c in CHANNELS},
            "palette": {"amber": {"day": pal["amber"], "night": list(pal["amber"])},
                        "ember": pal["ember"], "blue": pal["blue"],
                        "green": pal["green"], "red": pal["red"]},
            "level_gain": {n: DEFAULT_GAIN.get(n, 1.0) for n in HUES},
            "levels": {"ember_day": EMBER_LEVEL, "ember_night": EMBER_NIGHT},
        }

    def calibration(self) -> dict:
        """The calibration in force, in io.lamp's shape (for the calibration page)."""
        with self._cond:
            return {
                "palette": {n: {"day": list(self.palette[n]), "night": list(self.palette_night[n])}
                            for n in self.hues},
                "balance": dict(zip(CHANNELS, self.balance)),
                "min_duty": dict(zip(CHANNELS, self.min_duty)),
                "level_gain": dict(self.level_gain),
                "levels": {"ember_day": self.ember_day, "ember_night": self.ember_night},
                "amber_band": list(self.amber_band),
            }

    def set_palette(self, overrides: dict[str, Any] | None) -> None:
        """The page's saved colours over the release's palette, live.

        ``overrides`` maps colour names to [r, g, b] linear ratios 0..1 (or
        {day, night}); names the palette doesn't know - old colours too - are
        ignored, ``{}``/None is the release palette again. A colour on show
        changes at once (no fade).
        """
        palette, night = dict(self.base_palette), dict(self.base_palette_night)
        for name, value in (overrides or {}).items():
            if name in palette:
                palette[name], night[name] = parse_palette_entry(value, palette[name], night[name])
        with self._cond:
            if palette != self.palette or night != self.palette_night:
                self.palette, self.palette_night = palette, night
                self._wake()

    # -- LampDriver ------------------------------------------------------

    def show(self, look: Look, fade_s: float = 0.0, cut: bool = False) -> None:
        """Settle into ``look``.

        ``fade_s`` 0 is the standard transition (rise 0.6 s, fall 1.2 s, or
        a hue change through the 10 % trough); more is a slow glide of that
        length (the sleep timer's 20 s); ``cut`` jumps. The first show after
        start glides from the seed, or cuts if there is none.
        """
        with self._cond:
            now = self._clock()
            self._fed = now
            tripped = self._tripped
            if self._shown and look == self._look and not tripped:
                return
            start = self._base(now)     # the busy breath, if the watchdog had tripped
            self._tripped = False
            if not self._shown:
                self._shown = True
                start = self._seed if self._seed is not None else start
                cut = cut or self._seed is None
            self._set_look(look, now, start, fade_s, cut, keep_epoch=not tripped)
            self._wake()

    def reply(self, name: str) -> None:
        """A Reply (old words map on: ack/next/ha/signature kick, fail refuses, nudge sags
        once, confirmed blooms). Refuse is the HAL eye, bloom/ok/update_ok green overlays."""
        try:
            word, _, _ = parse_reply(name)
        except ValueError:
            word = ""
        kind = REPLY_ALIASES.get(word, word)
        if kind not in REPLY_S and kind not in OVERLAY_REPLIES:
            log.warning("lamp: unknown reply %r", name)
            return
        with self._cond:
            if self._countdown is not None:
                return      # the countdown outranks replies
            if kind in KEY_REPLIES and not self.key_replies:
                return
            if kind in OVERLAY_REPLIES:
                self.play(self._overlay_signal(kind))
                return
            self._start_reply(kind, self._clock())

    def _overlay_signal(self, kind: str) -> str:
        """The signal table's overlay for a reply word (geek mode may pick another)."""
        name = OVERLAY_REPLIES[kind]
        if name == "update_ok" and self.geek:
            return "update_ok_geek"
        return name

    def _start_reply(self, kind: str, now: float) -> None:
        with self._cond:
            if self._reply is not None and self._shown:
                self._start_blend(now, BLEND_S)     # from the frame on show, not a jump
            self._reply = kind
            self._reply_t0 = now
            self._reply_from = self._base(now)
            self._reply_slow = NIGHT_SLOW if self._night and kind in SLOW_AT_NIGHT else 1.0
            self._wake()

    def play(self, look: Look | str, times: int = 1) -> bool:
        """Play a pattern once (or ``times``) over the Look, then the Look again.

        ``look`` is a pattern Look (``contract.pattern``/``code``) or the name of
        one in the signal table (``bloom``, ``ok``, ``no``, ``73``...). Its
        colour is any palette colour or "show"; a change to or from moon blue
        dips to 10 % (the hue rule). Its level follows night and brightness.
        A reply like the others: a newer reply replaces it, the countdown
        outranks it. False if it isn't a pattern.
        """
        if isinstance(look, str):
            look = signals.TABLE.get(look) or Look()
        if look.motion != "pattern" or not look.steps:
            log.warning("lamp: play wants a pattern, not %r", look)
            return False
        with self._cond:
            if self._countdown is not None:
                return False
            now = self._clock()
            if self._reply is not None and self._shown:
                self._start_blend(now, BLEND_S)
            self._reply = "pattern"
            self._reply_t0 = now
            self._reply_from = self._base(now)
            self._reply_slow = 1.0
            self._pattern = self._overlay_from(look, self._reply_from, max(1, int(times)), False)
            self._wake()
            return True

    def play_egg(self, name: str | None = None) -> bool:
        """An Easter egg (geek mode or not - the caller decides): "close_encounters"
        or "tos_working" (4-6 random amber twinkles); None picks one (EGGS)."""
        if name is None:
            name = self._rng.choice(EGGS)
        if name == "tos_working":
            return self.play(signals.tos_working(self._rng))
        if name == "close_encounters":
            return self.play("close_encounters")
        log.warning("lamp: no egg %r", name)
        return False

    def farewell(self) -> bool:
        """Just before the shutdown fade: '73' in amber Morse if geek mode is on."""
        return self.play("73") if self.geek else False

    def set_geek(self, on: bool) -> None:
        """Geek mode (the owner's setting, off by default): identify sends the
        callsign, '73' before shutting down, Close Encounters after an update."""
        with self._cond:
            self.geek = bool(on)

    def set_callsign(self, callsign: str | None) -> None:
        """The room's callsign for geek-mode identify: letters/digits, up to 3."""
        clean = "".join(c for c in str(callsign or "").lower() if c.isalnum())[:3]
        with self._cond:
            self.callsign = clean

    def countdown(self, progress: float | None) -> None:
        with self._cond:
            if progress is None:
                if self._countdown is not None:
                    self._countdown = None
                    self._start_blend(self._clock(), COUNTDOWN_RESTORE_S)
                    self._wake()
                return
            progress = min(1.0, max(0.0, float(progress)))
            if progress != self._countdown:
                if self._countdown is None and (self._reply is not None or self._identify is not None
                                                or self._preview is not None or self._preview_look is not None):
                    self._start_blend(self._clock(), BLEND_S)   # never cut a kick in one frame
                self._countdown = progress
                self._reply = None
                self._identify = None
                self._preview = None    # a shutdown ends any calibration ...
                self._preview_look = None   # ... and any "show me"
                self._wake()

    def set_night(self, night: bool) -> None:
        with self._cond:
            if night == self._night:
                return
            now = self._clock()
            start = self._base(now)
            look = self._target_look()
            if look.motion == "breath" and look.period_s > 0:
                # Keep the breath's phase across the tempo change (2.8 s -> 4.2 s).
                old = self._period(look)
                phase = ((now - self._epoch) % old) / old
                self._night = night
                self._epoch = now - phase * self._period(look)
            else:
                self._night = night
            self._glide(start, now, NIGHT_SWITCH_S, exact=True)
            self._wake()

    def close(self, halt_dark: bool = True) -> None:
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        try:
            if halt_dark:
                self.output.write(0.0, 0.0, 0.0)
            self.output.close()
        except Exception:
            log.debug("lamp close failed", exc_info=True)

    # -- the owner's controls, the keeper's hooks --------------------------

    def set_brightness(self, scale: str | float) -> None:
        """"low" | "normal" | "bright" (0.6 / 1.0 / 1.25) or a factor: every level, capped at 1.

        Never takes ember below its night level, nor anything lit below
        10 %: dark must only mean off.
        """
        if isinstance(scale, str):
            value = BRIGHTNESS.get(scale)
            if value is None:
                log.warning("lamp: unknown brightness %r", scale)
                return
        else:
            value = _float(scale, 1.0, 0.1, 2.0)
        with self._cond:
            if value == self._scale:
                return
            now = self._clock()
            start = self._base(now)
            self._scale = value
            self._glide(start, now, 0.0)
            self._wake()

    @property
    def brightness(self) -> float:
        return self._scale

    def set_key_replies(self, on: bool) -> None:
        """Key replies off: no kick when a key is heard (refusals and beckons stay)."""
        with self._cond:
            self.key_replies = bool(on)

    def identify(self, seconds: float | None = IDENTIFY_S) -> None:
        """"Which radio?": HI in green Morse (the room's callsign in geek mode), three
        times over ~9 s (as many ~3 s repeats as fit ``seconds``), bright even at night,
        then back to the Look; 0/None ends it."""
        with self._cond:
            now = self._clock()
            if not seconds or seconds <= 0:
                if self._identify is not None:
                    self._identify = None
                    self._start_blend(now, RESTORE_S)
                    self._wake()
                return
            if self._countdown is not None:
                return
            look = self._identify_look()
            reps = max(1, int(round(float(seconds) / max(pattern_len(look.steps), 0.1))))
            ov = self._overlay_from(look, self._base(now), reps, True)
            ov["steps"].append((0.0, IDENTIFY_BACK_S, "base", IDENTIFY_BACK_S))   # blend back
            ov["total"] = pattern_len(ov["steps"])
            ov["t0"] = now
            self._identify = ov
            self._wake()

    @property
    def identifying(self) -> bool:
        return self._identify is not None

    def seed(self, duty: Any) -> None:
        """What the boot glow left on the pins (linear duty r, g, b): the first show glides from it.

        Writes nothing. Only before the first show; later it is ignored.
        """
        with self._cond:
            if self._shown:
                return
            rgb = _ratio_rgb(duty, (0.0, 0.0, 0.0))
            self._seed = self._frame_from_duty(rgb)
            self._written = rgb
            self._last = rgb

    def seed_level(self, colour: str, level: float) -> None:
        """As ``seed``, given as a colour and perceptual level."""
        with self._cond:
            if self._shown:
                return
            c = self._canon(colour)
            level = _clamp01(float(level))
            if c == "off":
                self._seed = DARK_FRAME
            else:
                self._seed = Frame(self._onehot(c), level, level)
            self._written = self._resolve(self._seed, self._gate)
            self._last = self._written

    def watchdog(self, seconds: float | None) -> None:
        """Fall back to the slow ember breath (busy, don't unplug) if nothing feeds the
        engine for ``seconds``: ``show()`` and ``feed()`` do. None disarms it."""
        with self._cond:
            self._watchdog_s = float(seconds) if seconds else None
            self._fed = self._clock()
            self._wake()

    def feed(self) -> None:
        """The keeper is alive (call it every tick): holds off the watchdog, ends its breath."""
        with self._cond:
            now = self._clock()
            self._fed = now
            if self._tripped:
                start = self._base(now)
                self._tripped = False
                self._set_look(self._look, now, start, 0.0, False, keep_epoch=False)
                self._wake()

    @property
    def tripped(self) -> bool:
        return self._tripped

    # -- calibration previews ----------------------------------------------

    def preview(self, rgb: Any | None, level: float = 1.0, hold_s: float = PREVIEW_S) -> None:
        """Show a raw colour (linear ratios 0..1) at a perceptual ``level``.

        For the Lamp colours page: drawn over Looks and replies (only the
        shutdown countdown outranks it), with no night level or brightness, so
        what the slider says is what shows (balance and minimum duty still
        apply - they are the hardware). It lasts ``hold_s`` after the last
        call; None ends it now, blending back to the Look.
        """
        with self._cond:
            now = self._clock()
            if rgb is None:
                if self._preview is not None:
                    self._preview = None
                    self._start_blend(now, RESTORE_S)
                    self._wake()
                return
            if self._countdown is not None:
                return
            ratio = _ratio_rgb(rgb, (0.0, 0.0, 0.0))
            level = _clamp01(float(level))
            self._preview = self._raw_duty(ratio, level)
            self._preview_until = now + max(0.0, float(hold_s))
            self._wake()

    def preview_colour(self, colour: str, level: float, hold_s: float = PREVIEW_S) -> None:
        """Show a palette colour at a perceptual level through the whole calibration
        (day/night palette, level gain, gate) - what that colour really looks like there."""
        c = self._canon(colour)
        with self._cond:
            if c == "off":
                self.preview(None)
                return
            if self._countdown is not None:
                return
            level = _clamp01(float(level))
            self._preview = self.duty_for(c, level)
            self._preview_until = self._clock() + max(0.0, float(hold_s))
            self._wake()

    def preview_duty(self, duty: Any, hold_s: float = PREVIEW_S) -> None:
        """Write raw linear duty (r, g, b 0..1) as it is - no gamma, balance or minimum
        duty - for ``hold_s`` (the calibration page finding the floors); None ends it."""
        with self._cond:
            if duty is None:
                self.preview(None)
                return
            if self._countdown is not None:
                return
            self._preview = _ratio_rgb(duty, (0.0, 0.0, 0.0))
            self._preview_until = self._clock() + max(0.0, float(hold_s))
            self._wake()

    def preview_look(self, name: str, seconds: float = PREVIEW_LOOK_S) -> None:
        """Show a meaning's Look for ``seconds`` with the real transitions, then the live
        Look again (the lamp page's "show me"): playing, ready, asleep, phone, busy,
        starting, pairing, asking, setup (CQ), no_internet (red alert), fault (SOS)."""
        look = preview_looks().get(name)
        if look is None:
            log.warning("lamp: no preview look %r", name)
            return
        with self._cond:
            if self._countdown is not None:
                return
            now = self._clock()
            start = self._base(now)
            self._preview_look = look
            self._preview_look_until = now + max(0.1, float(seconds))
            self._epoch = now
            self._transition(start, self._look_frame(look, now), now, 0.0)
            self._wake()

    def preview_reply(self, name: str) -> None:
        """Draw a reply for the lamp page: "heard" (kick), "no" (the HAL eye), "ok"
        (bloom and OK), "hello" (identify: HI) - also with key replies off; the
        language's own names work too."""
        if name == "hello":
            self.identify(IDENTIFY_S)
            return
        kind = PREVIEW_REPLIES.get(name, REPLY_ALIASES.get(name, name))
        if kind in OVERLAY_REPLIES:
            with self._cond:
                if self._countdown is None:
                    self.play(self._overlay_signal(kind))
            return
        if kind not in REPLY_S:
            log.warning("lamp: no preview reply %r", name)
            return
        with self._cond:
            if self._countdown is not None:
                return
            self._start_reply(kind, self._clock())

    def stop_preview(self) -> None:
        """End every preview (raw, colour, look, identify): back to the live Look."""
        with self._cond:
            self.preview(None)
            self.identify(0)
            self._end_preview_look(self._clock())

    def _end_preview_look(self, now: float) -> None:
        if self._preview_look is None:
            return
        start = self._base(now)
        self._preview_look = None
        self._epoch = now
        self._transition(start, self._look_frame(self._target_look(), now), now, 0.0)
        self._wake()

    def duty_for(self, colour: str, level: float) -> RGB:
        """The linear duty a steady ``colour`` at perceptual ``level`` gets (no night, no brightness)."""
        c = self._canon(colour)
        if c == "off" or level <= LIT:
            return (0.0, 0.0, 0.0)
        level = _clamp01(float(level))
        return self._resolve(Frame(self._onehot(c), level, level), None)

    @property
    def previewing(self) -> bool:
        return self._preview is not None

    # -- rendering -------------------------------------------------------

    @property
    def night(self) -> bool:
        return self._night

    @property
    def look(self) -> Look:
        return self._look

    @property
    def shown(self) -> bool:
        return self._shown

    def render(self, now: float) -> tuple[RGB, bool]:
        """(linear duty per channel, still moving) for the frame at ``now``.

        Advances the state machine (transitions finish, replies expire, the
        watchdog trips) but writes nothing, so tests can drive it with any clock.
        """
        with self._cond:
            self._now = now
            self._check_watchdog(now)
            if self._preview_look is not None and now >= self._preview_look_until:
                self._end_preview_look(now)
            base = self._base(now)
            frame = base
            if self._identify is not None:
                drawn = self._draw_identify(now - self._identify["t0"], frame)
                if drawn is None:       # its last step was back to the Look
                    self._identify = None
                else:
                    frame = drawn
            if self._reply is not None:
                drawn = self._draw_reply(now - self._reply_t0, frame)
                if drawn is None:       # finished: back to what is underneath
                    end = self._reply_t0 + self._reply_len()
                    final = self._draw_reply(self._reply_len() - 1e-9, self._base(end))
                    kind, self._reply = self._reply, None
                    if kind != "pattern" and final is not None and _differs_f(final, self._base(end)):
                        self._start_blend(now, RESTORE_S)
                else:
                    frame = drawn
            if self._countdown is not None:
                frame = self._draw_countdown(frame, self._countdown)
            self._last_frame = frame
            duty = self._resolve(frame, self._gate)
            if self._preview is not None:
                if now >= self._preview_until:
                    self._preview = None
                    self._start_blend(now, RESTORE_S)
                else:
                    duty = self._preview
            if self._blend_from is not None:
                x = (now - self._blend_t0 + KICK_LEAD_S) / self._blend_s
                if x >= 1.0:
                    self._blend_from = None
                else:
                    # Linear in perceptual space: even to the eye, no frame
                    # carries more than its share of the jump.
                    a = perceptual3(self._blend_from)
                    duty = linear3(_lerp3(a, perceptual3(duty), max(0.0, x)))
            moving = (
                self._reply is not None or self._trans is not None
                or self._target_look().motion != "solid"
                or self._identify is not None or self._preview is not None
                or self._blend_from is not None or self._preview_look is not None
                or any(f is not None for f in self._gate_fade)
            )
            self._last = duty
            return duty, moving

    def step(self, now: float) -> bool:
        """Render one frame and write it if it changed noticeably; True while moving.

        Before the first show nothing is written: the boot glow is left alone.
        """
        duty, moving = self.render(now)
        if not self._shown:
            return moving
        old = self._written
        if old is None or any(_noticeable(a, b, exact=not moving) for a, b in zip(old, duty)):
            self._written = duty
            self.output.write(*duty)
        return moving

    # -- internals -------------------------------------------------------

    def _wake(self) -> None:
        self._dirty = True
        self._cond.notify_all()

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._closed:
                    return
                self._dirty = False
            try:
                moving = self.step(self._clock())
            except Exception:
                log.exception("lamp frame failed")
                moving = False
            with self._cond:
                if self._closed:
                    return
                if not self._dirty:
                    # Steady: sleep until someone changes something (or the
                    # watchdog's deadline, if it is armed).
                    timeout = FRAME_S if moving else self._watchdog_left()
                    self._cond.wait(timeout)

    def _watchdog_left(self) -> float | None:
        if self._watchdog_s is None or self._tripped or not self._shown:
            return None
        return max(FRAME_S, self._watchdog_s - (self._clock() - self._fed))

    def _check_watchdog(self, now: float) -> None:
        if (self._watchdog_s is None or self._tripped or not self._shown
                or now - self._fed <= self._watchdog_s):
            return
        log.warning("lamp: nothing from the keeper for %.0f s - busy breath", now - self._fed)
        start = self._base(now)
        self._tripped = True
        self._epoch = now
        self._transition(start, self._look_frame(signals.get("busy"), now), now, 0.0)

    def _target_look(self) -> Look:
        if self._preview_look is not None:
            return self._preview_look
        return signals.get("busy") if self._tripped else self._look

    def _set_look(self, look: Look, now: float, start: Frame, fade_s: float, cut: bool,
                  keep_epoch: bool = True) -> None:
        old = self._target_look() if keep_epoch else Look()
        same_motion = (
            old.motion == look.motion != "solid" and old.period_s == look.period_s
        )
        self._look = look
        if self._tripped or self._preview_look is not None:
            return          # drawn when the keeper comes back / the preview ends
        if not same_motion:
            self._epoch = now   # a new breath starts at its trough, a beckon with a kick
        if cut:
            self._trans = None
            return
        self._transition(start, self._look_frame(look, now), now, fade_s)

    def _glide(self, start: Frame, now: float, fade_s: float, exact: bool = False) -> None:
        """Same Look, new levels (night, brightness): a brightness transition."""
        self._transition(start, self._look_frame(self._target_look(), now), now, fade_s, exact)

    def _transition(self, start: Frame, target: Frame, now: float, fade_s: float,
                    exact: bool = False) -> None:
        """Start the transition rule from ``start`` to ``target``.

        A change of hue family dips through HUE_TROUGH; anything else is a
        brightness change: RISE_S up (ease-out), FALL_S down (long tail),
        or ``fade_s`` if given. Falls are NIGHT_SLOW slower at night unless
        ``exact``.
        """
        night = NIGHT_SLOW if self._night else 1.0
        if start.level <= LIT and target.level <= LIT:
            self._trans = None
            return
        if start.level > LIT and target.level > LIT and self._family(start.w) != self._family(target.w):
            self._trans = {
                "kind": "hue", "from": start, "t0": now,
                "fall": HUE_FALL_S * night, "rise": HUE_RISE_S,
                "trough": min(HUE_TROUGH, start.level, max(target.env, LIT)),
            }
            return
        rising = target.env >= start.env if target.level > LIT else False
        if start.level <= LIT:
            rising = True
        dur = fade_s if fade_s > 0.0 else (RISE_S if rising else FALL_S)
        if not rising and not exact:
            dur *= night
        if start == target or dur <= 0.0:
            self._trans = None
            return
        self._trans = {"kind": "fade", "from": start, "t0": now, "dur": dur, "rising": rising}

    def _level(self, look: Look) -> tuple[float, float]:
        """(peak, trough) of ``look`` as drawn now: night, ember calibration, brightness."""
        colour = self._canon(look.colour)
        if colour == "off" or look.level <= 0.0:
            return 0.0, 0.0
        day = look.level
        if look.night is not None:
            night = look.night
        elif colour == "ember":
            night = day * EMBER_NIGHT / EMBER_LEVEL
        else:
            night = min(day, NIGHT_CAP)
        if colour == "ember":
            day *= self.ember_day / EMBER_LEVEL
            night *= self.ember_night / EMBER_NIGHT
        peak = night if self._night else day
        scaled = min(1.0, peak * self._scale)
        if self._scale > 1.0:
            scaled = min(scaled, max(peak, LOOK_CAP))   # keep the headroom for replies
        least = self._night_ember() if colour == "ember" else VISIBLE_MIN
        scaled = max(scaled, min(peak, least))
        amber_min = self._amber_min() if colour in AMBERS else 0.0
        scaled = max(scaled, amber_min)
        trough = 0.0
        if look.motion == "breath":
            # The trough stays above the band (amber) and above what the main
            # channel's minimum duty can draw, and the peak keeps ~3:1 in light
            # over it - a breath must read as a breath on any LED.
            if self._night and look.night_floor is not None and night > 0.0:
                share = look.night_floor / night
            else:
                share = look.floor / look.level
            trough = max(min(scaled, share * scaled), amber_min, self._floor_level(colour))
            scaled = min(1.0, max(scaled, trough * BREATH_MIN_SWING))
        return scaled, trough

    def _amber_min(self) -> float:
        """The lowest level amber is drawn at: above the band, so it stays amber -
        but never over AMBER_MIN_CAP (with a high green floor ready must stay dim)."""
        cap = AMBER_MIN_CAP[1] if self._night else AMBER_MIN_CAP[0]
        return min(1.0, self._band()[1] * AMBER_MARGIN, max(cap, self.amber_band[1] * AMBER_MARGIN))

    def _band(self) -> tuple[float, float]:
        """The amber band in force: io.lamp.amber_band, or higher if the green floor needs it.

        Amber's green must be at least about its floor where amber is drawn
        as amber; below that it can only be held far above its share (yellow-
        green) or dropped (ember-red). So the band's top is at least the level
        where amber's green wants GATE_ON of its floor, and amber blends into
        ember below it - one honest, gradual change instead of either.
        """
        lo, hi = self.amber_band
        g = max(self.palette["amber"][1], self.palette_night["amber"][1]) * self.balance[1]
        gain = max(self.level_gain.get("amber", 1.0), 1e-6)
        if g > 0.0:
            need = min(1.0, (self.min_duty[1] * AMBER_GATE_ON / g) ** (1.0 / GAMMA) / gain)
            if need > hi:
                lo, hi = max(lo, need * 0.6), need
        return lo, hi

    def _floor_level(self, colour: str) -> float:
        """The perceptual level at which ``colour``'s main channel reaches its minimum duty."""
        if colour not in self.palette:
            return 0.0
        ratio = self._ratio(colour, 1.0)
        i = max(range(3), key=lambda k: ratio[k] * self.balance[k])
        top = ratio[i] * self.balance[i]
        if top <= 0.0:
            return 0.0
        gain = max(self.level_gain.get(colour, 1.0), 1e-6)
        return min(1.0, (self.min_duty[i] / top) ** (1.0 / GAMMA) / gain)

    def _canon(self, colour: str) -> str:
        """``colour`` as the engine draws it: a palette colour (new ones too), an old
        name mapped onto its family, or "off"."""
        if colour in self.palette:
            return colour
        return canonical(colour)

    def _onehot(self, name: str) -> tuple[float, ...]:
        if name in ONEHOT and len(self.hues) == 3:
            return ONEHOT[name]
        return tuple(1.0 if h == name else 0.0 for h in self.hues)

    def _family(self, w: tuple[float, ...]) -> str:
        return family(w, self.hues, self.families)

    def _night_ember(self) -> float:
        return self.ember_night

    def _look_frame(self, look: Look, now: float) -> Frame:
        colour = self._canon(look.colour)
        peak, trough = self._level(look)
        if peak <= 0.0:
            return DARK_FRAME
        w = self._onehot(colour)
        t = now - self._epoch
        if look.motion == "breath":
            return Frame(w, trough + (peak - trough) * breath_shape(t, self._period(look)), trough)
        if look.motion == "filament":
            # Judged at the steady level: the minor channel never toggles with the shimmer.
            depth = FILAMENT_DEPTH_NIGHT if self._night else FILAMENT_DEPTH
            return Frame(w, min(1.0, peak * (1.0 + depth * filament_noise(now, self._phases))), peak)
        if look.motion == "beckon":
            if look.rhythm == "long-short":
                rest = look.period_s if look.period_s > 0 else SETUP_REST_S
                k = long_short_shape(t, rest, self._decay())
            else:
                rest = look.period_s if look.period_s > 0 else BECKON_REST_S
                k = pair_shape(t, rest, self._decay())
            return Frame(w, self._beckon_level(peak, k), peak)
        if look.motion == "pattern" and look.steps:
            steps = self._look_steps(look, peak, colour)
            last = steps[-1]
            prev = (last[0], self._onehot(last[2]) if last[2] != "base" else w)
            own = Frame(w, peak, peak)
            return self._steps_frame(steps, max(0.0, t), prev, own, True) or own
        return Frame(w, peak, peak)

    def _look_steps(self, look: Look, peak: float, colour: str) -> list[tuple]:
        """A pattern Look's steps (the night's at night) resolved at ``peak``: levels and
        colour names ("base"/"show"/None are the Look's own colour)."""
        steps = look.night_steps if self._night and look.night_steps else look.steps
        out = []
        for share, seconds, name, edge in steps:
            name = colour if name in (None, "show", "base") else self._canon(name)
            out.append((min(1.0, share * peak), seconds, name, edge))
        return out

    def _period(self, look: Look) -> float:
        """A breath's period as drawn now: 1.5x slower at night."""
        return look.period_s * (NIGHT_SLOW if self._night else 1.0)

    def _decay(self) -> float:
        return KICK_DECAY_S * (NIGHT_SLOW if self._night else 1.0)

    def _beckon_level(self, level: float, k: float) -> float:
        """A beckon's level: from ``level`` up to its top (BECKON_TOP, lower at night) by ``k``."""
        least = (BECKON_TOP_NIGHT if self._night else BECKON_TOP) * min(1.0, self._scale)
        top = min(1.0, max(level * BECKON_GAIN, least))
        return level + (top - level) * k

    def _base(self, now: float) -> Frame:
        """The Look (with its transition, if one runs) at ``now``, before any overlay."""
        target = self._look_frame(self._target_look(), now)
        tr = self._trans
        if tr is None:
            return target
        x = now - tr["t0"]
        start: Frame = tr["from"]
        if tr["kind"] == "fade":
            u = x / tr["dur"]
            if u >= 1.0:
                self._trans = None
                return target
            return _lerp_frame(start, target, ease_out(u) if tr["rising"] else tail(u))
        fall, rise, trough = tr["fall"], tr["rise"], tr["trough"]
        if x < fall:
            c = tail(x / fall)
            return Frame(start.w, _lerp(start.level, trough, c), _lerp(start.env, trough, c))
        if x < fall + rise:
            c = ease_out((x - fall) / rise)
            return Frame(target.w, _lerp(trough, target.level, c), _lerp(trough, target.env, c))
        self._trans = None
        return target

    def _reply_len(self) -> float:
        if self._reply is None:
            return 0.0
        if self._reply == "stop":
            return FALL_S * self._reply_slow
        if self._reply == "kick":
            return kick_len(KICK_DECAY_S * self._reply_slow) - KICK_LEAD_S
        if self._reply == "swell":
            return SWELL_S * self._reply_slow
        if self._reply == "pattern":
            return self._pattern["total"]
        return REPLY_S[self._reply]

    def _draw_reply(self, t: float, base: Frame) -> Frame | None:
        """The reply's frame ``t`` seconds in, or None once it is over."""
        name = self._reply
        t = max(0.0, t)
        if name is None or t >= self._reply_len():
            return None
        if name == "pattern":
            return self._steps_frame(self._pattern["steps"], t,
                                     (self._reply_from.level, self._reply_from.w), base, False)
        if name == "kick":
            start = self._reply_from.level
            if start <= LIT or base.level <= LIT:
                return base         # nothing to kick: the lamp is dark
            k = kick_shape(t + KICK_LEAD_S, KICK_DECAY_S * self._reply_slow)
            top = kick_peak(start, self._night, ember=base.w[1] > 0.5)
            return Frame(base.w, min(1.0, base.level + max(0.0, top - start) * k), base.env)
        if name == "refuse1":
            # The volume at its limit: one plain sag (no red).
            f = sag_shape(t, 1)
            return Frame(base.w, base.level * f, min(base.env, base.env * f))
        if name == "swell":
            x = t / (SWELL_S * self._reply_slow)
            gain = 1.0 + SWELL_GAIN * (0.5 - 0.5 * math.cos(2 * math.pi * x))
            return Frame(base.w, min(1.0, base.level * gain), base.env)
        # stop: from where it was, down to the ready level, with the fall's tail
        start = self._reply_from
        ready = self._level(Look("amber", READY_LEVEL, night=READY_NIGHT))[0]
        if start.level <= ready:
            return base
        c = tail(t / self._reply_len())
        return Frame(base.w, _lerp(start.level, ready, c), _lerp(start.env, ready, c))

    def _identify_look(self) -> Look:
        if self.geek and self.callsign:
            return signals.identify_look(self.callsign)
        look = signals.get("identify")
        return look if look.motion == "pattern" and look.steps else signals.DEFAULTS["identify"]

    def _draw_identify(self, t: float, base: Frame) -> Frame | None:
        """The identify code over ``base`` (None once it is over)."""
        return self._overlay_frame(self._identify, t, base)

    def _step_colour(self, colour: str | None, own: str, base: Frame) -> str:
        """A step's colour name: its own, the Look's (None), the colour on show, or "base"."""
        if colour == "base":
            return "base"
        name = own if colour is None else colour
        if name == "show":
            if base.level <= LIT:
                return "amber"
            i = max(range(len(base.w)), key=lambda k: base.w[k])
            return self.hues[i] if i < len(self.hues) else "amber"
        return self._canon(name)

    def _overlay_from(self, look: Look, base: Frame, times: int, absolute: bool) -> dict:
        """An overlay ready to draw: steps resolved (levels, colours), and a dip to
        the hue trough wherever it crosses to or from moon blue."""
        steps = look.night_steps if self._night and look.night_steps else look.steps
        if absolute:            # identify: its own level, no brightness
            peak = (look.night if self._night and look.night is not None else look.level)
        else:
            own = self._step_colour(None, look.colour, base)
            peak = self._level(dataclasses.replace(look, colour=own, motion="solid"))[0]
        out: list[tuple] = []
        prev_level, prev_w = base.level, base.w
        for share, seconds, colour, edge in tuple(steps) * times:
            name = self._step_colour(colour, look.colour, base)
            level = base.level if name == "base" else min(1.0, share * peak)
            w = base.w if name == "base" else self._onehot(name)
            if (prev_level > HUE_TROUGH * 1.2 and level > HUE_TROUGH * 1.2
                    and self._family(prev_w) != self._family(w)):
                trough_name = self._colour_of(prev_w)
                out.append((HUE_TROUGH, OVERLAY_DIP_S, trough_name, OVERLAY_DIP_S))
            out.append((level, seconds, name, edge))
            prev_level, prev_w = level, w
        return {"steps": out, "total": pattern_len(out), "t0": self._clock(), "from": base}

    def _colour_of(self, w: tuple[float, ...]) -> str:
        i = max(range(len(w)), key=lambda k: w[k])
        return self.hues[i] if i < len(self.hues) else "amber"

    def _steps_frame(self, steps: list[tuple], u: float, prev: tuple[float, tuple[float, ...]],
                     base: Frame, loop: bool) -> Frame | None:
        """The frame ``u`` seconds into resolved ``steps`` (level, seconds, colour, edge).

        Each step eases in from the one before over its edge: a crisp 22 ms
        attack, a ~25 ms exponential release, or the step's own. Colours of
        one fade family cross-fade on the way; to or from moon blue the swap
        happens at the low end (the steps dip there first). Time is absolute,
        so an edge never lands on a frame boundary by accident.
        """
        total = pattern_len(steps)
        if total <= 0.0:
            return None
        if u >= total:
            if not loop:
                return None
            u %= total
        p_level, p_w = prev
        for level, seconds, name, edge in steps:
            if name == "base":
                level, w = base.level, base.w
            else:
                w = self._onehot(name)
            if u < seconds:
                rising = level >= p_level
                e = edge if edge is not None else (EDGE_UP if rising else EDGE_DOWN)
                e = min(e, seconds)
                if e > 0.0 and u < e:
                    x = u / e
                    c = x if (rising and edge is None) else (tail(x) if not rising else ease_out(x))
                    lv = _lerp(p_level, level, c)
                    if self._family(p_w) != self._family(w):
                        w = w if rising else p_w
                    else:
                        w = _lerpn(p_w, w, c)
                    return Frame(w, lv, lv)
                return Frame(w, level, level)
            u -= seconds
            p_level, p_w = level, w
        return Frame(p_w, p_level, p_level)

    def _overlay_frame(self, ov: dict | None, t: float, base: Frame) -> Frame | None:
        if ov is None:
            return None
        return self._steps_frame(ov["steps"], max(0.0, t), (ov["from"].level, ov["from"].w), base, False)

    def _draw_countdown(self, frame: Frame, p: float) -> Frame:
        """Knob held off: glow down along the countdown to the night ember."""
        floor = self.ember_night
        ember = Frame(self._onehot("ember"), floor, floor)
        if frame.level <= LIT or self._family(frame.w) == "warm":
            return _lerp_frame(frame, ember, p)
        trough = min(HUE_TROUGH, frame.level)     # blue: down, swap, up - never purple
        if p < 0.5:
            c = 2 * p
            return Frame(frame.w, _lerp(frame.level, trough, c), _lerp(frame.env, trough, c))
        c = 2 * p - 1
        return Frame(ember.w, _lerp(trough, floor, c), _lerp(trough, floor, c))

    def _start_blend(self, now: float, seconds: float) -> None:
        """Blend from the duty on show to whatever comes next: no one-frame jumps."""
        if not self._shown:
            return
        self._blend_from = self._last
        self._blend_t0 = now
        self._blend_s = seconds

    def _ratio(self, name: str, env: float) -> RGB:
        """``name``'s linear ratio for a colour judged at ``env``: day/night over log(level)."""
        day, night = self.palette[name], self.palette_night[name]
        if env >= 1.0:
            rgb = day
        elif env <= NIGHT_ANCHOR:
            rgb = night
        else:
            rgb = _lerp3(night, day, math.log(env / NIGHT_ANCHOR) / math.log(1.0 / NIGHT_ANCHOR))
        if name == "ember":
            # Ember is the main channel alone, at every level and brightness: a
            # trace of green would make asleep read as ready.
            top = max(range(3), key=lambda i: rgb[i])
            rgb = tuple(c if i == top else 0.0 for i, c in enumerate(rgb))  # type: ignore[assignment]
        if name in AMBERS:
            lo, hi = self._band()
            if env < hi:
                w = 1.0 if env <= lo or hi <= lo else (hi - env) / (hi - lo)
                rgb = _lerp3(rgb, self._ratio("ember", env), ease(w))
        return rgb

    def _resolve(self, frame: Frame, gate: list[bool] | None) -> RGB:
        """Frame -> linear duty: palette, gain, gamma, balance, minimum duty (gated)."""
        if frame.level <= LIT:
            return (0.0, 0.0, 0.0)
        env = frame.env if frame.env > LIT else frame.level
        ratio: RGB = (0.0, 0.0, 0.0)
        gain = 0.0
        for weight, name in zip(frame.w, self.hues):
            if weight <= 0.0:
                continue
            r = self._ratio(name, env)
            ratio = (ratio[0] + weight * r[0], ratio[1] + weight * r[1], ratio[2] + weight * r[2])
            gain += weight * self.level_gain[name]
        lit = min(1.0, frame.level * gain) ** GAMMA
        judged = min(1.0, env * gain) ** GAMMA
        raw = [c * lit * b for c, b in zip(ratio, self.balance)]
        want = [c * judged * b for c, b in zip(ratio, self.balance)]
        main = max(range(3), key=lambda i: raw[i])
        off_at, on_at = GATE_OFF, GATE_ON
        if sum(frame.w[i] for i in self._amber_idx if i < len(frame.w)) >= 0.5:   # amber: hold its green longer
            off_at, on_at = AMBER_GATE_OFF, AMBER_GATE_ON
        out = [0.0, 0.0, 0.0]
        for i in range(3):
            if raw[i] <= 0.0:
                continue
            floor = self.min_duty[i]
            fade = 1.0
            if i != main:
                on = gate[i] if gate is not None else True
                near = raw[i] <= floor * GATE_SWITCH
                if on and want[i] < floor * off_at and (near or gate is None):
                    on = False
                elif not on and want[i] >= floor * on_at:
                    on = True
                    if gate is not None and not near:
                        self._gate_fade[i] = self._now     # far above the floor: fade in
                if gate is not None:
                    gate[i] = on
                    if not on:
                        self._gate_fade[i] = None
                    elif self._gate_fade[i] is not None:
                        x = (self._now - self._gate_fade[i]) / GATE_FADE_S
                        if x >= 1.0 or x < 0.0:
                            self._gate_fade[i] = None
                        else:
                            fade = ease(x)
                if not on:
                    continue
            lit = max(raw[i], floor)
            out[i] = min(1.0, floor + (lit - floor) * fade)
        return (out[0], out[1], out[2])

    def _raw_duty(self, ratio: RGB, level: float) -> RGB:
        if level <= LIT:
            return (0.0, 0.0, 0.0)
        raw = [c * level ** GAMMA * b for c, b in zip(ratio, self.balance)]
        out = []
        for duty, floor in zip(raw, self.min_duty):
            out.append(0.0 if duty < floor * GATE_OFF else min(1.0, max(duty, floor)))
        return (out[0], out[1], out[2])

    def _frame_from_duty(self, duty: RGB) -> Frame:
        """Best guess of the Frame behind a duty (the boot glow): colour family and level."""
        r, g, b = (d / max(bal, 1e-6) for d, bal in zip(duty, self.balance))
        if max(r, g, b) <= 0.0:
            return DARK_FRAME
        if b > r:
            name, channel = "blue", b
        else:
            share = g / r if r > 0 else 0.0
            mid = (self.palette["amber"][1] + self.palette["ember"][1]) / 2
            name, channel = ("amber" if share >= mid else "ember"), r
        top = max(self.palette[name]) or 1.0
        level = (min(1.0, channel / top)) ** (1.0 / GAMMA) / max(self.level_gain[name], 1e-6)
        level = _clamp01(level)
        return Frame(self._onehot(name), level, level)


# Nominal lengths (by day) of the replies the engine draws itself; the engine
# stretches the night-slow ones. The rest are overlays (OVERLAY_REPLIES).
REPLY_S: dict[str, float] = {
    "kick": kick_len() - KICK_LEAD_S,
    "refuse1": SAG_S,
    "swell": SWELL_S,
    "stop": FALL_S,
    # Overlays (drawn by play()); listed so callers that ask "does the engine
    # know this word?" (main.py _engine_knows) get yes. Nominal lengths by day.
    "refuse": 1.9,
    "refuse3": 1.9,
    "bloom": 1.0,
    "ok": 4.5,
    "update_ok": 4.5,
}


def _differs_f(a: Frame, b: Frame) -> bool:
    return (abs(a.level - b.level) > 1e-3 or abs(a.env - b.env) > 1e-3
            or max(abs(x - y) for x, y in zip(a.w, b.w)) > 1e-3)


def _noticeable(a: float, b: float, exact: bool) -> bool:
    """Worth a write: 1 % of the value (or a tiny absolute step); any change when settling."""
    if exact:
        return a != b
    return abs(a - b) > 1e-5 + 0.01 * max(a, b)


def open_lamp(spec: dict) -> LampEngine | None:
    """The lamp engine for the descriptor's ``io.lamp`` block, or None for no lamp.

    A ``pwm-sysfs`` lamp whose overlays aren't there falls back to gpiozero
    software PWM (flickery at night, but a lamp), with a warning.
    """
    backend = spec.get("backend", "pwm-sysfs")
    if backend == "none":
        return None
    output = open_output(spec)
    if isinstance(output, NullOutput) and backend == "pwm-sysfs":
        log.warning("lamp: pwm-gpio overlays not found; falling back to gpiozero PWM")
        # gpiozero gets its own default frequency: 800 Hz is beyond lgpio.
        fallback = {k: v for k, v in spec.items() if k != "frequency_hz"}
        output = open_output({**fallback, "backend": "gpio"})
    if isinstance(output, NullOutput):
        return None
    signals.remap(spec.get("signals"))      # io.lamp.signals: a new design without code
    return LampEngine(
        output,
        palette=spec.get("palette"),
        balance=spec.get("balance"),
        min_duty=spec.get("min_duty"),
        level_gain=spec.get("level_gain"),
        levels=spec.get("levels"),
        amber_band=spec.get("amber_band"),
    )
