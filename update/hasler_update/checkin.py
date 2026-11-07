"""The opt-in daily check-in: "Tell the makers how the radio is" (docs/ota.md §5).

Off by default (``checkin: true`` in box.yaml is the owner's switch on the key
page's Updates). After the daily ``check --stage`` (cli.py), and only then,
one HTTPS POST of about 850 bytes (schema 2) to ``checkin_url`` (the makers'
own server, layout.DEFAULT_CHECKIN_URL, unless box.yaml names another; ``""``
means nowhere), with box.yaml's ``checkin_token`` or else the radio's own word
(``own_token``) - what a maker would ask in the first minute on the phone,
pseudonymous, and nothing that says where the radio is or when someone is home.

Schema 2, every field (docs/ota.md §5 has the same list in words):

    box_id (the random UUID that is already the ring key), short_id, variant,
    version (the one running: /api/health, else the current link, else the
    check's status.json), channel, update_mode, check (ok / error / stale),
    release {state: trial|probation|committed|rolled_back, version, from,
    reason (raw, only when rolled back)}, health {ok, failed checks}, faults
    {now, day: the lamp's fault words now and in the last 24 h}, player
    {stalls_24h, restarts_24h, daemon_restarts_24h}, ytdlp {working, version,
    failures_24h} or null, os_updates {state, last_ok_d} or null, power
    {last_off, cuts_30d, undervolt_now, undervolt_24h, throttled_24h}, temp_c
    {now, max_24h}, storage {free_mb, total_gb, state}, uptime_d (whole days),
    hw {model, code, ram_gb} (the Pi model, its revision code, RAM rounded to
    a power of two - never its serial), os {debian, kernel, moode}.

The 24 h counts come from haslerd's DayCounters (``/api/health`` "day"); a
haslerd without them sends 0 / null there. No send time: the server stamps
what it receives (the radio's clock is the one thing without an RTC).

Never: an IP (the endpoint drops it), the SSID, the hostname, the room, a
place, the timezone, what plays, the volume, standby, when it last played, a
phone, a station, an account, a token, the Pi's serial, the SD card's CID, the
kernel's boot id. ``FORBIDDEN_KEYS`` is the list, and a test asserts no payload
ever carries one.

``build_payload`` is a pure function of the updater's files (box.yaml, the
check's status.json, apply's state.json), two documents haslerd serves
(``/api/diagnostics``, ``/api/health``) and a few generic facts about the
machine (``machine_facts``), so the key page's "See what it sends"
(``hasler-update checkin --preview --stdin``, fed those two documents by
haslerd itself) shows exactly today's payload. ``after_check`` is the
transport: skipped on a metered connection, at most once every MIN_GAP_S,
never retried in a hurry, and held back a random 30-120 min when the radio
was switched on less than 30 min ago (so the makers' "last seen" doesn't date
someone coming home); the attempt is recorded in ``checkin.json`` next to
status.json for the page ("Sent: today 04:12", "Couldn't send today").
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .layout import HEALTH_URL, Layout, box_settings
from .state import iso, parse_iso, read_json, write_json

SCHEMA = 2
DIAGNOSTICS_URL = HEALTH_URL.rsplit("/", 1)[0] + "/diagnostics"
# After a check-in: haslerd fetches the makers' replies to the owner's requests
# ("Send to the makers", haslerd/web/support_api.py) - only if one was sent.
REPLIES_URL = HEALTH_URL.rsplit("/", 1)[0] + "/support/replies"
TIMEOUT_S = 15
MAX_BYTES = 2048                 # the Worker refuses more; ours is about 850
MIN_GAP_S = 20 * 3600            # once a day, however often the check runs
BOOT_QUIET_S = 30 * 60           # switched on less than this ago: hold the check-in back...
BOOT_DELAY_S = (30 * 60, 120 * 60)   # ...by a random this long
USER_AGENT = "hasler-checkin/2"
TOKEN_HEADER = "X-Hasler-Token"
STATUS_FILE = "checkin.json"
REASON_MAX = 120
LOW_FREE_BYTES = 1024 ** 3       # storage "low" below 1 GB or 5 %...
LOW_FREE_SHARE = 0.05
FULL_FREE_BYTES = 20 * 1024 ** 2  # ...and "full" where the lamp's storage fault starts (lamp/faults.py)
FULL_FREE_SHARE = 0.01

# Never in a payload, at any depth (the test walks it).
FORBIDDEN_KEYS = frozenset((
    "ip", "address", "mac", "ssid", "wifi", "hostname", "name", "room", "place", "city", "location",
    "lat", "lon", "latitude", "longitude", "playing", "volume", "standby", "last_play_s", "phone",
    "phones", "preset", "station", "stream_events", "stream_events_recent", "track", "renderer",
    "token", "cookie", "password", "psk", "secret", "update_url", "login_url", "auth_url", "account",
    "email", "id", "machine_id", "dns_name", "tailnet", "words", "timezone", "tz",
    "serial", "cid", "board_serial", "boot_id", "at",
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


# -- the machine: generic facts thousands of Pis share ---------------------------------------


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def machine_facts(layout: Layout, run: Callable[..., Any] = subprocess.run) -> dict:
    """``{"hw": {model, code, ram_gb}, "os": {debian, kernel, moode}}``. The
    model string and revision code (not the ``Serial`` line), RAM rounded up to
    a power of two, and version strings. Paths under ``layout.root`` (tests)."""
    model = _read(layout._p("/proc/device-tree/model")).replace("\x00", "").strip()[:48] or None
    code = None
    m = re.search(r"^Revision\s*:\s*([0-9a-fA-F]+)\s*$", _read(layout._p("/proc/cpuinfo")), re.M)
    if m:
        code = m.group(1).lower()[-8:]
    ram_gb = None
    m = re.search(r"^MemTotal:\s*(\d+)\s*kB", _read(layout._p("/proc/meminfo")), re.M)
    if m and int(m.group(1)) > 0:
        ram_gb = 2 ** max(0, math.ceil(math.log2(int(m.group(1)) / 1024 / 1024)))
    debian = _read(layout._p("/etc/debian_version")).strip()[:16] or None
    kernel = os.environ.get("HASLER_KERNEL") or (os.uname().release if layout.root == Path("/") else None)
    moode = os.environ.get("HASLER_MOODE_VERSION")
    if moode is None and layout.root == Path("/"):
        try:
            out = run(["dpkg-query", "-W", "-f", "${Version}", "moode-player"], capture_output=True, text=True, timeout=10)
            moode = (out.stdout or "").strip().split("-")[0] if out.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            moode = None
    return {"hw": {"model": model, "code": code, "ram_gb": ram_gb},
            "os": {"debian": debian, "kernel": (kernel or None) and str(kernel)[:40], "moode": moode or None}}


def running_version(layout: Layout | None, health: dict, check: dict, diagnostics: dict | None = None) -> str:
    """What runs: haslerd's /api/health, else the ``current`` link, else the
    check's status.json (a snapshot from before a switch: last, never first)."""
    v = str(health.get("version") or "")
    if v and v != "unknown":
        return v
    if layout is not None:
        try:
            name = os.readlink(layout.current)
            if name:
                return Path(name).name
        except OSError:
            pass
    installed = check.get("installed") if isinstance(check.get("installed"), dict) else {}
    return str(installed.get("version") or (diagnostics or {}).get("version") or "")


def _release(state: dict) -> dict | None:
    phase = state.get("phase")
    if phase == "rolled_back":
        return {"state": "rolled_back", "version": state.get("failed") or state.get("to"), "from": state.get("from"),
                "reason": str(state.get("reason") or "")[:REASON_MAX] or None}
    if phase in ("trial", "probation", "committed"):
        return {"state": phase, "version": state.get("to"), "from": state.get("from"), "reason": None}
    return None


def _check_word(check: dict, now: float) -> str | None:
    result = check.get("result")
    if result == "ok" and check.get("index_expires"):
        try:
            if parse_iso(str(check["index_expires"])) < now:
                return "stale"
        except ValueError:
            pass
    return result if result in ("ok", "error") else None


def _days_since(stamp: Any, now: float) -> int | None:
    try:
        return max(0, int((now - parse_iso(str(stamp))) // 86400)) if stamp else None
    except ValueError:
        return None


def _os_updates(os_up: dict | None, now: float) -> dict | None:
    """The OS lane in one word that always has a value (B3: deferred and
    never-run used to come out as null)."""
    if not isinstance(os_up, dict):
        return None
    if not os_up.get("enabled", True):
        state = "off"
    elif os_up.get("paused"):
        state = "paused"
    elif os_up.get("deferred"):
        state = "deferred"
    elif os_up.get("last_result") in ("ok", "failed"):
        state = os_up["last_result"]
    elif os_up.get("last_result"):
        state = "failed"
    else:
        state = "never"
    last_ok = os_up.get("last_run") if os_up.get("last_result") == "ok" else None
    return {"state": state, "last_ok_d": _days_since(last_ok, now)}


def _storage(st: dict) -> dict:
    free, total = st.get("free_bytes"), st.get("total_bytes")
    free = free if isinstance(free, (int, float)) else None
    total = total if isinstance(total, (int, float)) and total > 0 else None
    if st.get("read_only"):
        state = "read-only"
    elif free is None:
        state = "full" if st.get("bad") else None
    elif st.get("bad") or free < FULL_FREE_BYTES or (total and free < FULL_FREE_SHARE * total):
        state = "full"
    elif free < LOW_FREE_BYTES or (total and free < LOW_FREE_SHARE * total):
        state = "low"
    else:
        state = "ok"
    return {"free_mb": None if free is None else int(free // 1024 ** 2),
            "total_gb": None if total is None else round(total / 1024 ** 3), "state": state}


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def build_payload(box: dict, check: dict, apply_state: dict, diagnostics: dict, health: dict,
                  short: str, now: float | None = None, facts: dict | None = None,
                  layout: Layout | None = None) -> dict:
    """Today's check-in. Every field is picked by hand; nothing is copied wholesale."""
    now = time.time() if now is None else now
    facts = facts if isinstance(facts, dict) else {}
    checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
    failed = sorted(k for k, v in checks.items() if (v == "absent" if k == "panel" else not v))
    day = health.get("day") if isinstance(health.get("day"), dict) else {}
    comps = health.get("components") if isinstance(health.get("components"), dict) else {}
    ytdlp = comps.get("yt-dlp") if isinstance(comps.get("yt-dlp"), dict) else None
    temp = diagnostics.get("temperature") if isinstance(diagnostics.get("temperature"), dict) else {}
    thr = temp.get("throttled") if isinstance(temp.get("throttled"), dict) else {}
    storage = diagnostics.get("storage") if isinstance(diagnostics.get("storage"), dict) else {}
    running = diagnostics.get("running") if isinstance(diagnostics.get("running"), dict) else {}
    cuts = diagnostics.get("power_cuts") if isinstance(diagnostics.get("power_cuts"), dict) else {}
    shutdown = diagnostics.get("last_shutdown") if isinstance(diagnostics.get("last_shutdown"), dict) else {}
    faults_now = [str(f) for f in (diagnostics.get("faults") or [])]
    uptime = running.get("uptime_s")
    temp_c = temp.get("c")
    under_now = bool(thr.get("under_voltage_now"))
    hw = facts.get("hw") if isinstance(facts.get("hw"), dict) else {}
    osf = facts.get("os") if isinstance(facts.get("os"), dict) else {}
    temps = [t for t in (day.get("temp_max"), temp_c) if isinstance(t, (int, float)) and not isinstance(t, bool)]
    return {
        "schema": SCHEMA,
        "box_id": str(box.get("box_id") or ""),
        "short_id": short,
        "variant": str(box.get("variant") or diagnostics.get("variant") or ""),
        "version": running_version(layout, health, check, diagnostics),
        "channel": box.get("channel"),
        "update_mode": box.get("update_mode"),
        "check": _check_word(check, now),
        "release": _release(apply_state),
        "health": {"ok": bool(health.get("ok")), "failed": failed},
        "faults": {"now": faults_now, "day": sorted(set(str(f) for f in (day.get("faults") or [])) | set(faults_now))},
        "player": {"stalls_24h": _int(day.get("stalls")) or 0, "restarts_24h": _int(day.get("mpd_restarts")) or 0,
                   "daemon_restarts_24h": _int(day.get("daemon_starts")) or 0},
        "ytdlp": None if ytdlp is None else {
            "working": ytdlp.get("working") if isinstance(ytdlp.get("working"), bool) else None,
            "version": str(ytdlp.get("version") or "")[:24] or None,
            "failures_24h": _int(day.get("ytdlp_failures")) or 0},
        "os_updates": _os_updates(health.get("os_updates") if isinstance(health.get("os_updates"), dict) else None, now),
        "power": {"last_off": shutdown.get("kind"),
                  "cuts_30d": _int(cuts.get("last_30d")) if cuts.get("last_30d") is not None else _int(cuts.get("month")),
                  "undervolt_now": under_now, "undervolt_24h": bool(day.get("undervolt")) or under_now,
                  "throttled_24h": bool(day.get("throttled")) or bool(thr.get("throttled_now") or thr.get("freq_capped_now"))},
        "temp_c": {"now": round(temp_c) if isinstance(temp_c, (int, float)) else None,
                   "max_24h": round(max(temps)) if temps else None},
        "storage": _storage(storage),
        "uptime_d": int(uptime // 86400) if isinstance(uptime, (int, float)) else None,
        "hw": {"model": hw.get("model"), "code": hw.get("code"), "ram_gb": hw.get("ram_gb")},
        "os": {"debian": osf.get("debian"), "kernel": osf.get("kernel"), "moode": osf.get("moode")},
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
    payload = build_payload(box, check, state, diag, health, short_id(layout._p("/etc/machine-id")), now,
                            facts=machine_facts(layout), layout=layout)
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


def poke_replies(url: str = REPLIES_URL, timeout: float = 30) -> None:
    """Ask haslerd (loopback) to fetch the makers' replies; never raises."""
    try:
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={
            "User-Agent": USER_AGENT, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(4096)
    except (urllib.error.URLError, OSError, ValueError):
        pass


def server_root(url: str) -> str:
    """box.yaml's ``checkin_url`` as the server's root: it may be the root itself or the full
    ``…/v1/checkin`` (haslerd's web/support_api.base_url reads it the same way)."""
    return re.sub(r"/v1/checkin$", "", str(url or "").strip().rstrip("/"))


def checkin_endpoint(url: str) -> str:
    """Where the check-in goes: ``<root>/v1/checkin``, whichever form box.yaml gave."""
    root = server_root(url)
    return root + "/v1/checkin" if root else ""


def own_token(layout: Layout, url: str) -> str:
    """The radio's own word with the makers' server (haslerd asked for it at the owner's first
    "Send to the makers": <haslerd state>/secrets/support.json), when it is for this server."""
    base = server_root(url)
    for path in (layout.haslerd_state / "secrets" / "support.json", layout.haslerd_state / "support.json"):
        doc = read_json(path, None)
        if isinstance(doc, dict) and doc.get("server") == base and doc.get("token"):
            return str(doc["token"])
    return ""


def status_path(layout: Layout) -> Path:
    return layout.upd / STATUS_FILE


def boot_uptime(layout: Layout) -> float | None:
    """Seconds since the radio was switched on (/proc/uptime), or None."""
    try:
        return float(_read(layout._p("/proc/uptime")).split()[0])
    except (ValueError, IndexError):
        return None


def after_check(layout: Layout, box: dict | None = None, now: float | None = None,
                fetch: Callable[[str], dict] = fetch_json,
                post: Callable[..., int] = post_json,
                poke: Callable[[], None] | None = None,
                uptime: Callable[[], float | None] | None = None,
                sleep: Callable[[float], None] = time.sleep) -> dict | None:
    """After the daily check: send today's check-in if the owner said yes.

    Returns the record written to ``checkin.json`` ({"at", "ok", "error",
    "skipped", "short_id", "bytes"}), or None when the switch is off (nothing
    is written then, either). Never raises: a failed send is logged in the
    record and the page says "couldn't send today".

    ``uptime`` (seconds since boot; /proc/uptime by default, ``None`` to skip
    the hold): a radio switched on less than BOOT_QUIET_S ago waits a random
    BOOT_DELAY_S first, so the server's "last seen" doesn't date a power-on
    (``Persistent=true`` fires a missed check right at boot).
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
        rec["error"] = "checkin_url is switched off in box.yaml"
    elif box.get("metered"):
        rec["skipped"] = "metered"
    elif last_at is not None and now - last_at < MIN_GAP_S:
        return previous                                        # sent today already: nothing to do
    else:
        up = (uptime or (lambda: boot_uptime(layout)))()
        if isinstance(up, (int, float)) and up < BOOT_QUIET_S:
            delay = random.uniform(*BOOT_DELAY_S)
            sleep(delay)
            now += delay
            rec["at"] = iso(now)
        try:
            payload = collect(layout, fetch=fetch, now=now)
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            rec["bytes"] = len(body)
            status = post(checkin_endpoint(url), payload, str(box.get("checkin_token") or "") or own_token(layout, url))
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
    if rec["ok"]:
        (poke or poke_replies)()    # the makers' replies ride on the daily check-in
    return rec
