"""The key-layout web page: one HTML file and a small JSON API.

Standard library only (``http.server``), like the YTM proxy, so the box gains
no dependencies. It listens on the LAN with no login - the same trust moOde's
own web UI extends - and can do nothing worse than change what a key plays.

Requests run on their own threads; everything that touches the layout goes
through ``WebApp`` under one lock, and the change itself is a reference swap
(see keymap.apply), so the 50 Hz panel loop never waits on the web.

The ``/api/ytm/*`` endpoints front the YouTube Music catalogue (search, the
owner's library and sign-in; player/sources/ytmusic.py). They answer
``{"available": false, ...}`` when there is no catalogue - no ytm source, or no
ytmusicapi - so the page can simply hide that part. They do not take the layout
lock: the catalogue has its own, and a slow search must not hold up a save.
The sign-in body is the account's cookie: it is never logged or echoed back.

The remote control (``/api/press``, ``/api/volume``, ``/api/next``,
``/api/standby``) drives the radio exactly as the cabinet does: a pressed key is
dispatched as the same ``Event`` the panel would emit, so "press the playing
key again" means the same thing from the phone as from the wood. Volume is on
the knob's 0-100 scale; the player maps it inside ``volume_limit``, which
``/api/settings`` can change (persisted to ``settings.json`` in the state dir,
read again at startup by main.build, overriding haslerd.yaml). The pilot
lamp's ``location`` and ``night`` mode live there too (lamp/night.py); GET adds
today's sunrise/sunset and the night being dimmed, and ``WebApp.lamp_listeners``
hear a change at once.

The lamp's colours (Settings -> Pilot lamp -> Lamp colours): ``GET
/api/lamp/palette`` is ``{defaults, saved, effective, levels, lamp,
preview_s}`` (names from the engine's palette, never "off"); ``PUT`` (or POST)
``{"palette": {name: [r, g, b]}}`` replaces the saved colours (settings.json
``lamp_palette``, linear ratios 0..1; a colour equal to the release's is not
kept), ``DELETE`` goes back to the release's palette. ``WebApp.lamp_listeners``
hear the doc, so the engine's ``set_palette`` follows at once.
``POST /api/lamp/preview {"rgb": [r, g, b], "level": "full"|"night"|"ember"|0..1}``
shows a colour on the lamp for 30 s after the last call (only the shutdown
countdown outranks it); ``{"rgb": null}`` ends it.

Knob calibration (``/api/knobs``) is the guided version of what we did by
hand on the bench: the page asks the owner to hold each knob still at a few
positions, reads ``GET /api/knobs/<id>/sample`` (the panel loop's own recent
readings - the web never touches the ADC), previews a fit
(``POST .../fit``, panel/calibrate.py), and ``PUT .../calibration`` saves it
to the box's calibration overlay (boxconfig, ``/etc/hasler/variant.d``) and
applies it live; ``DELETE`` goes back to the shipped curve. Positions are
measured from the knob's quiet end - see calibrate.py.

**Who may change things.** The page has no login, so every state-changing
request (POST/PUT/PATCH/DELETE) must look like it came from the page itself
(``request_refusal``): ``Content-Type: application/json`` (a cross-site form
or ``fetch`` can't send that without a CORS preflight, which this server never
answers), a ``Host`` that is one of the box's own names or addresses (defeats
DNS rebinding: an attacker's name pointed at the box is refused) and an
``Origin`` / ``Referer``, when sent, on that same host. Anything else is a 403.
``/api/components/yt-dlp/canary`` is the yt-dlp lane's canary, loopback only
(haslerd/ytdlp_lane.py); ``/api/software/yt-dlp/update|rollback`` are the
Music services buttons.
Installing or rolling back a release, changing the Wi-Fi (except from a phone
on the setup hotspot), resetting the keys and changing the release channel
also need a press on the radio (web/confirm.py); ``/api/software`` is the
"About this radio" card (web/software.py).

``/api/browse`` serves the curated library (web/catalog.py), and anything under
``/api/ha`` (and ``/api/sources``, ``/api/casting``, ``/api/group``,
``/api/peer`` - web/group_api.py, multiroom) is handed to
``WebApp.extra_routes`` - objects with ``route(method, path, body) -> (status,
doc) | None`` (web/ha_api.py, web/sources_api.py) - so Home Assistant's
endpoints live with Home Assistant's code. ``/api/info`` and
``/api/art/current`` (web/integration.py) are what the Home Assistant
integration reads besides ``/api/status`` (whose ``art`` points at the latter).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..actions import Actions
from ..lamp import night
from ..lamp import engine as lamp_engine
from .. import boxconfig
from ..panel import calibrate
from ..panel.controls import RAW_HISTORY, AnalogSelector, Event, Pot
from ..player.base import Preset
from ..player.glr_config import SpotifyGuests, guests_from
from ..player.moode import treble_db
from . import keymap, stations
from ..net import HOTSPOT_ADDRS
from .confirm import ConfirmBroker, ConfirmError, ConfirmUnavailable
from . import integration
from .room import Room
from .software import NotInstalled, Software, SoftwareError
from .wifi import Wifi
from . import wifi_page

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
FONTS = STATIC / "fonts"
FONT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.woff2$")
MAX_BODY = 256 * 1024

PRESS_EVENTS = ("tap", "hold")
SETTINGS_FILE = "settings.json"
VOLUME_LIMIT_RANGE = (30, 100)   # below 30 the knob is barely audible; 100 = no ceiling
VOLUME_FLOOR_GAP = 10            # the floor stays this far under the ceiling: the knob keeps a sweep
VOLUME_GUESS = 40.0              # a relative nudge with no reading starts here, as volume_step does
KNOB_ID = re.compile(r"^/api/knobs/([A-Za-z0-9_.-]{1,64})/(sample|fit|calibration)$")
HYSTERESIS_MAX = 5000
CONFIRM_ID = re.compile(r"^/api/confirm/([A-Za-z0-9_-]{10,64})$")
# Names the box answers to besides its own hostname (+ web.hostnames in box.yaml).
OWN_NAMES = ("localhost", "hasler", "hasler.local")
LOOPBACK = ("127.0.0.1", "::1")
# Paths served by ``WebApp.extra_routes`` (web/ha_api.py, web/sources_api.py, web/knobs_api.py).
EXTRA_PREFIXES = ("/api/ha", "/api/sources", "/api/casting", "/api/bluetooth", "/api/group", "/api/peer",
                  "/api/knobs/functions", "/api/knobs/try",   # web/knobs_api.py
                  "/api/help",                                # web/help_api.py
                  "/api/spotify")                             # web/spotify_account_api.py

# A key's tap action as the remote's caption says it, when it isn't a preset.
TAP_LABELS = {
    "cycle_source": "Next source",
    "standby_toggle": "Standby",
    "join_group": "Join group",
    "volume_up": "Volume up",
    "volume_down": "Volume down",
    "bt_pair": "Bluetooth pairing",
    "bluetooth": "Bluetooth",
    "ha_scene": "Scene",
    "ha_script": "Script",
    "ha_toggle": "Toggle",
    "ha_automation": "Automation",
    "ha_light": "Lights",
    "ha_service": "Home Assistant",
}


# -- settings.json ----------------------------------------------------------


def load_settings(path: Path | None) -> dict:
    """The page's settings file, or {} if absent or unreadable. Never raises."""
    if path is None or not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        log.exception("could not read %s; using box.yaml's settings and the defaults", path)
        return {}
    return doc if isinstance(doc, dict) else {}


def save_settings(path: Path, doc: dict) -> None:
    """Write atomically (tmp + fsync + rename), world-readable (0644), stamped
    with the config ``schema`` so a later migration knows what it reads: a
    power cut leaves the old file."""
    doc = {"schema": boxconfig.SCHEMA, **doc}
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    try:
        boxconfig.atomic_write_text(path, text, mode=0o644)
    except PermissionError as exc:
        raise Unavailable(f"can't save settings to {path}: {exc.strerror}") from exc


def volume_limit_from(raw: Any) -> int:
    """A submitted ceiling -> int in VOLUME_LIMIT_RANGE, or ValueError saying why."""
    lo, hi = VOLUME_LIMIT_RANGE
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw):
        raise ValueError(f"volume_limit must be a whole number from {lo} to {hi}")
    value = int(raw)
    if not lo <= value <= hi:
        raise ValueError(f"volume_limit must be from {lo} to {hi}")
    return value


def volume_floor_range(limit: int | None) -> tuple[int, int]:
    """What ``volume_floor`` may be under this ceiling: 0..limit-10."""
    limit = VOLUME_LIMIT_RANGE[1] if limit is None else int(limit)
    return 0, max(0, limit - VOLUME_FLOOR_GAP)


def volume_floor_from(raw: Any, limit: int | None) -> int:
    """A submitted floor -> int in volume_floor_range(limit), or ValueError saying why."""
    lo, hi = volume_floor_range(limit)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw):
        raise ValueError(f"volume_floor must be a whole number from {lo} to {hi}")
    value = int(raw)
    if not lo <= value <= hi:
        raise ValueError(f"volume_floor must be from {lo} to {hi} (volume_limit - {VOLUME_FLOOR_GAP})")
    return value


# settings.json keys the lamp listens to (kept in memory without a settings file).
LAMP_KEYS = ("location", "night", "lamp_palette")


def _rgb_from(raw: Any, name: str) -> list[float]:
    """[r, g, b], three numbers 0..1 (linear LED ratios)."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 3:
        raise ValueError(f"{name} must be [r, g, b]")
    out = []
    for value in raw:
        value = _number(value, name)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name}: each channel must be 0..1")
        out.append(round(value, 4))
    return out


def palette_from(raw: Any, known: Any) -> dict[str, list[float]]:
    """A saved lamp palette: ``{name: [r, g, b]}`` with names from ``known``."""
    if not isinstance(raw, dict):
        raise ValueError("palette must be {name: [r, g, b]}")
    out = {}
    for name, rgb in raw.items():
        if name not in known or name == "off":
            raise ValueError(f"unknown lamp colour {name!r} (known: {', '.join(n for n in known if n != 'off')})")
        out[name] = _rgb_from(rgb, name)
    return out


class Unavailable(Exception):
    """This radio can't do that right now (HTTP 503) - not the request's fault."""


def _number(raw: Any, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(raw)
    if value != value:   # NaN
        raise ValueError(f"{name} must be a number")
    return value


class WebApp:
    """The page's operations, independent of HTTP so they test without a socket."""

    def __init__(
        self,
        actions: Actions,
        panel: Any,
        keys_file: Path | None,
        sources: dict[str, bool],
        ha: bool,
        station_db: str = stations.MOODE_DB,
        wifi: Wifi | None = None,
        catalog: Any = None,
        settings_file: Path | None = None,
        browse: Any = None,
        mono: Any = None,
    ) -> None:
        self.actions = actions
        self.panel = panel
        self.keys_file = keys_file
        self.sources = sources
        # A bool, or a callable answering "is Home Assistant connected?" live.
        self.ha = ha
        self.settings_file = settings_file
        # The curated Browse library (web/catalog.Catalog), or None.
        self.browse = browse
        # audio.MonoSwitch (L+R downmix for the one speaker), or None.
        self.mono = mono
        # Handlers for /api/ha/*: ``route(method, path, body) -> (status, doc) | None``.
        self.extra_routes: list[Any] = []
        # multiroom.group.Group (web/group_api.py), or None: multiroom not installed.
        self.group: Any = None
        # Called with the whole saved settings doc after the lamp's location or
        # night mode changes, so the lamp needn't re-read settings.json.
        self.lamp_listeners: list[Callable[[dict], None]] = []
        # Without a settings file (no state dir) the lamp's choices live here.
        self._lamp_settings: dict = {}
        # The lamp engine (lamp/engine.LampEngine: set_palette, preview), or
        # None for a radio without one; main() sets it once the lamp is up.
        self.lamp: Any = None
        # player/watchdog.StreamWatchdog (main.py): MPD stopping a preset by itself.
        self.stream_watch: Any = None
        self.station_db = station_db
        # /api/info and /api/art/current, for the Home Assistant integration (web/integration.py).
        self.art = integration.ArtProxy(station_db)
        self.machine_id_file = integration.MACHINE_ID
        # The room name and the casting names that follow it (web/room.py).
        self.room = Room(settings_file, renderers=lambda: getattr(self.actions.player, "renderers", None))
        self.room.load(load_settings(settings_file))
        # Spotify for guests on the Wi-Fi: go-librespot's zeroconf_enabled follows
        # settings.json spotify_guests (player/glr_config.py). main.build_web
        # applies it at start; put_settings on every change.
        self.spotify_guests = SpotifyGuests()
        self.wifi = wifi or Wifi(db=station_db)
        self.lock = threading.Lock()
        # The ytm source (for now_playing) and its catalogue, if the player has them.
        self.ytm = (getattr(actions.player, "sources", None) or {}).get("ytm")
        self.catalog = catalog if catalog is not None else getattr(self.ytm, "catalog", None)
        # The descriptor's own layout, for "reset to defaults". Taken before any
        # saved overlay is laid on top.
        self.defaults = (copy.deepcopy(actions.variant.presets), copy.deepcopy(actions.variant.actions))
        self.default_combos = copy.deepcopy(list(actions.variant.combos or []))
        # Knob calibration. ``box_paths`` (boxconfig.Paths) says where the
        # overlay lives - None and calibrations can be previewed but not saved.
        # ``reload_variant()`` (main.apply_overlay) - or ``on_overlay_saved(
        # variant_name)`` - re-applies the box's overlay to the running panel
        # after a save or delete; with neither the one knob is respecced here.
        self.box_paths: Any = None
        self.reload_variant: Callable[[], Any] | None = None
        self.on_overlay_saved: Callable[[str], None] | None = None
        self._knob_lock = threading.Lock()
        # A press on the radio for the risky buttons (web/confirm.py). The lamp
        # hook is read at call time: the LampKeeper sets it after build_web.
        self.confirm = ConfirmBroker(panel, reply=self._lamp_reply)
        # "About this radio" (web/software.py); main.build_web adds box paths.
        self.software = Software(
            state_dir=settings_file.parent if settings_file is not None else None,
            idle=self._idle, fallback_version=self._release_version)
        # The yt-dlp lane's reader, canary and trigger (haslerd/ytdlp_lane.py); main.build_web.
        self.ytdlp_lane: Any = None
        # Extra Host names accepted for state-changing requests (box.yaml web.hostnames).
        self.allowed_hosts: set[str] = set()
        # Local addresses that are the setup hotspot: Wi-Fi changes from a phone
        # joined to it need no key press (that phone is next to the radio, and
        # the radio has no other network then).
        self.hotspot_addrs: set[str] = set(HOTSPOT_ADDRS)

    def _lamp_reply(self, name: str) -> None:
        reply = getattr(self.actions, "_reply", None)
        if callable(reply):
            reply(name)

    def _idle(self) -> bool:
        from .. import health
        return bool(health.health_report(self).get("idle"))

    @staticmethod
    def _release_version() -> str:
        from .. import health
        return health._version()

    # -- who may change things ------------------------------------------------

    def own_names(self) -> set[str]:
        names = set(OWN_NAMES) | set(LOOPBACK) | set(HOTSPOT_ADDRS) | self.allowed_hosts
        try:
            host = socket.gethostname().lower().rstrip(".")
            if host:
                names |= {host, host.split(".")[0], host.split(".")[0] + ".local"}
        except OSError:
            pass
        return names

    def request_refusal(self, method: str, headers: Any, local_addr: str | None,
                        form: bool = False) -> str | None:
        """Why a state-changing request is refused (CSRF / DNS rebinding), or None.

        ``headers`` is the request's (``.get``); ``local_addr`` the address the
        client connected to - a Host that is that very IP is the box. ``form``
        also takes a plain HTML form post (the Wi-Fi page without JavaScript);
        its Host and Origin are checked all the same.
        """
        ctype = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if form and ctype == "application/x-www-form-urlencoded":
            pass
        elif ctype != "application/json":
            return f"{method} needs Content-Type: application/json"
        host_header = (headers.get("Host") or "").strip().lower()
        host = _host_only(host_header).strip("[]").rstrip(".")
        local = (local_addr or "").lower()
        if local.startswith("::ffff:"):
            local = local[7:]
        if not host or (host not in self.own_names() and host != local):
            return f"not this radio's address: {host_header or '(no Host)'}"
        for name in ("Origin", "Referer"):
            value = headers.get(name)
            if value is None:
                continue
            try:
                netloc = urlparse(value.strip()).netloc.lower()
            except ValueError:
                netloc = ""
            if netloc != host_header:
                return f"cross-site request refused ({name}: {value.strip()[:80] or 'empty'})"
            break       # Origin decides when present; Referer only without it
        return None

    def wifi_join(self, body: Any, local_addr: str | None) -> None:
        body = body if isinstance(body, dict) else {}
        ssid = str(body.get("ssid", ""))
        if (local_addr or "") not in self.hotspot_addrs:
            self.confirm.consume(body.get("confirm"), "wifi", {"ssid": ssid})
        self.wifi.join(ssid, str(body.get("password", "")), str(body.get("security", "auto")))

    def wifi_retry(self, body: Any) -> dict:
        """Setup mode's "try my Wi-Fi again" (haslerd/wifiwatch.py). No key press:
        the helper does nothing unless the radio is its own hotspot, and then the
        only way here is from a phone on that hotspot. ``force`` (default) drops
        that phone for the minute it takes."""
        body = body if isinstance(body, dict) else {}
        force = bool(body.get("force", True))
        watch = getattr(self.wifi, "watch", None)
        reply = watch.try_now(force=force) if watch is not None else self.wifi.retry(force=force)
        return {"ok": bool(reply.get("ok", True)), **{k: v for k, v in reply.items() if k != "ok"}}

    def confirmed_reset(self, body: Any) -> dict:
        body = body if isinstance(body, dict) else {}
        self.confirm.consume(body.get("confirm"), "reset", {})
        return self.reset()

    # -- software (web/software.py) ---------------------------------------------

    def software_install(self, body: Any) -> dict:
        body = body if isinstance(body, dict) else {}
        version = str(body.get("version") or "")
        self.confirm.consume(body.get("confirm"), "update", {"version": version})
        return {"approved": self.software.install(version, str(body.get("when") or "now"))}

    def software_rollback(self, body: Any) -> None:
        body = body if isinstance(body, dict) else {}
        to = str(body.get("to") or "")
        self.confirm.consume(body.get("confirm"), "rollback", {"to": to})
        self.software.rollback(to)

    # -- the yt-dlp lane (haslerd/ytdlp_lane.py) ----------------------------------

    def _lane(self) -> Any:
        lane = getattr(self, "ytdlp_lane", None)
        if lane is None or not lane.root.is_dir():
            raise NotInstalled("the YouTube Music helper's updates are not on this radio")
        return lane

    def ytdlp_update(self) -> None:
        """Music services' "Update now": a lane run that also canaries the current one."""
        self._lane().update_now()

    def ytdlp_rollback(self, body: Any) -> None:
        """Music services' "Go back" - a press on the radio, like a release rollback."""
        body = body if isinstance(body, dict) else {}
        lane = self._lane()
        self.confirm.consume(body.get("confirm"), "rollback", {"component": "yt-dlp"})
        lane.rollback()

    def ytdlp_canary(self, body: Any, client: str | None) -> tuple[int, dict]:
        lane = getattr(self, "ytdlp_lane", None)
        if lane is None:
            return 404, {"ok": False, "error": "no yt-dlp lane"}
        return lane.canary(body, client)

    def software_settings(self, body: Any) -> dict:
        body = body if isinstance(body, dict) else {}
        self.software.check_settings(body)      # a bad value is a 400, not a key press
        channel = body.get("channel")
        if channel is not None and channel != self.software.box_settings()["channel"]:
            self.confirm.consume(body.get("confirm"), "channel", {"channel": str(channel)})
        return self.software.put_settings(body)

    def load_saved(self) -> None:
        with self.lock:
            keymap.apply_saved(self.keys_file, self.actions, self.panel)

    # -- operations -------------------------------------------------------

    def config(self) -> dict:
        return keymap.config_doc(self.actions, self.sources, self._ha_connected())

    def _ha_connected(self) -> bool:
        if not callable(self.ha):
            return bool(self.ha)
        try:
            return bool(self.ha())
        except Exception:
            log.debug("ha connected check failed", exc_info=True)
            return False

    def save(self, doc: Any) -> dict:
        with self.lock:
            specs, table, combos = keymap.parse_layout(doc, self.actions)   # raises keymap.Invalid
            if self.keys_file is not None:
                if combos is None:
                    keymap.save(self.keys_file, specs, table)           # keeps the file's combos
                else:
                    keymap.save(self.keys_file, specs, table, combos)
            keymap.apply(self.actions, self.panel, specs, table, combos)
            self._rewarm()
        log.info("key layout saved from the web page")
        return self.config()

    def reset(self) -> dict:
        with self.lock:
            if self.keys_file is not None:
                self.keys_file.unlink(missing_ok=True)
            specs, actions_table = copy.deepcopy(self.defaults)
            known = {f"{cid}.{e}" for cid in keymap.key_ids(self.actions) for e in keymap.EVENTS}
            table = {k: actions_table.get(k) for k in known}
            keymap.apply(self.actions, self.panel, specs, table, copy.deepcopy(self.default_combos))
            self._rewarm()
        log.info("key layout reset to the descriptor's defaults")
        return self.config()

    def play(self, raw: Any) -> None:
        """The page's Test button: play a preset as typed, saved or not."""
        errors: list[dict] = []
        spec = keymap.preset_from_page({**(raw or {}), "n": 0}, "preset", errors)
        if spec is None:
            raise keymap.Invalid(errors)
        preset = Preset.from_spec(spec)
        group = self.group
        if group is not None and group.following:
            group.leave("the Test button")      # it plays here, alone
        with self.lock:
            if self.actions.standby:
                self.actions._wake()
            self.actions.player.play_preset(preset)

    def stop(self) -> None:
        with self.lock:
            self.actions.player.stop()

    def status(self) -> dict:
        keys = set(keymap.key_ids(self.actions))
        try:
            states = self.panel.states()
        except Exception:
            states = {}
        pressed = [cid for cid, state in states.items() if cid in keys and state is True]
        tone = states.get("tone")
        current = self.actions.player.current()
        try:
            volume = self.actions.player.get_volume()
        except Exception:
            volume = None
        source, waiting, renderer = self._source(current)
        playing = keymap.preset_to_page(current) if current else None
        # MPD stopped it by itself (player/watchdog.py): not playing, whatever
        # current() says, until the watchdog has it back.
        watch = self.stream_watch
        stream = watch.status_doc() if watch is not None else {"recovering": False, "stalled_s": None}
        if watch is not None and watch.silent:
            playing = None
        track = self._track() if source == "ytm" else None
        return {
            "pressed": pressed,
            "playing": playing,
            # True while MPD should be playing and isn't, and the radio is
            # bringing it back; stalled_s = for how long (None = not stalled).
            "recovering": stream["recovering"],
            "stalled_s": stream["stalled_s"],
            "standby": self.actions.standby,
            "volume": None if volume is None else round(volume),
            # What owns the speaker: radio | spotify | ytm | bluetooth | airplay.
            # waiting = selected with PU but nothing connected yet.
            "source": source,
            "waiting": waiting,
            "renderer": renderer,
            # The YouTube Music track playing, named: {title, artist, album, thumb} or None.
            "track": track,
            # The picture for what plays (station logo / track thumbnail), served by
            # the radio itself; ``k`` changes with the picture. None = nothing to show.
            "art": None if self.actions.standby else self.art.url_for(source, playing, track),
            # The remote's keys, k1..k6 then PU, and which one is lit.
            "keys": self._keys(),
            "current_key": self._current_key(current),
            "volume_limit": self._volume_limit(),
            # The tone knob: 0-100 and what it does (None = no tone pot read yet):
            # {"value", "treble_db", "effect", "label", "text": "Warmer · 40%"}.
            "tone": self._tone_doc(tone),
            # The tone knob's end switch: True = radio on, False = clicked off, None = not fitted.
            "knob_on": states.get("power") if isinstance(states.get("power"), bool) else None,
            # Multiroom: None alone, else {"role", "joining", "leader": {id, name}|None, "members": n}.
            "group": self._group_status(),
        }

    def _tone_doc(self, tone: Any) -> dict | None:
        if not isinstance(tone, (int, float)) or isinstance(tone, bool):
            return None
        doc = {"value": round(tone), "treble_db": treble_db(tone)}
        knobs = getattr(self, "knob_functions", None)   # dsp/knobs.py, set by main
        if knobs is not None and knobs.assignable("tone"):
            try:
                doc.update(knobs.describe("tone", tone))
            except Exception:
                log.debug("tone knob description failed", exc_info=True)
        return doc

    def _group_status(self) -> dict | None:
        group = self.group
        if group is None:
            return None
        try:
            return group.status_doc()
        except Exception:
            log.debug("multiroom status failed", exc_info=True)
            return None

    def info(self) -> dict:
        """``GET /api/info``: who this radio is, for the Home Assistant integration
        and for other Haslers (``multiroom``: {enabled, role, leader, members,
        playing, source, since} - multiroom/group.py ``info_doc``)."""
        doc = integration.info(self.actions.variant.name, self._release_version(), self.machine_id_file)
        group = self.group
        try:
            doc["name"] = self.room.name()
            if group is not None:
                # The id other radios list as a member must be the group's own.
                doc["id"], doc["name"] = group.identity()
            doc["multiroom"] = group.info_doc() if group is not None else \
                {"enabled": False, "role": "alone", "leader": None, "members": [], "playing": False,
                 "source": None, "since": None}
        except Exception:
            log.debug("multiroom info failed", exc_info=True)
        return doc

    def art_current(self) -> tuple[bytes, str, str] | None:
        """``GET /api/art/current``: (bytes, content type, etag), or None."""
        if self.actions.standby:
            return None
        return self.art.current(*integration.now_playing(self))

    def _keys(self) -> list[dict]:
        """Each key's caption from the live layout: preset name, or what its tap does."""
        table = self.actions.variant.actions
        presets = self.actions.presets
        out = []
        for cid in keymap.key_ids(self.actions):
            tap = keymap.to_page(table.get(f"{cid}.tap"))
            n = name = source = None
            if tap is None:
                name = ""
            elif tap["do"] == "preset":
                n = tap["n"]
                preset = presets.get(n)
                name = preset.name if preset is not None else f"Preset {n}"
                source = preset.source if preset is not None else None
            elif tap["do"] == "custom":
                name = str(tap.get("label") or "")
            else:
                name = TAP_LABELS.get(tap["do"], tap["do"])
            out.append({"id": cid, "label": keymap.label_for(cid), "n": n, "name": name, "source": source})
        return out

    def _current_key(self, current: Any) -> str | None:
        """The key whose tap preset is the one playing (first such key), else None."""
        if current is None or self.actions.standby:
            return None
        table = self.actions.variant.actions
        for cid in keymap.key_ids(self.actions):
            tap = table.get(f"{cid}.tap") or {}
            if tap.get("do") != "preset":
                continue
            try:
                n = int(tap.get("n"))
            except (TypeError, ValueError):
                continue
            preset = self.actions.presets.get(n)
            if preset is not None and current.n == n and current.source == preset.source:
                return cid
        return None

    def _volume_limit(self) -> int | None:
        """The mixer ceiling in percent: the player's, else the saved one, else None."""
        limit = getattr(getattr(self.actions.player, "config", None), "volume_limit", None)
        if limit is None:
            limit = load_settings(self.settings_file).get("volume_limit")
        try:
            return None if limit is None else int(limit)
        except (TypeError, ValueError):
            return None

    def _volume_floor(self) -> int | None:
        """Mixer % at the knob's first step: the player's, else the saved one, else None."""
        floor = getattr(getattr(self.actions.player, "config", None), "volume_floor", None)
        if floor is None:
            floor = load_settings(self.settings_file).get("volume_floor")
        try:
            return None if floor is None else int(floor)
        except (TypeError, ValueError):
            return None

    # -- remote control ---------------------------------------------------

    def press(self, body: Any) -> None:
        """A cabinet key pressed from the phone: the very Event the panel would send.
        ``{"combo": ["k1", "k2"]}`` fires that key combination ("Try it")."""
        body = body if isinstance(body, dict) else {}
        if "combo" in body:
            cid = keymap.find_combo(self.actions, body.get("combo"))
            if cid is None:
                raise ValueError(f"no key combination {body.get('combo')!r}")
            with self.lock:
                self.actions.dispatch(Event(cid, "combo"))
            return
        key, event = body.get("key"), body.get("event", "tap")
        if key not in keymap.key_ids(self.actions):
            raise ValueError(f"no key called {key!r}")
        if event not in PRESS_EVENTS:
            raise ValueError("event must be \"tap\" or \"hold\"")
        with self.lock:
            self.actions.dispatch(Event(key, event))   # logs and swallows, like the panel

    def volume(self, body: Any) -> int:
        """Set ({"value"}) or nudge ({"delta"}) the volume on the knob scale; returns it."""
        body = body if isinstance(body, dict) else {}
        player = self.actions.player
        with self.lock:
            if "value" in body:
                target = _number(body["value"], "value")
            elif "delta" in body:
                delta = _number(body["delta"], "delta")
                current = player.get_volume()
                target = (VOLUME_GUESS if current is None else float(current)) + delta
            else:
                raise ValueError("expected {\"value\": 0-100} or {\"delta\": ±n}")
            target = max(0.0, min(100.0, target))
            player.set_volume(target)
        return round(target)

    def next(self) -> None:
        with self.lock:
            self.actions.player.next_track()

    def set_standby(self, body: Any) -> bool:
        """Sleep, wake (and resume the last preset), or toggle with no "on"."""
        body = body if isinstance(body, dict) else {}
        on = body.get("on")
        if on is not None and not isinstance(on, bool):
            raise ValueError("on must be true or false")
        with self.lock:
            actions = self.actions
            want = (not actions.standby) if on is None else on
            if want and not actions.standby:
                actions._standby(None, {})          # what the knob's "off" does
            elif not want and actions.standby:
                actions._standby_toggle(None, {})   # wake, persist, resume_last
            return actions.standby

    # -- settings ---------------------------------------------------------

    def settings(self, host: str | None = None) -> dict:
        try:
            ssid = self.wifi.current_ssid()
        except Exception:
            log.debug("could not read the Wi-Fi name", exc_info=True)
            ssid = ""
        return {
            "volume_limit": self._volume_limit(),
            # What PUT accepts for volume_limit, so the page needn't hard-code it.
            "volume_limit_range": list(VOLUME_LIMIT_RANGE),
            # Mixer % at the knob's first step: the quietest audible one. Saved
            # here wins over box.yaml's moode.volume_floor.
            "volume_floor": self._volume_floor(),
            # What PUT accepts for volume_floor under the current ceiling.
            "volume_floor_range": list(volume_floor_range(self._volume_limit())),
            "mono": self._mono_doc(),
            **self._lamp_doc(),
            "wifi": {"ssid": ssid},
            "room": self.room.doc(),
            "spotify_guests": self._spotify_guests(),
            "version": f"haslerd {__version__}",
            "moode_url": f"http://{_host_only(host) or socket.gethostname() + '.local'}/",
        }

    def put_settings(self, body: Any, host: str | None = None) -> dict:
        """Validate, persist, apply live: ``volume_limit``, ``volume_floor``, ``mono``,
        ``location``, ``night``, ``room`` (web/room.py), ``spotify_guests``."""
        keys = {"volume_limit", "volume_floor", "mono", "location", "night", "room", "spotify_guests"}
        if not isinstance(body, dict) or not (keys & body.keys()):
            raise ValueError("expected {\"volume_limit\": 30-100}, {\"volume_floor\": 0-(limit-10)}, "
                             "{\"mono\": true|false}, "
                             "{\"location\": {\"name\", \"lat\", \"lon\"}} and/or "
                             "{\"night\": \"sun\"|\"off\"|\"22:00-07:00\"}, "
                             "{\"room\": {\"name\", \"casting_names\"}}, "
                             "{\"spotify_guests\": true|false}")
        limit = volume_limit_from(body["volume_limit"]) if "volume_limit" in body else None
        floor = (volume_floor_from(body["volume_floor"], limit if limit is not None else self._volume_limit())
                 if "volume_floor" in body else None)
        if floor is None and limit is not None:
            # A lower ceiling pulls a floor that no longer fits down with it.
            current = self._volume_floor()
            if current is not None and current > volume_floor_range(limit)[1]:
                floor = volume_floor_range(limit)[1]
        place = night.location_from(body["location"]) if "location" in body else None
        mode = night.night_from(body["night"]) if "night" in body else None
        room = self.room.change(body["room"]) if "room" in body else None
        mono = body.get("mono")
        if "mono" in body and not isinstance(mono, bool):
            raise ValueError("mono must be true or false")
        if mono is not None and (self.mono is None or not self.mono.available):
            raise ValueError("mono output isn't installed on this radio (run install.sh)")
        guests = body.get("spotify_guests")
        if "spotify_guests" in body and not isinstance(guests, bool):
            raise ValueError("spotify_guests must be true or false")
        with self.lock:
            if mono is not None:
                try:
                    self.mono.set(mono)
                except RuntimeError as exc:          # audio.MonoError
                    raise ValueError(str(exc)) from exc
            if limit is not None or floor is not None:
                self._apply_volume_limit(limit, floor)
            if self.settings_file is not None:
                doc = load_settings(self.settings_file)
                if limit is not None:
                    doc["volume_limit"] = limit
                if floor is not None:
                    doc["volume_floor"] = floor
                if mono is not None:
                    doc["mono"] = mono
                if place is not None:
                    doc["location"] = place
                if mode is not None:
                    doc["night"] = mode
                if room is not None:
                    doc["room"] = room
                if guests is not None:
                    doc["spotify_guests"] = guests
                doc.setdefault("schema", boxconfig.SCHEMA)
                save_settings(self.settings_file, doc)
            else:
                doc = self._lamp_settings
                if place is not None:
                    doc["location"] = place
                if mode is not None:
                    doc["night"] = mode
            if room is not None:
                self.room.commit(room, body["room"])
            if guests is not None:
                if self.settings_file is None:
                    self._guests = guests
                self.spotify_guests.apply_async(guests)
            if place is not None or mode is not None:
                self._lamp_settings = {k: doc[k] for k in LAMP_KEYS if k in doc}
                for listener in self.lamp_listeners:
                    try:
                        listener(dict(doc))
                    except Exception:
                        log.exception("a lamp settings listener failed")
        if place is not None:
            log.info("lamp location set to %s (%.4f, %.4f) from the web page",
                     place["name"] or "unnamed", place["lat"], place["lon"])
        if mode is not None:
            log.info("lamp night mode set to %s from the web page", mode)
        if limit is not None:
            log.info("volume limit set to %s%% from the web page", limit)
        if floor is not None:
            log.info("volume floor set to %s%% from the web page", floor)
        if mono is not None:
            log.info("output set to %s from the web page", "mono" if mono else "stereo")
        if guests is not None:
            log.info("Spotify for guests %s from the web page", "on" if guests else "off")
        return self.settings(host)

    def lamp_settings(self) -> dict:
        """The saved ``location`` / ``night`` (settings.json, else this session's)."""
        if self.settings_file is not None:
            return load_settings(self.settings_file)
        return self._lamp_settings

    # -- the lamp's colours ------------------------------------------------

    def _palette_defaults(self) -> dict[str, tuple[float, float, float]]:
        base = getattr(self.lamp, "base_palette", None)
        if not isinstance(base, dict):
            base = lamp_engine.DEFAULT_PALETTE
        return {name: tuple(rgb) for name, rgb in base.items() if name != "off"}

    def _saved_palette(self, defaults: dict) -> dict[str, list[float]]:
        """settings.json ``lamp_palette``, keeping only well-formed known colours."""
        raw = self.lamp_settings().get("lamp_palette")
        if not isinstance(raw, dict):
            return {}
        saved = {}
        for name, rgb in raw.items():
            try:
                saved.update(palette_from({name: rgb}, defaults))
            except ValueError:
                log.warning("ignoring lamp_palette %r in settings.json", name)
        return saved

    def lamp_palette(self) -> dict:
        defaults = self._palette_defaults()
        saved = self._saved_palette(defaults)
        effective = {name: list(saved.get(name, rgb)) for name, rgb in defaults.items()}
        return {
            "defaults": {name: list(rgb) for name, rgb in defaults.items()},
            "saved": saved,
            "effective": effective,
            "levels": dict(lamp_engine.PREVIEW_LEVELS),
            "preview_s": lamp_engine.PREVIEW_S,
            "lamp": self.lamp is not None,
        }

    def put_lamp_palette(self, body: Any) -> dict:
        """Replace the saved colours: ``{"palette": {name: [r, g, b]}}`` ({} = release)."""
        if not isinstance(body, dict) or "palette" not in body:
            raise ValueError('expected {"palette": {"amber": [r, g, b], ...}}')
        defaults = self._palette_defaults()
        palette = palette_from(body["palette"], defaults)
        # Only what differs from the release: a later release's retune still lands.
        palette = {n: rgb for n, rgb in palette.items()
                   if any(abs(a - b) > 1e-6 for a, b in zip(rgb, defaults[n]))}
        with self.lock:
            if self.settings_file is not None:
                doc = load_settings(self.settings_file)
                if palette:
                    doc["lamp_palette"] = palette
                else:
                    doc.pop("lamp_palette", None)
                doc.setdefault("schema", boxconfig.SCHEMA)
                save_settings(self.settings_file, doc)
            else:
                doc = self._lamp_settings
                if palette:
                    doc["lamp_palette"] = palette
                else:
                    doc.pop("lamp_palette", None)
            self._lamp_settings = {k: doc[k] for k in LAMP_KEYS if k in doc}
            for listener in self.lamp_listeners:
                try:
                    listener(dict(doc))
                except Exception:
                    log.exception("a lamp settings listener failed")
        log.info("lamp colours %s from the web page",
                 "set (" + ", ".join(sorted(palette)) + ")" if palette else "back to the release's")
        return self.lamp_palette()

    def lamp_preview(self, body: Any) -> dict:
        """``{"rgb": [r, g, b], "level": name|0..1}`` on the lamp for a while; ``{"rgb": null}`` ends it."""
        if not isinstance(body, dict) or "rgb" not in body:
            raise ValueError('expected {"rgb": [r, g, b], "level": "full"|"night"|"ember"} or {"rgb": null}')
        rgb = None if body["rgb"] is None else _rgb_from(body["rgb"], "rgb")
        raw = body.get("level", "full")
        if isinstance(raw, str):
            if raw not in lamp_engine.PREVIEW_LEVELS:
                raise ValueError("level must be one of " + ", ".join(lamp_engine.PREVIEW_LEVELS))
            level = lamp_engine.PREVIEW_LEVELS[raw]
        else:
            level = _number(raw, "level")
            if not 0.0 <= level <= 1.0:
                raise ValueError("level must be 0..1")
        lamp = self.lamp
        preview = getattr(lamp, "preview", None)
        if callable(preview):
            preview(rgb, level)
        return {"lamp": lamp is not None, "showing": rgb is not None and lamp is not None,
                "expires_s": lamp_engine.PREVIEW_S if rgb is not None else 0}

    def _lamp_doc(self) -> dict:
        saved = self.lamp_settings()
        try:
            sun = night.tonight(saved)
        except Exception:                           # never let the sky break Settings
            log.debug("could not work out tonight's sun", exc_info=True)
            sun = None
        return {"location": night.location_doc(saved), "night": night.mode_from_settings(saved),
                "tonight": sun}

    def _spotify_guests(self) -> bool:
        """settings.json ``spotify_guests`` (default on): go-librespot's zeroconf."""
        if self.settings_file is not None:
            return guests_from(load_settings(self.settings_file))
        return getattr(self, "_guests", guests_from({}))

    def apply_spotify_guests(self):
        """Make go-librespot's config match the saved setting (off-thread; at start)."""
        return self.spotify_guests.apply_async(self._spotify_guests())

    def _mono_doc(self) -> dict:
        if self.mono is None or not self.mono.available:
            return {"available": False, "on": False}
        return {"available": True, "on": self.mono.is_on()}

    def _apply_volume_limit(self, limit: int | None, floor: int | None = None) -> None:
        """New ceiling and/or floor, same knob position: re-set the knob so the
        change is heard.

        The knob is read under the old ceiling first, so lowering the limit
        turns the speaker down at once rather than at the next knob move. In
        standby nothing is re-applied: setting a level could unmute a mixer
        that mutes by volume 0.
        """
        player = self.actions.player
        config = getattr(player, "config", None)
        if config is None or not hasattr(config, "volume_limit"):
            return
        try:
            knob = player.get_volume()
        except Exception:
            knob = None
        if limit is not None:
            config.volume_limit = limit
        if floor is not None:
            config.volume_floor = floor
        if knob is not None and not self.actions.standby:
            try:
                player.set_volume(float(knob))
            except Exception:
                log.exception("could not re-apply the volume under the new limit")

    # -- knob calibration -------------------------------------------------

    @property
    def variant_name(self) -> str:
        return self.actions.variant.name

    def _knobs(self) -> list[Any]:
        return [c for c in getattr(self.panel, "controls", []) if isinstance(c, (Pot, AnalogSelector))]

    def _knob(self, cid: str) -> Any:
        for control in self._knobs():
            if control.id == cid:
                return control
        raise ValueError(f"no knob called {cid!r} on the {self.variant_name}")

    def _overlay(self) -> dict:
        """The box's overlay ({} if none, no paths, or unreadable). Never raises."""
        if self.box_paths is None:
            return {}
        try:
            doc = boxconfig.read_overlay(self.box_paths, self.variant_name)
        except Exception:
            log.debug("could not read the calibration overlay", exc_info=True)
            return {}
        return doc if isinstance(doc, dict) else {}

    def knobs(self) -> dict:
        overlay = self._overlay()
        entries = overlay.get("controls") or {}
        states = self._states()
        return {
            "variant": self.variant_name,
            "overlay": bool(entries or overlay.get("io")),
            "knobs": [self._knob_doc(c, entries, states) for c in self._knobs()],
        }

    def _states(self) -> dict:
        try:
            return self.panel.states()
        except Exception:
            return {}

    def _knob_doc(self, control: Any, entries: dict, states: dict) -> dict:
        now = control.sample(5)
        doc: dict[str, Any] = {
            "id": control.id,
            "label": control.label or control.id.replace("_", " ").capitalize(),
            "kind": "pot" if isinstance(control, Pot) else "selector",
            "channel": control.channel,
            "raw": None if now is None else now["raw"],
            "value": control.state(),
            "source": "box" if entries.get(control.id) else "shipped",
            "end_switch": None,
        }
        if isinstance(control, Pot):
            spec = control.spec
            doc.update({
                "taper": spec.taper,
                "range": list(spec.counts_range) if spec.counts_range else None,
                "curve": [[r, v] for r, v in spec.curve] if spec.curve else None,
                "hysteresis_counts": spec.hysteresis,
                "direction": spec.direction,
            })
            if control.end_switch_id:
                state = states.get(control.end_switch_id)
                doc["end_switch"] = {"id": control.end_switch_id,
                                     "state": state if isinstance(state, bool) else None}
        else:
            doc.update({"positions": list(range(control.positions)),
                        "thresholds": control.thresholds})
        return doc

    def knob_sample(self, cid: str, n: Any = 15) -> dict:
        """The knob's last ``n`` polls: median raw, spread, noise, held still?"""
        control = self._knob(cid)
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise ValueError("n must be a whole number") from None
        if not 1 <= n <= RAW_HISTORY:
            raise ValueError(f"n must be from 1 to {RAW_HISTORY}")
        doc = control.sample(n)
        if doc is None:
            return {"raw": None, "min": None, "max": None, "stable": False, "noise": None,
                    "value": None, "count": 0}
        doc["value"] = control.scale(doc["raw"]) if isinstance(control, Pot) else control.decode(doc["raw"])
        return doc

    def knob_fit(self, cid: str, body: Any) -> dict:
        """Preview a calibration from measured points. Saves nothing.

        A selector takes the same ``points`` as a pot, one per position with
        ``position = i / (n - 1)``; its preview's ``curve`` is those readings
        ([raw, i / (n - 1) * 100], sorted by raw) beside the ``thresholds``
        it decodes by, and PUTting that curve back saves the thresholds.
        """
        control = self._knob(cid)
        body = body if isinstance(body, dict) else {}
        if isinstance(control, AnalogSelector):
            raws = self._selector_raws(control, body)
            try:
                thresholds = calibrate.fit_selector(raws, self._noise(body))
            except ValueError as exc:     # a measurement to redo, not a bad request
                return {"curve": None, "thresholds": None, "direction": None,
                        "hysteresis_counts": control.band, "warnings": [str(exc)],
                        "redo": list(range(control.positions))}
            return self._selector_doc(control, raws, thresholds)
        points = body.get("points")
        if not isinstance(points, list):
            raise ValueError("expected {\"points\": [{\"position\": 0-1, \"raw\": n}, ...]}")
        return calibrate.fit_pot(points, body.get("direction") or "auto", self._noise(body)).to_doc()

    @staticmethod
    def _noise(body: dict) -> float | None:
        noise = body.get("noise")
        return None if noise is None else _number(noise, "noise")

    @staticmethod
    def _selector_doc(control: AnalogSelector, raws: list[int], thresholds: list[int]) -> dict:
        n = control.positions
        return {
            "curve": sorted([int(r), round(i / (n - 1) * 100.0, 1)] for i, r in enumerate(raws)),
            "thresholds": thresholds,
            "direction": "normal" if raws[-1] > raws[0] else "reversed",
            "hysteresis_counts": control.band,
            "warnings": [],
            "redo": [],
        }

    @staticmethod
    def _selector_raws(control: AnalogSelector, body: dict) -> list[int]:
        """One reading per position, in position order, from ``points``
        ({position: i/(n-1), raw}), ``curve`` ([raw, i/(n-1)*100]) or
        ``positions`` ([raw, ...])."""
        n = control.positions
        if "points" in body or "curve" in body:
            items = body.get("points") if "points" in body else body.get("curve")
            if not isinstance(items, list) or len(items) != n:
                raise ValueError(f"expected one reading for each of the {n} positions")
            pairs = []
            for item in items:
                if "points" in body:
                    if not isinstance(item, dict):
                        raise ValueError("each point must be {\"position\", \"raw\"}")
                    pos, raw = item.get("position"), item.get("raw")
                else:
                    if not isinstance(item, (list, tuple)) or len(item) != 2:
                        raise ValueError("each curve point must be [raw, value]")
                    raw, pos = item[0], _number(item[1], "a curve value") / 100.0
                pos, raw = _number(pos, "position"), _number(raw, "raw")
                index = pos * (n - 1)
                if abs(index - round(index)) > 0.01 or not 0 <= round(index) < n:
                    raise ValueError(f"a {n}-position selector's positions are i/{n - 1} for i = 0..{n - 1}")
                pairs.append((int(round(index)), int(round(raw))))
            if sorted(i for i, _ in pairs) != list(range(n)):
                raise ValueError("measure each position once")
            return [raw for _, raw in sorted(pairs)]
        raws = body.get("positions")
        if not isinstance(raws, list) or len(raws) != n:
            raise ValueError(f"expected {{\"points\": [...]}} - one reading for each of the {n} positions")
        return [int(round(_number(raw, "a position's raw"))) for raw in raws]

    def _selector_thresholds(self, control: AnalogSelector, body: dict) -> list[int]:
        if "thresholds" not in body:
            return calibrate.fit_selector(self._selector_raws(control, body), self._noise(body))
        thresholds = body.get("thresholds")
        if not isinstance(thresholds, list) or len(thresholds) != control.positions - 1:
            raise ValueError(f"expected {{\"thresholds\": [...]}} with {control.positions - 1} counts")
        values = [int(_number(t, "a threshold")) for t in thresholds]
        steps = [b - a for a, b in zip(values, values[1:])]
        if not (all(d > 0 for d in steps) or all(d < 0 for d in steps)):
            raise ValueError("thresholds must run one way, rising or falling")
        return values

    def _pot_entry(self, control: Pot, body: dict) -> dict:
        direction = body.get("direction")
        if direction is not None and direction not in calibrate.DIRECTIONS:
            raise ValueError("direction must be auto, normal or reversed")
        if "points" in body:
            noise = body.get("noise")
            fit = calibrate.fit_pot(body["points"] if isinstance(body["points"], list) else [],
                                    direction or "auto", None if noise is None else _number(noise, "noise"))
            if not fit.ok:
                raise ValueError("; ".join(fit.warnings) or "these points don't make a curve")
            curve, direction, hysteresis = fit.curve, fit.direction, fit.hysteresis_counts
        elif "curve" in body:
            curve, measured = calibrate.check_curve(body["curve"])
            if direction not in (None, "auto", measured):
                raise ValueError(f"this curve is {measured}, not {direction}")
            direction = measured
            hysteresis = body.get("hysteresis_counts", control.hysteresis)
        else:
            raise ValueError("expected {\"curve\", \"direction\", \"hysteresis_counts\"} or {\"points\"}")
        if "hysteresis_counts" in body and "points" in body:
            hysteresis = body["hysteresis_counts"]
        if isinstance(hysteresis, bool) or not isinstance(hysteresis, (int, float)) \
                or hysteresis != int(hysteresis) or not 1 <= hysteresis <= HYSTERESIS_MAX:
            raise ValueError(f"hysteresis_counts must be a whole number from 1 to {HYSTERESIS_MAX}")
        return {"curve": curve, "direction": direction, "hysteresis_counts": int(hysteresis)}

    def put_calibration(self, cid: str, body: Any) -> dict:
        """Save a knob's calibration to the box overlay and apply it live."""
        control = self._knob(cid)
        body = body if isinstance(body, dict) else {}
        if isinstance(control, AnalogSelector):
            entry: dict = {"thresholds": self._selector_thresholds(control, body)}
        else:
            entry = self._pot_entry(control, body)
        paths = self._require_paths()
        with self._knob_lock:
            self._write_overlay(paths, {"controls": {cid: entry}})
            self._apply_knob(control, entry)
        log.info("%s calibrated from the web page: %s", cid, entry)
        return self._knob_doc(control, (self._overlay().get("controls") or {}), self._states())

    def delete_calibration(self, cid: str) -> dict:
        """Drop the box's calibration for a knob: back to the shipped one, live."""
        control = self._knob(cid)
        paths = self._require_paths()
        with self._knob_lock:
            self._write_overlay(paths, {"controls": {cid: None}})
            self._apply_knob(control, None)
        log.info("%s back to the shipped calibration", cid)
        return self._knob_doc(control, (self._overlay().get("controls") or {}), self._states())

    def _write_overlay(self, paths: Any, patch: dict) -> None:
        try:
            boxconfig.write_overlay(paths, self.variant_name, patch)
        except PermissionError as exc:
            raise Unavailable(f"can't save the calibration to {paths.overlay_file(self.variant_name)}: "
                              f"{exc.strerror}") from exc

    def _require_paths(self) -> Any:
        if self.box_paths is None:
            raise Unavailable("this radio has no box config directory, so calibrations can't be saved")
        return self.box_paths

    def _apply_knob(self, control: Any, entry: dict | None) -> None:
        """Make the running panel match the overlay just written."""
        hook = self.on_overlay_saved
        reload = self.reload_variant
        if hook is not None or reload is not None:
            try:
                hook(self.variant_name) if hook is not None else reload()
                return
            except Exception:
                log.exception("re-applying the overlay failed; respeccing %s directly", control.id)
        spec = dict(self._shipped_spec(control.id))
        spec.update(entry or {})
        control.respec_from_spec(spec)

    def _shipped_spec(self, cid: str) -> dict:
        """The release's own spec for a control (never the overlay): the
        release's descriptor, else the file the running variant was loaded
        from - ``actions.variant.controls`` already has the overlay in it."""
        from ..variant import load_variant
        sources = []
        if self.box_paths is not None:
            sources.append(lambda: self.box_paths.shipped_variant(self.variant_name))
        sources.append(lambda: self.actions.variant.path)
        for source in sources:
            try:
                path = source()
                if path is not None and Path(path).exists():
                    controls = load_variant(path).controls
                    return next((c for c in controls if c.get("id") == cid), {})
            except Exception:
                log.debug("could not read a shipped descriptor", exc_info=True)
        log.warning("no shipped descriptor for %s; %s goes back to the defaults", self.variant_name, cid)
        return {}

    # -- health -------------------------------------------------------------

    def health(self) -> tuple[int, dict]:
        """``GET /api/health`` (docs/ota-format.md): haslerd/health.py's report."""
        try:
            from .. import health
        except ImportError:
            return 503, {"ok": False, "error": "health report not installed"}
        return 200, health.health_report(self)

    # -- browse -----------------------------------------------------------

    def browse_doc(self) -> dict:
        if self.browse is None:
            return {"available": False, "sections": []}
        try:
            return {"available": True, "sections": self.browse.sections()}
        except Exception:
            log.exception("browse catalogue failed")
            return {"available": False, "sections": []}

    def browse_radio(self, query: str) -> dict:
        more = getattr(self.browse, "more_radio", None)
        if not callable(more):
            return {"items": []}
        try:
            return {"items": more(query)}
        except Exception:
            log.exception("browse radio search failed")
            return {"items": []}

    # -- extra routes (/api/ha) ---------------------------------------------

    def route_extra(self, method: str, path: str, body: Any) -> tuple[int, Any] | None:
        for handler in self.extra_routes:
            answer = handler.route(method, path, body)
            if answer is not None:
                return answer
        return None

    def _track(self) -> dict | None:
        now_playing = getattr(self.ytm, "now_playing", None)
        if not callable(now_playing):
            return None
        try:
            info = now_playing()
        except Exception:
            log.debug("ytm now_playing failed", exc_info=True)
            return None
        if not info:
            return None
        return {k: str(info.get(k) or "") for k in ("title", "artist", "album", "thumb")}

    def _source(self, current: Any) -> tuple[str, bool, dict | None]:
        player = self.actions.player
        try:
            source = player.active_source()
        except Exception:
            source = "radio"
        waiting = False
        if current is None and source == "radio":
            # MPD idle and nothing else reporting: if PU parked us on a
            # renderer, say so rather than "radio".
            parked = self.actions.cycle_position
            if parked and parked != "radio":
                source, waiting = parked, True
        renderer = None
        if source == "bluetooth":
            info = getattr(player, "renderer_info", None)
            renderer = info() if callable(info) else None
            if renderer is not None:
                # Connected but paused is not "waiting for a phone".
                waiting = not renderer.get("device")
        return source, waiting, renderer

    def cover(self, vid: str) -> str | None:
        """Cover URL for a YouTube video id, or None if it isn't one / no ytm."""
        from ..player.sources.ytm import VIDEO_ID
        if not VIDEO_ID.match(vid or ""):
            return None
        ytm = (getattr(self.actions.player, "sources", None) or {}).get("ytm")
        cover = getattr(ytm, "cover", None)
        return cover(vid) if callable(cover) else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

    def _resync_playlists(self) -> None:
        """Signing in or out changes what moOde's YT playlists should be."""
        ytm = (getattr(self.actions.player, "sources", None) or {}).get("ytm")
        sync = getattr(ytm, "playlists", None)
        if sync is not None:
            sync.kick()

    def stations(self, query: str) -> list[dict]:
        return stations.search(query, self.station_db)

    # -- YouTube Music ----------------------------------------------------

    def _ytm_available(self) -> bool:
        try:
            return self.catalog is not None and bool(self.catalog.available)
        except Exception:
            return False

    def ytm_search(self, query: str, kind: str) -> dict:
        if not self._ytm_available():
            return {"available": False, "results": []}
        return {"available": True, "results": self.catalog.search(query, kind or "songs")}

    def ytm_library(self) -> dict:
        if not self._ytm_available():
            return {"available": False, "signed_in": False, "items": []}
        return {"available": True, "signed_in": bool(self.catalog.signed_in), "items": self.catalog.library()}

    def ytm_account(self) -> dict:
        if not self._ytm_available():
            return {"available": False, "signed_in": False, "name": ""}
        return {"available": True, **self.catalog.account()}

    def ytm_sign_in(self, body: Any) -> dict:
        if not self._ytm_available():
            raise ValueError("YouTube Music sign-in is not available on this radio")
        headers = body.get("headers") if isinstance(body, dict) else None
        if not isinstance(headers, str):
            raise ValueError("expected {\"headers\": \"…\"}")
        account = self.catalog.sign_in(headers)
        self._resync_playlists()
        return account

    def ytm_sign_out(self) -> dict:
        if not self._ytm_available():
            return {"signed_in": False, "name": ""}
        account = self.catalog.sign_out()
        self._resync_playlists()
        return account

    def _rewarm(self) -> None:
        prefetch = getattr(self.actions.player, "prefetch", None)
        if callable(prefetch):
            prefetch(list(self.actions.presets.values()))


def _host_only(host: str | None) -> str:
    """The Host header without its port ("[::1]:8080" -> "[::1]"), or ""."""
    host = (host or "").strip()
    if not host or any(c in host for c in "/\\ @?#"):
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end > 0 else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "haslerd"

        def log_message(self, fmt, *args):  # quiet: the journal is for the panel
            log.debug("web %s - " + fmt, self.address_string(), *args)

        def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, doc: Any) -> None:
            self._send(status, json.dumps(doc).encode(), "application/json; charset=utf-8")

        def _body(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("request too large")
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw or b"{}")

        def _guard(self, fn: Callable[[], None]) -> None:
            try:
                fn()
            except keymap.Invalid as exc:
                self._json(400, {"ok": False, "errors": exc.errors, "error": str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"ok": False, "errors": [], "error": str(exc)})
            except (Unavailable, SoftwareError) as exc:
                self._json(503, {"ok": False, "errors": [], "error": str(exc)})
            except ConfirmError as exc:
                self._json(403, {"ok": False, "errors": [], "error": str(exc), "confirm_required": True})
            except ConfirmUnavailable as exc:
                self._json(409, {"ok": False, "errors": [], "error": str(exc), "confirm_unavailable": True})
            except NotInstalled as exc:
                self._json(404, {"ok": False, "errors": [], "error": str(exc), "installed": False})
            except Exception as exc:
                log.exception("web request failed: %s %s", self.command, self.path)
                self._json(500, {"ok": False, "errors": [], "error": str(exc) or type(exc).__name__})

        def _local_addr(self) -> str | None:
            try:
                return str(self.connection.getsockname()[0])
            except (OSError, AttributeError, IndexError):
                return None

        def _refused(self) -> bool:
            """CSRF / DNS-rebinding check for a state-changing request; True = 403 sent."""
            why = app.request_refusal(self.command, self.headers, self._local_addr())
            if why is None:
                return False
            log.warning("web: refused %s %s from %s: %s", self.command, urlparse(self.path).path,
                        self.client_address[0] if self.client_address else "?", why)
            # Drain the body so the connection closes cleanly.
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
                if length:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            self._json(403, {"ok": False, "errors": [], "error": why})
            return True

        def _extra(self, method: str) -> bool:
            """Offer /api/ha*, /api/sources, /api/casting* to the extra routes.
            True if this request was theirs."""
            url = urlparse(self.path)
            if not any(url.path == p or url.path.startswith(p + "/") for p in EXTRA_PREFIXES):
                return False

            def run() -> None:
                body = None if method == "GET" else self._body()
                # The path keeps its query string, for GETs that take parameters.
                answer = app.route_extra(method, self.path, body)
                if answer is None:
                    self._json(404, {"ok": False, "errors": [], "error": "not found"})
                else:
                    status, doc = answer
                    self._json(int(status), doc)
            self._guard(run)
            return True

        def _font(self, name: str) -> None:
            path = FONTS / name
            if not FONT_NAME.match(name) or not path.is_file():
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, path.read_bytes(), "font/woff2", "public, max-age=31536000, immutable")

        def do_GET(self) -> None:
            if self._extra("GET"):
                return
            url = urlparse(self.path)
            if url.path == "/wifi":
                self._wifi_page(parse_qs(url.query).get("lang", [""])[0])
            elif url.path in ("/", "/index.html"):
                page = STATIC / "index.html"
                if page.exists():
                    self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"page not installed", "text/plain")
            elif url.path.startswith("/art/"):
                # Cover art for moOde's player (see config/moode/ytm-art.sed):
                # moOde points its <img> here for our YouTube tracks.
                self._guard(lambda: self._art(url.path[len("/art/"):]))
            elif url.path == "/api/wifi":
                self._guard(lambda: self._json(200, app.wifi.status()))
            elif url.path == "/api/config":
                self._guard(lambda: self._json(200, app.config()))
            elif url.path == "/api/status":
                self._guard(lambda: self._json(200, app.status()))
            elif url.path == "/api/stations":
                q = parse_qs(url.query).get("q", [""])[0]
                self._guard(lambda: self._json(200, app.stations(q)))
            elif url.path == "/api/ytm/search":
                query = parse_qs(url.query)
                q, kind = query.get("q", [""])[0], query.get("type", ["songs"])[0]
                self._guard(lambda: self._json(200, app.ytm_search(q, kind)))
            elif url.path == "/api/ytm/library":
                self._guard(lambda: self._json(200, app.ytm_library()))
            elif url.path == "/api/ytm/account":
                self._guard(lambda: self._json(200, app.ytm_account()))
            elif url.path.startswith("/fonts/"):
                self._guard(lambda: self._font(url.path[len("/fonts/"):]))
            elif url.path == "/api/settings":
                self._guard(lambda: self._json(200, app.settings(self.headers.get("Host"))))
            elif url.path == "/api/lamp/palette":
                self._guard(lambda: self._json(200, app.lamp_palette()))
            elif url.path == "/api/knobs":
                self._guard(lambda: self._json(200, app.knobs()))
            elif (m := KNOB_ID.match(url.path)) and m.group(2) == "sample":
                n = parse_qs(url.query).get("n", ["15"])[0]
                self._guard(lambda: self._json(200, app.knob_sample(m.group(1), n)))
            elif url.path == "/api/info":
                self._guard(lambda: self._json(200, app.info()))
            elif url.path == "/api/art/current":
                self._guard(self._art_current)
            elif url.path == "/api/health":
                self._guard(lambda: self._json(*app.health()))
            elif url.path == "/api/software":
                self._guard(lambda: self._json(200, app.software.status()))
            elif m := CONFIRM_ID.match(url.path):
                def poll() -> None:
                    doc = app.confirm.poll(m.group(1))
                    self._json(404, {"state": "expired"}) if doc is None else self._json(200, doc)
                self._guard(poll)
            elif url.path == "/api/browse":
                self._guard(lambda: self._json(200, app.browse_doc()))
            elif url.path == "/api/browse/radio":
                q = parse_qs(url.query).get("q", [""])[0]
                self._guard(lambda: self._json(200, app.browse_radio(q)))
            else:
                self._send(404, b"not found", "text/plain")

        def _art_current(self) -> None:
            pic = app.art_current()
            if pic is None:
                self._send(404, b"no art", "text/plain")
                return
            data, ctype, etag = pic
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            # "current" changes with the music: revalidate every time (cheap: ETag).
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(data)

        def _art(self, vid: str) -> None:
            cover = app.cover(vid)
            if cover is None:
                self._send(404, b"no cover", "text/plain")
                return
            self.send_response(302)
            self.send_header("Location", cover)
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self) -> None:
            if self._refused() or self._extra("PUT"):
                return
            path = urlparse(self.path).path
            if path == "/api/config":
                self._guard(lambda: self._json(200, {"ok": True, "config": app.save(self._body())}))
            elif path == "/api/settings":
                self._guard(lambda: self._json(
                    200, {"ok": True, **app.put_settings(self._body(), self.headers.get("Host"))}))
            elif (m := KNOB_ID.match(path)) and m.group(2) == "calibration":
                self._guard(lambda: self._json(
                    200, {"ok": True, **app.put_calibration(m.group(1), self._body())}))
            elif path == "/api/software/settings":
                self._guard(lambda: self._json(200, {"ok": True, **app.software_settings(self._body())}))
            elif path == "/api/lamp/palette":
                self._guard(lambda: self._json(200, {"ok": True, **app.put_lamp_palette(self._body())}))
            else:
                self._send(404, b"not found", "text/plain")

        def _wifi_page(self, lang: str | None, status: int = 200, form: dict | None = None, **kw: Any) -> None:
            """The Wi-Fi setup page, rendered for this phone (web/wifi_page.py)."""
            page = STATIC / "wifi.html"
            if not page.exists():
                self._send(404, b"page not installed", "text/plain")
                return
            lang = wifi_page.pick_lang(lang, self.headers.get("Accept-Language"))
            try:
                wifi = {} if kw.get("state") == "done" else app.wifi.status()
            except Exception:
                log.warning("wifi status failed", exc_info=True)
                wifi = {}
            html = wifi_page.render(page.read_text("utf-8"), wifi, lang, form=form, **kw)
            self._send(status, html.encode(), "text/html; charset=utf-8")

        def _wifi_form(self) -> None:
            """POST /wifi: the setup page's form without JavaScript. Same rules as
            /api/wifi (a key press unless the phone is on the setup network)."""
            why = app.request_refusal("POST", self.headers, self._local_addr(), form=True)
            if why is not None:
                log.warning("web: refused POST /wifi: %s", why)
                self._send(403, why.encode(), "text/plain; charset=utf-8")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(413, b"request too large", "text/plain")
                return
            form = wifi_page.parse_form(self.rfile.read(length) if length else b"")
            lang = wifi_page.pick_lang(form.get("lang"), self.headers.get("Accept-Language"))
            strings = wifi_page.strings((STATIC / "wifi.html").read_text("utf-8"))
            s = {**strings.get("en", {}), **strings.get(lang, {})}
            try:
                body = wifi_page.join_body(form)
                app.wifi_join(body, self._local_addr())
            except wifi_page.FormError as exc:
                error = s.get(exc.key, exc.key)
            except (ConfirmError, ConfirmUnavailable):
                error = s.get("err_confirm_js", "")
            except ValueError as exc:
                error = s.get("err_save", "{error}").replace("{error}", str(exc))
            else:
                self._wifi_page(lang, state="done", saved_ssid=body["ssid"])
                return
            self._wifi_page(lang, 400, form=form, error=error)

        def do_POST(self) -> None:
            if urlparse(self.path).path == "/wifi":
                self._wifi_form()
                return
            if self._refused() or self._extra("POST"):
                return
            path = urlparse(self.path).path
            if path == "/api/press":
                self._guard(lambda: (app.press(self._body()), self._json(200, {"ok": True})))
            elif path == "/api/volume":
                self._guard(lambda: self._json(200, {"ok": True, "volume": app.volume(self._body())}))
            elif path == "/api/next":
                self._guard(lambda: (app.next(), self._json(200, {"ok": True})))
            elif path == "/api/standby":
                self._guard(lambda: self._json(200, {"ok": True, "standby": app.set_standby(self._body())}))
            elif path == "/api/play":
                def play() -> None:
                    app.play(self._body().get("preset"))
                    self._json(200, {"ok": True})
                self._guard(play)
            elif path == "/api/stop":
                self._guard(lambda: (app.stop(), self._json(200, {"ok": True})))
            elif path == "/api/lamp/preview":
                self._guard(lambda: self._json(200, {"ok": True, **app.lamp_preview(self._body())}))
            elif path == "/api/lamp/palette":       # as PUT, for clients that only POST
                self._guard(lambda: self._json(200, {"ok": True, **app.put_lamp_palette(self._body())}))
            elif path == "/api/settings":            # as PUT, for clients that only POST
                self._guard(lambda: self._json(
                    200, {"ok": True, **app.put_settings(self._body(), self.headers.get("Host"))}))
            elif path == "/api/wifi/retry":
                self._guard(lambda: self._json(200, app.wifi_retry(self._body())))
            elif path == "/api/wifi":
                def join() -> None:
                    app.wifi_join(self._body(), self._local_addr())
                    self._json(200, {"ok": True})
                self._guard(join)
            elif path == "/api/reset":
                self._guard(lambda: self._json(200, {"ok": True, "config": app.confirmed_reset(self._body())}))
            elif path == "/api/confirm/request":
                def ask() -> None:
                    body = self._body()
                    body = body if isinstance(body, dict) else {}
                    self._json(200, {"ok": True, **app.confirm.request(body.get("action"), body.get("args"))})
                self._guard(ask)
            elif path == "/api/software/check":
                self._guard(lambda: (self._body(), app.software.check(), self._json(200, {"ok": True})))
            elif path == "/api/software/install":
                self._guard(lambda: self._json(200, {"ok": True, **app.software_install(self._body())}))
            elif path == "/api/software/rollback":
                self._guard(lambda: (app.software_rollback(self._body()), self._json(200, {"ok": True})))
            elif path == "/api/software/yt-dlp/update":
                self._guard(lambda: (self._body(), app.ytdlp_update(), self._json(200, {"ok": True})))
            elif path == "/api/software/yt-dlp/rollback":
                self._guard(lambda: (app.ytdlp_rollback(self._body()), self._json(200, {"ok": True})))
            elif path == "/api/components/yt-dlp/canary":
                # The lane's canary (loopback only; ytdlp_lane.py): it takes a while.
                self._guard(lambda: self._json(*app.ytdlp_canary(
                    self._body(), self.client_address[0] if self.client_address else None)))
            elif path == "/api/ytm/account":
                self._guard(lambda: self._json(200, {"ok": True, "account": app.ytm_sign_in(self._body())}))
            elif (m := KNOB_ID.match(path)) and m.group(2) == "fit":
                self._guard(lambda: self._json(200, {"ok": True, **app.knob_fit(m.group(1), self._body())}))
            else:
                self._send(404, b"not found", "text/plain")

        def do_PATCH(self) -> None:
            if self._refused():
                return
            if not self._extra("PATCH"):
                self._send(404, b"not found", "text/plain")

        def do_DELETE(self) -> None:
            if self._refused() or self._extra("DELETE"):
                return
            path = urlparse(self.path).path
            if m := CONFIRM_ID.match(path):
                self._guard(lambda: self._json(200, {"ok": app.confirm.cancel(m.group(1))}))
            elif path == "/api/software/install":
                self._guard(lambda: (app.software.cancel_install(), self._json(200, {"ok": True})))
            elif path == "/api/ytm/account":
                self._guard(lambda: self._json(200, {"ok": True, "account": app.ytm_sign_out()}))
            elif path == "/api/lamp/palette":
                self._guard(lambda: self._json(200, {"ok": True, **app.put_lamp_palette({"palette": {}})}))
            elif (m := KNOB_ID.match(path)) and m.group(2) == "calibration":
                self._guard(lambda: self._json(200, {"ok": True, **app.delete_calibration(m.group(1))}))
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


class WebServer:
    def __init__(self, app: WebApp, host: str = "0.0.0.0", port: int = 8080) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        server = ThreadingHTTPServer((self.host, self.port), _handler(self.app))
        server.daemon_threads = True
        self.port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever, name="web", daemon=True)
        self._thread.start()
        self._server = server
        log.info("key page on http://%s:%s/", self.host, self.port)

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
