"""The health check after a switch (docs/ota.md §4 steps 6 and 8).

A baseline is probed on the *old* release first, so only regressions count: a
box that never had MPD answering, a moOde UI or go-librespot running does not
fail on them. After the restart the new release must pass within 90 s and
keep passing for 60 s:

* haslerd ``active (running)`` (Type=notify: READY=1 seen) and no restarts;
* ``/api/health`` 200 reporting the new version, no check worse than baseline;
* MPD answering on 6600, the sound cards of the baseline present;
* go-librespot and the moOde UI as they were.

MPD hanging is not haslerd's doing (2026-10-25: MPD 0.24's ffmpeg decoder
stuck on an HLS stream; the innocent 1.1.3 was rolled back). A sample whose
only problems are MPD's, while haslerd itself is fine and its /api/health
says it is bringing MPD back (``mpd.recovering``), is neither a pass nor a
failure: the wait goes on (the hold starts again once MPD answers), for at
most ``MPD_GRACE_S`` in all. A release that keeps MPD down longer than that,
or whose haslerd doesn't see it, fails as before.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .layout import HEALTH_URL

APPEAR_S = 90.0
BOOT_APPEAR_S = 300.0   # a trial judged after a reboot: moOde starts MPD late (bench: >90 s)
HOLD_S = 60.0
INTERVAL_S = 5.0
MPD_GRACE_S = 120.0     # MPD being restarted by haslerd/systemd: this long at most, per judgement


def _good(key: str, value) -> bool:
    if key == "panel":
        return value in ("ok", "allowed_absent")
    return bool(value)


class HealthChecker:
    def __init__(
        self,
        cards_file: Path = Path("/proc/asound/cards"),
        health_url: str = HEALTH_URL,
        run: Callable = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        appear_s: float = APPEAR_S,
        hold_s: float = HOLD_S,
        interval_s: float = INTERVAL_S,
    ) -> None:
        self.cards_file = cards_file
        self.health_url = health_url
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self.appear_s = appear_s
        self.hold_s = hold_s
        self.interval_s = interval_s

    # --- probes ---------------------------------------------------------------

    def unit(self, name: str) -> dict:
        try:
            out = self.run(["systemctl", "show", name, "-p", "ActiveState,SubState,NRestarts,MainPID"],
                           capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def api(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.health_url, timeout=5) as r:
                if r.status != 200:
                    return None
                return json.loads(r.read(1 << 16))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    @staticmethod
    def mpd(host: str = "127.0.0.1", port: int = 6600) -> bool:
        try:
            with socket.create_connection((host, port), timeout=3) as s:
                return s.recv(64).startswith(b"OK MPD")
        except OSError:
            return False

    def cards(self) -> list[str]:
        try:
            text = self.cards_file.read_text()
        except OSError:
            return []
        out = []
        for line in text.splitlines():
            if "[" in line and "]" in line and line[:3].strip().isdigit():
                out.append(line.split("[", 1)[1].split("]", 1)[0].strip())
        return sorted(out)

    @staticmethod
    def http_ok(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def probe(self) -> dict:
        d = self.unit("haslerd.service")
        try:
            restarts = int(d.get("NRestarts", "0") or 0)
        except ValueError:
            restarts = 0
        return {
            "haslerd": d.get("ActiveState") == "active" and d.get("SubState") == "running",
            "restarts": restarts,
            "api": self.api(),
            "mpd": self.mpd(),
            "cards": self.cards(),
            "librespot": self.unit("go-librespot.service").get("ActiveState") == "active",
            "moode_ui": self.http_ok("http://127.0.0.1/"),
        }

    def baseline(self) -> dict:
        return self.probe()

    # --- judging ------------------------------------------------------------------

    @staticmethod
    def problems(sample: dict, baseline: dict, version: str, restarts0: int) -> list[str]:
        out = []
        if not sample.get("haslerd"):
            out.append("haslerd not active")
        if sample.get("restarts", 0) > restarts0:
            out.append("haslerd restarted")
        api = sample.get("api")
        base_api = baseline.get("api") or {}
        if not isinstance(api, dict):
            out.append("/api/health not answering")
        else:
            if str(api.get("version")) != version:
                out.append(f"/api/health reports {api.get('version')}, not {version}")
            checks = api.get("checks") or {}
            base_checks = base_api.get("checks") or {}
            for key, value in checks.items():
                # With a baseline: only what was good must stay good. Without
                # one (the old release had no /api/health): everything must be.
                required = _good(key, base_checks[key]) if key in base_checks else not base_api
                if required and not _good(key, value):
                    out.append(f"check {key} failed ({value})")
            if base_api.get("ok") and not api.get("ok"):
                out.append("/api/health ok=false")
        for key in ("mpd", "librespot", "moode_ui"):
            if baseline.get(key) and not sample.get(key):
                out.append(f"{key} lost")
        missing = set(baseline.get("cards") or []) - set(sample.get("cards") or [])
        if missing:
            out.append(f"sound card missing: {', '.join(sorted(missing))}")
        return out

    @staticmethod
    def mpd_recovery_only(sample: dict, problems: list[str]) -> bool:
        """Every problem is MPD's, haslerd itself is fine and says it is bringing MPD back."""
        api = sample.get("api")
        if not problems or not sample.get("haslerd") or not isinstance(api, dict):
            return False
        if not (api.get("mpd") or {}).get("recovering"):
            return False
        mpd_only = ("check mpd failed", "/api/health ok=false", "mpd lost")
        if not all(p.startswith(mpd_only) for p in problems):
            return False
        # ok=false must be MPD's alone: every other check as good as can be.
        others = {k: v for k, v in (api.get("checks") or {}).items() if k != "mpd"}
        return "/api/health ok=false" not in problems or all(_good(k, v) for k, v in others.items())

    def wait(self, version: str, baseline: dict) -> tuple[bool, str]:
        """Pass within ``appear_s``, then hold for ``hold_s``. (ok, reason)."""
        start = self.clock()
        restarts0: int | None = None
        passed_at: float | None = None
        last: list[str] = ["no sample"]
        grace = 0.0                     # spent waiting out an MPD recovery
        grace_at: float | None = None
        while True:
            sample = self.probe()
            if restarts0 is None and sample.get("haslerd"):
                restarts0 = sample.get("restarts", 0)
            last = self.problems(sample, baseline, version, restarts0 if restarts0 is not None else 1 << 30)
            now = self.clock()
            if last and self.mpd_recovery_only(sample, last):
                if grace_at is not None:
                    grace += now - grace_at
                grace_at = now
                if grace < MPD_GRACE_S:
                    passed_at = None    # MPD back first, then the whole hold again
                    self.sleep(self.interval_s)
                    continue
                return False, "MPD not back within %ds: %s" % (MPD_GRACE_S, "; ".join(last))
            else:
                grace_at = None
            if last:
                if passed_at is not None:
                    return False, "health regressed during hold: " + "; ".join(last)
                if now - start >= self.appear_s + grace:
                    return False, "not healthy within %ds: %s" % (self.appear_s, "; ".join(last))
            else:
                if passed_at is None:
                    passed_at = now
                if now - passed_at >= self.hold_s:
                    return True, "healthy"
            self.sleep(self.interval_s)


def idle_now(health_url: str = HEALTH_URL) -> tuple[bool, str]:
    """haslerd's veto: idle=true in /api/health (standby, nothing for 10 min)."""
    try:
        with urllib.request.urlopen(health_url, timeout=5) as r:
            doc = json.loads(r.read(1 << 16))
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"haslerd health not answering ({e})"
    if doc.get("idle") is True:
        return True, "idle"
    return False, "radio in use (playing=%s, standby=%s)" % (doc.get("playing"), doc.get("standby"))
