"""``GET /api/report``: "Tell the makers" - the report an owner sends by message.

Remote help is live and needs both people at once. This is the other half: a
few lines the owner copies or shares at 23:00 and a maker reads at 09:00. The
first six lines are what a maker asks on the phone, in that order, so an owner
who only phones can read them out ("Say this to the makers" at the top of
Settings -> Something's wrong?)::

    Hasler · Kitchen · radio ab12 · Hasler HM (hasler-hm-hat)
    Version 1.4.0 · Stable · last checked 2 h ago
    Lamp now: red, tapping SOS — something needs a maker: the memory card is full
    Playing: key 2, Radio Clásica · volume 38 · not in standby
    Running for 3 d 4 h · last switched off: the power was cut
    Wi-Fi: on Casa · internet: yes

    Your note: "Since yesterday it goes quiet after a few minutes"

    --- details for the makers ---
    checks: player OK · sound card OK · keys OK · lamp OK · page OK
    faults: storage (12 MB free)
    player restarts last hour: 0 · stalls today: 3
    stream events (last 5): ...
    last update: 1.4.0 installed 7 Nov (previous: 1.3.0 kept)
    security updates: 3 on 21 Oct · yt-dlp 2026.10.22 nightly, working
    temperature 61 °C · throttled: no · free space 12 MB
    updates: Automatic · check-in: off · remote help: last ended 2 d ago (stopped from the page)

    GET /api/report?note=<the owner's line>&wifi=1|0  -> {"text": ..., "json": {...}}

``wifi=0`` leaves the Wi-Fi name out ("include my Wi-Fi name" on the page,
on by default: it is what the makers ask first). Everything in the report is
unprivileged and already served elsewhere; nothing here reads a secret.

**The secret rule lives here, once** (``build``): the JSON twin is put
together from hand-picked fields - never a token, a cookie, the setup
hotspot's password, a phone's name, an account name, a login link, a
station's stream URL - then run through ``diagnostics.strip_secrets`` as a
belt, and the text is rendered *from the stripped JSON*, so the text can
never carry what the JSON doesn't. The JSON twin is the makers' machine-
readable copy (the same facts, keyed) and the shape a later upload would send.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import integration
from .diagnostics import strip_secrets

log = logging.getLogger(__name__)

PATHS = ("/api/report",)
NOTE_MAX = 600                 # the owner's line, at most
EVENTS_SHOWN = 5               # stream events in the details
EVENTS_READ_BYTES = 64 * 1024  # the record on the card, read for "stalls today"

# What the lamp says now (/api/status "lamp", lamp/meanings.STATUS) as
# "<colour>, <rhythm> — <meaning>": the Pilot lamp page's table, in a maker's
# shorthand, so the owner can read the lamp out whatever their eyes say.
LAMP_NOW: dict[str, tuple[str, str, str]] = {
    "playing": ("gold", "a steady filament", "playing"),
    "ready": ("amber", "steady", "ready"),
    "asleep": ("ember", "still", "asleep"),
    "phone": ("moon blue", "breathing", "a phone is playing"),
    "busy": ("amber", "breathing", "busy, don’t unplug"),
    "asking": ("amber", "two kicks, again and again", "asking for a key press"),
    "setup": ("amber", "tapping CQ", "needs setting up: not on the Wi-Fi"),
    "starting": ("amber", "breathing", "starting"),
    "pairing": ("moon blue", "breathing", "looking for a phone"),
    "no_internet": ("red", "a double pulse every few seconds", "no internet"),
    "offline": ("red", "a double pulse every few seconds", "no internet"),
    "fault": ("red", "tapping SOS", "something needs a maker"),
    "off": ("dark", "no light", "off, safe to unplug"),
}
# The lamp's SOS reasons (lamp/faults.py) in the owner's words - the same
# words as the page's FAULT_WORDS (index.html), kept in step by hand.
FAULT_WORDS = {
    "storage": "the memory card is full, or won’t write",
    "panel": "the keys aren’t answering",
    "sound": "the amplifier isn’t answering",
    "heat": "it’s too hot inside",
    "update": "an update got stuck half-way",
}
CHECK_WORDS = {"mpd": "player", "audio_card": "sound card", "panel": "keys", "lamp": "lamp", "web": "page"}
MODE_WORDS = {"auto": "Automatic", "ask": "Ask", "off": "Off"}
WIFI_WITHHELD = "on (name left out)"


# -- small words --------------------------------------------------------------


def _parse_iso(s: Any) -> float | None:
    """An ISO stamp (the updater's ``...Z`` or haslerd's local ``+01:00``) -> epoch, or None."""
    if not isinstance(s, str) or not s:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def ago_words(stamp: Any, now: float) -> str:
    t = _parse_iso(stamp) if isinstance(stamp, str) else (float(stamp) if isinstance(stamp, (int, float)) else None)
    if t is None:
        return "never"
    m = max(0, round((now - t) / 60))
    if m < 2:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    h = round(m / 60)
    if h < 36:
        return f"{h} h ago"
    return f"{round(h / 24)} d ago"


def day_words(stamp: Any) -> str:
    t = _parse_iso(stamp)
    if t is None:
        return ""
    return time.strftime("%-d %b", time.localtime(t))


def size_words(n: Any) -> str:
    if not isinstance(n, (int, float)) or isinstance(n, bool):
        return "?"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB".replace(".0 GB", " GB")
    return f"{max(0, int(n)) // 1024 ** 2} MB"


def span_words(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return "not known"
    s = int(seconds)
    if s >= 86400:
        return f"{s // 86400} d {s % 86400 // 3600} h"
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{max(1, round(s / 60))} min"


def fault_words(reasons: Any) -> str:
    return "; ".join(FAULT_WORDS.get(str(r), str(r).replace("_", " ")) for r in (reasons or []))


def lamp_words(lamp: Any, faults: Any) -> str:
    """"red, tapping SOS — something needs a maker: the memory card is full"."""
    if not lamp:
        return "no lamp on this radio"
    colour, rhythm, meaning = LAMP_NOW.get(str(lamp), ("", "", str(lamp).replace("_", " ")))
    if lamp == "fault" and faults:
        meaning += ": " + fault_words(faults)
    return f"{colour}, {rhythm} — {meaning}" if colour else meaning


def key_words(current_key: Any) -> str:
    k = str(current_key or "")
    return "key " + k[1:] if len(k) > 1 and k[0] == "k" and k[1:].isdigit() else k


# -- the pieces (never raise) --------------------------------------------------------


def _safe(fn: Callable[[], Any], what: str) -> Any:
    try:
        return fn()
    except Exception:
        log.debug("report: no %s", what, exc_info=True)
        return None


def _help_doc(app: Any) -> dict | None:
    """The remote-help state, from the HelpApi among the extra routes (None without one)."""
    for handler in getattr(app, "extra_routes", ()) or ():
        if type(handler).__name__ == "HelpApi":
            return _safe(handler.doc, "remote help")
    return None


def stalls_today(app: Any, now: float) -> int | None:
    """How many times a stream stalled today (the watchdog's record on the card:
    player/watchdog.py StreamEvents, action "stalled"); None without a record."""
    events = getattr(getattr(app, "stream_watch", None), "events", None)
    path = getattr(events, "path", None)
    if path is None:
        return None
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - EVENTS_READ_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    count = 0
    for line in text.splitlines():
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and doc.get("action") == "stalled" and str(doc.get("t") or "").startswith(today):
            count += 1
    return count


def _event_doc(e: Any) -> dict | None:
    """One stream event for the report: when, what, which preset by name and
    number - never its URL (a station's stream URL can carry a key)."""
    if not isinstance(e, dict):
        return None
    preset = e.get("preset") if isinstance(e.get("preset"), dict) else {}
    out = {"t": e.get("t"), "action": e.get("action"), "preset": None, "mpd_state": e.get("mpd_state"),
           "error": str(e.get("error"))[:120] if e.get("error") else None}
    if preset:
        out["preset"] = {"n": preset.get("n"), "name": str(preset.get("name") or "")[:60], "source": preset.get("source")}
    for k in ("reason", "attempt", "previous"):
        if e.get(k) is not None:
            out[k] = str(e[k])[:80] if isinstance(e[k], str) else e[k]
    return out


def _playing_doc(status: dict) -> dict:
    """What plays, without a phone's name: a phone is "a phone over Bluetooth"."""
    pl = status.get("playing") if isinstance(status.get("playing"), dict) else None
    source = status.get("source")
    doc: dict = {"standby": bool(status.get("standby")), "volume": status.get("volume"),
                 "from": status.get("played_from"), "key": key_words(status.get("current_key")) or None,
                 "name": None, "source": None, "recovering": bool(status.get("recovering")),
                 "stalled_s": status.get("stalled_s")}
    if pl:
        doc["name"], doc["source"] = str(pl.get("name") or "")[:60], pl.get("source")
    elif status.get("played_from") == "phone" or status.get("waiting"):
        doc["source"] = source
    return doc


def _software_doc(app: Any) -> dict | None:
    sw = getattr(app, "software", None)
    st = _safe(sw.status, "software") if sw is not None else None
    if not isinstance(st, dict):
        return None
    res = st.get("last_result") if isinstance(st.get("last_result"), dict) else None
    os_up = st.get("os_updates") if isinstance(st.get("os_updates"), dict) else None
    chk = st.get("checkin") if isinstance(st.get("checkin"), dict) else None
    previous = next((r for r in (st.get("releases") or []) if isinstance(r, dict) and r.get("status") == "previous"), None)
    return {
        "version": st.get("version"), "channel": st.get("channel"), "update_mode": st.get("update_mode"),
        "last_check": st.get("last_check"), "stale": bool(st.get("stale")), "check_error": st.get("check_error"),
        "last_result": {k: res.get(k) for k in ("ok", "version", "back_to", "reason", "why", "at", "manual", "probation")} if res else None,
        "previous": previous.get("version") if previous else None,
        "security_updates": ({"last_upgrade": os_up.get("last_upgrade"), "last_result": os_up.get("last_result"),
                              "paused": os_up.get("paused")} if os_up else None),
        "checkin": {"enabled": bool(chk.get("enabled")), "last": chk.get("last")} if chk else None,
    }


# -- the report --------------------------------------------------------------------


def build(app: Any, note: str = "", wifi: bool = True, now: float | None = None) -> dict:
    """``{"text", "json"}`` for ``app`` (a WebApp, or anything shaped like one)."""
    from .. import health
    from . import diagnostics

    now = time.time() if now is None else now
    note = " ".join(str(note or "").split())[:NOTE_MAX]
    diag = _safe(lambda: diagnostics.owner_doc(app), "diagnostics") or {}
    rep = _safe(lambda: health.health_report(app), "health") or {}
    status = _safe(app.status, "status") if callable(getattr(app, "status", None)) else None
    status = status if isinstance(status, dict) else {}
    sw = _software_doc(app)
    help_doc = _help_doc(app)
    variant = getattr(getattr(app, "actions", None), "variant", None)
    room = _safe(lambda: app.room.custom(), "room name") if getattr(app, "room", None) is not None else None
    wifi_doc = diag.get("wifi") if isinstance(diag.get("wifi"), dict) else {}
    checks = rep.get("checks") if isinstance(rep.get("checks"), dict) else {}
    mpd = rep.get("mpd") if isinstance(rep.get("mpd"), dict) else {}
    comps = rep.get("components") if isinstance(rep.get("components"), dict) else {}
    ytdlp = comps.get("yt-dlp") if isinstance(comps.get("yt-dlp"), dict) else None
    temp = diag.get("temperature") if isinstance(diag.get("temperature"), dict) else {}
    thr = temp.get("throttled") if isinstance(temp.get("throttled"), dict) else None
    storage = diag.get("storage") if isinstance(diag.get("storage"), dict) else {}
    faults = [str(f) for f in (diag.get("faults") or status.get("fault_reasons") or [])]
    lamp = status.get("lamp")
    doc = {
        "schema": 1,
        "at": diag.get("at") or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "radio": {
            "room": " ".join(str(room).split())[:60] if room else None,
            "short_id": integration.short_id(getattr(app, "machine_id_file", integration.MACHINE_ID)),
            "model": getattr(variant, "title", None) or "Hasler",
            "variant": getattr(variant, "name", None) or diag.get("variant"),
            "version": (sw or {}).get("version") or rep.get("version") or diag.get("version"),
            "channel": (sw or {}).get("channel"),
            "last_check": (sw or {}).get("last_check"),
            "updates_installed": sw is not None,
        },
        "lamp": {"now": lamp, "words": lamp_words(lamp, faults), "faults": faults, "fault_words": fault_words(faults),
                 "fault_cleared_at": diag.get("fault_cleared_at")},
        "playing": _playing_doc(status),
        "running": {"uptime_s": (diag.get("running") or {}).get("uptime_s"),
                    "words": (diag.get("running") or {}).get("words"),
                    "last_shutdown": (diag.get("last_shutdown") or {}).get("kind"),
                    "last_shutdown_words": (diag.get("last_shutdown") or {}).get("words"),
                    "power_cuts_month": (diag.get("power_cuts") or {}).get("month")},
        "network": {"wifi": (wifi_doc.get("ssid") or None) if wifi else None, "wifi_withheld": not wifi,
                    "hotspot": bool(wifi_doc.get("hotspot")), "signal": wifi_doc.get("signal"),
                    "online": diag.get("online", status.get("online"))},
        "note": note or None,
        "checks": {k: checks.get(k) for k in CHECK_WORDS if k in checks},
        "fault_details": {
            "storage_free_bytes": storage.get("free_bytes"), "storage_read_only": bool(storage.get("read_only")),
            "temperature_c": temp.get("c"),
        },
        "player": {"restarts_last_hour": int(mpd.get("restarts_last_hour") or 0) if mpd else None,
                   "unresponsive_s": mpd.get("unresponsive_s") if mpd else None,
                   "recovering": bool(rep.get("recovering")), "recovering_reason": rep.get("recovering_reason"),
                   "stalled_s": rep.get("stalled_s"), "stalls_today": stalls_today(app, now)},
        "stream_events": [d for d in (_event_doc(e) for e in (rep.get("stream_events_recent") or [])[-EVENTS_SHOWN:]) if d],
        "software": sw,
        "ytdlp": ({"version": ytdlp.get("version"), "channel": ytdlp.get("channel"), "working": ytdlp.get("working")}
                  if ytdlp else None),
        "temperature": {"c": temp.get("c"), "hot": bool(temp.get("hot")),
                        "throttled": thr.get("raw") if thr else None, "throttled_now": bool(thr and thr.get("any_now")),
                        "throttled_seen": bool(thr and thr.get("any_seen"))},
        "storage": {"free_bytes": storage.get("free_bytes"), "total_bytes": storage.get("total_bytes"),
                    "read_only": bool(storage.get("read_only")), "bad": bool(storage.get("bad"))},
        "remote_help": ({"state": help_doc.get("state"), "last": help_doc.get("last"),
                         "available": help_doc.get("available")} if isinstance(help_doc, dict) else None),
    }
    doc = strip_secrets(doc)
    return {"text": render(doc, now), "json": doc}


def header_lines(doc: dict, now: float | None = None) -> list[str]:
    """The six lines a maker asks for on the phone ("Say this to the makers")."""
    now = time.time() if now is None else now
    r, lamp, pl, run, net = doc["radio"], doc["lamp"], doc["playing"], doc["running"], doc["network"]
    model = str(r.get("model") or "Hasler")
    line1 = " · ".join(b for b in ("Hasler", r.get("room") or "no room name", f"radio {r.get('short_id') or '?'}",
                                    f"{model} ({r.get('variant')})" if r.get("variant") else model))
    if r.get("updates_installed"):
        channel = str(r.get("channel") or "stable").capitalize()
        channel = "Bench" if channel == "Bench" else channel
        line2 = f"Version {r.get('version') or '?'} · {channel} · last checked {ago_words(r.get('last_check'), now)}"
    else:
        line2 = f"Version {r.get('version') or '?'} · updates not set up on this radio"
    line3 = "Lamp now: " + str(lamp.get("words") or "")
    if pl.get("standby"):
        what = "nothing"
    elif pl.get("name"):
        what = (f"{pl['key']}, " if pl.get("key") else "from the page, ") + str(pl["name"])
        if pl.get("recovering"):
            what += " (stalled" + (f" for {span_words(pl.get('stalled_s'))}" if pl.get("stalled_s") else "") + ")"
    elif pl.get("from") == "phone" or pl.get("source") in ("bluetooth", "airplay", "spotify"):
        what = "a phone" + (f" over {pl['source']}" if pl.get("source") else "")
    elif pl.get("from") == "room":
        what = "along with another room"
    else:
        what = "nothing"
    vol = f"volume {pl['volume']}" if isinstance(pl.get("volume"), (int, float)) else "volume ?"
    line4 = f"Playing: {what} · {vol} · {'in standby' if pl.get('standby') else 'not in standby'}"
    line5 = f"Running for {run.get('words') or span_words(run.get('uptime_s'))} · last switched off: {run.get('last_shutdown_words') or 'not known'}"
    if net.get("hotspot"):
        wifi = "setup mode, not on the home Wi-Fi"
    elif net.get("wifi_withheld"):
        wifi = WIFI_WITHHELD
    elif net.get("wifi"):
        wifi = f"on {net['wifi']}"
    else:
        wifi = "none (a cable?)"
    online = net.get("online")
    line6 = f"Wi-Fi: {wifi} · internet: {'yes' if online else 'no' if online is False else 'not known'}"
    return [line1, line2, line3, line4, line5, line6]


def render(doc: dict, now: float | None = None) -> str:
    """The whole report as plain text, from the (stripped) JSON twin."""
    now = time.time() if now is None else now
    lines = header_lines(doc, now)
    if doc.get("note"):
        lines += ["", f"Your note: \"{doc['note']}\""]
    lines += ["", "--- details for the makers ---"]
    checks = doc.get("checks") or {}
    bits = []
    for key, word in CHECK_WORDS.items():
        if key not in checks:
            continue
        v = checks[key]
        if key == "panel":
            bits.append(f"{word} {'OK' if v == 'ok' else 'not fitted' if v == 'allowed_absent' else 'ABSENT'}")
        else:
            bits.append(f"{word} {'OK' if v else 'FAILED'}")
    lines.append("checks: " + (" · ".join(bits) if bits else "none"))
    lamp, det = doc.get("lamp") or {}, doc.get("fault_details") or {}
    faults = lamp.get("faults") or []
    if faults:
        extra = []
        for f in faults:
            if f == "storage":
                extra.append("storage (" + ("read-only" if det.get("storage_read_only") else f"{size_words(det.get('storage_free_bytes'))} free") + ")")
            elif f == "heat" and isinstance(det.get("temperature_c"), (int, float)):
                extra.append(f"heat ({round(det['temperature_c'])} °C)")
            else:
                extra.append(f)
        lines.append("faults: " + " · ".join(extra))
    else:
        lines.append("faults: none" + (f" (the last one cleared {ago_words(lamp.get('fault_cleared_at'), now)})" if lamp.get("fault_cleared_at") else ""))
    pl = doc.get("player") or {}
    stalls = pl.get("stalls_today")
    lines.append(f"player restarts last hour: {pl.get('restarts_last_hour') if pl.get('restarts_last_hour') is not None else '?'}"
                 f" · stalls today: {stalls if stalls is not None else '?'}"
                 + (f" · stalled now: {pl.get('recovering_reason') or 'yes'}" if pl.get("recovering") else ""))
    events = doc.get("stream_events") or []
    if events:
        lines.append(f"stream events (last {len(events)}):")
        for e in events:
            p = e.get("preset") or {}
            who = f" · {p.get('n')} ({p.get('name')})" if p else ""
            why = f" · {e['error']}" if e.get("error") else f" · {e['reason']}" if e.get("reason") else ""
            lines.append(f"  {e.get('t') or '?'} {e.get('action') or '?'}{who}{why}")
    else:
        lines.append("stream events: none recorded")
    sw = doc.get("software")
    if sw:
        res = sw.get("last_result")
        if not res:
            upd = "none yet"
        elif res.get("ok") is False:
            upd = (f"{res.get('version')} rolled back to {res.get('back_to') or 'the one before'}"
                   + (f" on {day_words(res.get('at'))}" if res.get("at") else "")
                   + (f" — {res.get('why')}" if res.get("why") else "")
                   + (f" (raw: {res.get('reason')})" if res.get("reason") else ""))
        elif res.get("ok"):
            upd = (f"{res.get('version')} {'went back by hand' if res.get('manual') else 'installed'}"
                   + (f" {day_words(res.get('at'))}" if res.get("at") else "") + (" · settling in" if res.get("probation") else ""))
        else:
            upd = f"{res.get('version')} installing now"
        if sw.get("previous"):
            upd += f" (previous: {sw['previous']} kept)"
        lines.append("last update: " + upd + (f" · last check failed: {sw['check_error']}" if sw.get("check_error") else
                                              " · no fresh update information" if sw.get("stale") else ""))
        sec = sw.get("security_updates") or {}
        up = sec.get("last_upgrade") if isinstance(sec.get("last_upgrade"), dict) else None
        sec_words = (f"{up.get('count')} on {day_words(up.get('at'))}" if up else "none yet") + (" · paused" if sec.get("paused") else "")
    else:
        lines.append("last update: updates not set up on this radio")
        sec_words = "not on this radio"
    yd = doc.get("ytdlp")
    yd_words = (f"yt-dlp {yd.get('version') or '?'}" + (f" {yd['channel']}" if yd.get("channel") else "")
                + (", working" if yd.get("working") else ", not working" if yd.get("working") is False else "")) if yd else "yt-dlp: not on this radio"
    lines.append(f"security updates: {sec_words} · {yd_words}")
    t, st = doc.get("temperature") or {}, doc.get("storage") or {}
    thr = "no" if t.get("throttled") in (None, "0x0") and not t.get("throttled_now") else (
        "now" if t.get("throttled_now") else f"earlier this boot ({t.get('throttled')})" if t.get("throttled_seen") else f"no ({t.get('throttled')})")
    lines.append(f"temperature {round(t['c']) if isinstance(t.get('c'), (int, float)) else '?'} °C · throttled: {thr}"
                 f" · free space {size_words(st.get('free_bytes'))}" + (" (read-only)" if st.get("read_only") else ""))
    rh = doc.get("remote_help")
    if rh is None or rh.get("available") is False:
        help_words = "not on this radio"
    elif rh.get("state") and rh["state"] != "off":
        help_words = {"waiting": "waiting for a maker to approve the link", "connected": "on now",
                      "starting": "starting", "ending": "ending"}.get(str(rh["state"]), str(rh["state"]))
    elif isinstance(rh.get("last"), dict) and rh["last"].get("at"):
        why = str(rh["last"].get("text") or "").rstrip(".")
        help_words = f"last ended {ago_words(rh['last']['at'], now)}" + (f" ({why[0].lower() + why[1:]})" if why else "")
    else:
        help_words = "never used"
    chk = (sw or {}).get("checkin") or {}
    lines.append(f"updates: {MODE_WORDS.get(str((sw or {}).get('update_mode')), 'not set up')}"
                 f" · check-in: {'on' if chk.get('enabled') else 'off'} · remote help: {help_words}")
    return "\n".join(lines) + "\n"


class ReportApi:
    """``GET /api/report``, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        url = urlparse(path)
        if (url.path.rstrip("/") or "/") not in PATHS:
            return None
        if method.upper() != "GET":
            return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}
        q = parse_qs(url.query)
        note = q.get("note", [""])[0]
        wifi = q.get("wifi", ["1"])[0].strip().lower() not in ("0", "false", "no", "off")
        return 200, {"ok": True, **build(self.app, note=note, wifi=wifi)}
