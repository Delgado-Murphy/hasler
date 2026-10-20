"""``/api/knobs/functions`` and ``/api/knobs/try``: which effect each knob plays.

    GET    /api/knobs/functions   {"effects": [...], "knobs": [...], "try": {...} | null}
    PUT    /api/knobs/functions   {"<knob id>": {"effect", "params"}} or {"<knob id>": null}
                                  (null = back to the descriptor's default) -> same doc
    POST   /api/knobs/try         {"knob", "effect", "params", "position"?} - plays it now for
                                  30 s without saving; a repeat call restarts the 30 s
                                  -> {"ok", "seconds", ...doc}
    POST   /api/knobs/try         {"knob", "cancel": true} - back at once
    DELETE /api/knobs/try         back at once (every knob)

Effects: ``{"id", "label", "description", "ends": [left, right], "params":
[{"id", "label", "unit", "min", "max", "step", "default", "plusminus"?}]}``.
Knobs: ``{"id", "label", "effect", "params", "default_effect",
"default_params", "assignable", "position": 0..1 | null, "note"?,
"trying"?}`` - ids as ``/api/knobs`` has them. ``position`` (optional) in a
try stands in for the knob, for a radio whose knob can't be read.

Saved under ``knob_functions`` in settings.json (dsp/knobs.py), applied live.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from ..dsp import effects as fx
from ..dsp.knobs import SETTINGS_KEY, KnobFunctions
from .server import load_settings, save_settings

log = logging.getLogger(__name__)

PREFIXES = ("/api/knobs/functions", "/api/knobs/try")


class KnobFunctionsApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any, knobs: KnobFunctions) -> None:
        self.app = app
        self.knobs = knobs

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PREFIXES:
            return None
        method = method.upper()
        if p == "/api/knobs/functions":
            if method == "GET":
                return 200, self.doc()
            if method in ("PUT", "POST"):
                return 200, {"ok": True, **self.put(body)}
        else:
            if method == "POST":
                if isinstance(body, dict) and body.get("cancel") is True:
                    had = self.knobs.cancel_try(body.get("knob"))
                    return 200, {"ok": True, "cancelled": had, **self.doc()}
                seconds = self.knobs.start_try(body)
                return 200, {"ok": True, "seconds": int(seconds), **self.doc()}
            if method == "DELETE":
                had = self.knobs.cancel_try(None)
                return 200, {"ok": True, "cancelled": had, **self.doc()}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    # -- documents --------------------------------------------------------

    def _live(self) -> tuple[dict, dict]:
        """(labels, positions 0..1) from the running panel, by knob id."""
        labels: dict[str, str] = {}
        positions: dict[str, float] = {}
        for control in getattr(self.app.panel, "controls", []) or []:
            if getattr(control, "kind", None) != "pot":
                continue
            if getattr(control, "label", None):
                labels[control.id] = control.label
            try:
                v = control.state()
            except Exception:
                v = None
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                positions[control.id] = fx.clamp01(v / 100.0)
        return labels, positions

    def doc(self) -> dict:
        labels, positions = self._live()
        knobs = []
        specs = {c.get("id"): c for c in self.knobs._pots()}
        for cid in self.knobs.knob_ids():
            d = self.knobs.knob_doc(cid, labels.get(cid) or specs.get(cid, {}).get("label"),
                                    None if cid in self.knobs.trials and self.knobs.position(cid) is not None
                                    else positions.get(cid))
            if d["assignable"] and specs.get(cid, {}).get("end_switch") and "note" not in d:
                d["note"] = "Its end-stop switch still turns the radio on and off."
            knobs.append(d)
        return {"effects": [e.doc() for e in fx.EFFECTS.values()], "knobs": knobs,
                "try": self.knobs.try_left()}

    def put(self, body: Any) -> dict:
        chosen = self.knobs.parse(body)          # ValueError -> 400
        with self.app.lock:
            if self.app.settings_file is not None:
                doc = load_settings(self.app.settings_file)
                saved = doc.get(SETTINGS_KEY) if isinstance(doc.get(SETTINGS_KEY), dict) else {}
                for cid, a in chosen.items():
                    if a is None:
                        saved.pop(cid, None)
                    else:
                        saved[cid] = {"effect": a.effect, "params": dict(a.params)}
                doc[SETTINGS_KEY] = saved
                save_settings(self.app.settings_file, doc)
            self.knobs.set_saved(chosen)
        log.info("knob functions from the web page: %s",
                 ", ".join(f"{c}={'default' if a is None else a.effect}" for c, a in chosen.items()))
        return self.doc()
