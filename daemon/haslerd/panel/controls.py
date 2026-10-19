"""Control primitives.

Six kinds cover every cabinet we have or expect:

``momentary``          spring-back key. Emits ``tap`` on release, ``hold`` at the
                       threshold while still held (and again every ``repeat_ms``
                       if set - a volume key that ramps while held).
``latching_toggle``    independent stay-put switch. Emits ``on`` / ``off``.
``interlocked_bank``   mechanically ganged keys where exactly one is down.
                       Emits ``select`` / ``hold`` / ``clear``.
``rotary_switch``      N-position wafer switch. Emits ``position``.
``pot``                potentiometer on an ADC channel. Emits ``change``.
                       May carry an ``end_switch`` (a pot with a click at the
                       end-stop) which becomes its own control.
``analog_selector``    N-position switch wired as a divider onto an ADC
                       channel, decoded by ``thresholds``. Emits ``position``.

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

import dataclasses
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping

from .calibrate import curve_direction, summarize

log = logging.getLogger(__name__)

DEFAULT_HOLD_S = 0.8
DEFAULT_BOUNCE_S = 0.03
DEFAULT_BANK_SETTLE_S = 0.06
DEFAULT_OFF_HOLD_S = 5.0
DEFAULT_MEDIAN_N = 5
DEFAULT_HYSTERESIS_COUNTS = 250
DEFAULT_SELECTOR_BAND = 200      # counts a selector must clear a threshold by to move
DEFAULT_RANGE = (0, 26400)       # a 3.3 V rail at +/-4.096 V full scale
RAW_HISTORY = 256                # recent readings kept per analog control (~5 s at 50 Hz)


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

    ``repeat_s`` makes ``hold`` fire again at that interval for as long as the
    key stays down. Off by default: a hold that starts a scene must not repeat.
    """

    kind = "momentary"

    def __init__(
        self,
        id: str,
        pin: str,
        hold_s: float = DEFAULT_HOLD_S,
        bounce_s: float = DEFAULT_BOUNCE_S,
        repeat_s: float | None = None,
    ) -> None:
        super().__init__(id)
        self.pin = pin
        self.hold_s = hold_s
        self.repeat_s = repeat_s
        self._settled = _Settled(False, bounce_s)
        self._down_at = 0.0
        self._hold_fired = False
        self._last_hold = 0.0

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
                self._last_hold = now
                events.append(Event(self.id, "hold"))
        elif self._settled.committed and self.repeat_s and now - self._last_hold >= self.repeat_s:
            self._last_hold = now
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

    ``off_held`` is only armed once the switch has read on (closed) since the
    daemon started. An unplugged or broken ribbon reads exactly like a knob
    turned off, and must not shut the Pi down 5 s after every boot.
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
        self._armed = False
        self._unarmed_logged = False

    def update(self, now, digital, analog):
        events: list[Event] = []
        raw = digital.get(self.pin, False)
        changed = self._settled.update(now, raw)
        if raw and self._settled.committed:
            self._armed = True
        if changed:
            if self._settled.committed:
                events.append(Event(self.id, "on"))
                self._off_held_fired = False
            else:
                self._off_at = now
                self._off_held_fired = False
                events.append(Event(self.id, "off"))
                if not self._armed and not self._unarmed_logged:
                    self._unarmed_logged = True
                    log.info(
                        "%s: end switch off since start - shutdown not armed until "
                        "it reads on (check the ribbon if the knob is on)", self.id
                    )
        if self._armed and not self._settled.committed and not self._off_held_fired:
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


class _RawHistory:
    """The last RAW_HISTORY readings an analog control saw in normal polling.

    The calibration page reads these instead of the ADC: no extra I2C traffic,
    and nothing to collide with the panel loop. The panel thread appends, a web
    thread copies; a copy that races an append is simply taken again.

    The ADS1115 converts one channel per poll, so with two pots each reading
    repeats about twice - harmless for a median, and why ``sample`` counts
    polls, not conversions.
    """

    def _init_history(self) -> None:
        self._recent: deque[int] = deque(maxlen=RAW_HISTORY)

    def _seen(self, counts: int) -> None:
        self._recent.append(int(counts))

    def recent(self, n: int | None = None) -> list[int]:
        for _ in range(5):
            try:
                values = list(self._recent)
                break
            except RuntimeError:        # deque mutated during the copy
                continue
        else:
            values = []
        return values if n is None else values[-max(1, int(n)):]

    @property
    def raw(self) -> int | None:
        """The latest reading, or None before the first."""
        values = self.recent(1)
        return values[0] if values else None

    def sample(self, n: int = 15) -> dict | None:
        """Median / min / max / noise / stable of the last ``n`` polls (None if
        none). Not ``stable`` until there are ``n`` readings to judge by."""
        doc = summarize(self.recent(n))
        if doc is not None:
            doc["stable"] = doc["stable"] and doc["count"] >= max(1, int(n))
        return doc


@dataclass(frozen=True)
class PotSpec:
    """Everything that turns counts into 0-100. Swapped whole by ``Pot.respec``."""

    curve: tuple[tuple[int, float], ...] | None = None
    counts_range: tuple[int, int] | None = None
    taper: str = "linear"
    hysteresis: int = DEFAULT_HYSTERESIS_COUNTS
    direction: str = "normal"


_RESPEC_FIELDS = {"curve", "range", "counts_range", "taper", "hysteresis_counts", "direction"}


class Pot(_RawHistory, Control):
    """Potentiometer on an ADC channel, scaled to 0-100.

    Three stages, and all three matter on 60-year-old carbon tracks:

    1. **median-of-N** kills the scratch spikes a worn track throws off;
    2. **hysteresis** means a knob sitting still emits absolutely nothing;
    3. **curve** maps counts to 0-100 through measured points, which is how the
       audio taper - and the tone pot's post-detent-only range - get honoured.

    With ``backend: none`` there are no counts to read, so this emits nothing
    and the daemon runs happily with dead wheels.

    The scaling lives in one frozen ``PotSpec`` that ``respec`` replaces with a
    single reference swap, so the calibration page can change it from a web
    thread while the panel loop is mid-poll: an update sees the old spec or the
    new one, never half of each.
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
        direction: str | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__(id)
        self.channel = channel
        self.median_n = max(1, median_n)
        self.label = label
        self.end_switch_id: str | None = None     # set by build_controls
        curve_t = tuple(sorted((int(r), float(v)) for r, v in curve)) if curve else None
        self._spec = PotSpec(
            curve=curve_t,
            counts_range=tuple(counts_range) if counts_range else None,
            taper=taper,
            hysteresis=int(hysteresis_counts),
            direction=direction or curve_direction(curve_t),
        )
        self._samples: list[int] = []
        self._committed_counts: int | None = None
        self._value: float | None = None
        self._value_spec: PotSpec | None = None     # the spec _value was scaled under
        self._init_history()

    # Read-only views of the live spec.
    @property
    def spec(self) -> PotSpec:
        return self._spec

    @property
    def curve(self) -> list[tuple[int, float]] | None:
        return list(self._spec.curve) if self._spec.curve else None

    @property
    def counts_range(self) -> tuple[int, int] | None:
        return self._spec.counts_range

    @property
    def taper(self) -> str:
        return self._spec.taper

    @property
    def hysteresis(self) -> int:
        return self._spec.hysteresis

    @property
    def direction(self) -> str:
        return self._spec.direction

    def respec(self, **fields: Any) -> PotSpec:
        """Change the scaling live: any of ``curve``, ``range`` (or
        ``counts_range``), ``taper``, ``hysteresis_counts``, ``direction`` -
        the variant YAML's names. ``None`` clears ``curve`` / ``range`` and
        restores the default for the rest.

        The next poll re-reads the knob under the new spec (so a new curve is
        heard at once, not at the next turn) and emits ``change`` if the value
        moved. Safe to call from any thread.
        """
        unknown = set(fields) - _RESPEC_FIELDS
        if unknown:
            raise ValueError(f"{self.id}: can't respec {', '.join(sorted(unknown))}")
        spec = self._spec
        changes: dict[str, Any] = {}
        if "curve" in fields:
            curve = fields["curve"]
            changes["curve"] = tuple(sorted((int(r), float(v)) for r, v in curve)) if curve else None
        for key in ("range", "counts_range"):
            if key in fields:
                rng = fields[key]
                changes["counts_range"] = (int(rng[0]), int(rng[1])) if rng else None
        if "taper" in fields:
            changes["taper"] = fields["taper"] or "linear"
        if "hysteresis_counts" in fields:
            h = fields["hysteresis_counts"]
            changes["hysteresis"] = DEFAULT_HYSTERESIS_COUNTS if h is None else max(1, int(h))
        if "direction" in fields or "curve" in changes:
            d = fields.get("direction")
            if d not in (None, "normal", "reversed"):
                raise ValueError(f"{self.id}: direction must be normal or reversed")
            changes["direction"] = d or curve_direction(changes.get("curve", spec.curve))
        new = dataclasses.replace(spec, **changes)
        if new != spec:
            self._spec = new
            log.info("%s: respecced (curve %s, hysteresis %s, %s)", self.id,
                     "measured" if new.curve else new.taper, new.hysteresis, new.direction)
        return new

    def respec_from_spec(self, spec: Mapping[str, Any]) -> PotSpec:
        """Apply a whole variant ``pot`` spec (shipped, or shipped + overlay):
        every scaling field it leaves out goes back to its default."""
        return self.respec(
            curve=spec.get("curve"),
            range=spec.get("range"),
            taper=spec.get("taper"),
            hysteresis_counts=spec.get("hysteresis_counts"),
            direction=spec.get("direction"),
        )

    def reconfigure(self, new: "Pot") -> None:
        """Take ``new``'s scaling (variant.Panel.apply_specs, after an overlay
        change), keeping this pot's live reading and history."""
        self.median_n = new.median_n
        self.label = new.label
        self._spec = new.spec

    def scale(self, counts: int) -> float:
        """Counts -> 0-100 under the live spec."""
        return self._scale(counts, self._spec)

    @staticmethod
    def _scale(counts: int, spec: PotSpec) -> float:
        if spec.curve:
            return _piecewise(spec.curve, counts)
        lo, hi = spec.counts_range or DEFAULT_RANGE
        if hi == lo:
            return 0.0
        frac = min(1.0, max(0.0, (counts - lo) / (hi - lo)))
        if spec.taper == "log":
            # Audio-taper pots move slowly at the bottom and fast at the top.
            # Squaring the fraction is a serviceable inverse until Phase 0
            # measurements replace this with a real `curve`.
            frac = frac * frac
        return round(frac * 100.0, 1)

    def update(self, now, digital, analog):
        if self.channel not in analog:
            return []
        spec = self._spec          # one read: old or new, never a mix
        self._seen(analog[self.channel])
        self._samples.append(analog[self.channel])
        if len(self._samples) > self.median_n:
            self._samples.pop(0)
        if len(self._samples) < self.median_n:
            return []
        counts = sorted(self._samples)[len(self._samples) // 2]
        committed = self._committed_counts
        # A new spec re-reads the knob at once; only the panel thread touches
        # _value_spec, so a respec racing this poll is simply seen next poll.
        if spec is self._value_spec and committed is not None and abs(counts - committed) < spec.hysteresis:
            return []
        self._committed_counts = counts
        self._value_spec = spec
        value = self._scale(counts, spec)
        if value == self._value:
            return []
        self._value = value
        return [Event(self.id, "change", value)]

    def state(self):
        return self._value


class AnalogSelector(_RawHistory, Control):
    """N-position switch on one ADC channel (a resistor ladder on the wafer).

    ``thresholds`` are the N-1 counts midway between neighbouring positions,
    in position order - rising or falling, as the ladder runs (see
    calibrate.fit_selector). Without them the channel's range is split evenly.
    A reading must clear the threshold it crosses by ``band`` counts before the
    position moves, so a wiper resting on a boundary doesn't chatter.

    The Excelsior's ``speaker_sel`` is a ``rotary_switch`` on digital contacts,
    not this: it has nothing to calibrate.
    """

    kind = "analog_selector"

    def __init__(
        self,
        id: str,
        channel: int,
        positions: int | None = None,
        thresholds: list[int] | None = None,
        band: int = DEFAULT_SELECTOR_BAND,
        bounce_s: float = DEFAULT_BOUNCE_S,
        label: str | None = None,
    ) -> None:
        super().__init__(id)
        self.channel = channel
        self.label = label
        self.positions = positions or (len(thresholds) + 1 if thresholds else 2)
        self._thresholds = self._even(self.positions) if not thresholds else [int(t) for t in thresholds]
        self.band = band
        self._settled = _Settled(None, bounce_s)
        self._init_history()

    @staticmethod
    def _even(positions: int) -> list[int]:
        lo, hi = DEFAULT_RANGE
        step = (hi - lo) / positions
        return [int(lo + step * (i + 1)) for i in range(positions - 1)]

    @property
    def thresholds(self) -> list[int]:
        return list(self._thresholds)

    def respec(self, thresholds: list[int] | None = None) -> None:
        """New thresholds (None = even split), swapped in whole. Any thread."""
        new = self._even(self.positions) if not thresholds else [int(t) for t in thresholds]
        if len(new) != self.positions - 1:
            raise ValueError(f"{self.id}: {self.positions} positions need {self.positions - 1} thresholds")
        self._thresholds = new

    def respec_from_spec(self, spec: Mapping[str, Any]) -> None:
        self.respec(spec.get("thresholds"))

    def reconfigure(self, new: "AnalogSelector") -> None:
        """Take ``new``'s thresholds and band (variant.Panel.apply_specs)."""
        if new.positions != self.positions:
            raise ValueError(f"{self.id}: the number of positions needs a restart")
        self.band = new.band
        self.label = new.label
        self._thresholds = new.thresholds

    def decode(self, counts: int, current: int | None = None) -> int:
        thresholds = self._thresholds
        rising = len(thresholds) < 2 or thresholds[-1] > thresholds[0]
        pos = sum(1 for t in thresholds if (counts > t if rising else counts < t))
        if current is not None and pos != current and 0 <= current < self.positions:
            lo, hi = sorted((pos, current))
            crossed = thresholds[lo:hi]
            if any(abs(counts - t) < self.band for t in crossed):
                return current
        return pos

    def update(self, now, digital, analog):
        if self.channel not in analog:
            return []
        counts = analog[self.channel]
        self._seen(counts)
        if self._settled.update(now, self.decode(counts, self._settled.committed)):
            return [Event(self.id, "position", self._settled.committed)]
        return []

    def state(self):
        return self._settled.committed


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
    "analog_selector": AnalogSelector,
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
            pot = Pot(
                cid,
                channel=spec["channel"],
                curve=[tuple(p) for p in spec["curve"]] if spec.get("curve") else None,
                counts_range=tuple(spec["range"]) if spec.get("range") else None,
                taper=spec.get("taper") or "linear",
                median_n=spec.get("median_n", DEFAULT_MEDIAN_N),
                hysteresis_counts=spec.get("hysteresis_counts", DEFAULT_HYSTERESIS_COUNTS),
                direction=spec.get("direction"),
                label=spec.get("label"),
            )
            controls.append(pot)
            if end:
                pot.end_switch_id = end.get("id", f"{cid}_switch")
                controls.append(
                    EndSwitch(
                        pot.end_switch_id,
                        pin=end["pin"],
                        off_hold_s=end.get("off_hold_ms", DEFAULT_OFF_HOLD_S * 1000) / 1000,
                    )
                )
            continue

        if kind == "analog_selector":
            controls.append(
                AnalogSelector(
                    cid,
                    channel=spec["channel"],
                    positions=spec.get("positions"),
                    thresholds=spec.get("thresholds"),
                    band=spec.get("band_counts", DEFAULT_SELECTOR_BAND),
                    label=spec.get("label"),
                )
            )
            continue

        if kind == "momentary":
            controls.append(
                Momentary(
                    cid,
                    pin=spec["pin"],
                    hold_s=spec.get("hold_ms", DEFAULT_HOLD_S * 1000) / 1000,
                    repeat_s=spec["repeat_ms"] / 1000 if spec.get("repeat_ms") else None,
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
