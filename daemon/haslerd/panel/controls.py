"""Control primitives.

Five kinds cover every cabinet we have or expect:

``momentary``          spring-back key. Emits ``tap`` on release, ``hold`` at the
                       threshold while still held.
``latching_toggle``    independent stay-put switch. Emits ``on`` / ``off``.
``interlocked_bank``   mechanically ganged keys where exactly one is down.
                       Emits ``select`` / ``hold`` / ``clear``.
``rotary_switch``      N-position wafer switch. Emits ``position``.
``pot``                potentiometer on an ADC channel. Emits ``change``.
                       May carry an ``end_switch`` (a pot with a click at the
                       end-stop) which becomes its own control.

Every control is a pure function of (time, input snapshot) -> events, with time
passed in rather than read from the clock. That is what lets the whole panel be
tested on a laptop with no hardware and no sleeping.

THE TRAP THIS FILE EXISTS TO AVOID
----------------------------------
On an interlocked bank the outgoing key opens *before* the incoming key closes,
so there is a brief all-open window on every station change. Per-key edge
handlers fire a spurious "cleared" every single time. The fix is to debounce the
bank as a **vector**: no state is committed until the whole pattern has held
still for ``settle_ms``. The changeover gap is shorter than that, so it never
commits; a real release (key 0) is longer, so it does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

DEFAULT_HOLD_S = 0.8
DEFAULT_BOUNCE_S = 0.03
DEFAULT_BANK_SETTLE_S = 0.06
DEFAULT_OFF_HOLD_S = 5.0
DEFAULT_MEDIAN_N = 5
DEFAULT_HYSTERESIS_COUNTS = 250


@dataclass(frozen=True)
class Event:
    """Something the panel did. ``control`` is the id from the variant YAML."""

    control: str
    kind: str
    value: Any = None

    def __str__(self) -> str:
        return f"{self.control}.{self.kind}" + ("" if self.value is None else f"={self.value}")


class _Settled:
    """Holds a raw value until it has been stable for ``settle`` seconds.

    Works for a single bool or a whole tuple - which is exactly what the
    interlocked bank needs.
    """

    def __init__(self, value: Any, settle: float) -> None:
        self.committed = value
        self._candidate = value
        self._since = 0.0
        self._settle = settle

    def update(self, now: float, raw: Any) -> bool:
        """Feed a reading. Returns True if the committed value just changed."""
        if raw != self._candidate:
            self._candidate = raw
            self._since = now
            return False
        if self._candidate == self.committed:
            return False
        if now - self._since >= self._settle:
            self.committed = self._candidate
            return True
        return False


class Control:
    """Base: an id, and a way to turn an input snapshot into events."""

    kind = "control"

    def __init__(self, id: str) -> None:
        self.id = id

    def update(
        self, now: float, digital: Mapping[str, bool], analog: Mapping[int, int]
    ) -> list[Event]:
        raise NotImplementedError

    def state(self) -> Any:
        """Current committed state, for the probe display and MQTT."""
        return None


class Momentary(Control):
    """Spring-back key. ``tap`` on release; ``hold`` fires at the threshold.

    Hold fires *while the key is still down* rather than on release, so the
    action lands when you feel it - and a tap is then defined as "released
    before the hold fired".
    """

    kind = "momentary"

    def __init__(
        self,
        id: str,
        pin: str,
        hold_s: float = DEFAULT_HOLD_S,
        bounce_s: float = DEFAULT_BOUNCE_S,
    ) -> None:
        super().__init__(id)
        self.pin = pin
        self.hold_s = hold_s
        self._settled = _Settled(False, bounce_s)
        self._down_at = 0.0
        self._hold_fired = False

    def update(self, now, digital, analog):
        events: list[Event] = []
        changed = self._settled.update(now, digital.get(self.pin, False))
        if changed:
            if self._settled.committed:
                self._down_at = now
                self._hold_fired = False
            elif not self._hold_fired:
                events.append(Event(self.id, "tap"))
        if self._settled.committed and not self._hold_fired:
            if now - self._down_at >= self.hold_s:
                self._hold_fired = True
                events.append(Event(self.id, "hold"))
        return events

    def state(self):
        return self._settled.committed


class LatchingToggle(Control):
    """Independent stay-put switch. Emits ``on`` / ``off`` on settled change."""

    kind = "latching_toggle"

    def __init__(self, id: str, pin: str, bounce_s: float = DEFAULT_BOUNCE_S) -> None:
        super().__init__(id)
        self.pin = pin
        self._settled = _Settled(False, bounce_s)

    def update(self, now, digital, analog):
        if self._settled.update(now, digital.get(self.pin, False)):
            return [Event(self.id, "on" if self._settled.committed else "off")]
        return []

    def state(self):
        return self._settled.committed


class EndSwitch(Control):
    """The click at a pot's end-stop.

    Maintained, not momentary: it stays open while the knob is left. Emits
    ``off`` immediately (-> standby) and ``off_held`` after ``off_hold_s``
    (-> clean shutdown), which is how one switch gives both behaviours without
    any extra circuitry.
    """

    kind = "end_switch"

    def __init__(
        self,
        id: str,
        pin: str,
        off_hold_s: float = DEFAULT_OFF_HOLD_S,
        bounce_s: float = DEFAULT_BOUNCE_S,
    ) -> None:
        super().__init__(id)
        self.pin = pin
        self.off_hold_s = off_hold_s
        # Boot assumption: knob is on. A cold start with it off emits `off`.
        self._settled = _Settled(True, bounce_s)
        self._off_at = 0.0
        self._off_held_fired = False

    def update(self, now, digital, analog):
        events: list[Event] = []
        if self._settled.update(now, digital.get(self.pin, False)):
            if self._settled.committed:
                events.append(Event(self.id, "on"))
                self._off_held_fired = False
            else:
                self._off_at = now
                self._off_held_fired = False
                events.append(Event(self.id, "off"))
        if not self._settled.committed and not self._off_held_fired:
            if now - self._off_at >= self.off_hold_s:
                self._off_held_fired = True
                events.append(Event(self.id, "off_held"))
        return events

    def state(self):
        return self._settled.committed


class InterlockedBank(Control):
    """Mechanically ganged keys: exactly one down, or none.

    Debounced as a whole vector - see the module docstring. ``settle_ms`` must
    comfortably exceed the mechanical changeover gap and comfortably undershoot
    the shortest deliberate "all off" press. 60 ms suits the Excelsior; tune per
    cabinet against the real switches.
    """

    kind = "interlocked_bank"

    def __init__(
        self,
        id: str,
        pins: list[str],
        settle_s: float = DEFAULT_BANK_SETTLE_S,
        hold_s: float = DEFAULT_HOLD_S,
    ) -> None:
        super().__init__(id)
        self.pins = list(pins)
        self.hold_s = hold_s
        self._settled = _Settled(tuple([False] * len(pins)), settle_s)
        self._selected: int | None = None
        self._selected_at = 0.0
        self._hold_fired = False

    def _decode(self, vector: tuple[bool, ...]) -> int | None:
        down = [i for i, v in enumerate(vector) if v]
        # Two keys stably down is mechanically impossible on an interlocked
        # bank - treat it as a stuck key and hold the previous selection
        # rather than inventing one.
        return down[0] if len(down) == 1 else None

    def update(self, now, digital, analog):
        events: list[Event] = []
        raw = tuple(digital.get(p, False) for p in self.pins)
        if self._settled.update(now, raw):
            committed = self._settled.committed
            stuck = sum(committed) > 1
            index = self._decode(committed)
            if stuck:
                pass  # keep the previous selection; nothing sensible to emit
            elif index is None:
                if self._selected is not None:
                    self._selected = None
                    self._hold_fired = False
                    events.append(Event(self.id, "clear"))
            elif index != self._selected:
                self._selected = index
                self._selected_at = now
                self._hold_fired = False
                events.append(Event(self.id, "select", index))
        if self._selected is not None and not self._hold_fired:
            if now - self._selected_at >= self.hold_s:
                self._hold_fired = True
                events.append(Event(self.id, "hold", self._selected))
        return events

    def state(self):
        return self._selected


class RotarySwitch(Control):
    """N-position wafer switch read from a few contacts.

    ``pattern_map`` comes from Phase 0 metering: a dict of contact-pattern
    string ("10", "00", "01") to position index. Without one, falls back to
    one-hot with "nothing closed" as the last position.
    """

    kind = "rotary_switch"

    def __init__(
        self,
        id: str,
        pins: list[str],
        positions: int | None = None,
        pattern_map: dict[str, int] | None = None,
        bounce_s: float = DEFAULT_BOUNCE_S,
    ) -> None:
        super().__init__(id)
        self.pins = list(pins)
        self.positions = positions or (len(self.pins) + 1)
        self.pattern_map = pattern_map
        self._settled = _Settled(None, bounce_s)

    def _decode(self, vector: tuple[bool, ...]) -> int | None:
        if self.pattern_map is not None:
            return self.pattern_map.get("".join("1" if v else "0" for v in vector))
        down = [i for i, v in enumerate(vector) if v]
        if not down:
            return len(self.pins)
        if len(down) == 1:
            return down[0]
        return None

    def update(self, now, digital, analog):
        raw = self._decode(tuple(digital.get(p, False) for p in self.pins))
        if raw is None:
            return []
        if self._settled.update(now, raw):
            return [Event(self.id, "position", self._settled.committed)]
        return []

    def state(self):
        return self._settled.committed


class Pot(Control):
    """Potentiometer on an ADC channel, scaled to 0-100.

    Three stages, and all three matter on 60-year-old carbon tracks:

    1. **median-of-N** kills the scratch spikes a worn track throws off;
    2. **hysteresis** means a knob sitting still emits absolutely nothing;
    3. **curve** maps counts to 0-100 through measured points, which is how the
       audio taper - and the tone pot's post-detent-only range - get honoured.

    With ``backend: none`` there are no counts to read, so this emits nothing
    and the daemon runs happily with dead wheels.
    """

    kind = "pot"

    def __init__(
        self,
        id: str,
        channel: int,
        curve: list[tuple[int, float]] | None = None,
        counts_range: tuple[int, int] | None = None,
        taper: str = "linear",
        median_n: int = DEFAULT_MEDIAN_N,
        hysteresis_counts: int = DEFAULT_HYSTERESIS_COUNTS,
    ) -> None:
        super().__init__(id)
        self.channel = channel
        self.taper = taper
        self.median_n = max(1, median_n)
        self.hysteresis = hysteresis_counts
        self.curve = sorted(curve) if curve else None
        self.counts_range = counts_range
        self._samples: list[int] = []
        self._committed_counts: int | None = None
        self._value: float | None = None

    def _scale(self, counts: int) -> float:
        if self.curve:
            return _piecewise(self.curve, counts)
        lo, hi = self.counts_range or (0, 26400)
        if hi == lo:
            return 0.0
        frac = min(1.0, max(0.0, (counts - lo) / (hi - lo)))
        if self.taper == "log":
            # Audio-taper pots move slowly at the bottom and fast at the top.
            # Squaring the fraction is a serviceable inverse until Phase 0
            # measurements replace this with a real `curve`.
            frac = frac * frac
        return round(frac * 100.0, 1)

    def update(self, now, digital, analog):
        if self.channel not in analog:
            return []
        self._samples.append(analog[self.channel])
        if len(self._samples) > self.median_n:
            self._samples.pop(0)
        if len(self._samples) < self.median_n:
            return []
        counts = sorted(self._samples)[len(self._samples) // 2]
        if self._committed_counts is not None:
            if abs(counts - self._committed_counts) < self.hysteresis:
                return []
        self._committed_counts = counts
        value = self._scale(counts)
        if value == self._value:
            return []
        self._value = value
        return [Event(self.id, "change", value)]

    def state(self):
        return self._value


def _piecewise(points: list[tuple[int, float]], x: int) -> float:
    """Piecewise-linear lookup through measured (counts, value) points."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return round(y0 + (y1 - y0) * (x - x0) / (x1 - x0), 1)
    return points[-1][1]


_KINDS = {
    "momentary": Momentary,
    "latching_toggle": LatchingToggle,
    "interlocked_bank": InterlockedBank,
    "rotary_switch": RotarySwitch,
    "pot": Pot,
    "end_switch": EndSwitch,
}


def build_controls(specs: list[dict]) -> list[Control]:
    """Turn the ``controls:`` list from a variant YAML into control objects."""
    controls: list[Control] = []
    for spec in specs:
        spec = dict(spec)
        kind = spec.pop("kind", None)
        cid = spec.pop("id", None)
        if kind not in _KINDS:
            raise ValueError(f"unknown control kind {kind!r} for id {cid!r}")
        if not cid:
            raise ValueError(f"control of kind {kind!r} has no id")

        if kind == "interlocked_bank":
            release = spec.pop("release", None)
            controls.append(
                InterlockedBank(
                    cid,
                    pins=spec["pins"],
                    settle_s=spec.get("settle_ms", DEFAULT_BANK_SETTLE_S * 1000) / 1000,
                    hold_s=spec.get("hold_ms", DEFAULT_HOLD_S * 1000) / 1000,
                )
            )
            # Key 0 on the Excelsior. It may have no contacts at all, in which
            # case Phase 0 says so and the bank's own `clear` covers it.
            if release:
                controls.append(Momentary(f"{cid}_release", pin=release["pin"]))
            continue

        if kind == "pot":
            end = spec.pop("end_switch", None)
            controls.append(
                Pot(
                    cid,
                    channel=spec["channel"],
                    curve=[tuple(p) for p in spec["curve"]] if spec.get("curve") else None,
                    counts_range=tuple(spec["range"]) if spec.get("range") else None,
                    taper=spec.get("taper", "linear"),
                    median_n=spec.get("median_n", DEFAULT_MEDIAN_N),
                    hysteresis_counts=spec.get("hysteresis_counts", DEFAULT_HYSTERESIS_COUNTS),
                )
            )
            if end:
                controls.append(
                    EndSwitch(
                        end.get("id", f"{cid}_switch"),
                        pin=end["pin"],
                        off_hold_s=end.get("off_hold_ms", DEFAULT_OFF_HOLD_S * 1000) / 1000,
                    )
                )
            continue

        if kind == "momentary":
            controls.append(
                Momentary(
                    cid, pin=spec["pin"], hold_s=spec.get("hold_ms", DEFAULT_HOLD_S * 1000) / 1000
                )
            )
        elif kind == "latching_toggle":
            controls.append(LatchingToggle(cid, pin=spec["pin"]))
        elif kind == "rotary_switch":
            controls.append(
                RotarySwitch(
                    cid,
                    pins=spec["pins"],
                    positions=spec.get("positions"),
                    pattern_map=spec.get("map"),
                )
            )
    return controls
