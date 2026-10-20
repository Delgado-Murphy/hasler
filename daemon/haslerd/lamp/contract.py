"""The pilot lamp's shared vocabulary - see docs/lamp.md.

Three parts meet here and nowhere else:

* the **policy** (lamp/policy.py) turns what the box is doing (``BoxState``)
  into a ``Look`` plus a fade time, and turns events into ``Reply`` names;
* the **engine** (lamp/engine.py) owns a thread that renders Looks, fades,
  breaths, replies and the shutdown countdown onto an ``Output``;
* the **outputs** (lamp/outputs.py) write linear 0..1 duty per channel to the
  hardware (kernel pwm-gpio sysfs, gpiozero fallback, or a fake for tests).

Levels in a Look are *perceptual* (0..1). The engine applies night scaling,
then gamma, then the per-channel colour balance, and only then hands linear
duty to the Output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

# Palette names; the engine maps them to linear RGB ratios (calibrated by eye,
# overridable from the descriptor's lamp.palette). docs/lamp.md "Palette".
Colour = Literal["amber", "amber_pale", "ember", "blue", "white", "red", "off"]

Motion = Literal["solid", "breath"]


@dataclass(frozen=True)
class Look:
    """What the lamp should settle into. Immutable so ``==`` means "no change"."""

    colour: Colour = "off"
    level: float = 0.0            # perceptual 0..1; for a breath, the peak
    motion: Motion = "solid"
    period_s: float = 0.0         # breath period
    floor: float = 0.0            # breath trough, perceptual 0..1


DARK = Look()

# One-off replies drawn over the current Look, then the Look again
# (docs/lamp.md "Behaviour"): at most 3 s each, a newer one replaces an older.
Reply = Literal[
    "ack",        # key pressed: dip to 30 % and back (120 ms down, 200 ms up)
    "next",       # same playlist key again: one dip, as ack
    "stop",       # same radio key again: 0.8 s fade 100 -> 35 % (then policy's Look)
    "fail",       # 3 soft red pulses, 0.6 s each
    "ha",         # HA action sent: two dips, 2 x 150 ms
]

BootStage = Literal["firmware", "early", "daemon", "network", "done"]


@dataclass
class BoxState:
    """Everything the policy needs, gathered by the main loop each tick."""

    boot: BootStage = "done"
    standby: bool = False
    countdown: float | None = None   # 0..1 through the 5 s knob-off hold, else None
    shutting_down: bool = False
    hotspot: bool = False
    pairing: bool = False
    casting: bool = False            # a phone renderer has the audio
    cast_paused: bool = False
    offline_s: float = 0.0           # seconds without internet, 0 when online
    loading: bool = False            # a preset was started and no sound yet
    playing: bool = False            # our own source is making sound
    source: str = "radio"            # radio | ytm | spotify | bluetooth | airplay | ...
    panel_missing: bool = False
    night: bool = False
    # Multiroom (multiroom/group.py): "" alone or leading, "joining" while
    # this radio joins a group, "following" while it plays a leader's music.
    group: str = ""
    group_source: str = "radio"      # following: the leader's source (the tint)
    group_playing: bool = False      # following: the leader makes sound


class Output(Protocol):
    """Writes linear duty 0..1 per channel. Must be cheap and never raise."""

    def write(self, r: float, g: float, b: float) -> None: ...

    def close(self) -> None: ...


class LampDriver(Protocol):
    """The engine as the rest of haslerd sees it. Thread-safe, never blocks."""

    def show(self, look: Look, fade_s: float = 0.0) -> None:
        """Settle into ``look``; crossfade from what is showing over ``fade_s``."""

    def reply(self, name: Reply) -> None:
        """Draw a one-off reply over the current Look."""

    def countdown(self, progress: float | None) -> None:
        """Knob-off hold: 0..1 dims ember towards 2 %; None cancels (0.5 s back)."""

    def set_night(self, night: bool) -> None:
        """Sunset..sunrise: cap at 35 %, ember 2 %, fades x1.5."""

    def close(self, halt_dark: bool = True) -> None:
        """Stop the thread; leave the LED dark (the halt promise)."""
