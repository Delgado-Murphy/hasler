"""Bluetooth phones, straight from BlueZ and BlueALSA on the system D-Bus.

moOde owns Bluetooth as a renderer (on/off, the name - player/renderers.py)
and its agent does the pairing (player/btaccept.py). This is the rest, which
moOde's page does not do for a radio without a screen: the paired phones,
connecting the last one again, the phone's own play/pause/next (AVRCP) and
which codec it streams with.

Everything is one ``busctl`` call - no Python D-Bus library on the box, and
busctl answers in ~15 ms with JSON. haslerd runs as ``pi``, and both buses
let it in without a root helper: BlueZ's policy (bluetooth.conf) allows any
user to send to org.bluez, BlueALSA's (bluealsa.conf) the ``audio`` group,
which haslerd's unit has. Calls used:

    org.bluez  /  ObjectManager.GetManagedObjects      adapters, devices, players, batteries
    org.bluez  .../dev_XX  Device1.Connect / Disconnect
    org.bluez  .../hci0    Adapter1.RemoveDevice(o)      "forget"
    org.bluez  .../dev_XX/playerN  MediaPlayer1.Next / Previous / Play / Pause
    org.bluealsa /org/bluealsa  ObjectManager.GetManagedObjects   PCM1 Codec, Rate

BlueZ does not say when a device was last used, and its own store is
root-only, so haslerd keeps that itself (``bluetooth.json`` in the state
dir), noting each phone it sees connected.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MAC = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"   # the device can send audio
HISTORY_FILE = "bluetooth.json"
CACHE_S = 2.0                # the page polls every second; BlueZ need not
CONNECT_S = 8.0              # Device1.Connect: a phone out of range fails in ~5 s
RECONNECT_TRIES = 3          # most recently used phones tried on select / wake
NOTE_EVERY_S = 30.0          # how often a connected phone's "last used" is refreshed

# BlueALSA's codec names -> what the page shows.
CODECS = {
    "SBC": "SBC", "AAC": "AAC", "APTX": "aptX", "APTX-HD": "aptX HD", "LDAC": "LDAC",
    "MP3": "MP3", "FASTSTREAM": "FastStream", "LC3PLUS": "LC3plus", "OPUS": "Opus",
}
COMMANDS = {"next": "Next", "previous": "Previous", "play": "Play", "pause": "Pause"}

Runner = Callable[[list[str], float], "tuple[int, str, str]"]


class BluetoothError(Exception):
    """BlueZ said no, or isn't there. ``status`` is the HTTP status it maps to."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def valid_address(raw: object) -> str:
    """``aa:bb:..`` -> ``AA:BB:..``, or ValueError. Nothing else reaches busctl."""
    if not isinstance(raw, str) or not MAC.match(raw.strip().upper()):
        raise ValueError("address must be a Bluetooth address like B0:9C:B2:EB:15:F9")
    return raw.strip().upper()


def _busctl(args: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        result = subprocess.run(["busctl", "--system", f"--timeout={max(1, int(timeout))}", *args],
                                capture_output=True, text=True, timeout=timeout + 2)
    except FileNotFoundError:
        return 127, "", "busctl not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return result.returncode, result.stdout, result.stderr


def _unwrap(value: object) -> object:
    """busctl's JSON wraps every variant as {"type", "data"}; take the data."""
    if isinstance(value, dict) and set(value) == {"type", "data"}:
        return _unwrap(value["data"])
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap(v) for v in value]
    return value


def parse_objects(text: str) -> dict[str, dict[str, dict]]:
    """GetManagedObjects' JSON -> {path: {interface: {property: value}}}."""
    try:
        doc = json.loads(text)
    except ValueError:
        return {}
    data = doc.get("data") if isinstance(doc, dict) else None
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {}
    return {str(path): {str(i): (_unwrap(props) or {}) for i, props in (ifaces or {}).items()}
            for path, ifaces in data.items() if isinstance(ifaces, dict)}


def codec_label(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw:
        return None
    return CODECS.get(raw.upper(), raw)


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class Bluetooth:
    """The phones. Every outside touch (busctl, the clocks, the file) is injectable."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        run: Runner = _busctl,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        spawn: Callable[[Callable[[], None], str], None] | None = None,
    ) -> None:
        self.history_file = Path(state_dir) / HISTORY_FILE if state_dir is not None else None
        self._run, self._clock, self._wall = run, clock, wall
        self._spawn = spawn or (lambda fn, name: threading.Thread(target=fn, name=name, daemon=True).start())
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None
        self._codec_cache: tuple[float, dict] | None = None
        self._last_used: dict[str, float] | None = None   # address -> wall time
        self._noted_at = float("-inf")
        self._reconnecting = False
        self._noting = False
        self._paused: tuple[bool, float] = (False, float("-inf"))
        self._paused_refreshing = False

    # -- reading ----------------------------------------------------------

    def _objects(self, fresh: bool = False) -> dict | None:
        now = self._clock()
        with self._lock:
            if not fresh and self._cache is not None and now - self._cache[0] < CACHE_S:
                return self._cache[1]
        rc, out, err = self._run(["--json=short", "call", "org.bluez", "/",
                                  "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"], 3)
        if rc != 0:
            log.debug("BlueZ unreadable: %s", err.strip()[-200:])
            return None
        objects = parse_objects(out)
        with self._lock:
            self._cache = (now, objects)
        return objects

    def _drop_cache(self) -> None:
        with self._lock:
            self._cache = None
            self._codec_cache = None

    def adapter(self) -> dict | None:
        """{"path", "name", "powered"} of the first adapter, None without BlueZ."""
        objects = self._objects()
        if objects is None:
            return None
        for path in sorted(objects):
            props = objects[path].get("org.bluez.Adapter1")
            if props is not None:
                return {"path": path, "name": str(props.get("Alias") or props.get("Name") or ""),
                        "powered": bool(props.get("Powered"))}
        return None

    def devices(self) -> list[dict]:
        """Paired (or connected) devices, most recently used first."""
        objects = self._objects()
        if not objects:
            return []
        history = self._history()
        out = []
        for path in sorted(objects):
            ifaces = objects[path]
            dev = ifaces.get("org.bluez.Device1")
            if dev is None:
                continue
            address = str(dev.get("Address") or "").upper()
            if not MAC.match(address):
                continue
            paired = bool(dev.get("Paired") or dev.get("Bonded"))
            connected = bool(dev.get("Connected"))
            if not paired and not connected:
                continue    # a phone seen in a scan, never ours
            battery = (ifaces.get("org.bluez.Battery1") or {}).get("Percentage")
            uuids = [str(u).lower() for u in (dev.get("UUIDs") or [])]
            out.append({
                "address": address,
                "name": str(dev.get("Alias") or dev.get("Name") or address),
                "icon": str(dev.get("Icon") or "") or None,
                "paired": paired,
                "trusted": bool(dev.get("Trusted")),
                "connected": connected,
                "last_used": _iso(history.get(address)),
                "battery": int(battery) if isinstance(battery, (int, float)) else None,
                # Not part of the page's API: what reconnect needs.
                "_path": path,
                "_source": not uuids or A2DP_SOURCE_UUID in uuids,
                "_used": history.get(address),
            })
        out.sort(key=lambda d: -(d["_used"] or 0.0))   # stable: BlueZ order among the unknown
        if any(d["connected"] for d in out):
            self.note_connected(out)
        return out

    @staticmethod
    def public(device: dict) -> dict:
        return {k: v for k, v in device.items() if not k.startswith("_")}

    def connected(self) -> dict | None:
        return next((d for d in self.devices() if d["connected"]), None)

    def _player(self, device_path: str) -> tuple[str, dict] | None:
        """The connected phone's AVRCP player (path, props), if it has one."""
        objects = self._objects() or {}
        for path in sorted(objects):
            if path.startswith(device_path + "/"):
                props = objects[path].get("org.bluez.MediaPlayer1")
                if props is not None:
                    return path, props
        return None

    def codec(self, device_path: str) -> dict | None:
        """{"codec", "rate"} of the phone's A2DP stream into the radio, from BlueALSA."""
        now = self._clock()
        with self._lock:
            cached = self._codec_cache if self._codec_cache and now - self._codec_cache[0] < CACHE_S else None
        if cached is not None:
            objects = cached[1]
        else:
            rc, out, err = self._run(["--json=short", "call", "org.bluealsa", "/org/bluealsa",
                                      "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"], 2)
            if rc != 0:
                log.debug("BlueALSA unreadable: %s", err.strip()[-200:])
                return None
            objects = parse_objects(out)
            with self._lock:
                self._codec_cache = (now, objects)
        best = None
        for path in sorted(objects):
            pcm = objects[path].get("org.bluealsa.PCM1")
            if not pcm or str(pcm.get("Device") or "") != device_path:
                continue
            transport = str(pcm.get("Transport") or "")
            if not transport.startswith("A2DP"):
                continue
            doc = {"codec": codec_label(pcm.get("Codec")),
                   "rate": int(pcm["Rate"]) if isinstance(pcm.get("Rate"), (int, float)) else None}
            # The phone -> radio stream is the A2DP sink; prefer it, and a running one.
            rank = (transport == "A2DP-sink", bool(pcm.get("Running")))
            if best is None or rank > best[0]:
                best = (rank, doc)
        return best[1] if best else None

    def playing(self) -> dict | None:
        """The connected phone and what it streams, or None when no phone is connected:
        {"address", "name", "codec", "rate", "title", "artist", "paused"}."""
        device = self.connected()
        if device is None:
            return None
        doc = {"address": device["address"], "name": device["name"], "codec": None, "rate": None,
               "title": "", "artist": "", "paused": False}
        player = self._player(device["_path"])
        if player is not None:
            props = player[1]
            track = props.get("Track") or {}
            doc["title"] = str(track.get("Title") or "")
            doc["artist"] = str(track.get("Artist") or "")
            doc["paused"] = str(props.get("Status") or "") in ("paused", "stopped")
        codec = self.codec(device["_path"])
        if codec:
            doc.update(codec)
        return doc

    def paused(self) -> bool:
        """The connected phone's player says paused (the lamp's cast-paused look)."""
        device = self.connected()
        if device is None:
            return False
        player = self._player(device["_path"])
        return player is not None and str(player[1].get("Status") or "") in ("paused", "stopped")

    def paused_soon(self) -> bool:
        """paused() as last read, never blocking: a stale answer is refreshed
        off-thread (the lamp asks this on the panel loop, 20 times a second)."""
        now = self._clock()
        with self._lock:
            value, at = self._paused
            stale = now - at >= CACHE_S and not self._paused_refreshing
            if stale:
                self._paused_refreshing = True
        if stale:
            def run() -> None:
                try:
                    result = self.paused()
                except Exception:
                    result = False
                with self._lock:
                    self._paused = (result, self._clock())
                    self._paused_refreshing = False
            self._spawn(run, "bt-paused")
        return value

    # -- doing ------------------------------------------------------------

    def _device(self, address: str) -> dict:
        address = valid_address(address)
        if self._objects(fresh=True) is None:
            raise BluetoothError("Bluetooth isn't running on this radio", 503)
        device = next((d for d in self.devices() if d["address"] == address), None)
        if device is None:
            raise BluetoothError(f"no paired device {address}", 400)
        return device

    def _call(self, args: list[str], timeout: float, what: str) -> None:
        rc, _, err = self._run(["call", "org.bluez", *args], timeout)
        self._drop_cache()
        if rc != 0:
            msg = err.strip().splitlines()[-1] if err.strip() else f"busctl exit {rc}"
            raise BluetoothError(f"{what}: {msg}")

    def connect(self, address: str, timeout: float = CONNECT_S) -> dict:
        device = self._device(address)
        if not device["connected"]:
            self._call([device["_path"], "org.bluez.Device1", "Connect"], timeout,
                       f"couldn't connect {device['name']} - is it near, with Bluetooth on?")
            log.info("bluetooth: connected %s", device["name"])
        self.note(device["address"])
        return device

    def disconnect(self, address: str | None = None) -> list[str]:
        """Disconnect one device, or every connected one. Returns the names."""
        if address is not None:
            targets = [self._device(address)]
        else:
            if self._objects(fresh=True) is None:
                raise BluetoothError("Bluetooth isn't running on this radio", 503)
            targets = [d for d in self.devices() if d["connected"]]
        names = []
        for device in targets:
            if device["connected"]:
                self.note(device["address"])
                self._call([device["_path"], "org.bluez.Device1", "Disconnect"], 5,
                           f"couldn't disconnect {device['name']}")
                log.info("bluetooth: disconnected %s", device["name"])
                names.append(device["name"])
        return names

    def forget(self, address: str) -> str:
        device = self._device(address)
        adapter = device["_path"].rsplit("/", 1)[0]
        self._call([adapter, "org.bluez.Adapter1", "RemoveDevice", "o", device["_path"]], 5,
                   f"couldn't forget {device['name']}")
        history = self._history()
        if device["address"] in history:
            history.pop(device["address"])
            self._save_history()
        log.info("bluetooth: forgot %s", device["name"])
        return device["name"]

    def control(self, command: str) -> bool:
        """next / previous / play / pause / toggle on the connected phone (AVRCP)."""
        device = self.connected()
        if device is None:
            return False
        player = self._player(device["_path"])
        if command == "toggle":
            status = str(player[1].get("Status") or "") if player else ""
            command = "play" if status in ("paused", "stopped") else "pause"
        method = COMMANDS.get(command)
        if method is None:
            raise ValueError(f"unknown command {command!r}")
        if player is not None:
            args = [player[0], "org.bluez.MediaPlayer1", method]
        else:
            # A phone without a player object yet: the older, deprecated interface.
            args = [device["_path"], "org.bluez.MediaControl1", method]
        try:
            self._call(args, 3, f"{device['name']} didn't take {command}")
        except BluetoothError as exc:
            log.warning("bluetooth: %s", exc)
            return False
        log.info("bluetooth: %s on %s", command, device["name"])
        return True

    def control_async(self, command: str) -> None:
        """control() off the caller's thread (the panel loop never waits on BlueZ)."""
        self._spawn(lambda: self._safe(self.control, command), "bt-control")

    def reconnect(self, cancelled: Callable[[], bool] = lambda: False,
                  tries: int = RECONNECT_TRIES) -> str | None:
        """Connect the most recently used paired phone that answers.

        Phones are tried in order of last use, CONNECT_S each, until one
        connects or ``cancelled()`` (the owner chose something else). Nothing
        is done if a phone is already connected. Returns the name connected.
        """
        if self._objects(fresh=True) is None:
            return None
        devices = self.devices()
        already = next((d for d in devices if d["connected"]), None)
        if already is not None:
            return already["name"]
        for device in [d for d in devices if d["paired"] and d["_source"]][:tries]:
            if cancelled():
                return None
            rc, _, err = self._run(["call", "org.bluez", device["_path"], "org.bluez.Device1", "Connect"],
                                   CONNECT_S)
            self._drop_cache()
            if rc == 0 and cancelled():
                # The owner chose something else while the phone was answering:
                # it must not take the speaker from that.
                self._run(["call", "org.bluez", device["_path"], "org.bluez.Device1", "Disconnect"], 5)
                log.info("bluetooth: %s answered too late; let go", device["name"])
                return None
            if rc == 0:
                log.info("bluetooth: reconnected %s", device["name"])
                self.note(device["address"])
                return device["name"]
            log.info("bluetooth: %s didn't answer (%s)", device["name"],
                     (err.strip().splitlines() or ["?"])[-1])
        return None

    def reconnect_async(self, cancelled: Callable[[], bool] = lambda: False,
                        done: Callable[[str | None], None] | None = None) -> bool:
        """reconnect() on its own thread; one at a time. False if one already runs."""
        with self._lock:
            if self._reconnecting:
                return False
            self._reconnecting = True

        def run() -> None:
            name = None
            try:
                name = self.reconnect(cancelled)
            except Exception:
                log.exception("bluetooth reconnect failed")
            finally:
                with self._lock:
                    self._reconnecting = False
            if done is not None:
                self._safe(done, name)
        self._spawn(run, "bt-reconnect")
        return True

    @property
    def reconnecting(self) -> bool:
        with self._lock:
            return self._reconnecting

    @staticmethod
    def _safe(fn: Callable, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("bluetooth: %s failed", getattr(fn, "__name__", "call"))

    # -- last used --------------------------------------------------------

    def _history(self) -> dict[str, float]:
        if self._last_used is None:
            self._last_used = {}
            if self.history_file is not None:
                try:
                    raw = json.loads(self.history_file.read_text()).get("last_used", {})
                    self._last_used = {str(k).upper(): float(v) for k, v in raw.items()
                                       if MAC.match(str(k).upper()) and isinstance(v, (int, float))}
                except (OSError, ValueError, AttributeError):
                    pass
        return self._last_used

    def _save_history(self) -> None:
        if self.history_file is None:
            return
        tmp = self.history_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps({"last_used": self._history()}, indent=1, sort_keys=True))
            tmp.replace(self.history_file)
        except OSError as exc:
            log.warning("can't save %s: %s", self.history_file, exc)

    def note(self, address: str) -> None:
        """``address`` is in use now."""
        self._history()[valid_address(address)] = self._wall()
        self._save_history()

    def note_connected(self, devices: list[dict]) -> None:
        """Refresh "last used" for connected phones, at most every NOTE_EVERY_S."""
        now = self._clock()
        if now - self._noted_at < NOTE_EVERY_S:
            return
        self._noted_at = now
        history = self._history()
        for d in devices:
            if d["connected"]:
                history[d["address"]] = self._wall()
        self._save_history()

    def observe_async(self) -> None:
        """From the main loop while Bluetooth plays: note the phone, off-thread."""
        if self._clock() - self._noted_at < NOTE_EVERY_S:
            return
        with self._lock:
            if self._noting:
                return
            self._noting = True

        def run() -> None:
            try:
                self.devices()
            finally:
                with self._lock:
                    self._noting = False
        self._spawn(lambda: self._safe(run), "bt-observe")


class FakeBluetooth:
    """``--fake`` and tests: a phone or two, no BlueZ."""

    def __init__(self, devices: list[dict] | None = None) -> None:
        self.calls: list[tuple] = []
        self.list = devices if devices is not None else []
        self.reconnect_result: str | None = None
        self.codec_doc = {"codec": "SBC", "rate": 44100}

    def adapter(self) -> dict | None:
        return {"path": "/org/bluez/hci0", "name": "Hasler", "powered": True}

    def devices(self) -> list[dict]:
        return [dict(d) for d in self.list]

    public = staticmethod(Bluetooth.public)

    def connected(self) -> dict | None:
        return next((d for d in self.devices() if d.get("connected")), None)

    def playing(self) -> dict | None:
        d = self.connected()
        if d is None:
            return None
        return {"address": d["address"], "name": d["name"], **self.codec_doc,
                "title": "", "artist": "", "paused": False}

    def paused(self) -> bool:
        return False

    def paused_soon(self) -> bool:
        return False

    def _find(self, address: str) -> dict:
        address = valid_address(address)
        for d in self.list:
            if d["address"] == address:
                return d
        raise BluetoothError(f"no paired device {address}", 400)

    def connect(self, address: str, timeout: float = CONNECT_S) -> dict:
        d = self._find(address)
        self.calls.append(("connect", d["address"]))
        d["connected"] = True
        return d

    def disconnect(self, address: str | None = None) -> list[str]:
        self.calls.append(("disconnect", address))
        targets = [self._find(address)] if address else [d for d in self.list if d.get("connected")]
        for d in targets:
            d["connected"] = False
        return [d["name"] for d in targets]

    def forget(self, address: str) -> str:
        d = self._find(address)
        self.calls.append(("forget", d["address"]))
        self.list.remove(d)
        return d["name"]

    def control(self, command: str) -> bool:
        self.calls.append(("control", command))
        return True

    def control_async(self, command: str) -> None:
        self.control(command)

    def reconnect_async(self, cancelled=lambda: False, done=None) -> bool:
        self.calls.append(("reconnect",))
        if done is not None:
            done(self.reconnect_result)
        return True

    reconnecting = False

    def observe_async(self) -> None:
        pass
