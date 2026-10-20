"""The radio's room name ("Kitchen") and, if the owner wants, the casting names.

Saved in ``settings.json`` -

    "room": {"name": "Kitchen", "casting_names": false}

- and shown/changed through ``/api/settings`` (``room``). Unset, the name is
today's: the host name (integration.radio_name(), "hasler"). The room name is
who this radio is: the multiroom room list (main.build_group's identity),
``/api/info``'s ``name`` (Home Assistant's device name; HA keys on ``id``, so a
rename is safe) and the key page's title. The host name and ``hasler.local``
never change; the page shows the address avahi actually publishes (a second
Hasler on the network becomes ``hasler-2.local``).

Casting names stay "Hasler" unless ``casting_names`` is on: then AirPlay, UPnP,
Bluetooth (moOde, player/renderers.py) and Spotify Connect (go-librespot's
``device_name``) become "Hasler <Room>". Switching it off puts "Hasler" back;
renaming the room while it is on re-applies. Applying runs on its own thread,
one target at a time (moOde's worker takes one job at a time and can take a
minute over one), never in the request; ``doc()["apply"]`` is its progress.
go-librespot reads its name only at start, so a rename ends in ``systemctl
try-restart go-librespot.service`` (sudoers allows exactly that line).
"""

from __future__ import annotations

import logging
import os
import re
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..player import glr_config
from ..player.renderers import GO_LIBRESPOT_CONFIG, RendererError, spotify_device_name, valid_name
from . import integration

log = logging.getLogger(__name__)

CAST_BRAND = "Hasler"
NAME_MAX = 32
# Name -> casting targets, in the order they are applied.
STEPS = (("airplay", "AirPlay"), ("upnp", "UPnP / DLNA"), ("bluetooth", "Bluetooth"),
         ("spotify", "Spotify Connect"))
MOODE_TARGETS = ("airplay", "upnp", "bluetooth")
# moOde's worker busy with the previous job: keep asking this long, this often.
BUSY_WAIT_S = 180.0
BUSY_POLL_S = 5.0
# The config.yml edit and the restart live in player/glr_config.py (shared
# with Spotify for guests); the names stay here for callers and tests.
SPOTIFY_RESTART = glr_config.RESTART
_restart_spotify = glr_config.restart_go_librespot
set_device_name = glr_config.set_device_name
ADDRESS_TTL_S = 60.0


def room_name_from(raw: Any) -> str | None:
    """A room name, or None for "back to the default"; ValueError says why not."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    try:
        return valid_name(raw.strip() if isinstance(raw, str) else raw)
    except ValueError:
        raise ValueError("a room name is 1-32 letters, digits, spaces, dots or dashes, no quotes") from None


def casting_name(room: str | None) -> str:
    """What casting targets are called with the switch on: "Hasler Kitchen".
    No room name: just "Hasler"; a room already called "Hasler ..." is used as is."""
    if not room:
        return CAST_BRAND
    if room.lower().startswith(CAST_BRAND.lower()):
        return room
    name = f"{CAST_BRAND} {room}"[:NAME_MAX]
    return re.sub(r"[^\w.]+$", "", name, flags=re.UNICODE)


def _avahi_host() -> str | None:
    """The name avahi really publishes ("hasler-2.local" if another Hasler
    had "hasler"), over D-Bus; None if avahi can't be asked."""
    try:
        out = subprocess.run(["busctl", "--system", "call", "org.freedesktop.Avahi", "/",
                              "org.freedesktop.Avahi.Server", "GetHostNameFqdn"],
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.fullmatch(r's "([A-Za-z0-9.-]+)"', (out.stdout or "").strip())
    return m.group(1) if out.returncode == 0 and m else None


class Room:
    """The saved room, its casting names and their background apply."""

    def __init__(
        self,
        settings_file: Path | None = None,
        renderers: Callable[[], Any] = lambda: None,
        spotify_config: Path = GO_LIBRESPOT_CONFIG,
        restart_spotify: Callable[[], None] = _restart_spotify,
        avahi_host: Callable[[], str | None] = _avahi_host,
        default_name: Callable[[], str] = integration.radio_name,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.settings_file = settings_file
        self.renderers = renderers
        self.spotify_config = Path(spotify_config)
        self.restart_spotify = restart_spotify
        self.avahi_host = avahi_host
        self.default_name = default_name
        self.clock, self.sleep, self.wall = clock, sleep, wall
        # Shared with SourcesApi's PUT /api/casting: one moOde job at a time.
        self.renderer_lock = threading.Lock()
        self.saved: dict = {}
        self._lock = threading.Lock()
        self._want: str | None = None
        self._thread: threading.Thread | None = None
        self._apply: dict = {"state": "idle", "name": None, "steps": [], "finished": None}
        self._address: tuple[float, str] | None = None

    # -- the saved value ------------------------------------------------------

    def load(self, settings: dict) -> None:
        doc = settings.get("room") if isinstance(settings, dict) else None
        saved: dict = {}
        if isinstance(doc, dict):
            try:
                name = room_name_from(doc.get("name"))
            except ValueError:
                log.warning("ignoring saved room name %r", doc.get("name"))
                name = None
            if name:
                saved["name"] = name
            if isinstance(doc.get("casting_names"), bool):
                saved["casting_names"] = doc["casting_names"]
        self.saved = saved

    def custom(self) -> str | None:
        return self.saved.get("name")

    def name(self) -> str:
        """The room name, else the host name: who this radio is."""
        return self.custom() or self.default_name()

    def casting_on(self) -> bool:
        return bool(self.saved.get("casting_names"))

    def wanted_casting_name(self) -> str:
        return casting_name(self.custom()) if self.casting_on() else CAST_BRAND

    def address(self) -> str:
        now = self.clock()
        if self._address is not None and now - self._address[0] < ADDRESS_TTL_S:
            return self._address[1]
        try:
            addr = self.avahi_host()
        except Exception:
            addr = None
        if not addr:
            try:
                addr = (socket.gethostname().split(".")[0] or "hasler") + ".local"
            except OSError:
                addr = "hasler.local"
        self._address = (now, addr)
        return addr

    def doc(self) -> dict:
        with self._lock:
            apply = {**self._apply, "steps": [dict(s) for s in self._apply["steps"]]}
        return {
            "name": self.name(),
            "custom": self.custom() is not None,
            "default": self.default_name(),
            "casting_names": self.casting_on(),
            # What casting targets are called with the switch on.
            "casting_name": casting_name(self.custom()),
            "address": self.address(),
            "apply": apply,
        }

    def change(self, raw: Any) -> dict:
        """Validate a PUT's ``room``; returns the new saved block. Nothing is applied."""
        if not isinstance(raw, dict) or not ({"name", "casting_names"} & raw.keys()):
            raise ValueError("expected {\"room\": {\"name\": \"Kitchen\"} and/or {\"casting_names\": true|false}}")
        new = dict(self.saved)
        if "name" in raw:
            name = room_name_from(raw["name"])
            if name is None:
                new.pop("name", None)
            else:
                new["name"] = name
        if "casting_names" in raw:
            if not isinstance(raw["casting_names"], bool):
                raise ValueError("casting_names must be true or false")
            new["casting_names"] = raw["casting_names"]
        return new

    def commit(self, new: dict, asked: dict) -> None:
        """``new`` is saved: take it, and re-apply casting names if they change.
        Asking for ``casting_names`` always applies (that is the page's "try again")."""
        before = self.wanted_casting_name()
        old_name = self.name()
        self.saved = dict(new)
        if self.name() != old_name:
            log.info("room name set to %r from the web page", self.name())
        if "casting_names" in asked or self.wanted_casting_name() != before:
            self.apply(self.wanted_casting_name())

    # -- applying -------------------------------------------------------------

    def apply(self, name: str) -> None:
        """Rename every casting target to ``name``, in the background."""
        with self._lock:
            self._want = name
            self._apply = {"state": "applying", "name": name, "finished": None,
                           "steps": [{"id": i, "label": l, "state": "waiting", "note": ""} for i, l in STEPS]}
            if self._thread is not None and self._thread.is_alive():
                return          # the running pass sees _want and goes round again
            self._thread = threading.Thread(target=self._run, name="casting-names", daemon=True)
            self._thread.start()

    def wait(self, timeout: float = 5.0) -> None:
        """Tests: until the background apply has finished."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _step(self, sid: str, state: str, note: str = "") -> None:
        with self._lock:
            for s in self._apply["steps"]:
                if s["id"] == sid:
                    s["state"], s["note"] = state, note

    def _run(self) -> None:
        while True:
            with self._lock:
                name = self._want
            log.info("casting names -> %r", name)
            for sid, _ in STEPS:
                try:
                    state, note = (self._spotify(name) if sid == "spotify" else self._moode(sid, name))
                except Exception as exc:          # never let one target stop the rest
                    log.exception("renaming %s failed", sid)
                    state, note = "failed", str(exc) or type(exc).__name__
                self._step(sid, state, note)
                with self._lock:
                    if self._want != name:
                        break
            with self._lock:
                if self._want != name:
                    continue
                failed = any(s["state"] == "failed" for s in self._apply["steps"])
                self._apply["state"] = "failed" if failed else "done"
                self._apply["finished"] = self.wall()
                self._thread = None
                return

    def _moode(self, sid: str, name: str) -> tuple[str, str]:
        renderers = self.renderers()
        if renderers is None:
            return "skipped", "moOde isn't on this radio"
        try:
            target = next((t for t in renderers.describe() if t["id"] == sid), None)
        except Exception:
            target = None
        if target is None or not target.get("available"):
            return "skipped", (target or {}).get("reason") or "not available on this radio"
        if target.get("name") == name:
            return "done", "already " + name
        self._step(sid, "working")
        deadline = self.clock() + BUSY_WAIT_S
        while True:
            try:
                with self.renderer_lock:
                    renderers.set(sid, name=name)
                return "done", ""
            except RendererError as exc:
                if "busy" not in str(exc) or self.clock() >= deadline:
                    return "failed", str(exc)
                self._step(sid, "working", "waiting for moOde")
                self.sleep(BUSY_POLL_S)

    def _spotify(self, name: str) -> tuple[str, str]:
        if not self.spotify_config.is_file():
            return "skipped", "Spotify isn't set up on this radio"
        if spotify_device_name(self.spotify_config) == name:
            return "done", "already " + name
        self._step("spotify", "working")
        try:
            if not os.access(self.spotify_config, os.W_OK):
                return "failed", f"can't write {self.spotify_config}"
            set_device_name(self.spotify_config, name)
        except OSError as exc:
            return "failed", f"can't write go-librespot's settings: {exc.strerror or exc}"
        try:
            self.restart_spotify()
        except Exception as exc:
            return "failed", f"renamed; restart Spotify to see it ({exc})"
        log.info("Spotify Connect renamed %r; go-librespot restarted", name)
        return "done", ""
