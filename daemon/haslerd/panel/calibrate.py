"""Knob calibration: turn a handful of measured positions into a pot curve.

Pure functions, no hardware and no clock - the web page's guided flow
(``/api/knobs``) feeds them what the owner measured, and the tests feed them
what we measured by hand on 19 Oct 2026.

THE POSITION CONVENTION
-----------------------
A point is ``(position, raw)``. ``position`` runs 0..1 **from the quiet end**:
0 = the knob fully down (as quiet / as little treble as it goes), 1 = fully
up, 0.5 = half way round *as the owner turns it*. It says nothing about which
end of the track reads low: that is ``direction``, worked out from the ends -
``normal`` when raw rises towards the full end, ``reversed`` when it falls
(the Hasler's volume pot: low counts at the clockwise, loud end).

The curve's value at a point is simply ``position * 100`` in both directions;
a reversed pot just gives a falling curve, which ``controls._piecewise`` is
happy with once the points are sorted by raw.

What the hand calibration taught us, and so what this does:

* owners mis-place points - a point that breaks monotonicity is flagged for a
  redo (``Fit.redo``, indices into the list given), never silently used;
* estimated points were badly wrong - nothing is guessed: the curve goes only
  through measured points and ``_piecewise`` interpolates between them;
* both ends are pulled slightly inside the measured extremes, so silence and
  full are reachable through the wiper's noise;
* a fixed 250-count dead zone is useless once most of the turn lives in ~900
  counts - hysteresis comes from the flattest segment (about one knob unit
  there) and never sits below three times the sample noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

DIRECTIONS = ("auto", "normal", "reversed")
DEFAULT_NOISE_COUNTS = 10.0     # the bench wiper's +-10 counts (median-of-15 readings)
MIN_INTERIOR = 2                # points besides the two ends
HYSTERESIS_UNITS = 1.0          # knob units (of 100) per dead zone, at the flattest segment
NOISE_FACTOR = 3.0              # the dead zone never sits below this many noise widths
STEP_WARN_UNITS = 2.5           # warn when the noise floor makes steps coarser than this
END_MARGIN_MAX = 0.25           # an end moves inward at most this share of its segment
MIN_END_SPAN_NOISE = 20.0       # ends closer than this many noise widths: no knob there
STABLE_SPREAD_COUNTS = 60       # a sample whose max-min is within this is "held still"
SELECTOR_GAP_NOISE = 6.0        # selector positions closer than this many noise widths: warn


@dataclass
class Fit:
    """A proposed pot calibration. Nothing is saved until the owner says so."""

    curve: list[list[float]] | None    # [[raw, value], ...] sorted by raw; None if unusable
    direction: str                     # normal | reversed
    hysteresis_counts: int
    warnings: list[str] = field(default_factory=list)
    redo: list[int] = field(default_factory=list)   # indices into the points given

    @property
    def ok(self) -> bool:
        return self.curve is not None and not self.redo

    def to_doc(self) -> dict:
        return {
            "curve": self.curve,
            "direction": self.direction,
            "hysteresis_counts": self.hysteresis_counts,
            "warnings": list(self.warnings),
            "redo": list(self.redo),
        }


def summarize(raws: Sequence[int]) -> dict | None:
    """Median / min / max / noise of some raw readings, or None for none.

    ``noise`` is the half-width of the readings with the outermost tenth
    trimmed from each side (one each once there are 8+), so a single scratch
    doesn't count as noise. ``stable`` = the whole spread is within
    STABLE_SPREAD_COUNTS: the owner is holding the knob still.
    """
    if not raws:
        return None
    ordered = sorted(int(r) for r in raws)
    n = len(ordered)
    trim = max(1, n // 10) if n >= 8 else 0
    core = ordered[trim:n - trim] if trim else ordered
    return {
        "raw": ordered[n // 2],
        "min": ordered[0],
        "max": ordered[-1],
        "noise": round((core[-1] - core[0]) / 2.0, 1),
        "stable": ordered[-1] - ordered[0] <= STABLE_SPREAD_COUNTS,
        "count": n,
    }


def _points(points: Iterable) -> list[tuple[float, int]]:
    """Validate ``[(position, raw), ...]`` (or {"position", "raw"} dicts)."""
    out: list[tuple[float, int]] = []
    for i, p in enumerate(points):
        if isinstance(p, dict):
            pos, raw = p.get("position"), p.get("raw")
        else:
            try:
                pos, raw = p
            except (TypeError, ValueError):
                raise ValueError(f"point {i} must be (position, raw)") from None
        if isinstance(pos, bool) or not isinstance(pos, (int, float)) or not 0.0 <= pos <= 1.0:
            raise ValueError(f"point {i}: position must be a number from 0 to 1")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw or raw < 0:
            raise ValueError(f"point {i}: raw must be a count of 0 or more")
        out.append((float(pos), int(round(raw))))
    positions = [p for p, _ in out]
    if len(set(positions)) != len(positions):
        raise ValueError("two points have the same position")
    if 0.0 not in positions or 1.0 not in positions:
        raise ValueError("measure both ends: position 0 (fully down) and 1 (fully up)")
    if len(out) - 2 < MIN_INTERIOR:
        raise ValueError(f"measure at least {MIN_INTERIOR} positions between the ends")
    return out


def _on_every_best_chain(raws: list[int], sign: int, tol: float) -> set[int]:
    """Indices (in position order) on every longest chain from first to last
    whose raws step by more than ``tol`` in direction ``sign``.

    The chain must keep both ends. A point off every longest chain is a
    mis-placement; one that is on some but not all is half of a tie (two
    points that disagree - either could be the wrong one), so it is not
    trusted either.
    """
    n = len(raws)

    def follows(a: int, b: int) -> bool:
        return (raws[b] - raws[a]) * sign > tol

    NEG = -1
    fwd = [NEG] * n                     # longest chain 0 .. i
    fwd[0] = 1
    for i in range(1, n):
        best = max((fwd[j] for j in range(i) if fwd[j] > 0 and follows(j, i)), default=NEG)
        fwd[i] = best + 1 if best > 0 else NEG
    back = [NEG] * n                    # longest chain i .. n-1
    back[-1] = 1
    for i in range(n - 2, -1, -1):
        best = max((back[j] for j in range(i + 1, n) if back[j] > 0 and follows(i, j)), default=NEG)
        back[i] = best + 1 if best > 0 else NEG
    total = fwd[-1]
    if total <= 0:
        return set()
    on_some = [i for i in range(n) if fwd[i] > 0 and back[i] > 0 and fwd[i] + back[i] - 1 == total]
    by_rank: dict[int, list[int]] = {}
    for i in on_some:
        by_rank.setdefault(fwd[i], []).append(i)
    return {members[0] for members in by_rank.values() if len(members) == 1}


def fit_pot(points: Iterable, direction: str = "auto", noise: float | None = None) -> Fit:
    """Fit a pot from measured ``(position 0..1 from the quiet end, raw)`` points.

    ``noise`` is the reading noise in counts (the sample endpoint's ``noise``;
    DEFAULT_NOISE_COUNTS if not known). Raises ValueError for input that
    can't be a measurement at all; a measurement that is merely wrong comes
    back with ``redo`` set.
    """
    if direction not in DIRECTIONS:
        raise ValueError("direction must be auto, normal or reversed")
    pts = _points(points)
    noise = DEFAULT_NOISE_COUNTS if noise is None else max(0.0, float(noise))
    order = sorted(range(len(pts)), key=lambda i: pts[i][0])   # position order -> given index
    raws = [pts[i][1] for i in order]
    lo_end, hi_end = order[0], order[-1]

    measured = "normal" if raws[-1] > raws[0] else "reversed"
    span = abs(raws[-1] - raws[0])
    warnings: list[str] = []
    if span < max(MIN_END_SPAN_NOISE * noise, 100):
        return Fit(None, measured if direction == "auto" else direction, 0,
                   [f"the two ends read {raws[0]} and {raws[-1]} - nearly the same. "
                    "Is the wiper connected? Measure both ends again."],
                   sorted([lo_end, hi_end]))
    if direction != "auto" and direction != measured:
        return Fit(None, direction, 0,
                   [f"the ends say this pot is {measured} (fully down {raws[0]}, fully up "
                    f"{raws[-1]}), not {direction}. Measure the ends again, or use auto."],
                   sorted([lo_end, hi_end]))
    direction = measured
    sign = 1 if direction == "normal" else -1

    keep = _on_every_best_chain(raws, sign, tol=noise)
    redo = sorted(order[i] for i in range(len(order)) if i not in keep)
    if redo:
        warnings.append(
            "out of order: " + ", ".join(
                f"position {pts[i][0]:g} read {pts[i][1]}" for i in redo)
            + " - set the knob again and re-measure")
    good = [(pts[order[i]][0], raws[i]) for i in range(len(order)) if i in keep]
    if len(good) - 2 < MIN_INTERIOR:
        return Fit(None, direction, 0, warnings, redo)

    # Counts per knob unit, segment by segment (position order).
    slopes = [abs(r1 - r0) / ((p1 - p0) * 100.0) for (p0, r0), (p1, r1) in zip(good, good[1:])]
    flattest = min(slopes)
    floor = NOISE_FACTOR * noise
    hysteresis = max(1, math.ceil(max(HYSTERESIS_UNITS * flattest, floor)))
    if floor > STEP_WARN_UNITS * flattest:
        where = slopes.index(flattest)
        warnings.append(
            f"between positions {good[where][0]:g} and {good[where + 1][0]:g} the pot moves "
            f"only {flattest:.0f} counts per 1 %: the knob will step about "
            f"{hysteresis / flattest:.0f} % at a time there")

    # Pull each end inside the measured extreme so it is reachable through the noise.
    margin = max(float(hysteresis), floor)
    adjusted = [list(p) for p in good]
    for end, inner in ((0, 1), (-1, -2)):
        seg = good[inner][1] - good[end][1]
        step = min(margin, abs(seg) * END_MARGIN_MAX)
        adjusted[end][1] = good[end][1] + int(round(math.copysign(step, seg)))
    curve = sorted([[int(raw), round(pos * 100.0, 1)] for pos, raw in adjusted])
    return Fit(curve, direction, hysteresis, warnings, redo)


def check_curve(curve) -> tuple[list[list[float]], str]:
    """Validate a hand-supplied curve; returns it sorted by raw, and its direction."""
    if not isinstance(curve, list) or len(curve) < 2:
        raise ValueError("curve must be a list of at least two [raw, value] points")
    out: list[list[float]] = []
    for i, p in enumerate(curve):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError(f"curve point {i} must be [raw, value]")
        raw, value = p
        for name, v in (("raw", raw), ("value", value)):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                raise ValueError(f"curve point {i}: {name} must be a number")
        if raw < 0 or not 0 <= value <= 100:
            raise ValueError(f"curve point {i}: raw must be >= 0 and value 0-100")
        out.append([int(round(raw)), round(float(value), 1)])
    out.sort()
    raws = [p[0] for p in out]
    if len(set(raws)) != len(raws):
        raise ValueError("two curve points have the same raw count")
    values = [p[1] for p in out]
    steps = [b - a for a, b in zip(values, values[1:])]
    if all(s >= 0 for s in steps) and values[-1] > values[0]:
        return out, "normal"
    if all(s <= 0 for s in steps) and values[-1] < values[0]:
        return out, "reversed"
    raise ValueError("curve values must only rise or only fall with raw")


def curve_direction(curve) -> str:
    """normal if a (raw-sorted) curve rises, reversed if it falls."""
    if not curve:
        return "normal"
    pts = sorted(curve)
    return "reversed" if pts[-1][1] < pts[0][1] else "normal"


def fit_selector(position_raws: Sequence[int], noise: float | None = None) -> list[int]:
    """Thresholds midway between each position's raw, in position order.

    ``position_raws[i]`` is the reading with the selector in position i. They
    must step one way (rising or falling) and clear the noise; a selector
    whose positions sit too close for a reliable read raises ValueError.
    """
    raws = [int(round(r)) for r in position_raws]
    if len(raws) < 2:
        raise ValueError("a selector needs at least two positions")
    noise = DEFAULT_NOISE_COUNTS if noise is None else max(0.0, float(noise))
    steps = [b - a for a, b in zip(raws, raws[1:])]
    if not (all(s > 0 for s in steps) or all(s < 0 for s in steps)):
        raise ValueError("selector positions must read in order (each higher, or each lower, "
                         f"than the one before): got {raws}")
    gap = min(abs(s) for s in steps)
    if gap < SELECTOR_GAP_NOISE * noise:
        raise ValueError(f"two selector positions are only {gap} counts apart - too close "
                         "to tell apart reliably")
    return [int(round((a + b) / 2)) for a, b in zip(raws, raws[1:])]
