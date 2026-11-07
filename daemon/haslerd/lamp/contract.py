"""The pilot lamp's shared vocabulary - see docs/lamp.md.

Three parts meet here and nowhere else:

* the **policy** (lamp/policy.py) turns what the box is doing (``BoxState``)
  into a ``Look`` plus a fade time, and turns events into ``Reply`` names;
* the **engine** (lamp/engine.py) owns a thread that renders Looks,
  transitions, breaths, patterns, replies and the shutdown countdown onto an
  ``Output``;
* the **outputs** (lamp/outputs.py) write linear 0..1 duty per channel to the
  hardware (kernel pwm-gpio sysfs, gpiozero fallback, or a fake for tests).

The light language v2 (owner-approved, November 2026): **5 colours, 8
behaviours**. Colours: amber (the radio), ember (asleep - never moves), moon
blue (a phone), magic-eye green (OK - never steady, never dim), signal red
(a fault - never steady). Behaviours: steady, breath (busy - amber), kick (I
heard you), swell (a radio joined or left), green bloom (a small OK),
beckon (press a key), refuse (the HAL 9000 eye; a plain sag only for the
volume at its limit), alert (red alert: no internet), and Morse codes
(identify HI, setup CQ, OK, SOS). The designed signals live in a named
table (lamp/signals.py). Transitions: brightness rises fast and falls with a
long tail; amber, ember, green and red cross-fade, but a change to or from
moon blue dips to 10 % and swaps at the bottom, never fading through RGB.

Levels in a Look are *perceptual* (0..1). The engine applies night levels,
the owner's brightness, calibration (palette, level gain), gamma, balance and
minimum duty, and only then hands linear duty to the Output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

# The five colours (and "off"). The engine maps them to linear RGB ratios,
# calibrated per radio (io.lamp.palette; docs/lamp.md "Calibration"), and
# io.lamp.palette may add more.
Colour = Literal["amber", "ember", "blue", "green", "red", "gold", "off",
                 # Old names, kept so saved configs and old callers never crash:
                 "amber_pale", "white", "magenta"]
# gold: playing's filament, a slightly yellower tint of amber (same family).
COLOURS: tuple[str, ...] = ("amber", "ember", "blue", "green", "red", "gold", "off")
# Retired colours -> the colour of the same family in the new language. A
# Look in an old name still draws; a palette entry under an old name is
# ignored (it was tuned for a colour that no longer exists). "red" is a
# colour again (signal red, v2) - no longer an alias of ember.
ALIASES: dict[str, str] = {
    "amber_pale": "amber",     # playlists' tint: amber is the radio, whatever it plays
    "white": "amber",          # the old setup colour
    "magenta": "blue",         # the house is now a swell
}
ALL_COLOURS: tuple[str, ...] = COLOURS + tuple(ALIASES)
# The hue families a person tells apart (docs/lamp.md "Telling them apart"):
# ember and signal red are both red; what tells them apart is that ember
# never moves and red never stands still.
HUE_FAMILY: dict[str, str] = {"amber": "amber", "gold": "amber", "ember": "red", "red": "red",
                              "blue": "blue", "green": "green"}


def canonical(colour: str) -> str:
    """``colour`` in the new language: an alias maps onto its family; unknown -> "off"."""
    colour = ALIASES.get(colour, colour)
    return colour if colour in COLOURS else "off"


# solid: steady. breath: the busy breath (period_s, floor = trough).
# beckon: two kicks 250 ms apart (or long-short), then period_s of rest.
# pattern: a sequence of steps, looping - Morse, the red alert, any code the
# signal table wants (``pattern()``, ``code()``).
# filament: playing - steady but alive, a tiny irregular shimmer like an
# incandescent bulb (the engine's noise; never a rhythm).
Motion = Literal["solid", "breath", "beckon", "pattern", "filament"]
# A step of a pattern: (share of the Look's level - may exceed 1 for an
# accent over a base -, seconds[, colour[, edge seconds]]). Colour None: the
# Look's own; "show": the colour on show (overlays); "base": back to the Look
# underneath (overlays: level and colour). Edge None: a crisp attack (22 ms)
# or release (~25 ms time constant).
Step = tuple

# -- the levels (perceptual), day / night, docs/lamp.md "Levels" ----------------
#
# Re-derived from the owner's measured floors (red 0.03, green 0.045, blue
# 0.03 duty: 20 % / 24 % / 20 % perceptual) so no meaning sits on the floor.

PLAYING_LEVEL, PLAYING_NIGHT = 0.80, 0.35     # headroom above 80 % for a kick
READY_LEVEL, READY_NIGHT = 0.45, 0.30         # raised: clear of the floors, still amber
EMBER_LEVEL, EMBER_NIGHT = 0.20, 0.18         # standby, still; calibration io.lamp.levels
CASTING_LEVEL, CASTING_NIGHT = 0.55, 0.30     # moon blue's peak is 55 %
CAST_PAUSED_LEVEL = 0.20
SLEEP_LEVEL = 0.25                            # the sleep timer's last minute
NIGHT_CAP = 0.35                              # a Look with no night level of its own

# -- the behaviours' shapes (docs/lamp.md "Behaviours") -------------------------

# The breath (busy: shutting down, installing, starting, loading, joining):
# amber 22 % -> 70 %, 2.8 s; at night 18 % -> 35 % and 1.5x slower (4.2 s).
BREATH_S = 2.8
BREATH_RISE_S, BREATH_FALL_S, BREATH_REST_S = 1.1, 1.4, 0.3
BREATH_FLOOR = 0.22 / 0.70      # trough, as a share of the peak
BUSY_LEVEL, BUSY_NIGHT = 0.70, 0.35
BUSY_FLOOR, BUSY_NIGHT_FLOOR = 0.22, 0.18
# Kept for old callers: the "slow tempo" is the one breath now.
SLOW_S, SLOW_FLOOR = BREATH_S, BREATH_FLOOR
BECKON_REST_S = 2.2             # asking: two kicks, then this rest
SETUP_REST_S = 3.0              # setup: CQ, then this rest
IDENTIFY_REST_S = 1.3           # identify: HI, then this rest (a repeat ~3 s) ...
IDENTIFY_S = 9.0                # ... three times
# Morse: a unit of 130 ms for signals, 180 ms for persistent codes; at night
# 1.5x longer. Dash 3 units, gaps 1 / 3 (letters) / 7 (words).
MORSE_UNIT_S, MORSE_UNIT_SLOW_S, MORSE_NIGHT = 0.13, 0.18, 1.5

# How a beckon beats.
Rhythm = Literal["pair", "long-short"]


@dataclass(frozen=True)
class Look:
    """What the lamp should settle into. Immutable so ``==`` means "no change"."""

    colour: Colour = "off"
    level: float = 0.0            # perceptual 0..1 by day; for a breath, the peak
    motion: Motion = "solid"
    period_s: float = 0.0         # breath period (by day); for a beckon, the rest between pairs
    floor: float = 0.0            # breath trough by day, perceptual 0..1
    night: float | None = None    # the level at night; None: min(level, NIGHT_CAP), ember scaled
    rhythm: Rhythm = "pair"       # a beckon's beat: two kicks (asking) or long-short
    steps: tuple = ()             # a pattern's steps (``Step``), looping
    night_floor: float | None = None   # a breath's trough at night (None: scaled with the peak)
    night_steps: tuple = ()       # a pattern's steps at night (None: the day's)


DARK = Look()


def steady(colour: Colour, level: float, night: float | None = None) -> Look:
    return Look(colour, level, "solid", 0.0, 0.0, night)


def breathing(colour: Colour, peak: float, night: float | None = None,
              slow: bool = False, floor: float | None = None,
              night_floor: float | None = None) -> Look:
    """A breath at ``peak`` (``slow`` is kept for old callers: one tempo now)."""
    trough = peak * BREATH_FLOOR if floor is None else floor
    return Look(colour, peak, "breath", BREATH_S, trough, night, night_floor=night_floor)


def beckoning(colour: Colour, level: float, night: float | None = None,
              rest_s: float = BECKON_REST_S, rhythm: Rhythm = "pair") -> Look:
    """A beckon on ``level``: two kicks (``"pair"``) or a long glow and a kick
    (``"long-short"``), then ``rest_s``, until the Look changes."""
    return Look(colour, level, "beckon", rest_s, 0.0, night, rhythm)


def _clean_step(step) -> tuple:
    share, seconds = float(step[0]), float(step[1])
    colour = step[2] if len(step) > 2 else None
    edge = step[3] if len(step) > 3 else None
    return (max(0.0, share), max(0.0, seconds), None if colour is None else str(colour),
            None if edge is None else max(0.0, float(edge)))


def pattern(colour: str, level: float, steps, night: float | None = None,
            night_steps=None) -> Look:
    """A Look that plays ``steps`` (``Step``) again and again; ``night_steps`` at night.

    Any colour the palette knows (or ``"show"``: the colour on show, for an
    overlay). The engine gives each edge a crisp attack or release, so a step
    is never a one-frame jump.
    """
    clean = tuple(_clean_step(s) for s in steps)
    if not clean or sum(s[1] for s in clean) <= 0.0:
        raise ValueError("a pattern needs steps with some length")
    night_clean = tuple(_clean_step(s) for s in night_steps) if night_steps else ()
    return Look(colour, level, "pattern", 0.0, 0.0, night, "pair", clean,  # type: ignore[arg-type]
                None, night_clean)


def stretch(steps, k: float) -> tuple:
    """``steps`` k times slower (edges too): the night's tempo."""
    out = []
    for s in steps:
        s = _clean_step(s)
        out.append((s[0], s[1] * k, s[2], None if s[3] is None else s[3] * k))
    return tuple(out)


MORSE: dict[str, str] = {
    "a": ".-", "b": "-...", "c": "-.-.", "d": "-..", "e": ".", "f": "..-.", "g": "--.", "h": "....",
    "i": "..", "j": ".---", "k": "-.-", "l": ".-..", "m": "--", "n": "-.", "o": "---", "p": ".--.",
    "q": "--.-", "r": ".-.", "s": "...", "t": "-", "u": "..-", "v": "...-", "w": ".--", "x": "-..-",
    "y": "-.--", "z": "--..", "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
}


def code(text: str, unit_s: float = MORSE_UNIT_S, mark: float = 1.0, space: float = 0.15,
         colour: str | None = None, space_colour: str | None = None,
         rest_s: float | None = None, letter_units: float = 3.0) -> tuple:
    """``text`` in Morse as pattern steps: dot 1 unit, dash 3, gaps 1 / ``letter_units``
    (3; more is Farnsworth spacing) / 7 units, then ``rest_s`` (default 7 units).
    Marks at share ``mark`` in ``colour``, gaps at ``space`` in ``space_colour``."""
    steps: list[tuple] = []
    sp = (space, None, space_colour)
    words = [w for w in str(text).lower().split() if w]
    for wi, word in enumerate(words):
        letters = [MORSE[c] for c in word if c in MORSE]
        for li, sym_code in enumerate(letters):
            for si, sym in enumerate(sym_code):
                steps.append((mark, unit_s * (3 if sym == "-" else 1), colour))
                if si < len(sym_code) - 1:
                    steps.append((sp[0], unit_s, sp[2]))
            if li < len(letters) - 1:
                steps.append((sp[0], unit_s * letter_units, sp[2]))
        if wi < len(words) - 1:
            steps.append((sp[0], unit_s * 7, sp[2]))
    if not steps:
        raise ValueError(f"nothing to send in {text!r}")
    steps.append((sp[0], unit_s * 7 if rest_s is None else rest_s, sp[2]))
    return tuple(steps)


def morse(text: str, unit_s: float = 0.15, low: float = 0.15, gap_after: float = 7.0) -> tuple:
    """``text`` as (share, seconds) steps, single colour (``code`` does colours)."""
    return tuple((s[0], s[1]) for s in code(text, unit_s, 1.0, low, rest_s=unit_s * gap_after))


# One-off replies drawn over the current Look, then the Look again
# (docs/lamp.md "Replies"); a newer one replaces an older, blending from the
# frame on show. The old words map onto the new behaviours (REPLY_ALIASES).
Reply = Literal[
    "kick",       # I heard you: +40 % (at least 50 %) in 30 ms, 350 ms filament cool-down
    "refuse",     # no / failed: the HAL 9000 eye - to red 65 % in 300 ms, hold 700, back 900
    "refuse1",    # one sag to 25 %, no red: ONLY a volume key at its floor or ceiling
    "refuse3",    # kept for old callers: the HAL eye (a missing panel is a fault now: SOS)
    "swell",      # a radio joined or left the group: +30 % and back, raised cosine, 1.2 s
    "stop",       # same radio key again: glide down to the ready level
    "bloom",      # a small OK: cross-fade to green 200 ms, hold 300, back 500 (night x1.5)
    "ok",         # done (setup done): the bloom, then green Morse OK once
    "update_ok",  # an update installed: the bloom, then OK (geek mode: Close Encounters)
    # Old words, still spoken by callers:
    "ack", "next", "ha", "fail", "nudge", "signature",
    "confirmed",  # a key press answered a confirm request: a green bloom, even with key replies off
]
REPLY_ALIASES: dict[str, str] = {
    "ack": "kick", "next": "kick", "ha": "kick", "signature": "kick",
    "fail": "refuse", "nudge": "refuse1", "confirmed": "bloom",
}
# Replies that are a key being heard: the owner's "key replies off" drops them.
KEY_REPLIES: tuple[str, ...] = ("kick",)
# Swells and the kick's cool-down are this much slower at night (as falls are).
SLOW_AT_NIGHT: tuple[str, ...] = ("swell", "kick", "stop")


def signature(colour: Colour, level: float | None = None) -> str:
    """The reply text for a signature in ``colour``: ``"signature:blue@0.3"``.

    Kept for old callers: a signature now draws as a kick (the hue change to
    the destination is the Look's own transition). ``parse_reply`` takes it
    apart again.
    """
    text = f"signature:{colour}"
    return text if level is None else f"{text}@{level:g}"


def parse_reply(text: str) -> tuple[str, str | None, float | None]:
    """``"signature:blue@0.3"`` -> ("signature", "blue", 0.3); ``"ack"`` -> ("ack", None, None).

    Raises ValueError for a colour that isn't in the palette (old names are
    accepted) or a bad level.
    """
    name, _, rest = str(text).partition(":")
    if not rest:
        return name, None, None
    colour, _, level = rest.partition("@")
    if colour not in ALL_COLOURS or colour == "off":
        raise ValueError(f"unknown colour {colour!r}")
    if not level:
        return name, colour, None
    value = float(level)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"level {value} is not 0..1")
    return name, colour, value


BootStage = Literal["firmware", "early", "daemon", "network", "done"]


@dataclass
class BoxState:
    """Everything the policy needs, gathered by the main loop each tick."""

    boot: BootStage = "done"
    standby: bool = False
    countdown: float | None = None   # 0..1 through the 5 s knob-off hold, else None
    shutting_down: bool = False
    updating: bool = False           # an update is being installed (the updater's trial)
    halted: bool = False             # the system is going down for good: dark = unplug
    confirming: bool = False         # the page waits for a key press (web/confirm.py)
    hotspot: bool = False            # setup needed (the hotspot is up): CQ
    pairing: bool = False
    casting: bool = False            # a phone renderer has the audio
    cast_paused: bool = False
    offline_s: float = 0.0           # seconds without internet: the red alert once idle and awake
    joining: bool = False            # restarted onto a Wi-Fi just chosen, not on it yet: busy
                                     # (then the green OK - or the HAL eye and CQ if it can't)
    loading: bool = False            # a preset was started and no sound yet
    playing: bool = False            # our own source is making sound
    source: str = "radio"            # radio | ytm | spotify | bluetooth | airplay | ...
    panel_missing: bool = False      # a fault needing a maker: SOS (as ``fault``)
    fault: bool = False              # a fault needing a maker (rollback failed, SD read-only
                                     # or full, no sound card, overheating): SOS, even in standby
    update_ok: bool = False          # an update just installed (an event: policy.events_for;
                                     # the keeper may hold it ~12 s - the reply plays once)
    setup: bool = False              # setup needed (v2's name; ``hotspot`` means the same)
    offline: bool = False            # without internet long enough to say so (or offline_s)
    night: bool = False
    sleeping: bool = False           # the sleep timer's last minute (haslerd/sleep.py): dim
    # Multiroom (multiroom/group.py): "" alone or leading, "joining" while
    # this radio joins a group, "following" while it plays a leader's music.
    group: str = ""
    group_source: str = "radio"      # following: the leader's source
    group_playing: bool = False      # following: the leader makes sound


class Output(Protocol):
    """Writes linear duty 0..1 per channel. Must be cheap and never raise."""

    def write(self, r: float, g: float, b: float) -> None: ...

    def close(self) -> None: ...


class LampDriver(Protocol):
    """The engine as the rest of haslerd sees it. Thread-safe, never blocks."""

    def show(self, look: Look, fade_s: float = 0.0) -> None:
        """Settle into ``look``: the standard transition (0), or a glide of ``fade_s``."""

    def reply(self, name: str) -> None:
        """Draw a one-off reply over the current Look (a Reply; old words map on)."""

    def countdown(self, progress: float | None) -> None:
        """Knob-off hold: 0..1 glows down to the night ember; None snaps back."""

    def set_night(self, night: bool) -> None:
        """Sunset..sunrise: night levels, falls and swells x1.5, beckons half as tall."""

    def close(self, halt_dark: bool = True) -> None:
        """Stop the thread; leave the LED dark (the halt promise)."""
