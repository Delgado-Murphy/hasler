"""The lamp engine: one thread that renders Looks onto an Output - docs/lamp.md.

The policy says *what* to show (a ``Look`` and a fade time); this decides how
each frame looks. It sleeps on a condition while the lamp is steady (zero
CPU, zero writes) and renders at ~50 Hz only during fades, breaths, replies
and the shutdown countdown, so a slow preset start can never freeze it.

Pipeline per frame, per channel:

    palette ratio (linear) -> perceptual colour  ratio**(1/gamma) * level
    -> crossfade / breath / reply / countdown, all in perceptual space
    -> gamma 2.2 -> balance -> minimum duty -> Output.write

Blending in perceptual space keeps fades even to the eye, and at the
endpoints ``(ratio**(1/g) * L)**g == ratio * L**g``: the palette ratios are
exactly the linear channel mix at any level, so a colour keeps its hue as it
dims.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable

from .contract import SLOW_AT_NIGHT, Colour, Look, Output, Reply, parse_reply
from .outputs import CHANNELS, NullOutput, open_output

log = logging.getLogger(__name__)

RGB = tuple[float, float, float]

GAMMA = 2.2
FRAME_S = 0.02                  # 50 Hz while anything moves
NIGHT_CAP = 0.35                # sunset..sunrise ceiling
STANDBY_EMBER = 0.22            # the policy's standby level (~3.5 % red duty: chosen by eye 2026-10-21) ...
NIGHT_EMBER = 0.15              # ... and what it becomes at night (~1.5 % duty)
NIGHT_FADE = 1.5                # fades are this much slower at night
NIGHT_SWITCH_S = 1.5            # dusk/dawn crossfade
COUNTDOWN_FLOOR = 0.15          # knob-off hold ends at the night ember
COUNTDOWN_RESTORE_S = 0.5       # knob back on: back to the Look this fast
REPLY_RESTORE_S = 0.3           # a reply that ends off its Look blends back
LIT = 1e-4                      # perceptual level below which a channel is dark
PREVIEW_S = 30.0                # a calibration preview lasts this long after its last change
PREVIEW_RESTORE_S = 0.3         # ... then blends back to the Look this fast
# The levels the Lamp colours page previews at (perceptual): full, the night
# cap, standby ember.
PREVIEW_LEVELS: dict[str, float] = {"full": 1.0, "night": NIGHT_CAP, "ember": STANDBY_EMBER}

# Linear RGB ratios for this LED (R 220 ohm, G/B 100 ohm on 3.3 V): green is
# far more efficient than red, so warm colours carry very little of it.
# Calibrated by eye; the descriptor's io.lamp.palette overrides any of them.
# 2026-10-21: blue lost its green (green was ~70 % of its light, so it read
# green), amber_pale its blue; magenta is new - the house (another Hasler),
# brief signatures only, never a steady Look.
DEFAULT_PALETTE: dict[str, RGB] = {
    "amber":      (1.0, 0.15, 0.0),
    "amber_pale": (1.0, 0.22, 0.0),    # playlists: a touch lighter, never a new hue
    "ember":      (1.0, 0.10, 0.0),
    "blue":       (0.0, 0.0, 1.0),
    "white":      (1.0, 0.35, 0.06),
    "red":        (1.0, 0.03, 0.0),
    "magenta":    (0.25, 0.0, 1.0),
    "off":        (0.0, 0.0, 0.0),
}

# Smallest linear duty a lit channel gets: below it the hrtimer PWM can't
# draw a pulse reliably at 800 Hz (0.0005 = 625 ns).
DEFAULT_MIN_DUTY: RGB = (0.002, 0.0005, 0.0005)
# ... but only for a channel that carries a real share of the colour (its
# duty against the brightest channel's). A minor channel under its floor goes
# dark instead: lifting ember's 10 % green to the floor at 2 %/6 % turned it
# yellow-orange and unsteady, so deep ember is pure red - steady, orange-red.
MINOR_SHARE = 0.2

# Reply shapes (docs/lamp.md "Behaviour").
DIP_DEPTH = 0.3                 # key dip: to 30 % of the current level
DIP_DOWN_S, DIP_UP_S = 0.12, 0.20
HA_DOWN_S, HA_UP_S, HA_GAP_S = 0.06, 0.09, 0.10   # two 150 ms dips
STOP_S, STOP_FROM, STOP_TO = 0.8, 1.0, 0.35
FAIL_PULSES, FAIL_PULSE_S, FAIL_LEVEL = 3, 0.6, 0.6
FAIL_LEAD_S = 0.15              # the current Look fades out under the first pulse
SIG_IN_S, SIG_HOLD_S, SIG_OUT_S = 0.4, 0.6, 0.6   # signature: crossfade, hold, back
SIG_MIN_LEVEL = 0.35            # a signature with no level of its own: the Look's, at least this
SWELL_S, SWELL_GAIN = 0.8, 0.25  # pairing success: +25 % and back
REPLY_S: dict[str, float] = {
    "ack": DIP_DOWN_S + DIP_UP_S,
    "next": DIP_DOWN_S + DIP_UP_S,
    "ha": 2 * (HA_DOWN_S + HA_UP_S) + HA_GAP_S,
    "stop": STOP_S,
    "fail": FAIL_PULSES * FAIL_PULSE_S,
    "signature": SIG_IN_S + SIG_HOLD_S + SIG_OUT_S,
    "swell": SWELL_S,
    "nudge": DIP_DOWN_S + DIP_UP_S,
}


def ease(x: float) -> float:
    """Raised-cosine ease in/out, 0..1 -> 0..1."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return 0.5 - 0.5 * math.cos(math.pi * x)


def breath(t: float, floor: float, peak: float, period: float) -> float:
    """Raised-cosine breath starting at the trough."""
    if period <= 0.0:
        return peak
    return floor + (peak - floor) * (0.5 - 0.5 * math.cos(2 * math.pi * t / period))


def _lerp(a: RGB, b: RGB, w: float) -> RGB:
    return (a[0] + (b[0] - a[0]) * w, a[1] + (b[1] - a[1]) * w, a[2] + (b[2] - a[2]) * w)


def _scale(p: RGB, f: float) -> RGB:
    return (p[0] * f, p[1] * f, p[2] * f)


def _dip(t: float, down: float, up: float) -> float:
    """Level factor of one dip to DIP_DEPTH and back."""
    if t < down:
        return 1.0 - (1.0 - DIP_DEPTH) * ease(t / down)
    return DIP_DEPTH + (1.0 - DIP_DEPTH) * ease((t - down) / up)


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


class LampEngine:
    """contract.LampDriver on its own daemon thread named "lamp"."""

    def __init__(
        self,
        output: Output,
        palette: dict[str, Any] | None = None,
        balance: Any = None,
        min_duty: Any = None,
        clock: Callable[[], float] = time.monotonic,
        threaded: bool = True,
    ) -> None:
        self.output = output
        self.palette: dict[str, RGB] = dict(DEFAULT_PALETTE)
        for name, value in (palette or {}).items():
            self.palette[name] = _rgb(value, DEFAULT_PALETTE.get(name, (0.0, 0.0, 0.0)))
        # The release's palette (with the descriptor's overrides): what the
        # page's saved colours lay on top of, and what "Reset" goes back to.
        self.base_palette: dict[str, RGB] = dict(self.palette)
        self.balance = _rgb(balance, (1.0, 1.0, 1.0)) if balance is not None else (1.0, 1.0, 1.0)
        self.min_duty = _rgb(min_duty, DEFAULT_MIN_DUTY) if min_duty is not None else DEFAULT_MIN_DUTY
        self._clock = clock
        self._cond = threading.Condition(threading.RLock())
        now = clock()
        self._look: Look = Look()
        self._epoch = now               # breath phase origin
        self._from: RGB | None = None   # crossfade start, perceptual
        self._fade_t0 = now
        self._fade_s = 0.0
        self._reply: Reply | None = None
        self._reply_t0 = now
        self._reply_colour: Colour = "amber"
        self._reply_level = 0.0         # a signature's perceptual level
        self._reply_slow = 1.0          # SLOW_AT_NIGHT replies: NIGHT_FADE at night
        self._countdown: float | None = None
        self._preview: RGB | None = None      # perceptual, while calibrating
        self._preview_until = 0.0
        self._night = False
        self._last: RGB = (0.0, 0.0, 0.0)
        self._written: RGB | None = None
        self._dirty = False
        self._closed = False
        self._thread: threading.Thread | None = None
        if threaded:
            self._thread = threading.Thread(target=self._run, name="lamp", daemon=True)
            self._thread.start()

    # -- LampDriver ------------------------------------------------------

    def show(self, look: Look, fade_s: float = 0.0) -> None:
        with self._cond:
            if look == self._look:
                return
            now = self._clock()
            start = self._base(now)
            old = self._look
            same_breath = (
                old.motion == look.motion == "breath" and old.period_s == look.period_s
            )
            if not same_breath:
                self._epoch = now   # a new breath starts at its trough
            self._look = look
            self._start_fade(start, now, fade_s * (NIGHT_FADE if self._night else 1.0))
            self._wake()

    def reply(self, name: str) -> None:
        """A Reply, or a signature's text (contract.signature: "signature:blue@0.3")."""
        try:
            kind, hue, level = parse_reply(name)
        except ValueError:
            kind = ""
        if kind not in REPLY_S or (kind == "signature") != (hue is not None):
            log.warning("lamp: unknown reply %r", name)
            return
        with self._cond:
            if self._countdown is not None:
                return      # the countdown outranks replies
            self._reply = kind  # type: ignore[assignment]
            self._reply_t0 = self._clock()
            colour = self._look.colour
            if hue is not None:
                self._reply_colour = hue  # type: ignore[assignment]
                self._reply_level = level if level is not None else max(self._look.level, SIG_MIN_LEVEL)
            else:
                self._reply_colour = colour if colour != "off" else "amber"
            self._reply_slow = NIGHT_FADE if self._night and kind in SLOW_AT_NIGHT else 1.0
            self._wake()

    def _reply_len(self) -> float:
        return REPLY_S[self._reply] * self._reply_slow if self._reply is not None else 0.0

    def countdown(self, progress: float | None) -> None:
        with self._cond:
            if progress is None:
                if self._countdown is not None:
                    self._countdown = None
                    self._start_fade(self._last, self._clock(), COUNTDOWN_RESTORE_S)
                    self._wake()
                return
            progress = min(1.0, max(0.0, float(progress)))
            if progress != self._countdown:
                self._countdown = progress
                self._reply = None
                self._preview = None    # a shutdown ends any calibration
                self._wake()

    def set_palette(self, overrides: dict[str, Any] | None) -> None:
        """The page's saved colours over the release's palette, live.

        ``overrides`` maps palette names to [r, g, b] linear ratios 0..1;
        names the palette doesn't know are ignored, ``{}``/None is the release
        palette again. A colour on show changes at once (no fade).
        """
        palette = dict(self.base_palette)
        for name, value in (overrides or {}).items():
            if name in palette and name != "off":
                rgb = _rgb(value, palette[name])
                palette[name] = (_clamp01(rgb[0]), _clamp01(rgb[1]), _clamp01(rgb[2]))
        with self._cond:
            if palette != self.palette:
                self.palette = palette
                self._wake()

    def preview(self, rgb: Any | None, level: float = 1.0, hold_s: float = PREVIEW_S) -> None:
        """Show a raw colour (linear ratios 0..1) at a perceptual ``level``.

        For the Lamp colours page: drawn over Looks and replies (only the
        shutdown countdown outranks it), with no night cap so what the slider
        says is what shows. It lasts ``hold_s`` after the last call; None ends
        it now, blending back to the Look.
        """
        with self._cond:
            now = self._clock()
            if rgb is None:
                if self._preview is not None:
                    self._start_fade(self._preview, now, PREVIEW_RESTORE_S)
                    self._preview = None
                    self._wake()
                return
            if self._countdown is not None:
                return
            ratio = _rgb(rgb, (0.0, 0.0, 0.0))
            level = _clamp01(float(level))
            self._preview = tuple(_clamp01(c) ** (1.0 / GAMMA) * level for c in ratio)  # type: ignore[assignment]
            self._preview_until = now + max(0.0, float(hold_s))
            self._wake()

    @property
    def previewing(self) -> bool:
        return self._preview is not None

    def set_night(self, night: bool) -> None:
        with self._cond:
            if night == self._night:
                return
            now = self._clock()
            start = self._base(now)
            self._night = night
            self._start_fade(start, now, NIGHT_SWITCH_S)
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

    # -- rendering -------------------------------------------------------

    @property
    def night(self) -> bool:
        return self._night

    @property
    def look(self) -> Look:
        return self._look

    def render(self, now: float) -> tuple[RGB, bool]:
        """(linear duty per channel, still moving) for the frame at ``now``.

        Advances the state machine (fades finish, replies expire) but writes
        nothing, so tests can drive it with any clock.
        """
        with self._cond:
            base = self._base(now)
            frame = None
            if self._reply is not None:
                frame = self._draw_reply(now - self._reply_t0, base)
                if frame is None:       # finished: back to the (current) Look
                    length = self._reply_len()
                    end = self._reply_t0 + length
                    final = self._draw_reply(length - 1e-9, self._base(end))
                    self._reply = None
                    if final is not None and _differs(final, self._look_value(end)):
                        # e.g. fail ends dark: blend back from where it ended
                        self._start_fade(final, end, REPLY_RESTORE_S)
                        base = self._base(now)
            if self._preview is not None and now >= self._preview_until:
                self._start_fade(self._preview, self._preview_until, PREVIEW_RESTORE_S)
                self._preview = None
                base = self._base(now)
            moving = (
                self._reply is not None or self._from is not None or self._look.motion == "breath"
                or self._preview is not None    # so the thread sees it expire
            )
            if frame is None:
                frame = base
            if self._preview is not None:
                frame = self._preview
            if self._countdown is not None:
                target = self._colour("ember", self._night_level("ember", COUNTDOWN_FLOOR))
                frame = _lerp(frame, target, self._countdown)
            self._last = frame
            return self._duty(frame), moving

    def step(self, now: float) -> bool:
        """Render one frame and write it if it changed noticeably; True while moving."""
        duty, moving = self.render(now)
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
                    # Steady: sleep until someone changes something.
                    self._cond.wait(FRAME_S if moving else None)

    def _start_fade(self, start: RGB, now: float, fade_s: float) -> None:
        if fade_s > 0.0:
            self._from, self._fade_t0, self._fade_s = start, now, fade_s
        else:
            self._from = None

    def _night_level(self, colour: str, level: float) -> float:
        if not self._night:
            return level
        if colour == "ember":
            return level * (NIGHT_EMBER / STANDBY_EMBER)
        return min(level, NIGHT_CAP)

    def _colour(self, colour: str, level: float) -> RGB:
        ratio = self.palette.get(colour, (0.0, 0.0, 0.0))
        return tuple(max(0.0, c) ** (1.0 / GAMMA) * level for c in ratio)  # type: ignore[return-value]

    def _look_value(self, now: float) -> RGB:
        look = self._look
        peak = self._night_level(look.colour, look.level)
        level = peak
        if look.motion == "breath":
            # Night scales the floor with the peak so the breath stays visible.
            floor = look.floor * (peak / look.level) if look.level > 0 else 0.0
            level = breath(now - self._epoch, floor, peak, look.period_s)
        return self._colour(look.colour, level)

    def _base(self, now: float) -> RGB:
        target = self._look_value(now)
        if self._from is None:
            return target
        x = (now - self._fade_t0) / self._fade_s
        if x >= 1.0:
            self._from = None
            return target
        return _lerp(self._from, target, ease(x))

    def _draw_reply(self, t: float, base: RGB) -> RGB | None:
        """The reply's frame ``t`` seconds in, or None once it is over."""
        name = self._reply
        t = max(0.0, t)
        if name is None or t >= self._reply_len():
            return None
        t /= self._reply_slow           # drawn in day time
        if name in ("ack", "next", "nudge"):
            return _scale(base, _dip(t, DIP_DOWN_S, DIP_UP_S))
        if name == "ha":
            one = HA_DOWN_S + HA_UP_S
            if t < one:
                return _scale(base, _dip(t, HA_DOWN_S, HA_UP_S))
            if t < one + HA_GAP_S:
                return base
            return _scale(base, _dip(t - one - HA_GAP_S, HA_DOWN_S, HA_UP_S))
        if name == "signature":
            colour = self._reply_colour
            hue = self._colour(colour, self._night_level(colour, self._reply_level))
            if t < SIG_IN_S:
                return _lerp(base, hue, ease(t / SIG_IN_S))
            if t < SIG_IN_S + SIG_HOLD_S:
                return hue
            return _lerp(hue, base, ease((t - SIG_IN_S - SIG_HOLD_S) / SIG_OUT_S))
        if name == "swell":
            return _scale(base, 1.0 + SWELL_GAIN * (0.5 - 0.5 * math.cos(2 * math.pi * t / SWELL_S)))
        if name == "stop":
            level = STOP_FROM + (STOP_TO - STOP_FROM) * ease(t / STOP_S)
            colour = self._reply_colour
            return self._colour(colour, self._night_level(colour, level))
        # fail
        peak = self._night_level("red", FAIL_LEVEL)
        red = self._colour("red", breath(t, 0.0, peak, FAIL_PULSE_S))
        under = _scale(base, 1.0 - ease(t / FAIL_LEAD_S))
        return (red[0] + under[0], red[1] + under[1], red[2] + under[2])

    def _duty(self, p: RGB) -> RGB:
        raw = [
            0.0 if level <= LIT else min(1.0, level) ** GAMMA * balance
            for level, balance in zip(p, self.balance)
        ]
        top = max(raw)
        out = []
        for duty, floor in zip(raw, self.min_duty):
            if duty <= 0.0 or duty >= floor:
                out.append(min(1.0, duty))
            elif duty >= MINOR_SHARE * top:
                out.append(min(1.0, floor))     # a real part of the colour: keep it lit
            else:
                out.append(0.0)                 # a trace under the floor: dark, not unsteady
        return (out[0], out[1], out[2])


def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, v)) if v == v else 0.0     # NaN -> 0


def _differs(a: RGB, b: RGB) -> bool:
    return max(abs(x - y) for x, y in zip(a, b)) > 1e-3


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
    return LampEngine(
        output,
        palette=spec.get("palette"),
        balance=spec.get("balance"),
        min_duty=spec.get("min_duty"),
    )
