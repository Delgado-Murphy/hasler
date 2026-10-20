"""What a knob can do to the sound: the effects library.

Each effect turns a knob position (0 = fully left, 1 = fully right) and its
parameters into values for CamillaDSP blocks that scripts/hasler-dsp owns
(``BLOCKS`` there - every block is in the one config, neutral until an
effect moves it). Effects only ever *ask*; hasler-dsp adds the master gain
that cancels the highest point of the combined response, so no effect can
clip or push the old speaker past where it is with everything flat. A boost
on one side of a knob therefore sounds like a cut of everything else - that
is the price of never clipping, and why the boost maxima are modest.

Pure and tested; the knob plumbing is dsp/knobs.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

# The knob's middle, where the bipolar effects are exactly flat: a
# 60-year-old pot parked "at noon" is never exactly at noon.
CENTRE_DEADBAND = 0.02
# How much the vintage voice takes off above its top corner, at full blend.
VINTAGE_TOP_CUT_DB = 18.0
VINTAGE_BOTTOM_CUT_DB = 24.0  # and below its bottom corner
LOUDNESS_FULL_AT = 10.0      # knob volume at which the loudness lift is full


@dataclass(frozen=True)
class Param:
    id: str
    label: str
    unit: str
    min: float
    max: float
    step: float
    default: float
    plusminus: bool = False      # a symmetric +/- range in dB

    def doc(self) -> dict:
        d = {"id": self.id, "label": self.label, "unit": self.unit, "min": self.min,
             "max": self.max, "step": self.step, "default": self.default}
        if self.plusminus:
            d["plusminus"] = True
        return d


@dataclass(frozen=True)
class Sound:
    """One knob's contribution: block -> parameters, and the stereo width."""

    filters: dict[str, dict[str, float]] = field(default_factory=dict)
    width: float | None = None


@dataclass(frozen=True)
class Effect:
    id: str
    label: str
    description: str
    params: tuple[Param, ...]
    ends: tuple[str, str]
    # (position 0..1, params, context) -> Sound. Context: {"volume": 0-100 | None}.
    render: Callable[[float, dict, dict], Sound]
    # Bipolar: flat in the middle. Otherwise flat at the left end (width: right).
    bipolar: bool = False
    rest: float = 0.0            # the position that sounds like no effect at all

    def defaults(self) -> dict[str, float]:
        return {p.id: p.default for p in self.params}

    def doc(self) -> dict:
        return {"id": self.id, "label": self.label, "description": self.description,
                "params": [p.doc() for p in self.params], "ends": list(self.ends)}

    def text(self, position: float | None) -> str:
        """The knob in words: "Warmer · 40%", "Flat", "1960s radio · 100%"."""
        if self.id == "none":
            return "No effect"
        if position is None:
            return "Not read yet"
        if self.bipolar:
            amount = bipolar(position)
            if amount == 0:
                return "Flat"
            return f"{self.ends[1 if amount > 0 else 0]} · {round(abs(amount) * 100)}%"
        position = clamp01(position)
        if self.rest == 0:
            return self.ends[0] if position < CENTRE_DEADBAND else f"{self.ends[1]} · {round(position * 100)}%"
        # Resting at the right end (width): say how much of it is left.
        if position > 1 - CENTRE_DEADBAND:
            return self.ends[1]
        return self.ends[0] if position < CENTRE_DEADBAND else f"{self.ends[1]} · {round(position * 100)}%"


def bipolar(position: float) -> float:
    """0..1 -> -1..+1 with a flat spot in the middle."""
    x = (clamp01(position) - 0.5) * 2
    if abs(x) <= CENTRE_DEADBAND * 2:
        return 0.0
    # The deadband is taken out, so the knob still reaches +/-1 at its ends.
    return math.copysign((abs(x) - CENTRE_DEADBAND * 2) / (1 - CENTRE_DEADBAND * 2), x)


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _r(x: float) -> float:
    return round(x, 2) + 0.0      # never -0.0


# -- the effects --------------------------------------------------------------

def _none(_pos: float, _p: dict, _ctx: dict) -> Sound:
    return Sound()


def _treble(pos: float, p: dict, _ctx: dict) -> Sound:
    x = bipolar(pos)
    gain = x * (p["boost_db"] if x > 0 else p["cut_db"])
    return Sound({"Treble": {"gain": _r(gain), "freq": p["freq"]}})


def _bass(pos: float, p: dict, _ctx: dict) -> Sound:
    x = bipolar(pos)
    gain = x * (p["boost_db"] if x > 0 else p["cut_db"])
    return Sound({"Bass": {"gain": _r(gain), "freq": p["freq"]}})


def _tilt(pos: float, p: dict, _ctx: dict) -> Sound:
    t = bipolar(pos) * p["range_db"]
    return Sound({"Tilt low": {"gain": _r(-t), "freq": p["pivot"]},
                  "Tilt high": {"gain": _r(t), "freq": p["pivot"]}})


def loudness_share(volume: float | None, quiet_below: float) -> float:
    """0 at or above ``quiet_below`` (knob volume), 1 at LOUDNESS_FULL_AT and below.
    Linear in knob units, which the player maps linearly to dB."""
    if volume is None:
        return 0.5
    if quiet_below <= LOUDNESS_FULL_AT:
        return 1.0 if volume <= LOUDNESS_FULL_AT else 0.0
    return clamp01((quiet_below - volume) / (quiet_below - LOUDNESS_FULL_AT))


def _loudness(pos: float, p: dict, ctx: dict) -> Sound:
    share = clamp01(pos) * loudness_share(ctx.get("volume"), p["quiet_below"])
    return Sound({"Loudness low": {"gain": _r(share * p["bass_db"])},
                  "Loudness high": {"gain": _r(share * p["treble_db"])}})


def _presence(pos: float, p: dict, _ctx: dict) -> Sound:
    return Sound({"Presence": {"gain": _r(bipolar(pos) * p["range_db"]), "freq": p["freq"]}})


def _vintage(pos: float, p: dict, _ctx: dict) -> Sound:
    blend = clamp01(pos)
    if blend < CENTRE_DEADBAND:
        blend = 0.0
    # Shelves, not filters: at blend 0 every block is an exact pass-through
    # (a resting 10 Hz high-pass was not: it made a full-scale square wave
    # overshoot on the bench).
    return Sound({
        "Vintage low": {"gain": _r(-blend * VINTAGE_BOTTOM_CUT_DB), "freq": p["low_cut"]},
        "Vintage mid": {"gain": _r(blend * p["mid_bump"])},
        "Vintage high": {"gain": _r(-blend * VINTAGE_TOP_CUT_DB), "freq": p["high_cut"]},
    })


def _width(pos: float, _p: dict, _ctx: dict) -> Sound:
    w = clamp01(pos)
    return Sound(width=1.0 if w > 1 - CENTRE_DEADBAND else round(w, 3))


def _db(id: str, label: str, lo: float, hi: float, default: float, plusminus: bool = False) -> Param:
    return Param(id, label, "dB", lo, hi, 0.5, default, plusminus)


def _hz(id: str, label: str, lo: float, hi: float, step: float, default: float) -> Param:
    return Param(id, label, "Hz", lo, hi, step, default)


EFFECTS: dict[str, Effect] = {e.id: e for e in (
    Effect("tilt", "Tone (tilt)",
           "Bass and treble move in opposite ways around a pivot, like the tone "
           "control of an old radio: left is warmer, right is brighter. The "
           "strongest of the tone effects, because it works where a small "
           "speaker really plays.",
           (_db("range_db", "Range", 2, 12, 6, plusminus=True),
            _hz("pivot", "Pivot", 300, 2000, 50, 800)),
           ("Warmer", "Brighter"), _tilt, bipolar=True, rest=0.5),
    Effect("treble", "Treble",
           "A treble shelf: left cuts the highs, right lifts them.",
           (_db("cut_db", "Cut at the left end", 0, 18, 12),
            _db("boost_db", "Boost at the right end", 0, 9, 6),
            _hz("freq", "Corner", 1000, 8000, 100, 2500)),
           ("Darker", "Brighter"), _treble, bipolar=True, rest=0.5),
    Effect("bass", "Bass",
           "A bass shelf: left thins the low end, right fills it out (never "
           "past what the speaker gets with the knob in the middle).",
           (_db("cut_db", "Cut at the left end", 0, 18, 12),
            _db("boost_db", "Boost at the right end", 0, 9, 6),
            _hz("freq", "Corner", 60, 500, 10, 200)),
           ("Less bass", "More bass"), _bass, bipolar=True, rest=0.5),
    Effect("loudness", "Loudness",
           "Bass and a little treble lifted as the volume goes down, so music "
           "stays full when played quietly. The knob sets how much; at high "
           "volume it does nothing.",
           (_db("bass_db", "Bass lift (quiet)", 0, 12, 9),
            _db("treble_db", "Treble lift (quiet)", 0, 8, 3),
            Param("quiet_below", "Starts below volume", "%", 20, 100, 5, 70)),
           ("Off", "Full loudness"), _loudness, rest=0.0),
    Effect("presence", "Presence",
           "A wide peak where voices live (2-4 kHz): right brings speech and "
           "singers forward, left sets them back.",
           (_db("range_db", "Range", 2, 9, 6, plusminus=True),
            _hz("freq", "Centre", 1500, 5000, 100, 3000)),
           ("Recessed", "Forward"), _presence, bipolar=True, rest=0.5),
    Effect("vintage", "1960s radio",
           "Blends in the voice of a radio of the Hasler's day: the deep bass "
           "and the top end rolled off, a gentle bump in the middle.",
           (_hz("low_cut", "Low corner", 60, 300, 10, 120),
            _hz("high_cut", "High corner", 2500, 10000, 100, 6000),
            _db("mid_bump", "Mid bump", 0, 6, 3)),
           ("Today", "1960s radio"), _vintage, rest=0.0),
    Effect("width", "Stereo width",
           "From mono (left) to full stereo (right). Does nothing while Mono "
           "is on (Settings): the radio already sums left and right before "
           "any effect.",
           (), ("Mono", "Stereo"), _width, rest=1.0),
    Effect("none", "No effect", "The knob does nothing to the sound.", (), ("", ""), _none),
)}

# A knob's default effect from the verb it is bound to, when its descriptor
# has no ``effect:``.
VERB_EFFECTS = {"set_tone": "tilt", "set_treble": "treble", "set_bass": "bass", "knob_effect": "none"}


def effect(effect_id: Any) -> Effect:
    if not isinstance(effect_id, str) or effect_id not in EFFECTS:
        raise ValueError(f"no effect called {effect_id!r} (one of: {', '.join(EFFECTS)})")
    return EFFECTS[effect_id]


def params_from(effect_id: str, raw: Any, base: dict | None = None, clamp: bool = False) -> dict[str, float]:
    """Submitted params over ``base`` (else the effect's defaults) -> a full,
    checked set. ValueError says what is wrong; ``clamp`` (for saved files and
    descriptors) pulls an out-of-range number in instead of refusing it."""
    fx = effect(effect_id)
    out = dict(fx.defaults())
    if base:
        out.update({k: v for k, v in base.items() if k in out})
    if raw is None:
        return out
    if not isinstance(raw, dict):
        raise ValueError("params must be an object")
    known = {p.id: p for p in fx.params}
    for key, value in raw.items():
        if key not in known:
            raise ValueError(f"{fx.label}: no parameter {key!r}"
                             + (f" (one of: {', '.join(known)})" if known else " (it takes none)"))
        p = known[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{fx.label}: {p.label} must be a number")
        if not p.min <= value <= p.max:
            if not clamp:
                raise ValueError(f"{fx.label}: {p.label} must be {p.min:g} to {p.max:g} {p.unit}".rstrip())
            value = max(p.min, min(p.max, value))
        out[key] = float(value)
    return out


def render(effect_id: str, position: float | None, params: dict, volume: float | None = None) -> Sound:
    """One knob's blocks. ``position`` None (no reading yet) = no effect."""
    if position is None:
        return Sound()
    fx = effect(effect_id)
    return fx.render(clamp01(position), params_from(effect_id, None, params), {"volume": volume})


def combine(sounds: list[Sound]) -> dict:
    """Every knob's Sound -> the JSON hasler-dsp's ``set`` takes (from neutral)."""
    filters: dict[str, dict[str, float]] = {}
    width = 1.0
    for s in sounds:
        for block, params in s.filters.items():
            filters.setdefault(block, {}).update(params)
        if s.width is not None:
            width = min(width, s.width)
    return {"replace": True, "filters": filters, "width": width}
