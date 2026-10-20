"""Which effect each knob plays, and getting it into CamillaDSP.

Every pot in the descriptor except the volume knob can carry an effect
(dsp/effects.py). The default comes from the descriptor: a pot's own
``effect:`` (and optional ``effect_params:``), else the verb its ``change``
is bound to (set_tone -> tilt, set_treble -> treble, set_bass -> bass), else
none. The owner's choice is saved in settings.json -

    "knob_functions": {"tone": {"effect": "vintage", "params": {"low_cut": 150}}}

- and applied live: the next render uses it, no restart. A pot with an
``effect:`` but no binding in ``actions:`` still works (Actions sends any
unbound pot's ``change`` here), so a new cabinet's spare pots need no code.

The volume knob (bound to set_volume or volume_step) is never assignable.
The tone knob's end-stop switch is a control of its own and stays as it is.

Each knob move re-renders the whole chain from neutral - every knob's
effect at its position - and hands the result to ``hasler-dsp set`` on a
worker thread, newest state wins, so the panel loop never waits on a
subprocess. The loudness effect also follows the volume: while a knob plays
it, the worker looks at the volume every second and re-renders when it moved.

A *try* (the knob page's "listen") swaps one knob's effect for 30 s without
saving; it ends at the deadline, on a cancel, or when the page saves.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from . import effects as fx

log = logging.getLogger(__name__)

TRY_SECONDS = 30.0
VOLUME_VERBS = ("set_volume", "volume_step")
LOUDNESS_POLL_S = 1.0
LOUDNESS_MIN_STEP = 1.0     # knob units of volume before loudness re-renders
SETTINGS_KEY = "knob_functions"


@dataclass
class Assignment:
    effect: str
    params: dict


@dataclass
class Trial:
    assignment: Assignment
    position: float | None
    until: float


def helper_runner(command: str, timeout: float = 5.0) -> Callable[[dict], dict | None]:
    """``hasler-dsp set JSON`` as a runner: its printed JSON, or None."""
    def run(payload: dict) -> dict | None:
        try:
            done = subprocess.run([command, "set", json.dumps(payload, separators=(",", ":"))],
                                  capture_output=True, text=True, timeout=timeout)
        except Exception:
            log.warning("dsp helper %s failed to run", command, exc_info=True)
            return None
        try:
            doc = json.loads(done.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            log.warning("dsp helper said nothing useful (exit %s): %s", done.returncode, done.stderr.strip())
            return None
        if not doc.get("ok"):
            log.warning("dsp helper: %s", doc.get("error"))
        return doc
    return run


class KnobFunctions:
    def __init__(
        self,
        variant: Callable[[], Any],
        runner: Callable[[dict], dict | None] | None,
        volume: Callable[[], float | None] = lambda: None,
        legacy: Callable[[str, float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        threaded: bool = True,
    ) -> None:
        # variant() -> the live Variant (its controls follow the calibration page).
        self._variant = variant
        self.runner = runner
        self.volume = volume
        # No hasler-dsp on this box: the knob that has a tone command keeps it.
        self.legacy = legacy
        self.clock = clock
        self.threaded = threaded
        self.saved: dict[str, Assignment] = {}
        self.positions: dict[str, float] = {}       # 0..1, from the knobs' change events
        self.trials: dict[str, Trial] = {}
        self.last: dict | None = None               # hasler-dsp's last answer
        self.last_payload: dict | None = None
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._dirty = False
        self._applied_volume: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = False

    # -- which knobs ------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.runner is not None

    def _pots(self) -> list[dict]:
        v = self._variant()
        return [c for c in (getattr(v, "controls", None) or []) if isinstance(c, dict) and c.get("kind") == "pot"]

    def _verb(self, cid: str) -> str | None:
        spec = (getattr(self._variant(), "actions", None) or {}).get(f"{cid}.change")
        return spec.get("do") if isinstance(spec, dict) else None

    def assignable(self, cid: str) -> bool:
        return any(c.get("id") == cid for c in self._pots()) and self._verb(cid) not in VOLUME_VERBS

    def handles(self, cid: str) -> bool:
        """An unbound pot's change comes here iff the pot is assignable."""
        return self.assignable(cid)

    def default(self, cid: str) -> Assignment:
        spec = next((c for c in self._pots() if c.get("id") == cid), {})
        effect_id = spec.get("effect") or fx.VERB_EFFECTS.get(self._verb(cid) or "", "none")
        if effect_id not in fx.EFFECTS:
            log.error("%s: descriptor names unknown effect %r; using none", cid, effect_id)
            effect_id = "none"
        try:
            params = fx.params_from(effect_id, spec.get("effect_params"), clamp=True)
        except ValueError as exc:
            log.error("%s: descriptor effect_params ignored: %s", cid, exc)
            params = fx.params_from(effect_id, None)
        return Assignment(effect_id, params)

    def current(self, cid: str) -> Assignment:
        """What the knob plays now: a running try, else the saved choice, else the default."""
        with self._lock:
            trial = self.trials.get(cid)
            if trial is not None:
                return trial.assignment
            return self.saved.get(cid) or self.default(cid)

    def knob_ids(self) -> list[str]:
        return [c["id"] for c in self._pots() if c.get("id")]

    # -- saved choices ----------------------------------------------------

    def parse(self, body: Any) -> dict[str, Assignment | None]:
        """A PUT body -> {knob: Assignment or None (= back to default)}; ValueError if wrong."""
        if not isinstance(body, dict) or not body:
            raise ValueError('expected {"<knob id>": {"effect": "...", "params": {...}}} or {"<knob id>": null}')
        out: dict[str, Assignment | None] = {}
        for cid, raw in body.items():
            self._check_knob(cid)
            out[cid] = None if raw is None else self._assignment(cid, raw)
        self._check_unique({**{c: a for c, a in self.saved.items()}, **out})
        return out

    def _check_knob(self, cid: Any) -> None:
        if not isinstance(cid, str) or not any(c.get("id") == cid for c in self._pots()):
            raise ValueError(f"no knob called {cid!r} (knobs: {', '.join(self.knob_ids())})")
        if not self.assignable(cid):
            raise ValueError(f"the {cid} knob is the volume knob; it can't carry an effect")

    def _assignment(self, cid: str, raw: Any) -> Assignment:
        if not isinstance(raw, dict) or "effect" not in raw:
            raise ValueError(f'{cid}: expected {{"effect": "...", "params": {{...}}}}')
        unknown = set(raw) - {"effect", "params"}
        if unknown:
            raise ValueError(f"{cid}: unknown keys {', '.join(sorted(unknown))}")
        effect_id = fx.effect(raw["effect"]).id
        return Assignment(effect_id, fx.params_from(effect_id, raw.get("params")))

    def _check_unique(self, chosen: dict[str, Assignment | None], extra: tuple[str, Assignment] | None = None) -> None:
        """One knob per effect: two knobs on the same blocks would fight."""
        final = {cid: (chosen.get(cid) if chosen.get(cid) is not None else self.default(cid))
                 for cid in self.knob_ids() if self.assignable(cid)}
        if extra is not None:
            final[extra[0]] = extra[1]
        seen: dict[str, str] = {}
        for cid, a in final.items():
            if a.effect == "none":
                continue
            if a.effect in seen:
                raise ValueError(f"{fx.EFFECTS[a.effect].label} would be on both the {seen[a.effect]} "
                                 f"and the {cid} knob; one knob per effect")
            seen[a.effect] = cid

    def set_saved(self, chosen: dict[str, Assignment | None]) -> None:
        """Apply a parsed PUT (the API saves the file); ends tries on those knobs."""
        with self._lock:
            for cid, a in chosen.items():
                if a is None:
                    self.saved.pop(cid, None)
                else:
                    self.saved[cid] = a
                self.trials.pop(cid, None)
        self.apply()

    def saved_doc(self) -> dict:
        with self._lock:
            return {cid: {"effect": a.effect, "params": dict(a.params)} for cid, a in self.saved.items()}

    def apply_saved(self, settings: dict) -> None:
        """settings.json's ``knob_functions`` at start. A bad entry is logged and
        skipped: one typo must not cost the other knobs."""
        doc = settings.get(SETTINGS_KEY) if isinstance(settings, dict) else None
        if not isinstance(doc, dict):
            return
        saved: dict[str, Assignment] = {}
        for cid, raw in doc.items():
            try:
                self._check_knob(cid)
                if not isinstance(raw, dict):
                    raise ValueError("expected {effect, params}")
                effect_id = fx.effect(raw.get("effect")).id
                params = fx.params_from(effect_id, raw.get("params") if isinstance(raw.get("params"), dict) else None,
                                        clamp=True)
                saved[cid] = Assignment(effect_id, params)
            except ValueError as exc:
                log.warning("ignoring saved knob function %s=%r: %s", cid, raw, exc)
        try:
            self._check_unique(saved)
        except ValueError as exc:
            log.warning("ignoring saved knob functions: %s", exc)
            return
        with self._lock:
            self.saved = saved
        if saved:
            log.info("knob functions: %s", ", ".join(f"{c}={a.effect}" for c, a in saved.items()))

    # -- tries ------------------------------------------------------------

    def start_try(self, body: Any) -> float:
        """POST /api/knobs/try: {"knob", "effect", "params", "position"?}; seconds it lasts."""
        if not isinstance(body, dict):
            raise ValueError('expected {"knob": "...", "effect": "...", "params": {...}}')
        cid = body.get("knob")
        self._check_knob(cid)
        unknown = set(body) - {"knob", "effect", "params", "position", "cancel"}
        if unknown:
            raise ValueError(f"unknown keys {', '.join(sorted(unknown))}")
        a = self._assignment(cid, {k: body[k] for k in ("effect", "params") if k in body})
        others = {c: s for c, s in self.saved.items() if c != cid}
        self._check_unique(others, (cid, a))
        position = body.get("position")
        if position is not None and (isinstance(position, bool) or not isinstance(position, (int, float))
                                     or not 0 <= position <= 1):
            raise ValueError("position must be 0 to 1")
        with self._lock:
            # One try at a time: the page listens to one knob.
            self.trials = {cid: Trial(a, None if position is None else float(position),
                                      self.clock() + TRY_SECONDS)}
        log.info("trying %s on the %s knob for %.0f s", a.effect, cid, TRY_SECONDS)
        self.apply()
        return TRY_SECONDS

    def cancel_try(self, cid: Any = None) -> bool:
        with self._lock:
            if cid is None:
                had = bool(self.trials)
                self.trials = {}
            else:
                had = self.trials.pop(cid, None) is not None
        if had:
            self.apply()
        return had

    def try_left(self) -> dict | None:
        with self._lock:
            for cid, t in self.trials.items():
                return {"knob": cid, "effect": t.assignment.effect,
                        "seconds_left": max(0, round(t.until - self.clock()))}
        return None

    def _expire(self) -> bool:
        now = self.clock()
        with self._lock:
            gone = [c for c, t in self.trials.items() if t.until <= now]
            for c in gone:
                del self.trials[c]
        if gone:
            log.info("try on %s ended", ", ".join(gone))
        return bool(gone)

    # -- knob events ------------------------------------------------------

    def knob_moved(self, cid: str, value: float) -> None:
        """A pot settled at ``value`` (0-100)."""
        if not self.active:
            if self.legacy is not None:
                self.legacy(cid, value)
            return
        with self._lock:
            self.positions[cid] = fx.clamp01(float(value) / 100.0)
        self.apply()

    def position(self, cid: str) -> float | None:
        with self._lock:
            trial = self.trials.get(cid)
            if trial is not None and trial.position is not None:
                return trial.position
            return self.positions.get(cid)

    def describe(self, cid: str, value: float | None = None) -> dict:
        """For /api/status: {"effect", "label", "text"} (+ "treble_db" while
        hasler-dsp drives the knob: the treble or tilt shelf's gain, else 0).
        ``value``: the knob's live 0-100 reading, if the caller has one."""
        a = self.current(cid)
        e = fx.EFFECTS[a.effect]
        with self._lock:
            trial = self.trials.get(cid)
            pos = trial.position if trial is not None and trial.position is not None else None
        if pos is None:
            pos = fx.clamp01(value / 100.0) if value is not None else self.position(cid)
        doc: dict[str, Any] = {"effect": a.effect, "label": e.label, "text": e.text(pos),
                               "ends": list(e.ends)}
        if self.active:
            filters = fx.render(a.effect, pos, a.params, self._read_volume()).filters
            gain = (filters.get("Treble") or filters.get("Tilt high") or {}).get("gain", 0.0)
            doc["treble_db"] = round(float(gain), 1)
        return doc

    # -- rendering --------------------------------------------------------

    def _uses_loudness(self) -> bool:
        return any(self.current(c).effect == "loudness" and self.position(c) is not None
                   for c in self.knob_ids() if self.assignable(c))

    def payload(self) -> dict:
        """The whole chain, every knob at its position (hasler-dsp ``set`` JSON)."""
        volume = self._read_volume()
        sounds = []
        for cid in self.knob_ids():
            if not self.assignable(cid):
                continue
            a = self.current(cid)
            sounds.append(fx.render(a.effect, self.position(cid), a.params, volume))
        self._applied_volume = volume
        return fx.combine(sounds)

    def _read_volume(self) -> float | None:
        try:
            v = self.volume()
            return None if v is None else float(v)
        except Exception:
            return None

    def apply(self) -> None:
        """Re-render and hand it on (worker thread, newest wins)."""
        if not self.active:
            return
        if not self.threaded:
            self._run_once()
            return
        with self._wake:
            self._dirty = True
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="dsp", daemon=True)
                self._thread.start()
            self._wake.notify()

    def _run_once(self) -> None:
        payload = self.payload()
        if payload == self.last_payload:
            return
        self.last_payload = payload
        doc = self.runner(payload) if self.runner else None
        self.last = doc
        if doc:
            log.debug("dsp: %s -> master %s dB, %s", payload, doc.get("master_db"), doc.get("live"))

    def _worker(self) -> None:
        while not self._stop:
            with self._wake:
                if not self._dirty:
                    self._wake.wait(timeout=self._idle_timeout())
                dirty, self._dirty = self._dirty, False
            if self._expire():
                dirty = True
            if not dirty and self._uses_loudness():
                v = self._read_volume()
                if v is not None and (self._applied_volume is None
                                      or abs(v - self._applied_volume) >= LOUDNESS_MIN_STEP):
                    dirty = True
            if dirty:
                try:
                    self._run_once()
                except Exception:
                    log.exception("dsp render failed")

    def _idle_timeout(self) -> float | None:
        timeouts = []
        if self.trials:
            timeouts.append(max(0.05, min(t.until for t in self.trials.values()) - self.clock()))
        if self._uses_loudness():
            timeouts.append(LOUDNESS_POLL_S)
        return min(timeouts) if timeouts else None

    def tick(self) -> None:
        """Without the worker (tests): expire tries and follow the volume now."""
        changed = self._expire()
        if self._uses_loudness():
            v = self._read_volume()
            if v is not None and (self._applied_volume is None or abs(v - self._applied_volume) >= LOUDNESS_MIN_STEP):
                changed = True
        if changed:
            self._run_once()

    def close(self) -> None:
        with self._wake:
            self._stop = True
            self._wake.notify()

    # -- the API's document ----------------------------------------------

    def knob_doc(self, cid: str, label: str | None = None, position: float | None = None) -> dict:
        assignable = self.assignable(cid)
        pos = self.position(cid) if position is None else position
        doc: dict[str, Any] = {"id": cid, "label": label or cid.replace("_", " ").capitalize(),
                               "assignable": assignable, "position": None if pos is None else round(pos, 3)}
        if not assignable:
            doc.update({"effect": None, "params": {}, "default_effect": None, "default_params": {},
                        "note": "The volume knob always sets the volume."})
            return doc
        a, d = self.current(cid), self.default(cid)
        doc.update({"effect": a.effect, "params": dict(a.params),
                    "default_effect": d.effect, "default_params": dict(d.params)})
        with self._lock:
            if cid in self.trials:
                doc["trying"] = True
        if not self.active:
            doc["note"] = ("This radio has no hasler-dsp yet (an older release or no CamillaDSP): "
                           "choices are saved and take effect after the update.")
        return doc
