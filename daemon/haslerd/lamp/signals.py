"""The named signal table - docs/lamp.md "Signals".

The signatures that are a design choice rather than a level - busy, setup,
asking, identify - live here by name, so a new design (a green "done", a
red warning, a morse nod) is a change of table, not of the engine or the
policy. The policy and the engine look a signal up when they draw it
(``get``), so a remap takes effect at once.

A remap comes from the variant's ``io.lamp.signals`` (``engine.open_lamp``)
or from code (``remap``). Each entry is a Look, or a dict:

    signals:
      setup: {colour: amber, motion: pattern, level: 0.6, night: 0.4,
              steps: [[1.0, 0.8], [0.3, 0.3], [1.0, 0.2], [0.3, 1.2]]}
      identify: {colour: show, motion: pattern, level: 1.0, morse: "hi"}
      busy: {colour: ember, motion: breath, level: 0.5, night: 0.3, period_s: 6, floor: 0.12}

``colour`` is any palette colour (``io.lamp.palette`` may add new ones, e.g.
``green: [0.1, 1.0, 0.0]``) or ``show`` (the colour on show; overlays only).
``identify`` is drawn over everything at its own level (no night, no
brightness): it must be a ``pattern``.
"""

from __future__ import annotations

import logging
from typing import Any

from .contract import (
    BUSY_LEVEL, BUSY_NIGHT, IDENTIFY_REST_S, READY_LEVEL, READY_NIGHT, SETUP_REST_S, Look,
    beckoning, breathing, morse, pattern,
)

log = logging.getLogger(__name__)

# Identify: three quick flashes to full (each 50 ms up, 100 ms held, 80 ms
# down - the engine softens the edges), dipping to 18 % between, then a rest.
IDENTIFY_LOW = 0.18
FLASH = ((1.0, 0.15), (IDENTIFY_LOW, 0.15))
IDENTIFY_STEPS = FLASH * 3 + ((IDENTIFY_LOW, IDENTIFY_REST_S),)

DEFAULTS: dict[str, Look] = {
    # Busy - don't unplug: the deep slow ember breath (6 s, 12 % -> 50 %).
    "busy": breathing("ember", BUSY_LEVEL, night=BUSY_NIGHT, slow=True),
    # Set me up: the amber long-short beckon, "daah-dit", ~3 s a cycle.
    "setup": beckoning("amber", READY_LEVEL, READY_NIGHT, rest_s=SETUP_REST_S, rhythm="long-short"),
    # Asking you to press a key: two kicks, then 2.2 s (the colour on show
    # when the policy draws it over a state).
    "asking": beckoning("amber", READY_LEVEL, READY_NIGHT),
    # Which radio?: three full flashes in the colour on show, ~1.7 s a cycle.
    "identify": pattern("show", 1.0, IDENTIFY_STEPS, night=1.0),
}

TABLE: dict[str, Look] = dict(DEFAULTS)

_MOTIONS = ("solid", "breath", "beckon", "pattern")


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
            steps = morse(str(spec["morse"]), float(spec.get("unit_s", 0.15)))
        else:
            steps = spec.get("steps") or ()
        return pattern(colour, level, steps, night)
    if motion == "breath":
        period = float(spec.get("period_s", 3.0))
        floor = float(spec.get("floor", level * 0.45))
        return Look(colour, level, "breath", period, floor, night)  # type: ignore[arg-type]
    if motion == "beckon":
        rhythm = spec.get("rhythm", "pair")
        if rhythm not in ("pair", "long-short"):
            raise ValueError("rhythm must be pair or long-short")
        return beckoning(colour, level, night, float(spec.get("rest_s", 2.2)), rhythm)  # type: ignore[arg-type]
    return Look(colour, level, "solid", 0.0, 0.0, night)  # type: ignore[arg-type]


def remap(overrides: Any) -> dict[str, Look]:
    """Lay ``overrides`` ({name: Look or dict}) over the defaults; None/{} resets.

    A bad entry is logged and skipped (the default stays), never raised: a
    typo in a variant must not cost the radio its lamp. Returns the table.
    """
    table = dict(DEFAULTS)
    for name, spec in (overrides or {}).items() if isinstance(overrides, dict) else ():
        try:
            look = look_from(spec)
            if name == "identify" and look.motion != "pattern":
                raise ValueError("identify must be a pattern")
            table[str(name)] = look
        except (TypeError, ValueError) as exc:
            log.warning("lamp: ignoring signal %r: %s", name, exc)
    TABLE.clear()
    TABLE.update(table)
    return TABLE
