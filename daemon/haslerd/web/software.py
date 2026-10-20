"""The key page's "About this radio" card: version, updates, going back.

docs/ota.md sections 4-5. haslerd never writes the code it runs: it only
reads what the updater left (``/var/lib/hasler-update/status.json`` from the
unprivileged check, ``/var/lib/hasler-apply/state.json`` from the root apply,
``/opt/hasler/releases``) and starts the updater's units with ``systemctl
start --no-block``, which a polkit rule allows for exactly these names
(config/update/50-hasler-update.rules):

    hasler-update-check.service                 "Check now"
    hasler-update-apply@<seq>.service           install a staged release
    hasler-update-apply@rollback-X.Y.Z.service  "Go back to X.Y.Z"

Paths sit under ``HASLER_ROOT`` like the updater's (tests point it at a tmp
dir). ``box.yaml``'s ``update_mode`` and ``channel`` are written through
boxconfig.

"Install now" and "Install tonight" are an approval kept in haslerd's state
dir (``update-approved.json``); ``tick`` starts the apply unit once the radio
is idle by the updater's own rule (/api/health ``idle``: standby, nothing
played for 10 min) - "now" at the first such moment, "tonight" inside the
02:00-05:00 window. The apply re-checks everything itself.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

# Two update channels: bench (the makers' test radio) and stable (everyone).
CHANNELS = ("bench", "stable")
RETIRED_CHANNELS = ("beta",)      # read as stable (the updater does the same)
MODES = ("auto", "ask", "off")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?$")
# The same names the polkit rule allows (plus the check), checked again here.
UNIT = re.compile(r"^(hasler-update-check\.service|hasler-update-apply@([0-9]{1,9}|rollback|"
                  r"rollback-[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?)\.service)$")
CHECK_UNIT = "hasler-update-check.service"
COMPLETE = ".complete"
STALE_S = 3 * 24 * 3600          # no successful check for this long: "no fresh update info"
WINDOW = (2, 5)                   # the updater's night window, local hours
APPROVAL_FILE = "update-approved.json"
TICK_S = 30.0


class SoftwareError(RuntimeError):
    """The updater can't do that right now (systemd refused, not downloaded) - 503."""


class NotInstalled(Exception):
    """The updater isn't on this radio (an rsync-installed bench box) - 404."""


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_iso(s: Any) -> float | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        s = s.strip().rstrip("Z").split(".")[0].split("+")[0]
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _version_key(v: str) -> tuple:
    core, _, pre = v.partition("-")
    nums = tuple(int(x) if x.isdigit() else 0 for x in core.split("."))
    return nums + ((1,) if not pre else (0, pre))


def systemctl_start(unit: str) -> None:
    """``systemctl start --no-block <unit>``; raises RuntimeError with systemd's words."""
    proc = subprocess.run(["systemctl", "start", "--no-block", unit],
                          capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip())


class Software:
    """Reads the updater's files; starts its units; keeps the install approval."""

    def __init__(
        self,
        root: Path | None = None,
        box_paths: Any = None,
        state_dir: Path | None = None,
        systemctl: Callable[[str], None] = systemctl_start,
        idle: Callable[[], bool] | None = None,
        wall: Callable[[], float] = time.time,
        local_hour: Callable[[], int] | None = None,
        fallback_version: Callable[[], str] | None = None,
    ) -> None:
        self.root = Path(root if root is not None else (os.environ.get("HASLER_ROOT") or "/"))
        self.box_paths = box_paths
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.systemctl = systemctl
        self.idle = idle
        self.wall = wall
        self.local_hour = local_hour or (lambda: time.localtime(self.wall()).tm_hour)
        self.fallback_version = fallback_version
        self._lock = threading.Lock()
        self._next_tick = 0.0
        self._channel_warned = False   # a retired channel in box.yaml is logged once
        self._ticking: threading.Thread | None = None

    # -- paths ---------------------------------------------------------------

    def _p(self, rel: str) -> Path:
        return self.root / rel.lstrip("/")

    @property
    def releases_dir(self) -> Path:
        return self._p("/opt/hasler/releases")

    @property
    def status_file(self) -> Path:
        return self._p("/var/lib/hasler-update/status.json")

    @property
    def state_file(self) -> Path:
        return self._p("/var/lib/hasler-apply/state.json")

    @property
    def staged_dir(self) -> Path:
        return self._p("/var/lib/hasler-update/staged")

    def _link(self, name: str) -> Path | None:
        try:
            return self._p(f"/opt/hasler/{name}").resolve(strict=True)
        except OSError:
            return None

    @property
    def installed(self) -> bool:
        return any(p.exists() for p in (self.releases_dir, self.status_file, self.state_file))

    # -- box.yaml --------------------------------------------------------------

    def _box(self) -> dict:
        if self.box_paths is None:
            return {}
        try:
            from .. import boxconfig

            return boxconfig.read_box_file(self.box_paths)
        except Exception:
            log.debug("could not read box.yaml", exc_info=True)
            return {}

    def box_settings(self) -> dict:
        box = self._box()
        # No update_mode yet = a new radio: auto (small releases at night in
        # standby; major/migration releases still ask) - same as the updater.
        mode = box.get("update_mode", "auto")
        # A hand-written ``update_mode: off`` (or ``no``) is YAML 1.1's False to
        # PyYAML; the updater reads the word itself, and so must we.
        mode = "off" if mode is False else str(mode).lower()
        channel = str(box.get("channel", "stable")).lower()
        if channel not in CHANNELS:
            if not self._channel_warned:
                self._channel_warned = True
                why = "was retired" if channel in RETIRED_CHANNELS else "is not a channel"
                log.warning("box.yaml channel %r %s: treating it as stable", channel, why)
            channel = "stable"
        return {"update_mode": mode if mode in MODES else "ask", "channel": channel}

    # -- the status document -----------------------------------------------------

    def status(self) -> dict:
        if not self.installed:
            raise NotInstalled("updates are not installed on this radio yet")
        check = _read_json(self.status_file)
        check = check if isinstance(check, dict) else {}
        state = _read_json(self.state_file)
        state = state if isinstance(state, dict) else {}
        releases = self.releases(state)
        running = next((r for r in releases if r["status"] == "running"), None)
        version = running["version"] if running else self._fallback()
        now = self.wall()
        checked = _parse_iso(check.get("checked"))
        expires = _parse_iso(check.get("index_expires"))
        if check.get("result") == "ok":
            stale = (checked is None or now - checked > STALE_S) or (expires is not None and expires < now)
        else:
            stale = True
        box = self.box_settings()
        return {
            "version": version,
            "channel": box["channel"],
            "update_mode": box["update_mode"],
            "last_check": check.get("checked"),
            "check_error": check.get("message") if check.get("result") == "error" else None,
            "available": self._available(check, state, version),
            "last_result": self._last_result(state),
            "releases": releases,
            "stale": bool(stale),
            "approved": self._approval(),
            "busy": state.get("phase") == "trial",
        }

    def _fallback(self) -> str:
        if self.fallback_version is not None:
            try:
                return str(self.fallback_version())
            except Exception:
                pass
        from .. import __version__

        return __version__

    def releases(self, state: dict | None = None) -> list[dict]:
        state = state if state is not None else (_read_json(self.state_file) or {})
        bad = {int(s) for s in (state.get("bad") or []) if isinstance(s, int) or str(s).isdigit()}
        current, previous = self._link("current"), self._link("previous")
        out = []
        if self.releases_dir.is_dir():
            for d in self.releases_dir.iterdir():
                if not d.is_dir() or not (d / COMPLETE).exists() or not VERSION.match(d.name):
                    continue
                rel = _read_json(d / "release.json") or {}
                seq = rel.get("seq")
                try:
                    status = ("running" if d.resolve() == current else
                              "bad" if isinstance(seq, int) and seq in bad else
                              "previous" if d.resolve() == previous else "kept")
                    at = _iso((d / COMPLETE).stat().st_mtime)
                except OSError:
                    continue
                out.append({"version": str(rel.get("version") or d.name), "status": status, "installed_at": at})
        out.sort(key=lambda r: _version_key(r["version"]), reverse=True)
        return out

    def _available(self, check: dict, state: dict, version: str) -> dict | None:
        avail = check.get("available")
        if not isinstance(avail, dict) or not avail.get("version"):
            return None
        seq = avail.get("seq")
        if str(avail["version"]) == version or (isinstance(seq, int) and seq in (state.get("bad") or [])):
            return None
        staged = isinstance(seq, int) and (self.staged_dir / str(seq) / "STAGED").exists()
        return {"version": str(avail["version"]), "seq": seq if isinstance(seq, int) else None,
                "notes": str(avail.get("notes") or ""), "kind": avail.get("kind"),
                "requires_migration": bool(avail.get("requires_migration")), "staged": staged}

    @staticmethod
    def _last_result(state: dict) -> dict | None:
        phase = state.get("phase")
        history = [h for h in (state.get("history") or []) if isinstance(h, dict)]

        def last(event: str) -> str | None:
            return next((h.get("at") for h in reversed(history) if h.get("event") == event), None)

        if phase == "rolled_back":
            return {"ok": False, "version": state.get("failed") or state.get("to"),
                    "back_to": state.get("from"), "reason": state.get("reason") or "",
                    "at": last("rollback")}
        if phase in ("committed", "probation"):
            return {"ok": True, "version": state.get("to"), "reason": None, "at": last("commit"),
                    "manual": bool(state.get("manual")), "probation": phase == "probation"}
        if phase == "trial":
            return {"ok": None, "version": state.get("to"), "reason": "installing now", "at": last("switch")}
        return None

    # -- actions --------------------------------------------------------------------

    def _start(self, unit: str) -> None:
        if not UNIT.match(unit):
            raise ValueError(f"not an updater unit: {unit}")
        if not self.installed:
            raise NotInstalled("updates are not installed on this radio yet")
        log.info("starting %s from the key page", unit)
        try:
            self.systemctl(unit)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise SoftwareError(f"systemd would not start {unit}: {exc}") from exc

    def check(self) -> None:
        self._start(CHECK_UNIT)

    def install(self, version: str, when: str = "now") -> dict:
        """Approve the available release; ``tick`` starts it once the radio rests."""
        if when not in ("now", "tonight"):
            raise ValueError("when must be \"now\" or \"tonight\"")
        st = self.status()
        avail = st["available"]
        if not avail or avail["version"] != version:
            raise ValueError(f"{version} is not the update on offer")
        if avail["seq"] is None:
            raise ValueError("the update has no sequence number")
        if not avail["staged"]:
            raise SoftwareError(f"{version} hasn't been downloaded yet - try Check now"
                               + (" (updates are off)" if st["update_mode"] == "off" else ""))
        approval = {"version": version, "seq": avail["seq"], "when": when, "at": _iso(self.wall())}
        self._write_approval(approval)
        log.info("update to %s approved (%s) from the key page", version, when)
        self._next_tick = 0.0
        self.tick(force=True)
        return self._approval() or approval

    def cancel_install(self) -> None:
        self._write_approval(None)

    def rollback(self, to: str) -> None:
        if not isinstance(to, str) or not VERSION.match(to):
            raise ValueError("to must be a version like 1.2.0")
        kept = {r["version"]: r for r in self.releases()}
        if to not in kept:
            raise ValueError(f"{to} is not kept on this radio")
        if kept[to]["status"] == "running":
            raise ValueError(f"{to} is already running")
        self._start(f"hasler-update-apply@rollback-{to}.service")

    @staticmethod
    def check_settings(body: dict) -> None:
        """ValueError unless ``body`` is a valid ``put_settings`` request."""
        mode, channel = body.get("update_mode"), body.get("channel")
        if mode is not None and mode not in MODES:
            raise ValueError("update_mode must be auto, ask or off")
        if channel is not None and channel not in CHANNELS:
            raise ValueError("channel must be bench or stable")
        if mode is None and channel is None:
            raise ValueError("expected {\"update_mode\"} and/or {\"channel\"}")

    def put_settings(self, body: dict) -> dict:
        """``update_mode`` / ``channel`` into box.yaml (channel is confirm-gated by the caller)."""
        self.check_settings(body)
        mode, channel = body.get("update_mode"), body.get("channel")
        if self.box_paths is None:
            raise SoftwareError("this radio has no box.yaml to save that in")
        from .. import boxconfig

        box = boxconfig.read_box_file(self.box_paths)
        if mode is not None:
            box["update_mode"] = mode
        if channel is not None:
            box["channel"] = channel
        elif str(box.get("channel", "")).lower() in RETIRED_CHANNELS:
            box["channel"] = "stable"        # what it already read as; say so in the file
        try:
            boxconfig.write_box_file(self.box_paths, box)
        except PermissionError as exc:
            raise SoftwareError(f"can't write {self.box_paths.box_file}: {exc.strerror}") from exc
        log.info("updates set to %s from the key page", {k: v for k, v in body.items()
                                                         if k in ("update_mode", "channel")})
        return self.box_settings()

    # -- the approval and its tick ---------------------------------------------------

    def _approval_path(self) -> Path | None:
        return self.state_dir / APPROVAL_FILE if self.state_dir is not None else None

    def _approval(self) -> dict | None:
        path = self._approval_path()
        doc = _read_json(path) if path is not None else getattr(self, "_mem_approval", None)
        return doc if isinstance(doc, dict) and doc.get("seq") else None

    def _write_approval(self, doc: dict | None) -> None:
        path = self._approval_path()
        if path is None:
            self._mem_approval = doc
            return
        if doc is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(doc) + "\n")
        os.replace(tmp, path)

    def tick(self, now: float | None = None, force: bool = False) -> None:
        """From the main loop: start an approved install once the radio rests.

        Cheap when nothing is approved; the idle check (MPD, health) runs on
        a short thread so the panel loop never waits for it.
        """
        now = time.monotonic() if now is None else now
        if not force and now < self._next_tick:
            return
        self._next_tick = now + TICK_S
        if self._approval() is None:
            return
        if self._ticking is not None and self._ticking.is_alive():
            return
        self._ticking = threading.Thread(target=self._run_approved, name="software-tick", daemon=True)
        self._ticking.start()

    def _run_approved(self) -> None:
        with self._lock:
            approval = self._approval()
            if approval is None:
                return
            if approval.get("when") == "tonight" and not WINDOW[0] <= self.local_hour() < WINDOW[1]:
                return
            try:
                idle = bool(self.idle()) if self.idle is not None else False
            except Exception:
                log.debug("idle check failed", exc_info=True)
                idle = False
            if not idle:
                return
            # Cleared first: the unit restarts haslerd, and a deferred apply
            # must not come round again and again.
            self._write_approval(None)
            try:
                self._start(f"hasler-update-apply@{int(approval['seq'])}.service")
            except Exception:
                log.exception("could not start the approved update to %s", approval.get("version"))

    def join(self, timeout: float = 2.0) -> None:
        """Tests: wait for a tick's thread."""
        if self._ticking is not None:
            self._ticking.join(timeout)
