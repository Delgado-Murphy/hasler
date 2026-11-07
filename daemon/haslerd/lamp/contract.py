"""The pilot lamp's shared vocabulary - see docs/lamp.md.

Three parts meet here and nowhere else:

* the **policy** (lamp/policy.py) turns what the box is doing (``BoxState``)
  into a ``Look`` plus a fade time, and turns events into ``Reply`` names;
* the **engine** (lamp/engine.py) owns a thread that renders Looks,
  transitions, breaths, replies and the shutdown countdown onto an ``Output``;
* the **outputs** (lamp/outputs.py) write linear 0..1 duty per channel to the
  hardware (kernel pwm-gpio sysfs, gpiozero fallback, or a fake for tests).

The light language (agreed 2026-11): **3 colours, 6 behaviours, 2
transition rules**. Colours: amber (the radio), ember (asleep), moon blue (a
phone is in charge). Behaviours: steady, breath (busy - don't unplug), kick
(I heard you), refuse (no / failed), beckon (I need you / which radio),
swell (something arrived or left). Transitions: brightness rises fast and
falls with a long tail; a hue change dips to 10 % and swaps at the bottom,
never fading through RGB.

Levels in a Look are *perceptual* (0..1). The engine applies night levels,
the owner's brightness, calibration (palette, level gain), gamma, balance and
minimum duty, and only then hands linear duty to the Output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

# The three colours (and "off"). The engine maps them to linear RGB ratios,
# calibrated per radio (io.lamp.palette; docs/lamp.md "Calibration").
Colour = Literal["amber", "ember", "blue", "off",
                 # Old names, kept so saved configs and old callers never crash:
                 "amber_pale", "white", "red", "magenta"]
COLOURS: tuple[str, ...] = ("amber", "ember", "blue", "off")
# Retired colours -> the colour of the same family in the new language. A
# Look or signature in an old name still draws; a palette entry under an old
# name is ignored (it was tuned for a colour that no longer exists).
ALIASES: dict[str, str] = {
    "amber_pale": "amber",     # playlists' tint: amber is the radio, whatever it plays
    "white": "amber",          # setup is now an amber beckon
    "red": "ember",            # failure is now a motion (refuse), not a hue
    "magenta": "blue",         # the house is now a swell
}
ALL_COLOURS: tuple[str, ...] = COLOURS + tuple(ALIASES)


def canonical(colour: str) -> str:
    """``colour`` in the new language: an alias maps onto its family; unknown -> "off"."""
    colour = ALIASES.get(colour, colour)
    return colour if colour in COLOURS else "off"


# solid: steady. breath: the asymmetric busy breath (period_s, floor = trough).
# beckon: two kicks 250 ms apart, then period_s of rest, until the Look changes.
Motion = Literal["solid", "breath", "beckon"]

# -- the levels (perceptual), day / night, docs/lamp.md "Levels" ----------------

PLAYING_LEVEL, PLAYING_NIGHT = 0.80, 0.35     # headroom above 80 % for a kick
READY_LEVEL, READY_NIGHT = 0.30, 0.20
EMBER_LEVEL, EMBER_NIGHT = 0.22, 0.15         # standby; calibration io.lamp.levels
CASTING_LEVEL, CASTING_NIGHT = 0.55, 0.30     # moon blue's peak is 55 %
CAST_PAUSED_LEVEL = 0.20
SLEEP_LEVEL = 0.25                            # the sleep timer's last minute
NIGHT_CAP = 0.35                              # a Look with no night level of its own

# -- the behaviours' shapes (docs/lamp.md "Behaviours") -------------------------

BREATH_S = 3.4                  # rise 1.3 s ease-out, fall 1.7 s ease-in-out, rest 0.4 s
BREATH_RISE_S, BREATH_FALL_S, BREATH_REST_S = 1.3, 1.7, 0.4
BREATH_FLOOR = 0.45             # trough, as a share of the peak
SLOW_S, SLOW_FLOOR = 6.0, 0.40  # the slow tempo: shutting down, installing
BECKON_REST_S = 2.2             # between pairs of kicks
SETUP_REST_S = 4.0              # setup / hotspot: a patient beckon
IDENTIFY_REST_S = 1.0           # "which radio?": an eager one ...
IDENTIFY_S = 10.0               # ... for about this long


@dataclass(frozen=True)
class Look:
    """What the lamp should settle into. Immutable so ``==`` means "no change"."""

    colour: Colour = "off"
    level: float = 0.0            # perceptual 0..1 by day; for a breath, the peak
    motion: Motion = "solid"
    period_s: float = 0.0         # breath period; for a beckon, the rest between pairs
    floor: float = 0.0            # breath trough, perceptual 0..1 (scales with the peak at night)
    night: float | None = None    # the level at night; None: min(level, NIGHT_CAP), ember scaled


DARK = Look()


def steady(colour: Colour, level: float, night: float | None = None) -> Look:
    return Look(colour, level, "solid", 0.0, 0.0, night)


def breathing(colour: Colour, peak: float, night: float | None = None,
              slow: bool = False, floor: float | None = None) -> Look:
    """A breath at ``peak``: the standard tempo, or the slow one; floor a share of the peak."""
    period, share = (SLOW_S, SLOW_FLOOR) if slow else (BREATH_S, BREATH_FLOOR)
    return Look(colour, peak, "breath", period, peak * share if floor is None else floor, night)


def beckoning(colour: Colour, level: float, night: float | None = None,
              rest_s: float = BECKON_REST_S) -> Look:
    return Look(colour, level, "beckon", rest_s, 0.0, night)


# One-off replies drawn over the current Look, then the Look again
# (docs/lamp.md "Replies"); a newer one replaces an older, blending from the
# frame on show. The old words map onto the new behaviours (REPLY_ALIASES).
Reply = Literal[
    "kick",       # I heard you: +40 % (at least 50 %) in 30 ms, 350 ms filament cool-down
    "refuse",     # no / failed: sag to 25 % (150 ms), hold 200 ms, recover 600 ms - twice
    "refuse1",    # one sag: a volume key at its floor or ceiling
    "refuse3",    # three sags: the panel board is missing (at boot)
    "swell",      # something arrived or left: +30 % and back, raised cosine, 1.2 s
    "stop",       # same radio key again: glide down to the ready level
    # Old words, still spoken by callers:
    "ack", "next", "ha", "fail", "nudge", "signature",
    "confirmed",  # a key press answered a confirm request: a kick even with key replies off
]
REPLY_ALIASES: dict[str, str] = {
    "ack": "kick", "next": "kick", "ha": "kick", "signature": "kick",
    "fail": "refuse", "nudge": "refuse1", "confirmed": "kick",
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
    hotspot: bool = False
    pairing: bool = False
    casting: bool = False            # a phone renderer has the audio
    cast_paused: bool = False
    offline_s: float = 0.0           # seconds without internet (no Look of its own any more)
    loading: bool = False            # a preset was started and no sound yet
    playing: bool = False            # our own source is making sound
    source: str = "radio"            # radio | ytm | spotify | bluetooth | airplay | ...
    panel_missing: bool = False      # no Look of its own: three refusals at boot
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
