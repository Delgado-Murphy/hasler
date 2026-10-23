"""haslerd's side of the yt-dlp lane (docs/ota.md "The component lane (yt-dlp)").

scripts/hasler-ytdlp-update, run as the ytdlp user by yt-dlp-update.service,
does the work: signed candidates, a spare venv slot, the canary, the switch.
haslerd never writes /opt/yt-dlp. It

* **reads** the lane's ``state.json`` and ``events.jsonl`` for ``/api/health``
  (``components: {"yt-dlp": ...}``) and the key page's Music services block;
* **runs the canary** for it (``POST /api/components/yt-dlp/canary``, loopback
  only): the slot's yt-dlp resolving a YouTube Music track exactly as a key
  press would - haslerd's user, the owner's cookies, deno on haslerd's PATH,
  ``YtmSource.canary`` - then a 64 KB Range fetch. The cookies never leave
  this user; the request names a slot (``venv-a|b|c``), never a path;
* **starts the lane** (``systemctl start --no-block yt-dlp-update.service``,
  the one unit polkit lets pi start), saying why in
  ``<state dir>/yt-dlp-request.json`` (``now``, ``urgent``, ``judge``,
  ``rollback``), which the script reads once if fresh:

  - the network back after more than a day without a good check (this also
    covers a boot after days switched off);
  - YouTube Music tracks failing to resolve (``YtmSource.on_resolve``):
    2 different tracks within 10 min -> ``urgent``; during the 72 h probation
    after a switch, 3 different tracks within an hour (or the urgent rule)
    -> ``judge``. At most one of these every 30 min. A track that is simply
    gone (removed, private) and network errors don't count, nor anything
    while the radio is offline.
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
from collections import deque
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ROOT = "/opt/yt-dlp"
UNIT = "yt-dlp-update.service"
REQUEST_FILE = "yt-dlp-request.json"
MODES = ("now", "urgent", "judge", "rollback")
SLOT = re.compile(r"^venv-[abc]$")
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
FALLBACK_ID = "jNQXAC9IVRw"
MIN_BYTES = 65536

STALE_S = 24 * 3600                 # no good check for this long: check when the network is back
NET_RETRY_S = 3600                  # ... at most this often
URGENT_WINDOW_S, URGENT_IDS = 10 * 60, 2
JUDGE_WINDOW_S, JUDGE_IDS = 3600, 3
TRIGGER_GAP_S = 30 * 60             # one urgent/judge run per this
CANARY_PER_DAY = 12                 # haslerd's own cap (the script allows itself 4)
EVENTS_SHOWN = 5
NET_ERR = re.compile(r"Temporary failure in name resolution|Name or service not known|Network is unreachable|"
                     r"No route to host|Connection refused|Connection reset|timed out|getaddrinfo failed", re.I)


def _parse_iso(s: Any) -> float | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        s = s.strip().rstrip("Z").split(".")[0].split("+")[0]
        return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def systemctl_start(unit: str) -> None:
    proc = subprocess.run(["systemctl", "start", "--no-block", unit], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"exit {proc.returncode}").strip())


def request_run(state_dir: Path | None, mode: str, why: str,
                systemctl: Callable[[str], None] = systemctl_start, wall: Callable[[], float] = time.time) -> None:
    """Ask the lane for ``mode`` and start it (``check``: a plain run, no
    request). Raises RuntimeError/OSError."""
    if mode == "check":
        log.info("yt-dlp lane: starting %s (%s)", UNIT, why)
        systemctl(UNIT)
        return
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    if state_dir is None:
        raise RuntimeError("no state directory to leave the request in")
    path = Path(state_dir) / REQUEST_FILE
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps({"mode": mode, "at": _iso(wall()), "why": why[:200]}) + "\n")
    os.chmod(tmp, 0o644)                # the ytdlp user reads it
    os.replace(tmp, path)
    log.info("yt-dlp lane: starting %s (%s: %s)", UNIT, mode, why)
    systemctl(UNIT)


class YtdlpLane:
    """Reads the lane, runs its canary, starts it when YouTube Music needs it."""

    def __init__(
        self,
        root: str | Path | None = None,
        state_dir: Path | None = None,
        ytm: Any = None,
        systemctl: Callable[[str], None] = systemctl_start,
        wall: Callable[[], float] = time.time,
        background: bool = True,
    ) -> None:
        self.root = Path(root or os.environ.get("HASLER_YTDLP_ROOT") or ROOT)
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.ytm = ytm
        self.systemctl = systemctl
        self.wall = wall
        self.background = background
        self._lock = threading.Lock()
        self._canary_lock = threading.Lock()
        self._failures: deque[tuple[float, str]] = deque(maxlen=64)
        self._good_ids: deque[str] = deque(maxlen=8)
        self._canaries: deque[float] = deque(maxlen=CANARY_PER_DAY)
        self._online: bool | None = None
        self._last_net_start = -1e18
        self._last_trigger = -1e18
        self.last_error: str | None = None

    # -- reading the lane ------------------------------------------------------

    @property
    def installed(self) -> bool:
        return self.root.is_dir() and (self.root / "current").exists()

    def state(self) -> dict:
        doc = _read_json(self.root / "state.json")
        return doc if isinstance(doc, dict) else {}

    def _version(self, link: str) -> str | None:
        """A file read, never yt-dlp itself (health must stay quick)."""
        try:
            target = (self.root / link).resolve(strict=True)
        except OSError:
            return None
        for d in sorted(target.glob("lib/python3*/site-packages/yt_dlp-*.dist-info")):
            return d.name[len("yt_dlp-"):-len(".dist-info")]
        for f in sorted(target.glob("lib/python3*/site-packages/yt_dlp/version.py")):
            try:
                m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", f.read_text())
            except OSError:
                m = None
            if m:
                return m.group(1)
        return None

    def events(self, n: int = EVENTS_SHOWN) -> list[dict]:
        try:
            with open(self.root / "events.jsonl", "rb") as f:
                size = f.seek(0, os.SEEK_END)
                f.seek(max(0, size - 16384))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return []
        if size > 16384:
            lines = lines[1:]           # the first one is cut
        out = []
        for line in lines[-n:]:
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            if isinstance(doc, dict):
                out.append(doc)
        return out

    def doc(self) -> dict | None:
        """``components["yt-dlp"]`` of /api/health; None if the lane isn't on this radio."""
        if not self.root.is_dir():
            return None
        st = self.state()
        cur = st.get("current") if isinstance(st.get("current"), dict) else {}
        prev = st.get("previous") if isinstance(st.get("previous"), dict) else {}
        version = self._version("current") or cur.get("version")
        prev_link = self.root / "previous"
        previous = None
        if prev_link.exists() and self._version("previous") != version:
            previous = self._version("previous") or prev.get("version")
        canary = st.get("canary") if isinstance(st.get("canary"), dict) else None
        probation = st.get("probation_until")
        now = self.wall()
        with self._lock:
            recent = len({v for t, v in self._failures if now - t < JUDGE_WINDOW_S})
        return {
            "version": version,
            "previous": previous,
            "channel": cur.get("channel") if cur.get("version") == version or not cur.get("version") else None,
            "last_check": st.get("last_check"),
            "last_ok": st.get("last_ok"),
            "last_result": st.get("last_result"),
            "last_error": st.get("last_error"),
            "canary": canary,
            "probation_until": probation if (_parse_iso(probation) or 0) > now else None,
            "events_recent": self.events(),
            "resolve_failures_recent": recent,
            "working": self._working(st, version, canary, recent),
            "trigger_error": self.last_error,
        }

    @staticmethod
    def _working(st: dict, version: str | None, canary: dict | None, recent: int) -> bool | None:
        """True / False / None (don't know yet) - the key page's one word."""
        if not version:
            return False
        if canary and canary.get("version") == version and canary.get("result") in ("pass", "fail"):
            verdict = canary["result"] == "pass"
            return verdict and recent < URGENT_IDS
        if recent >= URGENT_IDS:
            return False
        return True if st.get("last_result") == "ok" else None

    # -- what YouTube Music tells us --------------------------------------------------

    def resolve_result(self, vid: str, ok: bool, error: str = "") -> None:
        """From YtmSource.on_resolve (a web/prefetch thread). Never raises."""
        try:
            if ok:
                with self._lock:
                    if vid in self._good_ids:
                        self._good_ids.remove(vid)
                    self._good_ids.append(vid)
                return
            if self._online is False or NET_ERR.search(error or ""):
                return
            with self._lock:
                self._failures.append((self.wall(), vid))
            self._maybe_trigger()
        except Exception:
            log.debug("yt-dlp lane: resolve_result failed", exc_info=True)

    def _maybe_trigger(self) -> None:
        now = self.wall()
        with self._lock:
            ids_10m = {v for t, v in self._failures if now - t < URGENT_WINDOW_S}
            ids_1h = {v for t, v in self._failures if now - t < JUDGE_WINDOW_S}
            if now - self._last_trigger < TRIGGER_GAP_S:
                return
            probation = (_parse_iso(self.state().get("probation_until")) or 0) > now
            if probation and (len(ids_1h) >= JUDGE_IDS or len(ids_10m) >= URGENT_IDS):
                mode, n = "judge", max(len(ids_1h), len(ids_10m))
            elif len(ids_10m) >= URGENT_IDS:
                mode, n = "urgent", len(ids_10m)
            else:
                return
            self._last_trigger = now
        self._start(mode, f"{n} YouTube Music tracks failed to resolve" + (" (in probation)" if mode == "judge" else ""))

    def tick(self, online: bool) -> None:
        """From the main loop (cheap): the network coming back after a long time."""
        was, self._online = self._online, bool(online)
        if not online or was:
            return
        now = self.wall()
        if now - self._last_net_start < NET_RETRY_S or not self.root.is_dir():
            return
        st = self.state()
        good = _parse_iso(st.get("last_ok"))
        if good is not None and now - good < STALE_S:
            return
        self._last_net_start = now
        age = "never" if good is None else f"{(now - good) / 3600:.0f} h ago"
        self._start("check", f"network back; last good yt-dlp check {age}")

    def _start(self, mode: str, why: str) -> None:
        def go() -> None:
            try:
                request_run(self.state_dir, mode, why, self.systemctl, self.wall)
                self.last_error = None
            except Exception as exc:     # noqa: BLE001 - shown in health, never fatal
                self.last_error = f"could not start {UNIT}: {exc}"
                log.warning("yt-dlp lane: %s", self.last_error)
        if self.background:
            threading.Thread(target=go, name="ytdlp-lane-start", daemon=True).start()
        else:
            go()

    # -- the canary ----------------------------------------------------------------

    def canary(self, body: Any, client: str | None) -> tuple[int, dict]:
        """``POST /api/components/yt-dlp/canary`` -> (HTTP status, doc)."""
        if (client or "").removeprefix("::ffff:") not in ("127.0.0.1", "::1"):
            return 403, {"ok": False, "error": "loopback only"}
        body = body if isinstance(body, dict) else {}
        slot = body.get("slot")
        if not isinstance(slot, str) or not SLOT.match(slot):
            return 400, {"ok": False, "error": "slot must be venv-a, venv-b or venv-c"}
        exe = self.root / slot / "bin" / "yt-dlp"
        try:
            real = Path(os.path.realpath(exe))
            inside = real.is_relative_to(Path(os.path.realpath(self.root / slot)))
        except (OSError, ValueError):
            inside = False
        if not inside or not real.is_file() or not os.access(real, os.X_OK):
            return 400, {"ok": False, "error": f"no yt-dlp in {slot}"}
        vid = body.get("id")
        if vid is not None and not (isinstance(vid, str) and VIDEO_ID.match(vid)):
            return 400, {"ok": False, "error": "id must be a YouTube video id"}
        if vid is None:
            with self._lock:
                vid = self._good_ids[-1] if self._good_ids else FALLBACK_ID
        try:
            min_bytes = max(1024, min(int(body.get("min_bytes") or MIN_BYTES), 1 << 20))
        except (TypeError, ValueError):
            min_bytes = MIN_BYTES
        now = self.wall()
        with self._lock:
            while self._canaries and now - self._canaries[0] > 86400:
                self._canaries.popleft()
            if len(self._canaries) >= CANARY_PER_DAY:
                return 429, {"ok": False, "error": f"{CANARY_PER_DAY} canaries today already"}
            self._canaries.append(now)
        if not self._canary_lock.acquire(blocking=False):
            return 429, {"ok": False, "error": "a canary is running"}
        try:
            fn = getattr(self.ytm, "canary", None)
            res = fn(str(exe), vid, min_bytes) if callable(fn) else canary_plain(str(exe), vid, min_bytes)
        except Exception as exc:  # noqa: BLE001 - a canary that crashes is a failed canary
            res = {"result": "fail", "stage": "resolve", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            self._canary_lock.release()
        log.info("yt-dlp canary %s %s: %s%s", slot, vid, res.get("result"),
                 f" ({res.get('stage')}: {res.get('error')})" if res.get("result") != "pass" else "")
        return 200, {"ok": True, "id": vid, "slot": slot, "via": "haslerd", **res}

    # -- the key page ------------------------------------------------------------------

    def update_now(self) -> None:
        request_run(self.state_dir, "now", "Update now (key page)", self.systemctl, self.wall)

    def rollback(self) -> None:
        if not (self.root / "previous").exists() or self._version("previous") in (None, self._version("current")):
            raise ValueError("there is no previous YouTube Music helper to go back to")
        request_run(self.state_dir, "rollback", "Go back (key page)", self.systemctl, self.wall)


def canary_plain(exe: str, vid: str, min_bytes: int, runner: Callable[..., Any] = subprocess.run) -> dict:
    """The canary without a YtmSource (ytm off on this radio): no cookies."""
    from .player.sources import ytm

    return ytm.canary_with(exe, vid, min_bytes, [], runner)
