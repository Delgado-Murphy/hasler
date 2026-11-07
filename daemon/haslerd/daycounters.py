"""The last 24 hours, counted: what the daily check-in reports (docs/ota.md §5).

The check-in fires once a day at a random hour, so a count of "today" or "the
last hour" said little (a stall at 15:00 is gone by 04:00). ``DayCounters``
keeps **hourly buckets for the last 25 hours** in ``<state>/counters.json`` and
sums the newest 24 for ``/api/health``'s ``"day"`` block, which the check-in
reads. Each bucket holds:

    stalls          the stream watchdog's "stalled" events (player/watchdog.py)
    mpd_restarts    haslerd's restarts of MPD (MpdRecovery)
    daemon_starts   haslerd starts within one boot (a crash or an update, not a power-on)
    faults          every lamp fault word seen (lamp/faults.py), as a set
    temp_max        the SoC's hottest reading, degC (the LampKeeper's 30 s poll)
    undervolt       the supply sagged (lamp/faults.throttled, read at most every POWER_POLL_S)
    throttled       the firmware slowed the CPU (heat or a sagging supply)
    ytdlp_failures  YouTube Music resolves that failed (haslerd/ytdlp_lane.py)

Cheap on the card: written at most once every FLUSH_S, and only when something
changed (a power cut loses at most that much), atomically. Never raises: a
full card costs the counts, not the radio. Thread-safe (the watchdog's thread,
the main loop and the web threads all count).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

FILE = "counters.json"
HOUR_S = 3600
KEEP_HOURS = 25
FLUSH_S = 300.0
POWER_POLL_S = 120.0
COUNTS = ("stalls", "mpd_restarts", "daemon_starts", "ytdlp_failures")
FLAGS = ("undervolt", "throttled")
# Stream events (player/watchdog.py StreamEvents actions) that are counted, and as what.
EVENT_FIELDS = {"stalled": "stalls", "mpd unresponsive -> restarted": "mpd_restarts"}


class DayCounters:
    def __init__(self, path: Path | None, wall: Callable[[], float] = time.time, flush_s: float = FLUSH_S) -> None:
        self.path = Path(path) if path is not None else None
        self.wall = wall
        self.flush_s = flush_s
        self._lock = threading.Lock()
        self._hours: dict[int, dict] = {}
        self._dirty = False
        self._flushed = wall()
        self._next_power = 0.0
        self._seen_bits: dict | None = None
        if self.path is not None:
            try:
                doc = json.loads(self.path.read_text())
                for k, v in (doc.get("hours") or {}).items():
                    if isinstance(v, dict):
                        self._hours[int(k)] = v
            except (OSError, ValueError, AttributeError, TypeError):
                pass

    # -- counting -----------------------------------------------------------------------

    def _bucket(self, now: float) -> dict:
        h = int(now // HOUR_S)
        for old in [k for k in self._hours if k <= h - KEEP_HOURS]:
            del self._hours[old]
        return self._hours.setdefault(h, {})

    def add(self, field: str, n: int = 1, now: float | None = None, flush: bool = False) -> None:
        if n <= 0:
            return
        now = self.wall() if now is None else now
        with self._lock:
            b = self._bucket(now)
            b[field] = int(b.get(field) or 0) + int(n)
            self._dirty = True
        self.maybe_flush(now, force=flush)

    def event(self, action: str) -> None:
        """A stream event (StreamEvents.on_add): count the ones that matter."""
        field = EVENT_FIELDS.get(str(action))
        if field:
            self.add(field)

    def sample(self, faults: list | None = None, temp_c: float | None = None, power: dict | None = None,
               now: float | None = None) -> None:
        """The LampKeeper's poll: the fault words now, the temperature, and (when
        given) a lamp/faults.throttled document."""
        now = self.wall() if now is None else now
        with self._lock:
            b = self._bucket(now)
            words = sorted({str(w) for w in (faults or [])} - set(b.get("faults") or []))
            if words:
                b["faults"] = sorted(set(b.get("faults") or []) | set(words))
                self._dirty = True
            if isinstance(temp_c, (int, float)):
                t = round(float(temp_c))
                if t > int(b.get("temp_max", -999)):
                    b["temp_max"] = t
                    self._dirty = True
            for flag in self._power_flags(power):
                if not b.get(flag):
                    b[flag] = 1
                    self._dirty = True
        self.maybe_flush(now)

    def _power_flags(self, doc: dict | None) -> list[str]:
        """undervolt / throttled from a throttle document: on now, or a ``*_seen``
        bit that came on since the last reading (it counts since boot, so the
        first reading after a start only says what is on now)."""
        if not isinstance(doc, dict):
            return []
        prev, self._seen_bits = self._seen_bits, {k: bool(doc.get(k)) for k in
                                                  ("under_voltage_seen", "throttled_seen", "freq_capped_seen")}
        newly = (lambda k: bool(doc.get(k)) and prev is not None and not prev.get(k))
        out = []
        if doc.get("under_voltage_now") or newly("under_voltage_seen"):
            out.append("undervolt")
        if doc.get("throttled_now") or doc.get("freq_capped_now") or newly("throttled_seen") or newly("freq_capped_seen"):
            out.append("throttled")
        return out

    def power_due(self, now: float | None = None) -> bool:
        """Time to read the throttle word again (vcgencmd costs a process)."""
        now = self.wall() if now is None else now
        if now < self._next_power:
            return False
        self._next_power = now + POWER_POLL_S
        return True

    # -- reading ------------------------------------------------------------------------

    def sums(self, now: float | None = None) -> dict:
        """The last 24 hours: ``{"stalls", "mpd_restarts", "daemon_starts",
        "ytdlp_failures": n, "faults": [words], "temp_max": degC | None,
        "undervolt": bool, "throttled": bool, "hours": how many hours of the 24 it has seen}``."""
        now = self.wall() if now is None else now
        h = int(now // HOUR_S)
        with self._lock:
            rows = [v for k, v in self._hours.items() if h - 24 < k <= h]
        out: dict[str, Any] = {k: sum(int(r.get(k) or 0) for r in rows) for k in COUNTS}
        out["faults"] = sorted({w for r in rows for w in (r.get("faults") or [])})
        temps = [r["temp_max"] for r in rows if isinstance(r.get("temp_max"), (int, float))]
        out["temp_max"] = max(temps) if temps else None
        for flag in FLAGS:
            out[flag] = any(r.get(flag) for r in rows)
        out["hours"] = len(rows)
        return out

    # -- the card -----------------------------------------------------------------------

    def maybe_flush(self, now: float | None = None, force: bool = False) -> None:
        now = self.wall() if now is None else now
        if self.path is None or not self._dirty:
            return
        if not force and now - self._flushed < self.flush_s:
            return
        with self._lock:
            doc = {"schema": 1, "hours": {str(k): v for k, v in sorted(self._hours.items())}}
            self._dirty = False
            self._flushed = now
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(json.dumps(doc, separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)
        except OSError:
            log.debug("day counters: could not write %s", self.path, exc_info=True)

    def close(self) -> None:
        self.maybe_flush(force=True)
