"""``/api/bluetooth``: the paired phones, the codec, and the Bluetooth keys.

The phones come from BlueZ (player/bluetooth.py); Bluetooth on/off and its
name stay moOde's (``/api/casting``). The owner's settings live in
``settings.json`` under ``bluetooth`` - read at startup (``apply_saved``),
applied live on a PUT:

    "bluetooth": {"keys": {"enabled": true, "again": "next", "hold": "pause"},
                  "reconnect": {"on_select": true, "on_wake": true}}

``keys``: while a phone plays over Bluetooth, the key whose tap is the
``bluetooth`` verb, pressed again, does ``again`` (next | previous | none) on
the phone, and held does ``hold`` (pause = pause/play | next | previous |
none) instead of whatever the hold is bound to. ``reconnect``: choosing
Bluetooth (that key, or the PU cycle landing there) and waking a radio that
slept on Bluetooth call the most recently used phone in range.

    GET  /api/bluetooth             {available, on, name, devices, playing, settings, pairing}
    POST /api/bluetooth/connect     {"address": "B0:9C:B2:EB:15:F9"}
    POST /api/bluetooth/disconnect  {"address": ...} or {} for every connected one
    POST /api/bluetooth/forget      {"address": ...}  - removes the pairing
    PUT  /api/bluetooth/settings    {"keys": {...}} and/or {"reconnect": {...}}

Errors are {"ok": false, "error"}: 400 a bad request or unknown device, 409
BlueZ said no (a phone out of range), 503 no Bluetooth on this radio.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from ..actions import BT_AGAIN, BT_HOLD, Actions, bt_settings_from
from ..player.bluetooth import BluetoothError
from .server import Unavailable, load_settings, save_settings

log = logging.getLogger(__name__)

PATHS = ("/api/bluetooth", "/api/bluetooth/connect", "/api/bluetooth/disconnect",
         "/api/bluetooth/forget", "/api/bluetooth/settings")


def apply_saved(actions: Actions, settings: dict) -> None:
    """settings.json's ``bluetooth`` onto the actions at startup; a bad value
    is logged and the defaults kept."""
    doc = settings.get("bluetooth") if isinstance(settings, dict) else None
    if not isinstance(doc, dict) or not doc:
        return
    try:
        merged = bt_settings_from({k: v for k, v in doc.items() if k in ("keys", "reconnect")})
    except ValueError as exc:
        log.warning("ignoring saved bluetooth settings %r: %s", doc, exc)
        return
    actions.bt_keys, actions.bt_reconnect = merged["keys"], merged["reconnect"]


class BluetoothApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def actions(self) -> Actions:
        return self.app.actions

    @property
    def bt(self) -> Any:
        return getattr(self.actions.player, "bluetooth", None)

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PATHS:
            return None
        method = method.upper()
        try:
            if p == "/api/bluetooth" and method == "GET":
                return 200, self.doc()
            if p == "/api/bluetooth/settings" and method in ("PUT", "POST"):
                return 200, {"ok": True, **self.put_settings(body)}
            if method == "POST" and p != "/api/bluetooth":
                return self.act(p.rsplit("/", 1)[1], body)
        except BluetoothError as exc:
            return exc.status, {"ok": False, "errors": [], "error": str(exc)}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    # -- reading ----------------------------------------------------------

    def _enabled(self) -> bool | None:
        renderers = getattr(self.actions.player, "renderers", None)
        try:
            enabled = renderers.enabled() if renderers is not None else {}
        except Exception:
            enabled = {}
        return enabled.get("bluetooth") if enabled else None

    def doc(self) -> dict:
        actions = self.actions
        bt = self.bt
        adapter = devices = playing = None
        if bt is not None:
            try:
                adapter = bt.adapter()
                devices = bt.devices() if adapter is not None else []
                playing = bt.playing() if adapter is not None else None
            except Exception:
                log.exception("could not read Bluetooth")
                adapter = None
        moode_on = self._enabled()
        left = max(0.0, actions.pairing_until - actions.clock()) if actions.pairing else 0.0
        return {
            "available": adapter is not None,
            # moOde's switch when it can be read, else whether the adapter is powered.
            "on": bool(adapter and adapter["powered"]) and moode_on is not False,
            "name": adapter["name"] if adapter else "",
            "devices": [bt.public(d) for d in devices or []],
            "playing": playing,
            "reconnecting": bool(getattr(bt, "reconnecting", False)),
            "settings": {"keys": dict(actions.bt_keys), "reconnect": dict(actions.bt_reconnect)},
            "choices": {"again": list(BT_AGAIN), "hold": list(BT_HOLD)},
            # Which keys carry the Bluetooth verb, e.g. ["k6.tap"].
            "bound_to": sorted(k for k, v in actions.variant.actions.items()
                               if isinstance(v, dict) and v.get("do") == "bluetooth"),
            "pairing": {"open": bool(actions.pairing), "seconds_left": int(round(left))},
        }

    # -- doing ------------------------------------------------------------

    def act(self, verb: str, body: Any) -> tuple[int, dict]:
        body = body if isinstance(body, dict) else {}
        bt = self.bt
        if bt is None:
            raise Unavailable("there is no Bluetooth on this radio")
        address = body.get("address")
        if verb == "disconnect":
            names = bt.disconnect(address if address not in (None, "") else None)
            log.info("bluetooth: %s disconnected from the web page", ", ".join(names) or "nobody")
        elif verb == "connect":
            if self._enabled() is False:
                return 409, {"ok": False, "errors": [], "error": "Bluetooth is switched off - turn it on first"}
            bt.connect(self._address(address))
        elif verb == "forget":
            name = bt.forget(self._address(address))
            log.info("bluetooth: %s forgotten from the web page", name)
        return 200, {"ok": True, **self.doc()}

    @staticmethod
    def _address(raw: Any) -> str:
        from ..player.bluetooth import valid_address
        return valid_address(raw)      # ValueError -> 400

    def put_settings(self, body: Any) -> dict:
        actions = self.actions
        merged = bt_settings_from(body, {"keys": actions.bt_keys, "reconnect": actions.bt_reconnect})
        with self.app.lock:
            if self.app.settings_file is not None:
                doc = load_settings(self.app.settings_file)
                doc["bluetooth"] = merged
                save_settings(self.app.settings_file, doc)
            actions.bt_keys, actions.bt_reconnect = merged["keys"], merged["reconnect"]
        log.info("bluetooth settings from the web page: %s", merged)
        return self.doc()
