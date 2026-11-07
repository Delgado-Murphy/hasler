"""Diagnostics for the key page: the owner's "How the radio is" card (This
radio) and the maker's support bundle (Maker tools -> Diagnostics -> Copy
support bundle). Both unprivileged, both read what haslerd already has or what
the kernel lets ``pi`` read; nothing here touches the I2C bus or the panel loop.

``GET /api/diagnostics`` - ``owner_doc(app)``, plain words beside the raw
values, so the page (and "Something's wrong?") can say or decide::

    {
      "wifi":          {"ssid": "Casa", "signal": 0-100 | null, "hotspot": bool,
                        "words": "On Casa, good signal"},
      "online":        bool | null,         # the internet probe (net.Connectivity); null = no probe
      "temperature":   {"c": 61.2 | null, "hot": bool,
                        "throttled": {"raw": "0x50000", "under_voltage_now": bool, ...,
                                      "any_now": bool, "any_seen": bool} | null,   # lamp/faults.throttled
                        "words": "warm (61 °C)"},
      "storage":       {"free_bytes": n, "total_bytes": n, "read_only": bool, "bad": bool,
                        "words": "9.1 GB free of 29 GB"},
      "last_shutdown": {"kind": "clean" | "power-cut" | "restart" | "unknown",
                        "words": "switched off properly"},                        # bootresume.shutdown_kind
      "power_cuts":    {"total": n, "month": n, "last_at": iso | null, "hint": bool},  # bootresume.power_cuts
      "running":       {"uptime_s": n (the system), "daemon_uptime_s": n, "since": iso | null,
                        "words": "3 d 4 h"},
      "player":        {"mpd": bool, "audio_card": bool, "recovering": bool, "restarts_last_hour": n,
                        "dsp": {"installed": bool, "running": bool | null, "clipped_samples": n | null},
                        "words": "player answering · sound card present · tone DSP live"},
      "lamp":          {"present": bool, "words": "..."},
      "panel":         "ok" | "absent" | "allowed_absent",
      "faults":        ["storage", ...],    # the lamp's SOS reasons now (lamp/faults.py words)
      "fault_cleared_at": iso | null,       # when the last SOS cleared (once per clearing)
      "software":      {"version", "last_check", "stale", "last_result", "os_updates", "metered"} | null,
      "variant": "hasler-hm", "version": "1.5.0", "at": iso
    }

``GET /api/diagnostics/bundle`` - ``bundle(app)``, the maker's superset: one
JSON to copy or email, secrets stripped (``strip_secrets``: no key named like
token, cookie, password, secret, psk, login_url, auth_url, credential or
api_key survives, box.yaml's ``update_url`` is dropped, and the Wi-Fi setup
hotspot's password is never read). Keys: ``diagnostics`` (the doc above),
``health``, ``info``, ``status``, ``knobs``, ``lamp_calibration`` (saved,
standard, led), ``software``, ``descriptor`` {name, path, box_variant,
overlay_path, title, audio}, ``backends`` (variant.describe_backends: which
MCP address / ADS the panel opened), ``overlay`` (the calibration file's
controls and io keys in force), ``box`` (box.yaml), ``settings``
(settings.json), ``pwm`` {period_ns, output, backend, calibration}, ``journal``
(the last 200 lines of haslerd and mpd; the unit's SupplementaryGroups has
systemd-journal), ``bootcfg`` (hasler-bootcfg-guard check: exit code and
output, when it is there), ``boot`` {first_sound_s, timeline}, ``stream_events``,
``power_cuts`` (every kept event), ``generated_at``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .. import bootresume
from ..lamp import faults as lamp_faults
from ..lamp import outputs as lamp_outputs

log = logging.getLogger(__name__)

SECRET_KEY = re.compile(r"token|cookie|passw|secret|psk|login_url|auth_url|credential|api_key", re.I)
BUNDLE_DROP = ("update_url",)           # box.yaml: the bench's private channel
JOURNAL_LINES = 200
JOURNAL_UNITS = ("haslerd", "mpd")
BOOTCFG_GUARD = Path("/usr/local/sbin/hasler-bootcfg-guard")
DSP_CACHE_S = 10.0                      # hasler-dsp status is a subprocess: not on every poll
HOT_C = lamp_faults.HOT_C
WARM_C, VERY_WARM_C = 60.0, 70.0
GOOD_SIGNAL, FAIR_SIGNAL = 70, 45
PROC_UPTIME = Path("/proc/uptime")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    at = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))
    return at[:-2] + ":" + at[-2:] if len(at) > 5 and at[-5] in "+-" else at


def _size(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB".replace(".0 GB", " GB")
    return f"{max(0, n) // 1024 ** 2} MB"


def _span(seconds: float | None) -> str:
    if seconds is None:
        return "not known"
    s = int(seconds)
    if s >= 86400:
        return f"{s // 86400} d {s % 86400 // 3600} h"
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60} min"
    return f"{max(1, round(s / 60))} min"


# -- the pieces (each never raises) --------------------------------------------


def wifi_doc(wifi: Any, hotspot: bool | None = None) -> dict:
    """The Wi-Fi name, its signal (0-100, from the scan the radio already has;
    None in setup mode or off a Pi) and the words for the card."""
    ssid, signal = "", None
    try:
        ssid = str(wifi.current_ssid() or "")
    except Exception:
        log.debug("diagnostics: no Wi-Fi name", exc_info=True)
    if hotspot is None:
        try:
            hotspot = bool(wifi._hotspot())
        except Exception:
            hotspot = False
    if ssid and not hotspot:
        fn = getattr(wifi, "signal_of", None)
        try:
            signal = fn(ssid) if callable(fn) else None
        except Exception:
            signal = None
    if hotspot:
        words = "Setup mode: not on your Wi-Fi"
    elif not ssid:
        words = "Not connected"
    elif signal is None:
        words = f"On {ssid}"
    else:
        strength = "good signal" if signal >= GOOD_SIGNAL else "fair signal" if signal >= FAIR_SIGNAL else "weak signal"
        words = f"On {ssid}, {strength}"
    return {"ssid": ssid, "signal": signal, "hotspot": bool(hotspot), "words": words}


def temperature_doc(temp_file: Path | None = None, throttled_file: Path | None = None) -> dict:
    c = lamp_faults.cpu_temp_c(temp_file or lamp_faults.CPU_TEMP)
    thr = lamp_faults.throttled(throttled_file or lamp_faults.THROTTLED_FILE)
    hot = c is not None and c >= HOT_C
    if c is None:
        words = "not known"
    else:
        word = "too hot" if hot else "hot" if c >= VERY_WARM_C else "warm" if c >= WARM_C else "cool"
        words = f"{word} ({round(c)} °C)"
    if thr:
        if thr["under_voltage_now"]:
            words += " · the power supply is sagging now"
        elif thr["throttled_now"] or thr["freq_capped_now"] or thr["soft_temp_limit_now"]:
            words += " · slowed down by the heat now"
        elif thr["under_voltage_seen"]:
            words += " · the power supply sagged once since switch-on"
        elif thr["any_seen"]:
            words += " · was slowed by heat once since switch-on"
    return {"c": c, "hot": hot, "throttled": thr, "words": words}


def storage_doc(state_dir: Path | None) -> dict:
    free = total = None
    read_only = False
    if state_dir is not None:
        try:
            st = os.statvfs(state_dir)
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            read_only = bool(st.f_flag & getattr(os, "ST_RDONLY", 1))
        except OSError:
            log.debug("diagnostics: statvfs failed", exc_info=True)
    bad = lamp_faults.storage_bad(state_dir)
    if read_only:
        words = "read-only: needs a maker"
    elif bad:
        words = f"nearly full ({_size(free)} free): needs a maker"
    elif free is None:
        words = "not known"
    else:
        words = f"{_size(free)} free of {_size(total)}"
    return {"free_bytes": free, "total_bytes": total, "read_only": read_only, "bad": bool(bad), "words": words}


SHUTDOWN_WORDS = {"clean": "switched off properly", "power-cut": "the power was cut",
                  "restart": "the radio's software restarted", "unknown": "not known"}


def shutdown_doc(kind: Any) -> dict:
    kind = kind if kind in SHUTDOWN_WORDS else "unknown"
    return {"kind": kind, "words": SHUTDOWN_WORDS[kind]}


def running_doc(daemon_uptime_s: float | None, uptime_file: Path = PROC_UPTIME, now: float | None = None) -> dict:
    """The system's uptime (/proc/uptime), the daemon's (health.py), and since when."""
    now = time.time() if now is None else now
    try:
        up: float | None = float(uptime_file.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        up = daemon_uptime_s
    return {"uptime_s": None if up is None else int(up), "daemon_uptime_s": daemon_uptime_s,
            "since": _iso(now - up) if up is not None else None, "words": _span(up)}


def dsp_status(command: str | None, run: Callable[..., Any] = subprocess.run) -> dict:
    """``hasler-dsp status`` -> {"installed", "running", "clipped_samples", "error"}.
    Not part of /api/health's ``ok``: an OTA trial must not roll back over the DSP."""
    if not command or not Path(command).exists():
        return {"installed": False, "running": None, "clipped_samples": None}
    try:
        out = run([command, "status"], capture_output=True, text=True, timeout=3)
        doc = json.loads((out.stdout or "").strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return {"installed": True, "running": None, "clipped_samples": None, "error": str(exc)[:200]}
    live = doc.get("live") if isinstance(doc, dict) and isinstance(doc.get("live"), dict) else {}
    clipped = live.get("clipped_samples")
    return {"installed": True, "running": bool(live.get("running")),
            "clipped_samples": clipped if isinstance(clipped, int) else None,
            **({"error": str(live["error"])[:200]} if live.get("error") else {})}


def _cached_dsp(app: Any, run: Callable[..., Any]) -> dict:
    now = time.monotonic()
    cache = getattr(app, "_dsp_cache", None)
    if cache and now - cache[0] < DSP_CACHE_S:
        return cache[1]
    command = getattr(getattr(getattr(app, "actions", None), "player", None), "config", None)
    doc = dsp_status(getattr(command, "dsp_command", None), run)
    try:
        app._dsp_cache = (now, doc)
    except AttributeError:
        pass
    return doc


def player_doc(health: dict, dsp: dict) -> dict:
    checks = health.get("checks") or {}
    mpd_doc = health.get("mpd") or {}
    bits = ["player answering" if checks.get("mpd") else "player not answering",
            "sound card present" if checks.get("audio_card") else "no sound card"]
    if dsp.get("installed"):
        bits.append("tone DSP live" if dsp.get("running") else "tone DSP not running")
    if health.get("recovering"):
        bits.append("bringing the station back")
    return {"mpd": bool(checks.get("mpd")), "audio_card": bool(checks.get("audio_card")),
            "recovering": bool(health.get("recovering")),
            "restarts_last_hour": int(mpd_doc.get("restarts_last_hour") or 0) if isinstance(mpd_doc, dict) else 0,
            "dsp": dsp, "words": " · ".join(bits)}


def lamp_doc(lamp: Any) -> dict:
    present = lamp is not None
    return {"present": present,
            "words": ("the lamp's engine is running; Show me on the Pilot lamp page is the real test"
                      if present else "no lamp on this radio")}


def software_doc(software: Any) -> dict | None:
    try:
        st = software.status()
    except Exception:           # NotInstalled on a bench box, or unreadable
        return None
    return {k: st.get(k) for k in ("version", "last_check", "stale", "last_result", "os_updates", "metered")}


# -- the owner's document ----------------------------------------------------------


def owner_doc(app: Any, run: Callable[..., Any] = subprocess.run, now: float | None = None) -> dict:
    """The /api/diagnostics document for ``app`` (a WebApp, or anything shaped like one)."""
    from .. import health

    now = time.time() if now is None else now
    report = health.health_report(app)
    conn = getattr(app, "connectivity", None)
    keeper = getattr(app, "lamp_keeper", None)
    settings_file = getattr(app, "settings_file", None)
    state_dir = Path(settings_file).parent if settings_file is not None else None
    hotspot = bool(getattr(conn, "hotspot", False)) if conn is not None else None
    cleared = getattr(keeper, "fault_cleared_at", None)
    return {
        "wifi": wifi_doc(getattr(app, "wifi", None), hotspot),
        "online": bool(getattr(conn, "online", False)) if conn is not None else None,
        "temperature": temperature_doc(getattr(app, "cpu_temp_file", None), getattr(app, "throttled_file", None)),
        "storage": storage_doc(state_dir),
        "last_shutdown": shutdown_doc(getattr(app, "shutdown_kind", None)),
        "power_cuts": bootresume.power_cuts(state_dir, now),
        "running": running_doc(report.get("uptime_s"), getattr(app, "uptime_file", None) or PROC_UPTIME, now),
        "player": player_doc(report, _cached_dsp(app, run)),
        "lamp": lamp_doc(getattr(app, "lamp", None)),
        "panel": (report.get("checks") or {}).get("panel"),
        "faults": list(getattr(keeper, "fault_reasons", None) or []),
        "fault_cleared_at": _iso(cleared) if isinstance(cleared, (int, float)) else None,
        "software": software_doc(getattr(app, "software", None)),
        "variant": getattr(getattr(app, "actions", None), "variant", None) and app.actions.variant.name,
        "version": report.get("version"),
        "at": _iso(now),
    }


# -- the maker's bundle ---------------------------------------------------------------


def strip_secrets(doc: Any) -> Any:
    """``doc`` without any key that looks like a secret (SECRET_KEY), at any depth."""
    if isinstance(doc, dict):
        return {k: strip_secrets(v) for k, v in doc.items() if not (isinstance(k, str) and SECRET_KEY.search(k))}
    if isinstance(doc, list):
        return [strip_secrets(v) for v in doc]
    return doc


def _run_text(argv: list[str], timeout: float, run: Callable[..., Any]) -> dict:
    """{"exit": code, "text": stdout+stderr (tail)} or {"error": why}."""
    try:
        out = run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": str(exc)[:200]}
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    return {"exit": int(out.returncode), "text": text[-20000:]}


def journal_tail(run: Callable[..., Any] = subprocess.run, lines: int = JOURNAL_LINES) -> dict:
    argv = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(lines)]
    for unit in JOURNAL_UNITS:
        argv += ["-u", unit]
    return _run_text(argv, 8.0, run)


def bootcfg_check(run: Callable[..., Any] = subprocess.run, guard: Path = BOOTCFG_GUARD) -> dict | None:
    """``hasler-bootcfg-guard check`` as pi (its checks are read-only); None without the script."""
    if not os.access(guard, os.X_OK):
        return None
    return _run_text([str(guard), "check"], 10.0, run)


def boot_doc(app: Any, run: Callable[..., Any] = subprocess.run) -> dict:
    first = getattr(getattr(app, "first_sound", None), "at", None)
    return {"first_sound_s": round(first, 1) if isinstance(first, (int, float)) else None,
            "timeline": _run_text(["systemd-analyze", "time"], 5.0, run)}


def pwm_doc(app: Any, period_file: Path | None = None) -> dict:
    lamp = getattr(app, "lamp", None)
    spec = (getattr(getattr(app, "actions", None), "variant", None) and app.actions.variant.io.get("lamp")) or {}
    try:
        period: int | None = int((period_file or lamp_outputs.PERIOD_FILE).read_text().strip())
    except (OSError, ValueError):
        period = None
    cal = getattr(lamp, "calibration", None)
    try:
        calibration = cal() if callable(cal) else None
    except Exception:
        calibration = None
    output = getattr(lamp, "output", None)
    return {"period_ns": period, "backend": spec.get("backend"),
            "output": type(output).__name__ if output is not None else None,
            "fallback": output is not None and type(output).__name__ == "GpiozeroOutput"
            and spec.get("backend", "pwm-sysfs") == "pwm-sysfs",
            "calibration": calibration}


def _safe(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        log.debug("diagnostics bundle part failed", exc_info=True)
        return {"error": str(exc)[:200]}


def bundle(app: Any, run: Callable[..., Any] = subprocess.run) -> dict:
    """The support bundle (module docstring); every part is best effort."""
    from .. import boxconfig, health
    from .server import load_settings

    variant = getattr(getattr(app, "actions", None), "variant", None)
    paths = getattr(app, "box_paths", None)
    box = _safe(lambda: boxconfig.read_box_file(paths)) if paths is not None else {}
    if isinstance(box, dict):
        box = {k: v for k, v in box.items() if k not in BUNDLE_DROP}
    report = _safe(lambda: health.health_report(app))
    lamp_cal = _safe(app.lamp_calibration) if callable(getattr(app, "lamp_calibration", None)) else None
    if isinstance(lamp_cal, dict):
        lamp_cal = {k: v for k, v in lamp_cal.items() if k != "looks"}
    doc = {
        "generated_at": _iso(time.time()),
        "diagnostics": _safe(lambda: owner_doc(app, run)),
        "health": report,
        "info": _safe(app.info) if callable(getattr(app, "info", None)) else None,
        "status": _safe(app.status) if callable(getattr(app, "status", None)) else None,
        "knobs": _safe(app.knobs) if callable(getattr(app, "knobs", None)) else None,
        "lamp_calibration": lamp_cal,
        "software": _safe(app.software.status) if getattr(app, "software", None) is not None else None,
        "descriptor": {
            "name": getattr(variant, "name", None),
            "path": str(variant.path) if getattr(variant, "path", None) else None,
            "box_variant": box.get("variant") if isinstance(box, dict) else None,
            "overlay_path": str(variant.overlay_path) if getattr(variant, "overlay_path", None) else None,
            "title": getattr(variant, "title", None),
            "audio": dict(getattr(variant, "audio", None) or {}),
        },
        "backends": getattr(getattr(app, "panel", None), "backends", None),
        "overlay": _safe(app._overlay) if callable(getattr(app, "_overlay", None)) else None,
        "box": box,
        "settings": _safe(lambda: load_settings(getattr(app, "settings_file", None))),
        "pwm": _safe(lambda: pwm_doc(app)),
        "journal": journal_tail(run),
        "bootcfg": bootcfg_check(run),
        "boot": boot_doc(app, run),
        "stream_events": report.get("stream_events_recent") if isinstance(report, dict) else None,
        "power_cuts": _safe(lambda: bootresume._read_power_cuts(Path(app.settings_file).parent))
        if getattr(app, "settings_file", None) is not None else None,
    }
    return strip_secrets(doc)
