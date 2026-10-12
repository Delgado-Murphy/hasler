"""The key-layout web page: one HTML file and a small JSON API.

Standard library only (``http.server``), like the YTM proxy, so the box gains
no dependencies. It listens on the LAN with no login - the same trust moOde's
own web UI extends - and can do nothing worse than change what a key plays.

Requests run on their own threads; everything that touches the layout goes
through ``WebApp`` under one lock, and the change itself is a reference swap
(see keymap.apply), so the 50 Hz panel loop never waits on the web.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..actions import Actions
from ..player.base import Preset
from . import keymap, stations

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_BODY = 256 * 1024


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
    ) -> None:
        self.actions = actions
        self.panel = panel
        self.keys_file = keys_file
        self.sources = sources
        self.ha = ha
        self.station_db = station_db
        self.lock = threading.Lock()
        # The descriptor's own layout, for "reset to defaults". Taken before any
        # saved overlay is laid on top.
        self.defaults = (copy.deepcopy(actions.variant.presets), copy.deepcopy(actions.variant.actions))

    def load_saved(self) -> None:
        with self.lock:
            keymap.apply_saved(self.keys_file, self.actions, self.panel)

    # -- operations -------------------------------------------------------

    def config(self) -> dict:
        return keymap.config_doc(self.actions, self.sources, self.ha)

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
            pressed = [cid for cid, state in self.panel.states().items() if cid in keys and state is True]
        except Exception:
            pressed = []
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
        }

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

    def stations(self, query: str) -> list[dict]:
        return stations.search(query, self.station_db)

    def _rewarm(self) -> None:
        prefetch = getattr(self.actions.player, "prefetch", None)
        if callable(prefetch):
            prefetch(list(self.actions.presets.values()))


def _handler(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "haslerd"

        def log_message(self, fmt, *args):  # quiet: the journal is for the panel
            log.debug("web %s - " + fmt, self.address_string(), *args)

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
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

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                page = STATIC / "index.html"
                if page.exists():
                    self._send(200, page.read_bytes(), "text/html; charset=utf-8")
                else:
                    self._send(404, b"page not installed", "text/plain")
            elif url.path == "/api/config":
                self._guard(lambda: self._json(200, app.config()))
            elif url.path == "/api/status":
                self._guard(lambda: self._json(200, app.status()))
            elif url.path == "/api/stations":
                q = parse_qs(url.query).get("q", [""])[0]
                self._guard(lambda: self._json(200, app.stations(q)))
            else:
                self._send(404, b"not found", "text/plain")

        def do_PUT(self) -> None:
            if urlparse(self.path).path == "/api/config":
                self._guard(lambda: self._json(200, {"ok": True, "config": app.save(self._body())}))
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/play":
                def play() -> None:
                    app.play(self._body().get("preset"))
                    self._json(200, {"ok": True})
                self._guard(play)
            elif path == "/api/stop":
                self._guard(lambda: (app.stop(), self._json(200, {"ok": True})))
            elif path == "/api/reset":
                self._guard(lambda: self._json(200, {"ok": True, "config": app.reset()}))
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
