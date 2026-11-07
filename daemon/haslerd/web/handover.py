"""Hand the radio to a new owner: it forgets you, keeps what belongs to the cabinet.

    POST /api/confirm/request {"action": "handover"}   -> a press on the radio, always
    POST /api/handover {"confirm": token}              -> {"setup": {"ssid", "password"}, "steps": [...]}
    GET  /api/handover                                 -> what it forgets and keeps, the makers' notes

``run`` goes through the steps below in order, each logged, none fatal: a
step that fails is noted in the answer and the next one runs, because a
half-forgotten radio handed over is worse than one that says what it could
not do. Nothing is wiped by directory: every file touched is named here
(ALLOW-LIST), so the knobs' calibration (/etc/hasler/variant.d), box.yaml,
the trust keys, the release, lamp-version, the caches and the makers' notes
(maker-notes.jsonl) stay. Afterwards the server restarts the radio: with no
network saved it opens its setup hotspot, with the NEW password this returned
once, and the new owner's first ten minutes hold word for word.

Forgets: Spotify (and the sign-in/sign-out backups, which hold a token),
YouTube Music, Home Assistant (and which HA the keys were made for), the
paired phones, the allowed addresses, the keys (back to the built-in
stations), the stations and reports, what it was playing, the room's name
(casting names back to "Hasler"), multiroom, every setting but the speaker's
(volume ceiling, quietest step, mono), the check-in switch in box.yaml, every
Wi-Fi network and the setup password, the journal.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .. import boxconfig
from ..state import StateStore
from .room import CAST_BRAND
from .server import SETTINGS_FILE, KEYS_BAK, load_settings, save_settings

log = logging.getLogger(__name__)

# settings.json keys that belong to the speaker, not the owner.
KEEP_SETTINGS = ("schema", "volume_limit", "volume_floor", "mono")
# Files in the state directory that go. Nothing else there is touched.
STATE_FILES = ("stations.json", "reports.jsonl", "state.json", "ha-instance.json", "bluetooth.json",
               "allowed-hosts.json", "keys.yaml", KEYS_BAK, "wifi-joined", "hotspot.txt",
               "support-requests.json")
# ... and in secrets/ (or beside the state on a box from before it).
SECRET_FILES = ("ha.json", "ytm-browser.json", "ytm-cookies.txt")
# box.yaml keys that are the owner's, not the cabinet's.
BOX_KEYS_DROPPED = ("checkin",)
MAKER_NOTES = "maker-notes.jsonl"
# The casting names go back to "Hasler" through moOde's worker: wait this long for it.
RENAME_WAIT_S = 20.0
JOURNAL_VACUUM = ("sudo", "-n", "/usr/bin/journalctl", "--vacuum-time=1s")

FORGETS = ("your Wi-Fi networks and this radio's setup password (a new one is made and shown to you once)",
           "your Spotify and YouTube Music sign-ins", "Home Assistant", "your phones",
           "your keys and stations", "the room's name", "the other addresses you allowed",
           "what it was playing, and the radio's log")
KEEPS = ("how the knobs are calibrated", "the lamp's colours", "the volume ceiling and the quietest step",
         "its software and updates", "the makers' notes about this cabinet")


def state_dir(app: Any) -> Path | None:
    settings_file = getattr(app, "settings_file", None)
    return Path(settings_file).parent if settings_file is not None else None


def maker_notes(app: Any) -> list[dict]:
    """The makers' notes about this cabinet (<state>/maker-notes.jsonl, one JSON
    object a line, if the file exists): the page lists them before the press,
    so the departing owner can ask for one to go. Never created here."""
    folder = state_dir(app)
    if folder is None:
        return []
    path = folder / MAKER_NOTES
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict):
            text = doc.get("text") or doc.get("note") or doc.get("message") or ""
            out.append({"at": doc.get("at") or doc.get("when") or "", "text": str(text)[:400]})
    return out


def describe(app: Any) -> dict:
    """``GET /api/handover``: what a hand-over does on this radio."""
    return {"forgets": list(FORGETS), "keeps": list(KEEPS), "maker_notes": maker_notes(app)}


class _Steps:
    def __init__(self) -> None:
        self.done: list[dict] = []

    def run(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            note = fn()
        except Exception as exc:
            log.warning("hand-over: %s failed: %s", name, exc, exc_info=True)
            self.done.append({"id": name, "ok": False, "note": str(exc) or type(exc).__name__})
            return None
        self.done.append({"id": name, "ok": True, "note": "" if note is None else str(note)})
        log.info("hand-over: %s done%s", name, f" ({note})" if note else "")
        return note


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False


def run(app: Any, vacuum: Callable[..., Any] = subprocess.run) -> dict:
    """The hand-over, in order. Returns the new setup network and every step's outcome."""
    steps = _Steps()
    folder = state_dir(app)
    actions = app.actions

    def spotify() -> str:
        api = getattr(app, "spotify_account", None)
        if api is None or not hasattr(api, "purge"):
            return "not on this radio"
        gone = api.purge()
        return ", ".join(gone) if gone else "nothing to forget"

    def ytm() -> str:
        src = getattr(app, "ytm", None)
        if src is None or not hasattr(src, "sign_out"):
            return "not on this radio"
        src.sign_out()
        return "signed out"

    def home_assistant() -> str:
        api = getattr(app, "ha_api", None)
        notes = []
        if api is not None and hasattr(api, "disconnect"):
            if getattr(api, "settings", None) is not None:
                api.disconnect()
                notes.append("disconnected")
        if folder is not None and _unlink(folder / "ha-instance.json"):
            notes.append("the keys' HA forgotten")
        return ", ".join(notes) or "nothing to forget"

    def bluetooth() -> str:
        bt = getattr(actions, "bluetooth", None)
        names: list[str] = []
        if bt is not None and hasattr(bt, "forget_all"):
            names = list(bt.forget_all() or [])
        if folder is not None:
            _unlink(folder / "bluetooth.json")
        return ", ".join(names) if names else "no phones"

    def hosts() -> str:
        gone = app.hosts.clear()
        return ", ".join(gone) if gone else "none allowed"

    def keys() -> str:
        app.reset(keep=False)
        return "built-in stations"

    def files() -> str:
        if folder is None:
            return "no state directory"
        # What it was playing: the store is swapped for an in-memory one, so
        # the restart's own save doesn't write state.json back.
        store = getattr(actions, "state", None)
        if store is not None:
            actions.state = StateStore(None)
        gone = [n for n in ("stations.json", "reports.jsonl", "state.json") if _unlink(folder / n)]
        return ", ".join(gone) if gone else "nothing there"

    def settings() -> str:
        settings_file = getattr(app, "settings_file", None)
        if settings_file is None:
            return "no settings file"
        with app.lock:
            doc = load_settings(settings_file)
            kept = {k: doc[k] for k in KEEP_SETTINGS if k in doc}
            kept.setdefault("schema", boxconfig.SCHEMA)
            save_settings(settings_file, kept)
        app.room.load(kept)
        return "kept " + ", ".join(k for k in KEEP_SETTINGS if k in kept and k != "schema")

    def casting() -> str:
        room = app.room
        room.apply(CAST_BRAND)
        room.wait(RENAME_WAIT_S)
        state = room.doc()["apply"]["state"]
        return f"phones see {CAST_BRAND}" if state == "done" else f"renaming: {state}"

    def multiroom() -> str:
        group = getattr(app, "group", None)
        if group is None or not hasattr(group, "configure"):
            return "not on this radio"
        group.configure(enabled=False)
        return "off"

    def box() -> str:
        paths = getattr(app, "box_paths", None)
        if paths is None or not paths.box_file.exists():
            return "no box.yaml"
        doc = boxconfig.read_box_file(paths)
        dropped = [k for k in BOX_KEYS_DROPPED if k in doc]
        if not dropped:
            return "nothing to drop"
        for k in dropped:
            del doc[k]
        boxconfig.write_box_file(paths, doc)
        return "dropped " + ", ".join(dropped)

    def wifi() -> str:
        gone = app.wifi.forget_all().get("forgotten") or []
        return ", ".join(gone) if gone else "none saved"

    def setup_network() -> dict:
        return app.wifi.set_hotspot_password(None)

    def secrets() -> str:
        if folder is None:
            return "no state directory"
        gone = [n for n in SECRET_FILES if _unlink(boxconfig.secret_file(folder, n))]
        gone += [n for n in STATE_FILES if n not in ("stations.json", "reports.jsonl", "state.json", "hotspot.txt")
                 and _unlink(folder / n)]
        return ", ".join(gone) if gone else "nothing left"

    def journal() -> str:
        out = vacuum(list(JOURNAL_VACUUM), capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or "journalctl failed").strip()[-200:])
        return "emptied"

    steps.run("spotify", spotify)
    steps.run("ytm", ytm)
    steps.run("home_assistant", home_assistant)
    steps.run("bluetooth", bluetooth)
    steps.run("hosts", hosts)
    steps.run("keys", keys)
    steps.run("files", files)
    steps.run("settings", settings)
    steps.run("casting", casting)
    steps.run("multiroom", multiroom)
    steps.run("box", box)
    steps.run("wifi", wifi)
    setup = steps.run("setup_network", setup_network)
    steps.run("secrets", secrets)
    steps.run("journal", journal)
    failed = [s["id"] for s in steps.done if not s["ok"]]
    log.warning("hand-over done: %d steps, %s", len(steps.done),
                "every one of them" if not failed else "failed: " + ", ".join(failed))
    return {"setup": setup if isinstance(setup, dict) else None, "steps": steps.done,
            "failed": failed, "at": time.time()}


def reboot_later(reboot: Callable[[], Any], seconds: float) -> threading.Timer:
    """The restart after the page has its answer; a daemon thread, never joined."""
    timer = threading.Timer(seconds, reboot)
    timer.daemon = True
    timer.start()
    return timer
