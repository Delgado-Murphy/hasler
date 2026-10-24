"""Key combinations: two keys held down together.

The owner's "hold 1 and 6 together to pair a phone" (not neighbours: on
the Hasler HM a held key blocks the one beside it). Panel.poll runs every
momentary key as usual and then hands its events through ``ComboWatch``,
which reads the same keys' debounced states and

* emits ``Event("k1+k2", "combo")`` once both keys of a configured pair have
  been down together for that combination's ``hold_s``;
* swallows both keys' own ``tap`` and ``hold`` (and a volume key's repeat
  ramp) from the moment the second key goes down until each key is let go,
  whether the combination completes or not - letting go early does nothing.

The rules, in the order they matter:

1. A combination only starts while neither key has fired its own hold: press
   k1, wait for its hold (a scene, the volume ramp), then press k2 and both
   keys do what they always did.
2. Only configured pairs start one. Keys in no combination - and pairs that
   are not a combination - behave exactly as without this module; with no
   combinations configured Panel.poll never calls it.
3. Once one is forming, any other key of a combination pressed meanwhile is
   swallowed too and changes nothing (a third finger is ignored).
4. Contact bounce: a key counts as let go only after it has read up for
   ``RELEASE_S`` (the keys' own 30 ms debounce lets a 40 ms flicker through,
   and the hold timer must not restart on it).

Like the controls, it is a pure function of (time, states): no clock, no
sleeping, so it is tested on a laptop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from .controls import Control, Event, Momentary

DEFAULT_COMBO_HOLD_S = 1.0
COMBO_HOLD_RANGE = (0.5, 3.0)
RELEASE_S = 0.08        # up this long = let go; shorter is a bouncing contact
MAX_COMBOS = 8


def combo_id(keys: Iterable[str]) -> str:
    """``k1+k2``. Callers pass the keys in descriptor order."""
    return "+".join(keys)


@dataclass(frozen=True)
class Combo:
    keys: tuple[str, str]       # descriptor order
    hold_s: float = DEFAULT_COMBO_HOLD_S

    @property
    def id(self) -> str:
        return combo_id(self.keys)


class ComboWatch:
    """Two-key combinations over a panel's momentary keys."""

    def __init__(self, controls: Sequence[Control], combos: Iterable[Mapping] = ()) -> None:
        self._keys: dict[str, Momentary] = {c.id: c for c in controls if isinstance(c, Momentary)}
        self._order = {cid: i for i, cid in enumerate(self._keys)}
        # (combos, their keys): one reference, swapped whole by configure.
        self._conf: tuple[tuple[Combo, ...], frozenset[str]] = ((), frozenset())
        # Per key: the poll it was first seen up since it was last down (None
        # while down). Up seen at one poll and still up RELEASE_S later = let
        # go: a loop that stalled in a slow action never makes a held key look
        # released.
        self._up_since: dict[str, float | None] = {}
        # The combination being formed or held: its keys, the pair, when the
        # second key went down, and whether it fired.
        self._claimed: set[str] = set()
        self._pair: Combo | None = None
        self._since = 0.0
        self._fired = False
        self.configure(combos)

    def configure(self, combos: Iterable[Mapping]) -> None:
        """A new list of ``{"keys": [a, b], "hold_s": s, ...}`` (extra fields,
        the action, are ignored here). Pairs naming a key this panel doesn't
        have as a momentary are skipped. Swapped whole; safe from any thread."""
        out: list[Combo] = []
        for raw in combos or ():
            keys = list(raw.get("keys") or ())
            if len(keys) != 2 or keys[0] == keys[1] or not all(k in self._keys for k in keys):
                continue
            keys.sort(key=self._order.__getitem__)
            hold = raw.get("hold_s", DEFAULT_COMBO_HOLD_S)
            try:
                hold = float(hold)
            except (TypeError, ValueError):
                hold = DEFAULT_COMBO_HOLD_S
            lo, hi = COMBO_HOLD_RANGE
            out.append(Combo((keys[0], keys[1]), min(hi, max(lo, hold))))
        self._conf = (tuple(out), frozenset(k for c in out for k in c.keys))

    @property
    def combos(self) -> tuple[Combo, ...]:
        return self._conf[0]

    @property
    def active(self) -> bool:
        return bool(self._conf[0])

    def _held(self, cid: str, now: float) -> bool:
        up = self._up_since.get(cid, float("-inf"))
        return up is None or now - up < RELEASE_S

    @staticmethod
    def _find(combos: tuple[Combo, ...], a: str, b: str) -> Combo | None:
        pair = {a, b}
        for combo in combos:
            if set(combo.keys) == pair:
                return combo
        return None

    def filter(self, now: float, events: list[Event]) -> list[Event]:
        """The panel's events for this poll, with combination keys' own
        events taken out while they are claimed and a ``combo`` event added
        when one completes."""
        combos, members = self._conf
        if not combos:
            return events
        newly_down: list[str] = []
        for cid in members:
            if self._keys[cid].state():
                if not self._held(cid, now):
                    newly_down.append(cid)
                self._up_since[cid] = None
            elif self._up_since.get(cid, float("-inf")) is None:
                self._up_since[cid] = now
        held = {cid for cid in members if self._held(cid, now)}
        out_extra: list[Event] = []

        if self._claimed:
            # Swallow a third key, and a let-go key's tap, until each is up.
            self._claimed |= set(newly_down)
            pair = self._pair
            if pair is not None and not self._fired:
                if all(k in held for k in pair.keys):
                    if now - self._since >= pair.hold_s:
                        self._fired = True
                        out_extra.append(Event(pair.id, "combo"))
                else:
                    self._pair = None       # let go early: nothing happens
            # The tap a let-go key emits this very poll is still swallowed:
            # it only leaves the claim once up for RELEASE_S.
            filtered = [e for e in events if e.control not in self._claimed]
            self._claimed &= held
            if not self._claimed:
                self._pair, self._fired = None, False
            return filtered + out_extra

        # Nothing forming: does a second key just make a configured pair with
        # one that is down and hasn't fired its own hold?
        if newly_down:
            ready = sorted(
                (cid for cid in held if not self._keys[cid].hold_fired),
                key=self._order.__getitem__,
            )
            for i, a in enumerate(ready):
                for b in ready[i + 1:]:
                    if a not in newly_down and b not in newly_down:
                        continue
                    combo = self._find(combos, a, b)
                    if combo is not None:
                        self._pair, self._since, self._fired = combo, now, False
                        self._claimed = set(combo.keys) | (set(newly_down) & held)
                        return [e for e in events if e.control not in self._claimed]
        return events

    def forming(self) -> str | None:
        """The combination being held but not yet fired (for the probe)."""
        pair = self._pair
        return pair.id if pair is not None and not self._fired else None
