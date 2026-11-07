"""The guided Lamp check (Maker tools): the calibration it saves, checked.

The page walks the owner through six by-eye steps on the real lamp (floors,
balance, amber, standby glow, blue, a verification sweep) and saves the result
into the box's calibration overlay, ``io.lamp`` of
``/etc/hasler/variant.d/<variant>.yaml`` - next to the knob curves, so a
release update never touches it. The keys are a contract with the lamp
engine, which reads them at start (and, when it offers ``set_calibration``,
at once)::

    min_duty:   {red, green, blue}            linear duty floor per channel
    balance:    {red, green, blue}            per-channel gain after gamma
    palette:
      amber:    {day: [r, g, b], night: [r, g, b]}
      ember:    [r, g, b]
      blue:     [r, g, b]
    level_gain: {amber, ember, blue}          perceptual level multiplier
    levels:     {ember_day, ember_night}      standby ember, perceptual
    calibrated: {at: ISO time, cpu_temp_c: number | null, version: 1}

Only those keys are written; anything else under ``io.lamp`` (pins, backend,
``amber_band``, other palette colours) is kept. ``calibrated`` is the
server's: the time and the CPU's temperature at save (amber drifts yellow as
the LED warms). Saving also drops the page's hand-set colours (settings.json
``lamp_palette``): the check supersedes them.

The page holds the draft and only sends choices; the engine draws everything:

* the floors and the balance are measured on raw channel duties
  (``POST /api/lamp/preview {"duty": [r, g, b]}``) - no gamma, balance or
  floor, since those are what they find;
* every other sample is ``{"draft": <the contract>, "colour": name, "level":
  0..1}`` (a steady colour) or ``{"draft": ..., "look": <sweep id>, "night":
  bool}`` (a Look or a behaviour, animated): the server builds a scratch
  engine with the draft (lamp/scratch.py) and streams its frames to the live
  lamp as raw duties. The answer carries ``jump``, the channel to blame if
  the owner says the colour jumped.
"""

from __future__ import annotations

import inspect
import math
import time
from pathlib import Path
from typing import Any, Callable

CHANNELS = ("red", "green", "blue")
GAINS = ("amber", "ember", "blue")
LEVELS = ("ember_day", "ember_night")
KEYS = ("min_duty", "balance", "palette", "level_gain", "levels")
CONTRACT = KEYS + ("calibrated",)
PALETTE_KEYS = ("amber", "ember", "blue")
VERSION = 1

MIN_DUTY_MAX = 0.05       # a floor above 5 % duty is a broken LED, not a calibration
BALANCE_RANGE = (0.05, 4.0)
GAIN_RANGE = (0.1, 4.0)
LEVEL_RANGE = (0.01, 1.0)

CPU_TEMP_FILE = Path("/sys/class/thermal/thermal_zone0/temp")

# What the check shows, the engine draws: every sample but the raw floors and
# balance is a scratch engine (lamp/scratch.py) with the page's draft
# calibration, so the check can never drift from the engine again.
COLOURS = ("amber", "ember", "blue")


def _attr(name: str) -> Callable[[Any], Any]:
    return lambda policy: getattr(policy, name, None)


def _boot(stage: str) -> Callable[[Any], Any]:
    def look(policy: Any) -> Any:
        table = getattr(policy, "BOOT", None)
        return table.get(stage) if isinstance(table, dict) else None
    return look


def _asking(policy: Any) -> Any:
    """A confirm request over ready: the beckon in the colour on show."""
    fn, ready = getattr(policy, "confirm_look", None), getattr(policy, "READY", None)
    return fn((ready, 0.0)) if callable(fn) and ready is not None else None


# The verification sweep, by day and at night: the policy's Looks as they are
# today, then the behaviours drawn over them. (id, what the owner calls it,
# what to watch for, the Look, events (at s, "reply" word | "show" Look), the
# scene's length - 0 for a Look on its own, which repeats by itself).
SWEEP: tuple = (
    ("playing", "Playing", "steady", _attr("PLAYING"), (), 0.0),
    ("ready", "Ready", "steady", _attr("READY"), (), 0.0),
    ("loading", "Tuning in", "breath", _attr("LOADING"), (), 0.0),
    ("boot", "Starting up", "breath", _boot("network"), (), 0.0),
    ("standby", "Standby", "steady", _attr("STANDBY"), (), 0.0),
    ("boot_standby", "Starting in standby", "breath", _attr("BOOT_STANDBY"), (), 0.0),
    ("busy", "Shutting down or updating", "breath", _attr("SHUTTING_DOWN"), (), 0.0),
    ("casting", "A phone playing", "steady", _attr("CASTING"), (), 0.0),
    ("cast_paused", "A phone paused", "steady", _attr("CAST_PAUSED"), (), 0.0),
    ("pairing", "Bluetooth pairing", "breath", _attr("PAIRING"), (), 0.0),
    ("hotspot", "Wi-Fi setup", "beckon", _attr("HOTSPOT"), (), 0.0),
    ("asking", "Asking for a key press", "beckon", _asking, (), 0.0),
    ("kick", "A key heard", "kick", _attr("PLAYING"), ((0.6, "reply", "kick"),), 2.0),
    ("refuse", "Can’t do that", "sag", _attr("READY"), ((0.6, "reply", "refuse"),), 3.5),
    ("swell", "Another radio joins", "swell", _attr("PLAYING"), ((0.6, "reply", "swell"),), 3.0),
    ("hue", "A phone takes over, then lets go", "hue", _attr("READY"),
     ((1.0, "show", _attr("CASTING")), (4.0, "show", _attr("READY"))), 7.0),
)
# What the page asks the owner to watch, by kind of item.
WATCH = {
    "steady": "Watch it for a few seconds: it should not flicker or change colour.",
    "breath": "It breathes. Watch for the colour jumping or flickering as it gets faint.",
    "beckon": "Two quick flashes, then a rest. The colour should stay the same in the flashes.",
    "kick": "Every couple of seconds it jumps up once and cools down. Same colour all the way?",
    "sag": "It dips down twice and comes back. Watch the colour at the bottom of the dip.",
    "swell": "It swells up and back. The colour should not change.",
    "hue": "It dims, turns blue, then back to amber. Watch for a pink or purple moment in between.",
}

def number(raw: Any, name: str, lo: float, hi: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a number")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be {lo:g}..{hi:g}")
    return round(value, 6)


def _exact(raw: Any, name: str, keys: tuple[str, ...]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be {{{', '.join(keys)}}}")
    missing = [k for k in keys if k not in raw]
    extra = sorted(str(k) for k in raw if k not in keys)
    if missing or extra:
        bits = ([f"missing {', '.join(missing)}"] if missing else []) + \
               ([f"unknown {', '.join(extra)}"] if extra else [])
        raise ValueError(f"{name}: " + "; ".join(bits))
    return raw


def _colour(raw: Any, name: str) -> list[float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError(f"{name} must be [r, g, b]")
    out = [number(v, name, 0.0, 1.0) for v in raw]
    if max(out) <= 0.0:
        raise ValueError(f"{name} can't be black")
    return out


def duty_from(raw: Any) -> list[float]:
    """A raw preview's ``[r, g, b]`` channel duties, each 0..1 (black is fine)."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError("duty must be [r, g, b]")
    return [number(v, "duty", 0.0, 1.0) for v in raw]


def check(body: Any) -> dict:
    """The page's calibration, validated: exactly the contract's keys but
    ``calibrated`` (the server's own; ignored if sent). Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("expected {" + ", ".join(KEYS) + "}")
    body = {k: v for k, v in body.items() if k != "calibrated"}
    _exact(body, "calibration", KEYS)
    md = _exact(body["min_duty"], "min_duty", CHANNELS)
    bal = _exact(body["balance"], "balance", CHANNELS)
    pal = _exact(body["palette"], "palette", PALETTE_KEYS)
    amber = _exact(pal["amber"], "palette.amber", ("day", "night"))
    gain = _exact(body["level_gain"], "level_gain", GAINS)
    lv = _exact(body["levels"], "levels", LEVELS)
    return {
        "min_duty": {c: number(md[c], f"min_duty.{c}", 0.0, MIN_DUTY_MAX) for c in CHANNELS},
        "balance": {c: number(bal[c], f"balance.{c}", *BALANCE_RANGE) for c in CHANNELS},
        "palette": {
            "amber": {"day": _colour(amber["day"], "palette.amber.day"),
                      "night": _colour(amber["night"], "palette.amber.night")},
            "ember": _colour(pal["ember"], "palette.ember"),
            "blue": _colour(pal["blue"], "palette.blue"),
        },
        "level_gain": {g: number(gain[g], f"level_gain.{g}", *GAIN_RANGE) for g in GAINS},
        "levels": {k: number(lv[k], f"levels.{k}", *LEVEL_RANGE) for k in LEVELS},
    }


def cpu_temp_c(path: Path | None = None) -> float | None:
    """The SoC's temperature in degC (one decimal), or None off a Pi."""
    try:
        return round(int((path or CPU_TEMP_FILE).read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def stamp(cal: dict, temp: float | None, now: float | None = None) -> dict:
    """``cal`` with its ``calibrated`` block: the save time (local, ISO) and temperature."""
    at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now if now is not None else time.time()))
    if len(at) > 5 and at[-5] in "+-":
        at = at[:-2] + ":" + at[-2:]            # +0100 -> +01:00
    return {**cal, "calibrated": {"at": at, "cpu_temp_c": temp, "version": VERSION}}


def saved_from(lamp: Any) -> dict:
    """The contract's keys found under an overlay's ``io.lamp`` ({} for none)."""
    if not isinstance(lamp, dict):
        return {}
    out = {k: lamp[k] for k in ("min_duty", "balance", "level_gain", "levels", "calibrated") if k in lamp}
    pal = lamp.get("palette")
    if isinstance(pal, dict):
        mine = {k: pal[k] for k in PALETTE_KEYS if k in pal}
        if mine:
            out["palette"] = mine
    return out


def _replace(old: Any, new: Any) -> Any:
    """An overlay patch (boxconfig.apply_patch) that turns ``old`` into exactly
    ``new``: nested keys ``new`` doesn't have are deleted (None)."""
    if not isinstance(new, dict):
        return new
    patch: dict = {k: None for k in (old if isinstance(old, dict) else {}) if k not in new}
    for k, v in new.items():
        patch[k] = _replace(old.get(k) if isinstance(old, dict) else None, v)
    return patch


def save_patch(current_lamp: Any, cal: dict) -> dict:
    """The overlay patch writing ``cal`` (stamped) into ``io.lamp``.

    Each contract key is replaced whole; other ``io.lamp`` keys - and palette
    colours the check doesn't own (white, magenta...) - are kept.
    """
    current = current_lamp if isinstance(current_lamp, dict) else {}
    lamp: dict = {}
    for key in CONTRACT:
        if key == "palette":
            old = current.get("palette") if isinstance(current.get("palette"), dict) else {}
            lamp["palette"] = {k: _replace(old.get(k), cal["palette"][k]) for k in PALETTE_KEYS}
        else:
            lamp[key] = _replace(current.get(key), cal[key])
    return {"io": {"lamp": lamp}}


def clear_patch() -> dict:
    """The overlay patch for "Back to standard": the contract's keys gone."""
    lamp: dict = {k: None for k in CONTRACT if k != "palette"}
    lamp["palette"] = {k: None for k in PALETTE_KEYS}
    return {"io": {"lamp": lamp}}


def standard(engine: Any, lamp: Any = None) -> dict:
    """The uncalibrated values, in the contract's shape: the engine's own
    (``lamp.calibration_defaults()`` when it offers one), else the module's
    constants, so the page's model starts where the lamp is today."""
    fn = getattr(lamp, "calibration_defaults", None)
    if callable(fn):
        try:
            doc = fn()
            return check(doc)
        except Exception:
            pass
    pal = dict(getattr(engine, "DEFAULT_PALETTE", {}) or {})
    base = getattr(lamp, "base_palette", None)
    if isinstance(base, dict):
        pal.update(base)

    def rgb(name: str, fallback: tuple) -> list[float]:
        value = pal.get(name, fallback)
        try:
            return [round(float(v), 6) for v in value]
        except (TypeError, ValueError):
            return list(fallback)

    floors = getattr(lamp, "min_duty", None) or getattr(engine, "DEFAULT_MIN_DUTY", (0.002, 0.0005, 0.0005))
    balance = getattr(lamp, "balance", None) or (1.0, 1.0, 1.0)
    amber = rgb("amber", (1.0, 0.15, 0.0))
    return {
        "min_duty": dict(zip(CHANNELS, (round(float(v), 6) for v in floors))),
        "balance": dict(zip(CHANNELS, (round(float(v), 6) for v in balance))),
        "palette": {"amber": {"day": amber, "night": list(amber)},
                    "ember": rgb("ember", (1.0, 0.10, 0.0)), "blue": rgb("blue", (0.0, 0.0, 1.0))},
        "level_gain": {g: 1.0 for g in GAINS},
        "levels": {"ember_day": float(getattr(engine, "EMBER_LEVEL", 0.22)),
                   "ember_night": float(getattr(engine, "EMBER_NIGHT", 0.15))},
    }


def scenes(policy: Any) -> dict:
    """Sweep id -> lamp/scratch.Scene, from the policy's Looks as they are now
    (an item whose Look the policy no longer has is left out)."""
    from ..lamp.scratch import Scene

    out = {}
    for lid, _name, _kind, source, events, length in SWEEP:
        look = source(policy)
        if look is None:
            continue
        evs = []
        for at, what, arg in events:
            if callable(arg):
                arg = arg(policy)
                if arg is None:
                    break
            evs.append((at, what, arg))
        else:
            out[lid] = Scene(look, tuple(evs), length)
    return out


def sweep(policy: Any) -> list[dict]:
    """The verification sweep's items for the page: names, what to watch,
    and the base Look's colour and level (for a rough swatch only)."""
    found = scenes(policy)
    out = []
    for lid, name, kind, _source, _events, _length in SWEEP:
        scene = found.get(lid)
        if scene is None:
            continue
        look = scene.look
        out.append({"id": lid, "name": name, "kind": kind, "watch": WATCH[kind],
                    "colour": str(look.colour), "level": float(look.level),
                    "motion": str(look.motion), "period_s": float(look.period_s),
                    "floor": float(look.floor)})
    return out


def draft_spec(body: Any, lamp: Any = None) -> dict:
    """The page's draft calibration (the contract, validated), with the live
    lamp's ``amber_band`` (not the check's to change) - a scratch engine's spec."""
    cal = check(body)
    current = getattr(lamp, "calibration", None)
    if callable(current):
        try:
            band = current().get("amber_band")
            if band is not None:
                cal = {**cal, "amber_band": list(band)}
        except Exception:
            pass
    return cal


def preview_target(body: dict) -> tuple:
    """A draft preview's target: ("colour", name, level) or ("scene", id, night)."""
    has_colour, has_scene = "colour" in body, ("look" in body or "scene" in body)
    if has_colour == has_scene:
        raise ValueError('a draft preview takes {"colour": name, "level": 0..1} '
                         'or {"look": sweep id, "night": bool}')
    if has_colour:
        colour = body["colour"]
        if colour not in COLOURS:
            raise ValueError("colour must be one of " + ", ".join(COLOURS))
        return "colour", colour, number(body.get("level", 1.0), "level", 0.0, 1.0)
    lid = body.get("look", body.get("scene"))
    if lid not in {item[0] for item in SWEEP}:
        raise ValueError("look must be one of " + ", ".join(item[0] for item in SWEEP))
    night = body.get("night", False)
    if not isinstance(night, bool):
        raise ValueError("night must be true or false")
    return "scene", lid, night


def accepts(fn: Any, name: str) -> bool:
    """Whether callable ``fn`` takes a keyword argument ``name``."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
