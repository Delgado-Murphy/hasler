"""Control primitives.

Six kinds cover every cabinet we have or expect:

``momentary``          spring-back key. Emits ``tap`` on release, ``hold`` at the
                       threshold while still held (and again every ``repeat_ms``
                       if set - a volume key that ramps while held), and
                       ``down`` the moment it is pressed (see below).
``latching_toggle``    independent stay-put switch. Emits ``on`` / ``off``.
``interlocked_bank``   mechanically ganged keys where exactly one is down.
                       Emits ``select`` / ``hold`` / ``clear``. Its ``keys:``
                       name each position (``k1``, ``pu`` ...); with
                       ``latching: false`` (a cabinet modified so the keys
                       spring back) each position is built as a ``momentary``
                       key of that id instead - see ``bank_keys``.
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

THE PRESS HINT
--------------
A tap is only a tap once the key is let go, so a lamp that waited for it
answered a press 150-300 ms late. Keys (momentary keys, and the keys of an
interlocked bank) also emit ``down`` from the first poll that reads the
contact closed (``DOWN_S``, no debounce: two agreeing polls would add 20 ms
and miss the lamp's 50 ms target). It is a hint for the pilot lamp only
(Actions answers it with the lamp's reply and nothing else): every action
still waits for its debounced tap, hold or select, one press hints once
however its contact bounces, and a glitch that never commits costs at most
one reply - never an action.
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
# The press hint (module docstring): a closed contact read this long is a
# key going down, for the lamp only. 0 = the first poll that sees it.
DOWN_S = 0.0
DEFAULT_BANK_SETTLE_S = 0.06
DEFAULT_OFF_HOLD_S = 5.0
DEFAULT_MEDIAN_N = 5
DEFAULT_HYSTERESIS_COUNTS = 250
DEFAULT_SELECTOR_BAND = 200      # counts a selector must clear a threshold by to move
DEFAULT_RANGE = (0, 26400)       # a 3.3 V rail at +/-4.096 V full scale
RAW_HISTORY = 256                # recent readings kept per analog control (~5 s at 50 Hz)

# The knob drift / loose-wiper detector (Pot.drift; the knob card's hint).
# ``past_end``: the median reading sits beyond the calibration's end by more
# than DRIFT_END_MARGIN for DRIFT_HOLD_S - the track's end moved, or the wiper
# wandered off it. The margin is max(3 x noise, DRIFT_END_HYSTERESIS x
# hysteresis): the shipped curves end up to ~40 counts inside the physical
# stops so 0 and 100 stay reachable, and that must never read as drift.
# ``wander``: the raw readings of the last DRIFT_WANDER_POLLS polls spread more
# than DRIFT_WANDER_HYSTERESIS x hysteresis while no change was committed -
# the median held still but the wiper didn't (a scratchy or loose contact).
# A ``jump`` of more than EXCURSION_FRACTION of the calibrated range between
# two consecutive medians is held for one poll (the mixer keeps its last
# committed level): a hand can't do that in 20 ms, a loose wiper can, and on
# the volume knob that excursion is full volume into a 1964 cone. The next
# poll agreeing commits it, so a real fast turn is at most 20 ms late.
DRIFT_HOLD_S = 3.0
DRIFT_END_HYSTERESIS = 3
DRIFT_WANDER_POLLS = 50
DRIFT_WANDER_HYSTERESIS = 3
EXCURSION_FRACTION = 0.25


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


class _PressHint:
    """The press hint (module docstring): which key went down, once per press.

    Fed what the raw contacts say is pressed - True for a single key, a
    position for a bank, None for nothing - it answers that value once it has
    read the same for ``down_s``, and not again until nothing has been
    pressed for ``clear_s`` (a bouncing contact is still the same press).
    """

    def __init__(self, down_s: float, clear_s: float) -> None:
        self.down_s = down_s
        self.clear_s = clear_s
        self._value: Any = None
        self._since = 0.0
        self._sent: Any = None

    def update(self, now: float, raw: Any) -> Any:
        if raw != self._value:
            self._value, self._since = raw, now
        if raw is None:
            if now - self._since >= self.clear_s:
                self._sent = None
            return None
        if raw != self._sent and now - self._since >= self.down_s:
            self._sent = raw
            return raw
        return None


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

    ``down_s`` (build_controls passes DOWN_S) adds ``down`` that long after
    the contact closes, before anything else (the press hint, module
    docstring); None, the constructor's default, leaves it out.
    """

    kind = "momentary"

    def __init__(
        self,
        id: str,
        pin: str,
        hold_s: float = DEFAULT_HOLD_S,
        bounce_s: float = DEFAULT_BOUNCE_S,
        repeat_s: float | None = None,
        down_s: float | None = None,
    ) -> None:
        super().__init__(id)
        self.pin = pin
        self.hold_s = hold_s
        self.repeat_s = repeat_s
        self._settled = _Settled(False, bounce_s)
        self._hint = _PressHint(min(down_s, bounce_s), bounce_s) if down_s is not None else None
        self._down_at = 0.0
        self._hold_fired = False
        self._last_hold = 0.0

    @property
    def down_since(self) -> float | None:
        """When this press went down (debounced), None while the key is up."""
        return self._down_at if self._settled.committed else None

    def update(self, now, digital, analog):
        events: list[Event] = []
        raw = bool(digital.get(self.pin, False))
        if self._hint is not None and self._hint.update(now, True if raw else None) \
                and not self._settled.committed:
            events.append(Event(self.id, "down"))
        changed = self._settled.update(now, raw)
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

    @property
    def hold_fired(self) -> bool:
        """Down, and this press already fired its hold (panel/combos.py)."""
        return bool(self._settled.committed and self._hold_fired)


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
        self._on_since: float | None = None
        self._unarmed_logged = False

    def update(self, now, digital, analog):
        events: list[Event] = []
        raw = digital.get(self.pin, False)
        changed = self._settled.update(now, raw)
        # Arm once the switch has read on for a debounce time, not on one sample:
        # the settled state starts on (the boot assumption), so a single glitch
        # would otherwise arm the shutdown on a ribbon that is really unplugged.
        if not raw:
            self._on_since = None
        elif self._on_since is None:
            self._on_since = now
        elif now - self._on_since >= self._settled._settle:
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
        down_s: float | None = None,
    ) -> None:
        super().__init__(id)
        self.pins = list(pins)
        self.hold_s = hold_s
        self._settled = _Settled(tuple([False] * len(pins)), settle_s)
        # The press hint (``down_s``, as Momentary's): a new key going down,
        # long before the vector settles.
        self._hint = _PressHint(min(down_s, settle_s), settle_s) if down_s is not None else None
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
        going = self._hint.update(now, self._decode(raw)) if self._hint is not None else None
        if going is not None and going != self._selected:
            events.append(Event(self.id, "down", going))
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


def bank_latching(spec: Mapping[str, Any]) -> bool:
    """An interlocked bank's keys stay down (the default) - False for a
    cabinet modified so they spring back like the HM's (``latching: false``,
    usually set per radio in /etc/hasler/variant.d/<variant>.yaml)."""
    return spec.get("latching", True) is not False


def bank_keys(spec: Mapping[str, Any]) -> list[dict]:
    """The keys of an interlocked bank, left to right: ``[{"id", "label"}]``.

    From the descriptor's ``keys:`` - one entry per pin, a bare id
    (``k3``, ``pu``) or ``{id, label}``. A bank without ``keys:`` names its
    positions ``<bank>_1`` .. ``<bank>_N``. The ids are what the action table,
    keys.yaml and the key page use: ``k3.tap`` is "key 3 selected", whether
    the key latches or springs back.
    """
    cid = spec.get("id") or "bank"
    pins = list(spec.get("pins") or [])
    raw = list(spec.get("keys") or [])
    out: list[dict] = []
    for i in range(len(pins)):
        item = raw[i] if i < len(raw) else None
        if isinstance(item, Mapping):
            kid, label = str(item.get("id") or f"{cid}_{i + 1}"), item.get("label")
        elif item:
            kid, label = str(item), None
        else:
            kid, label = f"{cid}_{i + 1}", str(i + 1)
        out.append({"id": kid, "label": str(label) if label else default_label(kid)})
    return out


def release_key(spec: Mapping[str, Any]) -> dict | None:
    """An interlocked bank's release key (key 0 on the Excelsior) as
    ``{"id", "label", "pin"}``, or None when it has none."""
    release = spec.get("release")
    if not isinstance(release, Mapping) or not release.get("pin"):
        return None
    kid = str(release.get("id") or f"{spec.get('id') or 'bank'}_release")
    return {"id": kid, "label": str(release.get("label") or default_label(kid)), "pin": release["pin"]}


def default_label(cid: str) -> str:
    """What a key is called when the descriptor doesn't say: ``k3`` -> ``3``,
    ``pu`` -> ``PU``."""
    if len(cid) > 1 and cid[0] == "k" and cid[1:].isdigit():
        return cid[1:]
    return cid.upper()


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
        # The drift detector (module constants above): what it found (None,
        # "past_end" or "wander"), since when the median has sat past the end,
        # the poll a change was last committed at, and a jump held one poll.
        self.drift: str | None = None
        self._past_end_since: float | None = None
        self._last_commit_poll = 0
        self._polls = 0
        self._held: int | None = None

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
        self._polls += 1
        counts = sorted(self._samples)[len(self._samples) // 2]
        committed = self._committed_counts
        self._watch_drift(now, counts, spec)
        # A new spec re-reads the knob at once; only the panel thread touches
        # _value_spec, so a respec racing this poll is simply seen next poll.
        if spec is self._value_spec and committed is not None and abs(counts - committed) < spec.hysteresis:
            self._held = None
            return []
        if spec is self._value_spec and committed is not None and self._is_jump(counts, committed, spec):
            # A full-scale excursion in one poll: hold the mixer where it was
            # until the next poll agrees (EXCURSION_FRACTION above).
            if self._held is None or abs(counts - self._held) >= spec.hysteresis:
                self._held = counts
                return []
        self._held = None
        self._committed_counts = counts
        self._value_spec = spec
        self._last_commit_poll = self._polls
        value = self._scale(counts, spec)
        if value == self._value:
            return []
        self._value = value
        return [Event(self.id, "change", value)]

    @staticmethod
    def _ends(spec: PotSpec) -> tuple[int, int]:
        """The calibrated range's low and high counts (the curve's ends, else the range)."""
        if spec.curve:
            return spec.curve[0][0], spec.curve[-1][0]
        lo, hi = spec.counts_range or DEFAULT_RANGE
        return (lo, hi) if lo <= hi else (hi, lo)

    def _is_jump(self, counts: int, committed: int, spec: PotSpec) -> bool:
        lo, hi = self._ends(spec)
        return abs(counts - committed) > EXCURSION_FRACTION * max(1, hi - lo)

    def _watch_drift(self, now: float, counts: int, spec: PotSpec) -> None:
        """Keep ``drift`` (module docstring: past_end / wander / None)."""
        doc = summarize(self.recent(15))
        noise = float(doc["noise"]) if doc else 0.0
        lo, hi = self._ends(spec)
        margin = max(3.0 * noise, float(DRIFT_END_HYSTERESIS * spec.hysteresis))
        beyond = counts < lo - margin or counts > hi + margin
        if beyond:
            if self._past_end_since is None:
                self._past_end_since = now
            if now - self._past_end_since >= DRIFT_HOLD_S:
                if self.drift != "past_end":
                    log.warning("%s: reads past its calibration (raw %d, calibrated %d..%d)", self.id, counts, lo, hi)
                self.drift = "past_end"
                return
        else:
            self._past_end_since = None
        recent = self.recent(DRIFT_WANDER_POLLS)
        quiet = self._polls - self._last_commit_poll >= DRIFT_WANDER_POLLS
        if (len(recent) >= DRIFT_WANDER_POLLS and quiet
                and max(recent) - min(recent) > DRIFT_WANDER_HYSTERESIS * spec.hysteresis):
            if self.drift != "wander":
                log.warning("%s: the wiper wanders while the knob is still (raw %d..%d)",
                            self.id, min(recent), max(recent))
            self.drift = "wander"
        elif self.drift == "past_end" and not beyond:
            self.drift = None
        elif self.drift == "wander" and not quiet:
            self.drift = None

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
            release = release_key({"id": cid, **spec})
            hold_s = spec.get("hold_ms", DEFAULT_HOLD_S * 1000) / 1000
            # Key 0's own hold (the Excelsior's: 5 s to shut down, the HM
            # tone knob's countdown), else the bank's.
            release_hold = (spec.get("release") or {}).get("hold_ms") if release else None
            if not bank_latching(spec):
                # A modified cabinet: the keys spring back. Each is a plain
                # momentary key (tap / hold / repeat, combinations) under its
                # bank id, key 0 first as it sits on the panel.
                if release:
                    controls.append(Momentary(release["id"], pin=release["pin"], down_s=DOWN_S,
                                              hold_s=release_hold / 1000 if release_hold else hold_s))
                for key, pin in zip(bank_keys({"id": cid, **spec}), spec["pins"]):
                    controls.append(Momentary(key["id"], pin=pin, hold_s=hold_s, down_s=DOWN_S))
                continue
            controls.append(
                InterlockedBank(
                    cid,
                    pins=spec["pins"],
                    settle_s=spec.get("settle_ms", DEFAULT_BANK_SETTLE_S * 1000) / 1000,
                    hold_s=spec.get("hold_ms", DEFAULT_HOLD_S * 1000) / 1000,
                    down_s=DOWN_S,
                )
            )
            # Key 0 on the Excelsior. It may have no contacts at all, in which
            # case Phase 0 says so and the bank's own `clear` covers it.
            if release:
                controls.append(Momentary(release["id"], pin=release["pin"], down_s=DOWN_S,
                                          hold_s=release_hold / 1000 if release_hold else DEFAULT_HOLD_S))
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
                    down_s=DOWN_S,
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
