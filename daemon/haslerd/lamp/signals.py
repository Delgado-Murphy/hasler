"""The named signal table - docs/lamp.md "Signals".

The designed signatures of the light language v2 live here by name, so a new
design is a change of table, not of the engine or the policy. The policy and
the engine look a signal up when they draw it (``get``), so a remap takes
effect at once.

Two kinds of entry, both Looks:

* **states** the lamp settles into (the policy shows them): ``busy``,
  ``starting``, ``pairing``, ``setup`` (Morse CQ), ``asking``,
  ``no_internet`` (red alert), ``fault`` (SOS);
* **overlays** drawn once over whatever shows (``engine.play(name)``, the
  replies, identify): ``identify`` (HI), ``bloom``, ``ok``, ``no`` (the HAL
  eye), ``update_ok``, ``73``, ``close_encounters``. Overlays are patterns;
  their steps may say ``"show"`` (the colour on show) and end with a
  ``"base"`` step (back to the Look underneath).

A remap comes from the variant's ``io.lamp.signals`` (``engine.open_lamp``)
or from code (``remap``). Each entry is a Look, or a dict:

    signals:
      setup: {colour: amber, motion: pattern, level: 0.8, night: 0.45,
              morse: "cq", unit_s: 0.18, space: 0.31, rest_s: 3}
      no_internet: {colour: amber, motion: pattern, level: 0.45,
                    steps: [[1.67, 0.2, red], [0.22, 0.12, red], [1.0, 5.68]]}
      busy: {colour: amber, motion: breath, level: 0.7, night: 0.35, floor: 0.22}

``colour`` is any palette colour (``io.lamp.palette`` may add new ones) or
``show``. Morse entries get their night tempo (x1.5) on their own; other
patterns may give ``night_steps``.
"""

from __future__ import annotations

import logging
import random
from typing import Any

from .contract import (
    BECKON_REST_S, BUSY_FLOOR, BUSY_LEVEL, BUSY_NIGHT, BUSY_NIGHT_FLOOR, CASTING_LEVEL,
    CASTING_NIGHT, IDENTIFY_REST_S, MORSE_NIGHT, MORSE_UNIT_S, MORSE_UNIT_SLOW_S, PLAYING_LEVEL,
    PLAYING_NIGHT, READY_LEVEL, READY_NIGHT, SETUP_REST_S, Look, beckoning, breathing, code,
    pattern, stretch,
)

log = logging.getLogger(__name__)

# -- states ---------------------------------------------------------------------

# Playing (and following a group that plays): like an incandescent filament -
# gold (a little yellower than amber), 80 % (night 35 %), with a tiny
# irregular shimmer (+-5 %, night +-3 %; the engine's filament noise).
PLAYING = Look("gold", PLAYING_LEVEL, "filament", night=PLAYING_NIGHT)

# Busy (shutting down, installing, starting, loading, joining): the amber breath.
BUSY = breathing("amber", BUSY_LEVEL, night=BUSY_NIGHT, floor=BUSY_FLOOR, night_floor=BUSY_NIGHT_FLOOR)
# Bluetooth pairing: the same breath in moon blue.
PAIRING = breathing("blue", CASTING_LEVEL, night=CASTING_NIGHT, floor=CASTING_LEVEL * BUSY_FLOOR / BUSY_LEVEL,
                    night_floor=CASTING_NIGHT * BUSY_NIGHT_FLOOR / BUSY_NIGHT)


def _morse(colour: str, text: str, level: float, night: float, unit: float, space: float,
           rest_s: float, space_colour: str | None = None, night_space: float | None = None) -> Look:
    day = code(text, unit, 1.0, space, None, space_colour, rest_s)
    by_night = code(text, unit, 1.0, space if night_space is None else night_space, None,
                    space_colour, rest_s)
    return pattern(colour, level, day, night, stretch(by_night, MORSE_NIGHT))


# Setup needed: CQ ("calling anyone") in amber, 80 % marks on a 25 % base,
# 180 ms units, then 3 s - persistent. At night 45 % marks on 22 % (amber
# can't be drawn lower: it would turn red, the colour of the alerts).
SETUP = _morse("amber", "cq", 0.80, 0.45, MORSE_UNIT_SLOW_S, 0.25 / 0.80, SETUP_REST_S,
               night_space=0.22 / 0.45)
# A fault that needs a maker: SOS in signal red at 150 ms units, then 7 s at a
# 10 % red glow - persistent, even in standby.
FAULT = _morse("red", "sos", 0.80, 0.45, 0.15, 0.10 / 0.80, 7.0)


def _alert(base: float, red: float, dip: float, every_s: float) -> tuple:
    """Two red flashes (200 ms at ``red``, 120 ms dip to ``dip``) over ``base``, every ``every_s``."""
    flash = ((red / base, 0.20, "red"), (dip / base, 0.12, "red"))
    return flash * 2 + ((1.0, every_s - 0.64, None),)


# No internet (only while awake and idle): Star Trek's red alert over ready.
NO_INTERNET = pattern("amber", READY_LEVEL, _alert(READY_LEVEL, 0.75, 0.10, 6.0), READY_NIGHT,
                      _alert(READY_NIGHT, 0.45, 0.10, 12.0))
# Asking you to press a key on the radio: two kicks, then 2.2 s.
ASKING = beckoning("amber", READY_LEVEL, READY_NIGHT, rest_s=BECKON_REST_S)

# -- overlays -------------------------------------------------------------------

IDENTIFY_REPEATS = 3


def identify_look(text: str = "hi") -> Look:
    """Identify: ``text`` (HI, or a callsign in geek mode) in green Morse at full -
    bright even at night (75 %) - its gaps at 10 % of the colour on show."""
    day = code(text, MORSE_UNIT_S, 1.0, 0.10, "green", "show", IDENTIFY_REST_S)
    return pattern("green", 1.0, day, 0.75, stretch(day, MORSE_NIGHT))


def _bloomed(after: tuple = (), level: float = 0.70, night: float = 0.40) -> Look:
    """The green bloom (cross-fade to green 200 ms, hold 300, back to the Look 500),
    then ``after``, then back to the Look. Night: 1.5x slower, 40 %."""
    day = ((1.0, 0.5, "green", 0.2), (0.0, 0.5, "base", 0.5))
    if after:
        day = day + ((0.0, 0.3, "base"),) + after + ((0.0, 0.5, "base", 0.5),)
    return pattern("green", level, day, night, stretch(day, 1.5))


BLOOM = _bloomed()
_OK = code("ok", MORSE_UNIT_S, 1.0, 0.10 / 0.70, "green", "show", 0.0)[:-1]
OK = _bloomed(_OK)
# The HAL 9000 eye: to red 65 % in 300 ms, hold 700, back 900 (1.9 s); night
# 35 % and 1.5x slower.
_HAL = ((1.0, 1.0, "red", 0.3), (0.0, 0.9, "base", 0.9))
NO = pattern("red", 0.65, _HAL, 0.35, stretch(_HAL, 1.5))
# Geek mode: 73 ("best regards") in amber before the shutdown fade.
_73 = code("73", MORSE_UNIT_S, 1.0, 0.10 / 0.80, None, "show", 0.0)[:-1] + ((0.0, 0.4, "base", 0.4),)
SEVENTY_THREE = pattern("amber", 0.80, _73, 0.45)
# Geek mode, after an update: the Close Encounters five notes (55/70/45/25/80 %,
# 300 ms each, the last held 900 ms), in amber.
_CE = tuple((lvl, 0.3 if i < 4 else 0.9, None, 0.06) for i, lvl in enumerate((0.55, 0.70, 0.45, 0.25, 0.80)))
CLOSE_ENCOUNTERS = pattern("amber", 1.0, _CE + ((0.0, 0.5, "base", 0.5),), 0.5)
UPDATE_OK = OK
UPDATE_OK_GEEK = _bloomed(((0.0, 0.3, "base"),) + tuple((s[0] / 0.70, s[1], "amber", s[3]) for s in _CE))


def tos_working(rng: random.Random | None = None) -> Look:
    """Star Trek TOS "working": 4-6 random 60-90 ms amber twinkles over the Look."""
    rng = rng or random.Random()
    steps: list[tuple] = []
    for _ in range(rng.randint(4, 6)):
        steps.append((1.0, rng.uniform(0.06, 0.09), "amber", 0.015))
        steps.append((0.0, rng.uniform(0.08, 0.22), "base", 0.03))
    return pattern("amber", 0.85, steps, 0.45)


DEFAULTS: dict[str, Look] = {
    "playing": PLAYING,
    "busy": BUSY,
    "starting": BUSY,
    "pairing": PAIRING,
    "setup": SETUP,
    "asking": ASKING,
    "no_internet": NO_INTERNET,
    "fault": FAULT,
    "identify": identify_look(),
    "bloom": BLOOM,
    "ok": OK,
    "no": NO,
    "update_ok": UPDATE_OK,
    "update_ok_geek": UPDATE_OK_GEEK,
    "73": SEVENTY_THREE,
    "close_encounters": CLOSE_ENCOUNTERS,
}
# Overlays (drawn once over the Look) - the rest are states.
OVERLAYS = ("identify", "bloom", "ok", "no", "update_ok", "update_ok_geek", "73", "close_encounters")
# How each signal moves, for the contrast rule (docs/lamp.md "Telling them apart").
MOTION_CLASS = {"playing": "still", "busy": "smooth", "starting": "smooth", "pairing": "smooth", "setup": "coded",
                "asking": "sharp", "no_internet": "sharp", "fault": "coded", "identify": "coded",
                "bloom": "smooth", "ok": "coded", "no": "smooth", "update_ok": "coded"}

TABLE: dict[str, Look] = dict(DEFAULTS)

_MOTIONS = ("solid", "breath", "beckon", "pattern", "filament")


def get(name: str) -> Look:
    """The signal ``name`` as drawn now (the default if it was never remapped)."""
    return TABLE.get(name) or DEFAULTS[name]


def look_from(spec: Any) -> Look:
    """A Look, or a dict as in the module's doc -> a Look. Raises ValueError."""
    if isinstance(spec, Look):
        return spec
    if not isinstance(spec, dict):
        raise ValueError(f"a signal must be a Look or a dict, not {type(spec).__name__}")
    colour = str(spec.get("colour", "amber"))
    motion = str(spec.get("motion", "solid"))
    if motion not in _MOTIONS:
        raise ValueError(f"motion must be one of {', '.join(_MOTIONS)}")
    level = float(spec.get("level", READY_LEVEL))
    night = spec.get("night")
    night = None if night is None else float(night)
    if not 0.0 < level <= 1.0 or (night is not None and not 0.0 < night <= 1.0):
        raise ValueError("level and night must be 0..1")
    if motion == "pattern":
        if "morse" in spec:
            return _morse(colour, str(spec["morse"]), level, night if night is not None else level,
                          float(spec.get("unit_s", MORSE_UNIT_S)), float(spec.get("space", 0.15)),
                          float(spec.get("rest_s", 2.0)), spec.get("space_colour"))
        return pattern(colour, level, spec.get("steps") or (), night, spec.get("night_steps"))
    if motion == "breath":
        floor = float(spec.get("floor", level * BUSY_FLOOR / BUSY_LEVEL))
        nf = spec.get("night_floor")
        return breathing(colour, level, night, floor=floor,  # type: ignore[arg-type]
                         night_floor=None if nf is None else float(nf))
    if motion == "beckon":
        rhythm = spec.get("rhythm", "pair")
        if rhythm not in ("pair", "long-short"):
            raise ValueError("rhythm must be pair or long-short")
        return beckoning(colour, level, night, float(spec.get("rest_s", BECKON_REST_S)), rhythm)  # type: ignore[arg-type]
    return Look(colour, level, motion, 0.0, 0.0, night)  # type: ignore[arg-type]


def remap(overrides: Any) -> dict[str, Look]:
    """Lay ``overrides`` ({name: Look or dict}) over the defaults; None/{} resets.

    A bad entry is logged and skipped (the default stays), never raised: a
    typo in a variant must not cost the radio its lamp. Returns the table.
    """
    table = dict(DEFAULTS)
    for name, spec in (overrides or {}).items() if isinstance(overrides, dict) else ():
        try:
            look = look_from(spec)
            if name in OVERLAYS and look.motion != "pattern":
                raise ValueError(f"{name} is drawn over the Look: it must be a pattern")
            table[str(name)] = look
        except (TypeError, ValueError) as exc:
            log.warning("lamp: ignoring signal %r: %s", name, exc)
    TABLE.clear()
    TABLE.update(table)
    return TABLE
