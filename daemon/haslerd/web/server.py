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
read again at startup by main.build, overriding haslerd.yaml).

``/api/browse`` serves the curated library (web/catalog.py), and anything under
``/api/ha`` is handed to ``WebApp.extra_routes`` - objects with
``route(method, path, body) -> (status, doc) | None`` (web/ha_api.py) - so
Home Assistant's endpoints live with Home Assistant's code.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..actions import Actions
from ..panel.controls import Event
from ..player.base import Preset
from ..player.moode import treble_db
from . import keymap, stations
from .wifi import Wifi

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
FONTS = STATIC / "fonts"
FONT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.woff2$")
MAX_BODY = 256 * 1024

PRESS_EVENTS = ("tap", "hold")
SETTINGS_FILE = "settings.json"
VOLUME_LIMIT_RANGE = (30, 100)   # below 30 the knob is barely audible; 100 = no ceiling
VOLUME_GUESS = 40.0              # a relative nudge with no reading starts here, as volume_step does

# A key's tap action as the remote's caption says it, when it isn't a preset.
TAP_LABELS = {
    "cycle_source": "Next source",
    "standby_toggle": "Standby",
    "volume_up": "Volume up",
    "volume_down": "Volume down",
    "bt_pair": "Bluetooth pairing",
    "ha_scene": "Scene",
    "ha_script": "Script",
    "ha_toggle": "Toggle",
    "ha_automation": "Automation",
}


# -- settings.json ----------------------------------------------------------


def load_settings(path: Path | None) -> dict:
    """The page's settings file, or {} if absent or unreadable. Never raises."""
    if path is None or not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        log.exception("could not read %s; using haslerd.yaml's settings", path)
        return {}
    return doc if isinstance(doc, dict) else {}


def save_settings(path: Path, doc: dict) -> None:
    """Write atomically, world-readable (0644): a power cut leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".settings-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def volume_limit_from(raw: Any) -> int:
    """A submitted ceiling -> int in VOLUME_LIMIT_RANGE, or ValueError saying why."""
    lo, hi = VOLUME_LIMIT_RANGE
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw):
        raise ValueError(f"volume_limit must be a whole number from {lo} to {hi}")
    value = int(raw)
    if not lo <= value <= hi:
        raise ValueError(f"volume_limit must be from {lo} to {hi}")
    return value


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
        self.station_db = station_db
        self.wifi = wifi or Wifi(db=station_db)
        self.lock = threading.Lock()
        # The ytm source (for now_playing) and its catalogue, if the player has them.
        self.ytm = (getattr(actions.player, "sources", None) or {}).get("ytm")
        self.catalog = catalog if catalog is not None else getattr(self.ytm, "catalog", None)
        # The descriptor's own layout, for "reset to defaults". Taken before any
        # saved overlay is laid on top.
        self.defaults = (copy.deepcopy(actions.variant.presets), copy.deepcopy(actions.variant.actions))

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
            specs, table = keymap.parse(doc, self.actions)   # raises keymap.Invalid
            if self.keys_file is not None:
                keymap.save(self.keys_file, specs, table)
            keymap.apply(self.actions, self.panel, specs, table)
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
            keymap.apply(self.actions, self.panel, specs, table)
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
        return {
            "pressed": pressed,
            "playing": keymap.preset_to_page(current) if current else None,
            "standby": self.actions.standby,
            "volume": None if volume is None else round(volume),
            # What owns the speaker: radio | spotify | ytm | bluetooth | airplay.
            # waiting = selected with PU but nothing connected yet.
            "source": source,
            "waiting": waiting,
            "renderer": renderer,
            # The YouTube Music track playing, named: {title, artist, album, thumb} or None.
            "track": self._track() if source == "ytm" else None,
            # The remote's keys, k1..k6 then PU, and which one is lit.
            "keys": self._keys(),
            "current_key": self._current_key(current),
            "volume_limit": self._volume_limit(),
            # The tone knob: 0-100 and what it does to the treble (None = no tone pot read yet).
            "tone": None if not isinstance(tone, (int, float)) or isinstance(tone, bool) else
                    {"value": round(tone), "treble_db": treble_db(tone)},
            # The tone knob's end switch: True = radio on, False = clicked off, None = not fitted.
            "knob_on": states.get("power") if isinstance(states.get("power"), bool) else None,
        }

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

    # -- remote control ---------------------------------------------------

    def press(self, body: Any) -> None:
        """A cabinet key pressed from the phone: the very Event the panel would send."""
        body = body if isinstance(body, dict) else {}
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
            "mono": self._mono_doc(),
            "wifi": {"ssid": ssid},
            "version": f"haslerd {__version__}",
            "moode_url": f"http://{_host_only(host) or socket.gethostname() + '.local'}/",
        }

    def put_settings(self, body: Any, host: str | None = None) -> dict:
        """Validate, persist, apply live: ``volume_limit`` and/or ``mono``."""
        if not isinstance(body, dict) or not ({"volume_limit", "mono"} & body.keys()):
            raise ValueError("expected {\"volume_limit\": 30-100} and/or {\"mono\": true|false}")
        limit = volume_limit_from(body["volume_limit"]) if "volume_limit" in body else None
        mono = body.get("mono")
        if "mono" in body and not isinstance(mono, bool):
            raise ValueError("mono must be true or false")
        if mono is not None and (self.mono is None or not self.mono.available):
            raise ValueError("mono output isn't installed on this radio (run install.sh)")
        with self.lock:
            if mono is not None:
                try:
                    self.mono.set(mono)
                except RuntimeError as exc:          # audio.MonoError
                    raise ValueError(str(exc)) from exc
            if limit is not None:
                self._apply_volume_limit(limit)
            if self.settings_file is not None:
                doc = load_settings(self.settings_file)
                if limit is not None:
                    doc["volume_limit"] = limit
                if mono is not None:
                    doc["mono"] = mono
                save_settings(self.settings_file, doc)
        if limit is not None:
            log.info("volume limit set to %s%% from the web page", limit)
        if mono is not None:
            log.info("output set to %s from the web page", "mono" if mono else "stereo")
        return self.settings(host)

    def _mono_doc(self) -> dict:
        if self.mono is None or not self.mono.available:
            return {"available": False, "on": False}
        return {"available": True, "on": self.mono.is_on()}

    def _apply_volume_limit(self, limit: int) -> None:
        """New ceiling, same knob position: re-set the knob so the change is heard.

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
        config.volume_limit = limit
        if knob is not None and not self.actions.standby:
            try:
                player.set_volume(float(knob))
            except Exception:
                log.exception("could not re-apply the volume under the new limit")

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
            except Exception as exc:
                log.exception("web request failed: %s %s", self.command, self.path)
                self._json(500, {"ok": False, "errors": [], "error": str(exc) or type(exc).__name__})

        def _extra(self, method: str) -> bool:
            """Offer /api/ha* to the extra routes. True if this request was theirs."""
            url = urlparse(self.path)
            if not (url.path == "/api/ha" or url.path.startswith("/api/ha/")):
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
            if url.path in ("/", "/index.html", "/wifi"):
                page = STATIC / ("wifi.html" if url.path == "/wifi" else "index.html")
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
            elif url.path == "/api/browse":
                self._guard(lambda: self._json(200, app.browse_doc()))
            elif url.path == "/api/browse/radio":
                q = parse_qs(url.query).get("q", [""])[0]
                self._guard(lambda: self._json(200, app.browse_radio(q)))
            else:
                self._send(404, b"not found", "text/plain")

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
            if self._extra("PUT"):
                return
            path = urlparse(self.path).path
            if path == "/api/config":
                self._guard(lambda: self._json(200, {"ok": True, "config": app.save(self._body())}))
            elif path == "/api/settings":
                self._guard(lambda: self._json(
                    200, {"ok": True, **app.put_settings(self._body(), self.headers.get("Host"))}))
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if self._extra("POST"):
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
            elif path == "/api/wifi":
                def join() -> None:
                    body = self._body()
                    app.wifi.join(str(body.get("ssid", "")), str(body.get("password", "")),
                                  str(body.get("security", "auto")))
                    self._json(200, {"ok": True})
                self._guard(join)
            elif path == "/api/reset":
                self._guard(lambda: self._json(200, {"ok": True, "config": app.reset()}))
            elif path == "/api/ytm/account":
                self._guard(lambda: self._json(200, {"ok": True, "account": app.ytm_sign_in(self._body())}))
            else:
                self._send(404, b"not found", "text/plain")

        def do_PATCH(self) -> None:
            if not self._extra("PATCH"):
                self._send(404, b"not found", "text/plain")

        def do_DELETE(self) -> None:
            if self._extra("DELETE"):
                return
            if urlparse(self.path).path == "/api/ytm/account":
                self._guard(lambda: self._json(200, {"ok": True, "account": app.ytm_sign_out()}))
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
