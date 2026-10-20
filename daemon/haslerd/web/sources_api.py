"""``/api/sources`` and ``/api/casting``: the PU cycle and the casting targets.

The cycle (which sources the PU key steps through, in order) and "take the
speaker back when a cast ends" are the owner's settings: they live in
``settings.json`` under ``sources`` -

    "sources": {"cycle": ["radio", "ytm", "bluetooth"], "resume_after_cast": true}

- read at startup (``apply_saved``) and applied live on a PUT. Absent = today's
behaviour (actions.SOURCE_CYCLE, no resume). Any known stop may be saved; one
that can't be used right now (a renderer switched off, no YouTube Music
presets) stays in the list and is skipped by the key, with the reason shown.

The casting targets are moOde's (player/renderers.py): AirPlay, Bluetooth and
UPnP are switched and renamed through moOde's own Renderers form. One target
per PUT, because moOde's worker takes one job at a time. Spotify Connect is
go-librespot, listed read-only: it is on whenever Spotify is set up, because
the same daemon plays the Spotify presets.

    GET  /api/sources          cycle, every stop with availability, casting
    PUT  /api/sources          {"cycle": [...]} and/or {"resume_after_cast": bool}
    GET  /api/casting          targets, who is casting now, the pairing window
    PUT  /api/casting          {"airplay": {"on": true, "name": "Hasler"}}  (one target)
    POST /api/casting/pair     {"seconds": 120} - Bluetooth discoverable
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..actions import (CYCLE_LABELS, CYCLE_STOPS, DEFAULT_PAIR_S, RENDERERS, SOURCE_CYCLE,
                       Actions, cycle_from)
from ..player.renderers import TARGETS, RendererError, spotify_device_name
from .server import Unavailable, load_settings, save_settings

log = logging.getLogger(__name__)

APLMETA_FILE = Path("/var/local/www/aplmeta.json")
PAIR_RANGE = (30, 300)
PREFIXES = ("/api/sources", "/api/casting")


def apply_saved(actions: Actions, settings: dict) -> None:
    """settings.json's ``sources`` onto the actions at startup. A bad value is
    logged and ignored: one typo must not cost the PU key."""
    doc = settings.get("sources") if isinstance(settings, dict) else None
    if not isinstance(doc, dict):
        return
    if "cycle" in doc:
        try:
            actions.source_cycle = cycle_from(doc["cycle"])
        except ValueError as exc:
            log.warning("ignoring saved source cycle %r: %s", doc["cycle"], exc)
    if isinstance(doc.get("resume_after_cast"), bool):
        actions.resume_after_cast = doc["resume_after_cast"]


class SourcesApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any, aplmeta: Path = APLMETA_FILE,
                 spotify_name: Any = spotify_device_name) -> None:
        self.app = app
        self.aplmeta = aplmeta
        self.spotify_name = spotify_name
        # One moOde job at a time, shared with the room's casting names (web/room.py).
        room = getattr(app, "room", None)
        self._renderer_lock = getattr(room, "renderer_lock", None) or threading.Lock()

    @property
    def actions(self) -> Actions:
        return self.app.actions

    @property
    def renderers(self) -> Any:
        return getattr(self.actions.player, "renderers", None)

    # -- routing ----------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in ("/api/sources", "/api/casting", "/api/casting/pair"):
            return None
        method = method.upper()
        if p == "/api/sources":
            if method == "GET":
                return 200, self.sources()
            if method in ("PUT", "POST"):
                return 200, {"ok": True, **self.put_sources(body)}
        elif p == "/api/casting":
            if method == "GET":
                return 200, self.casting()
            if method in ("PUT", "POST"):
                return 200, {"ok": True, **self.put_casting(body)}
        elif p == "/api/casting/pair" and method == "POST":
            return self.pair(body)
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    # -- the cycle --------------------------------------------------------

    def sources(self) -> dict:
        actions = self.actions
        cycle = tuple(actions.source_cycle)
        skip = actions.cycle_availability(cycle)
        ordered = list(cycle) + [s for s in CYCLE_STOPS if s not in cycle]
        return {
            "cycle": list(cycle),
            "default": list(SOURCE_CYCLE),
            "stops": [{"id": s, "label": CYCLE_LABELS[s], "in_cycle": s in cycle,
                       "available": skip.get(s) is None, "reason": skip.get(s)} for s in ordered],
            "resume_after_cast": bool(actions.resume_after_cast),
            # Which key's tap or hold steps the cycle, e.g. ["k6.hold"].
            "bound_to": sorted(k for k, v in actions.variant.actions.items()
                               if isinstance(v, dict) and v.get("do") == "cycle_source"),
            "casting": self.casting(),
        }

    def put_sources(self, body: Any) -> dict:
        if not isinstance(body, dict) or not ({"cycle", "resume_after_cast"} & body.keys()):
            raise ValueError("expected {\"cycle\": [...]} and/or {\"resume_after_cast\": true|false}")
        cycle = cycle_from(body["cycle"]) if "cycle" in body else None
        resume = body.get("resume_after_cast")
        if "resume_after_cast" in body and not isinstance(resume, bool):
            raise ValueError("resume_after_cast must be true or false")
        with self.app.lock:
            if self.app.settings_file is not None:
                doc = load_settings(self.app.settings_file)
                saved = doc.get("sources") if isinstance(doc.get("sources"), dict) else {}
                if cycle is not None:
                    saved["cycle"] = list(cycle)
                if resume is not None:
                    saved["resume_after_cast"] = resume
                doc["sources"] = saved
                save_settings(self.app.settings_file, doc)
            if cycle is not None:
                self.actions.source_cycle = cycle
            if resume is not None:
                self.actions.resume_after_cast = resume
        if cycle is not None:
            log.info("source cycle set to %s from the web page", " -> ".join(cycle))
        if resume is not None:
            log.info("resume after a cast %s from the web page", "on" if resume else "off")
        return self.sources()

    # -- casting ----------------------------------------------------------

    def _targets(self) -> list[dict]:
        renderers = self.renderers
        targets: list[dict] = []
        if renderers is not None:
            try:
                targets = renderers.describe()
            except Exception:
                log.exception("could not read moOde's renderers")
        if not targets:
            targets = [{"id": t.id, "label": t.label, "available": False, "on": False,
                        "name": "", "running": None, "managed_by": "moode"} for t in TARGETS.values()]
        for t in targets:
            if not t["available"] and not t.get("reason"):
                t["reason"] = "moOde isn't reachable on this radio"
        sources = getattr(self.actions.player, "sources", None)
        spotify = sources is None or "spotify" in sources
        try:
            name = self.spotify_name() if spotify else None
        except Exception:
            name = None
        targets.append({
            "id": "spotify", "label": "Spotify Connect", "available": spotify, "on": spotify,
            "name": name or "", "running": None, "managed_by": "haslerd", "read_only": True,
            "reason": None if spotify else "Spotify isn't set up on this radio",
            "note": "Shown in the Spotify app of the account the radio is signed in to",
        })
        return targets

    def _now(self) -> dict:
        actions = self.actions
        player = actions.player
        try:
            source = player.active_source()
        except Exception:
            source = "radio"
        casting = bool(actions.phone_has_it(source))
        who: dict = {"device": "", "title": "", "artist": "", "codec": None, "rate": None}
        if source == "bluetooth":
            info = getattr(player, "renderer_info", None)
            try:
                who.update(info() if callable(info) else {})
            except Exception:
                log.debug("renderer_info failed", exc_info=True)
        elif source == "airplay":
            try:
                meta = json.loads(self.aplmeta.read_text() or "{}")
                who["title"] = str(meta.get("title") or "")
                who["artist"] = str(meta.get("artist") or "")
            except (OSError, ValueError, AttributeError):
                pass
        return {"source": source, "casting": casting,
                "renderer": source if source in RENDERERS or (casting and source == "spotify") else None,
                **{k: str(who.get(k) or "") for k in ("device", "title", "artist")},
                # Bluetooth only: "aptX HD", 48000 - None otherwise or unknown.
                "codec": who.get("codec") or None,
                "rate": who.get("rate") if isinstance(who.get("rate"), int) else None}

    def casting(self) -> dict:
        actions = self.actions
        left = max(0.0, actions.pairing_until - actions.clock()) if actions.pairing else 0.0
        return {
            "targets": self._targets(),
            "now": self._now(),
            "pairing": {"open": bool(actions.pairing), "seconds_left": int(round(left)),
                        "default_seconds": DEFAULT_PAIR_S, "range": list(PAIR_RANGE)},
        }

    def put_casting(self, body: Any) -> dict:
        if not isinstance(body, dict) or len(body) != 1:
            raise ValueError("expected one target, e.g. {\"airplay\": {\"on\": true, \"name\": \"Hasler\"}}")
        (target, change), = body.items()
        if target == "spotify":
            raise ValueError("Spotify Connect is on whenever Spotify is set up; it can't be switched here")
        if target not in TARGETS:
            raise ValueError(f"no casting target called {target!r} (one of: {', '.join(TARGETS)})")
        if not isinstance(change, dict) or not ({"on", "name"} & change.keys()):
            raise ValueError("expected {\"on\": true|false} and/or {\"name\": \"...\"}")
        renderers = self.renderers
        if renderers is None:
            raise Unavailable("casting targets can't be switched on this radio (no moOde)")
        with self._renderer_lock:
            try:
                renderers.set(target, on=change.get("on"), name=change.get("name"))
            except RendererError as exc:
                raise Unavailable(str(exc)) from exc
        return self.casting()

    def pair(self, body: Any) -> tuple[int, dict]:
        body = body if isinstance(body, dict) else {}
        seconds = body.get("seconds", DEFAULT_PAIR_S)
        lo, hi = PAIR_RANGE
        if isinstance(seconds, bool) or not isinstance(seconds, int) or not lo <= seconds <= hi:
            raise ValueError(f"seconds must be a whole number from {lo} to {hi}")
        renderers = self.renderers
        if renderers is not None:
            # moOde owns Bluetooth: if its settings can't be read, opening a
            # pairing window would only pretend to work.
            try:
                enabled = renderers.enabled()
            except Exception:
                log.debug("moOde's renderers unreadable", exc_info=True)
                enabled = {}
            if not enabled:
                raise Unavailable("moOde isn't reachable on this radio, so Bluetooth pairing "
                                  "can't be opened - try again in a minute")
        else:
            enabled = {}   # no moOde at all (a laptop, --fake): the fake player pairs
        if enabled and not enabled.get("bluetooth"):
            return 409, {"ok": False, "errors": [], "error": "Bluetooth is switched off - turn it on first"}
        with self.app.lock:
            self.actions._bt_pair(None, {"seconds": seconds})
        log.info("bluetooth pairing opened for %ss from the web page", seconds)
        return 200, {"ok": True, **self.casting()}
