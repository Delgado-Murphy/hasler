"""The opt-in daily check-in: "Tell the makers how the radio is" (docs/ota.md §5).

Off by default (``checkin: true`` in box.yaml is the owner's switch on the key
page's Updates). After the daily ``check --stage`` (cli.py), and only then,
one HTTPS POST of about 400 bytes to ``checkin_url`` - what a maker would ask
in the first minute on the phone, pseudonymous, and nothing that says where
the radio is or when someone is home:

    box_id (the random UUID that is already the ring key), short_id, variant,
    version, channel, update mode, the last check's result, last_result (ok /
    rolled back + the raw reason), health ok + the failed checks, fault_reasons,
    MPD restarts and stalls today (counts), the yt-dlp lane working or not, the
    OS updates' last result, uptime in days, the last shutdown's kind, power
    cuts this month, free space, CPU °C, the throttle bits. About half a
    kilobyte with every field filled.

Never: an IP (the endpoint drops it), the SSID, the hostname, the room, a
place, what plays, the volume, standby, when it last played, a phone, a
station, an account, a token. ``FORBIDDEN_KEYS`` is the list, and a test
asserts no payload ever carries one.

``build_payload`` is a pure function of the updater's files (box.yaml, the
check's status.json, apply's state.json) and two documents haslerd serves
(``/api/diagnostics``, ``/api/health``), so the key page's "See what it
sends" (``hasler-update checkin --preview --stdin``, fed those two documents
by haslerd itself) shows exactly today's payload. ``after_check`` is the
transport: skipped on a metered connection, at most once every MIN_GAP_S,
never retried in a hurry; the attempt is recorded in ``checkin.json`` next to
status.json for the page ("Sent: today 04:12", "Couldn't send today").
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .layout import HEALTH_URL, Layout, box_settings
from .state import iso, parse_iso, read_json, write_json

SCHEMA = 1
DIAGNOSTICS_URL = HEALTH_URL.rsplit("/", 1)[0] + "/diagnostics"
TIMEOUT_S = 15
MAX_BYTES = 2048                 # the Worker refuses more; ours is about 400
MIN_GAP_S = 20 * 3600            # once a day, however often the check runs
USER_AGENT = "hasler-checkin/1"
TOKEN_HEADER = "X-Hasler-Token"
STATUS_FILE = "checkin.json"

# Never in a payload, at any depth (the test walks it).
FORBIDDEN_KEYS = frozenset((
    "ip", "address", "mac", "ssid", "wifi", "hostname", "name", "room", "place", "city", "location",
    "lat", "lon", "latitude", "longitude", "playing", "volume", "standby", "last_play_s", "phone",
    "phones", "preset", "station", "stream_events", "stream_events_recent", "track", "renderer",
    "token", "cookie", "password", "psk", "secret", "update_url", "login_url", "auth_url", "account",
    "email", "id", "machine_id", "dns_name", "tailnet", "words",
))


def short_id(machine_id_file: Path) -> str:
    """The first 4 hex of sha256(/etc/machine-id) - the same rule as haslerd's
    web/integration.py, so the makers' dashboard says what the Settings footer says."""
    try:
        seed = machine_id_file.read_text().strip()
    except OSError:
        seed = ""
    if not seed:
        seed = "host:" + socket.gethostname()
    return hashlib.sha256(seed.encode()).hexdigest()[:4]


def _last_result(state: dict) -> dict | None:
    phase = state.get("phase")
    if phase == "rolled_back":
        return {"ok": False, "version": state.get("failed") or state.get("to"), "back_to": state.get("from"),
                "reason": str(state.get("reason") or "")[:160]}
    if phase in ("committed", "probation"):
        return {"ok": True, "version": state.get("to"), "probation": phase == "probation"}
    if phase == "trial":
        return {"ok": None, "version": state.get("to")}
    return None


def _count_today(events: Any, action: str, today: str) -> int:
    n = 0
    for e in events if isinstance(events, list) else []:
        if isinstance(e, dict) and e.get("action") == action and str(e.get("t") or "").startswith(today):
            n += 1
    return n


def build_payload(box: dict, check: dict, apply_state: dict, diagnostics: dict, health: dict,
                  short: str, now: float | None = None) -> dict:
    """Today's check-in. Every field is picked by hand; nothing is copied wholesale."""
    now = time.time() if now is None else now
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
    failed = sorted(k for k, v in checks.items() if (v == "absent" if k == "panel" else not v))
    mpd = health.get("mpd") if isinstance(health.get("mpd"), dict) else {}
    comps = health.get("components") if isinstance(health.get("components"), dict) else {}
    ytdlp = comps.get("yt-dlp") if isinstance(comps.get("yt-dlp"), dict) else None
    os_up = health.get("os_updates") if isinstance(health.get("os_updates"), dict) else None
    temp = diagnostics.get("temperature") if isinstance(diagnostics.get("temperature"), dict) else {}
    thr = temp.get("throttled") if isinstance(temp.get("throttled"), dict) else None
    storage = diagnostics.get("storage") if isinstance(diagnostics.get("storage"), dict) else {}
    running = diagnostics.get("running") if isinstance(diagnostics.get("running"), dict) else {}
    cuts = diagnostics.get("power_cuts") if isinstance(diagnostics.get("power_cuts"), dict) else {}
    shutdown = diagnostics.get("last_shutdown") if isinstance(diagnostics.get("last_shutdown"), dict) else {}
    installed = check.get("installed") if isinstance(check.get("installed"), dict) else {}
    uptime = running.get("uptime_s")
    events = health.get("stream_events_recent")
    temp_c = temp.get("c")
    return {
        "schema": SCHEMA,
        "box_id": str(box.get("box_id") or ""),
        "short_id": short,
        "variant": str(box.get("variant") or diagnostics.get("variant") or ""),
        "version": str(installed.get("version") or health.get("version") or diagnostics.get("version") or ""),
        "channel": box.get("channel"),
        "update_mode": box.get("update_mode"),
        "check": check.get("result"),
        "last_result": _last_result(apply_state),
        "health": {"ok": bool(health.get("ok")), "failed": failed},
        "faults": [str(f) for f in (diagnostics.get("faults") or [])],
        "player": {"restarts_last_hour": int(mpd.get("restarts_last_hour") or 0) if mpd else None,
                   "stalls_today": _count_today(events, "stalled", today)},
        "ytdlp": ytdlp.get("working") if ytdlp else None,
        "os_updates": ("paused" if os_up.get("paused") else os_up.get("last_result")) if os_up else None,
        "uptime_days": round(uptime / 86400, 1) if isinstance(uptime, (int, float)) else None,
        "last_shutdown": shutdown.get("kind"),
        "power_cuts_month": cuts.get("month"),
        "free_bytes": storage.get("free_bytes"),
        "storage_bad": bool(storage.get("bad") or storage.get("read_only")),
        "temp_c": round(temp_c) if isinstance(temp_c, (int, float)) else None,
        "throttled": thr.get("raw") if thr else None,
        "at": iso(now),
    }


def forbidden_in(doc: Any, path: str = "") -> list[str]:
    """Every key of ``doc`` (at any depth) that FORBIDDEN_KEYS names, as paths."""
    out = []
    if isinstance(doc, dict):
        for k, v in doc.items():
            if str(k) in FORBIDDEN_KEYS:
                out.append(f"{path}/{k}")
            out += forbidden_in(v, f"{path}/{k}")
    elif isinstance(doc, list):
        for v in doc:
            out += forbidden_in(v, path)
    return out


def fetch_json(url: str, timeout: float = TIMEOUT_S) -> dict:
    """One of haslerd's own documents (loopback)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        doc = json.loads(r.read(1 << 20).decode("utf-8"))
    return doc if isinstance(doc, dict) else {}


def collect(layout: Layout, docs: dict | None = None, fetch: Callable[[str], dict] = fetch_json,
            now: float | None = None) -> dict:
    """Today's payload for this radio. ``docs`` (``{"diagnostics", "health"}``)
    comes from haslerd on stdin for the page's preview; otherwise both are
    fetched from haslerd's loopback page."""
    box = box_settings(layout)
    check = read_json(layout.check_status, {}) or {}
    state = read_json(layout.state_file, {}) or {}
    if docs is None:
        docs = {"diagnostics": fetch(DIAGNOSTICS_URL), "health": fetch(HEALTH_URL)}
    diag = docs.get("diagnostics") if isinstance(docs.get("diagnostics"), dict) else {}
    health = docs.get("health") if isinstance(docs.get("health"), dict) else {}
    payload = build_payload(box, check, state, diag, health, short_id(layout._p("/etc/machine-id")), now)
    bad = forbidden_in(payload)
    if bad:                                # belt and braces: never send what the rule forbids
        raise ValueError("the check-in would carry " + ", ".join(bad))
    return payload


def post_json(url: str, payload: dict, token: str = "", timeout: float = TIMEOUT_S) -> int:
    """POST ``payload`` to ``url`` (HTTPS only); the status code. Raises on failure."""
    if not url.lower().startswith("https://"):
        raise ValueError("checkin_url must be https://")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError(f"the check-in is {len(body)} bytes, more than {MAX_BYTES}")
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if token:
        headers[TOKEN_HEADER] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.status)


def status_path(layout: Layout) -> Path:
    return layout.upd / STATUS_FILE


def after_check(layout: Layout, box: dict | None = None, now: float | None = None,
                fetch: Callable[[str], dict] = fetch_json,
                post: Callable[..., int] = post_json) -> dict | None:
    """After the daily check: send today's check-in if the owner said yes.

    Returns the record written to ``checkin.json`` ({"at", "ok", "error",
    "skipped", "short_id", "bytes"}), or None when the switch is off (nothing
    is written then, either). Never raises: a failed send is logged in the
    record and the page says "couldn't send today".
    """
    now = time.time() if now is None else now
    box = box if box is not None else box_settings(layout)
    if not box.get("checkin"):
        return None
    rec: dict = {"at": iso(now), "ok": False, "error": None, "skipped": None,
                 "short_id": short_id(layout._p("/etc/machine-id")), "bytes": None}
    previous = read_json(status_path(layout), {}) or {}
    try:
        last_at = parse_iso(str(previous.get("at"))) if previous.get("ok") else None
    except ValueError:
        last_at = None
    url = str(box.get("checkin_url") or "")
    if not url:
        rec["error"] = "no checkin_url in box.yaml"
    elif box.get("metered"):
        rec["skipped"] = "metered"
    elif last_at is not None and now - last_at < MIN_GAP_S:
        return previous                                        # sent today already: nothing to do
    else:
        try:
            payload = collect(layout, fetch=fetch, now=now)
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            rec["bytes"] = len(body)
            status = post(url, payload, str(box.get("checkin_token") or ""))
            if 200 <= status < 300:
                rec["ok"] = True
            else:
                rec["error"] = f"the makers' server answered {status}"
        except urllib.error.HTTPError as e:
            rec["error"] = f"the makers' server answered {e.code}"
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as e:
            rec["error"] = str(e)[:160]
    try:
        write_json(status_path(layout), rec, 0o644)
    except OSError:
        pass
    return rec
