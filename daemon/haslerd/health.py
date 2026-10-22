"""``GET /api/health`` - what the updater asks before and after a switch.

docs/ota-format.md "Health endpoint". ``health_report(app)`` takes the WebApp
and reads what it already has (``app.actions``, ``app.actions.player``,
``app.panel``), all through ``getattr`` so a missing piece shows up as a failed
check rather than an exception. Importing this module does nothing.

``idle`` = safe to update now: in standby, no phone casting, and nothing
played for at least 10 minutes. "Last played" comes from, in order:
``actions.last_active_at`` (monotonic; set it from the main loop if you want
it exact), the time this module last *saw* the radio active, or - when it
has never looked before - the state file's mtime (state.json is rewritten when
standby or the last preset changes, so it bounds the last activity).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

IDLE_AFTER_S = 600.0


def _uptime_s() -> float:
    """Seconds since this process started (Linux /proc), else since first asked."""
    try:
        stat = Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()
        start_ticks = int(stat[19])
        boot_up = float(Path("/proc/uptime").read_text().split()[0])
        return max(0.0, boot_up - start_ticks / os.sysconf("SC_CLK_TCK"))
    except (OSError, ValueError, IndexError):
        global _FIRST
        if _FIRST is None:
            _FIRST = time.monotonic()
        return time.monotonic() - _FIRST


_FIRST: float | None = None


def _version() -> str:
    """The release's version (its release.json / manifest.json), else the package's."""
    import json

    root = os.environ.get("HASLER_RELEASE")
    candidates = [Path(root)] if root else []
    candidates.append(Path(__file__).resolve().parents[2])      # <release>/app/haslerd -> <release>
    for base in candidates:
        for name in ("release.json", "manifest.json"):
            try:
                v = json.loads((base / name).read_text()).get("version")
            except (OSError, ValueError, AttributeError):
                continue
            if v:
                return str(v)
    try:
        from . import __version__

        return __version__
    except ImportError:
        return "unknown"


def _mpd_ok(player: Any) -> bool:
    radio = (getattr(player, "sources", None) or {}).get("radio")
    mpd = getattr(radio, "mpd", None)
    if not callable(mpd):
        return False
    try:
        mpd().status()
        return True
    except Exception:
        return False


def _audio_card(cards: Path = Path("/proc/asound/cards")) -> bool:
    try:
        return any(line[:3].strip().isdigit() for line in cards.read_text().splitlines())
    except OSError:
        return False


def _panel_state(panel: Any) -> str:
    if panel is None:
        return "absent"
    if not getattr(panel, "absent", False):
        return "ok"
    variant = getattr(panel, "variant", None)
    allowed = getattr(panel, "allow_absent", None)
    if allowed is None:
        allowed = bool((getattr(variant, "io", None) or {}).get("panel_optional", False))
    return "allowed_absent" if allowed else "absent"


def _playing(actions: Any) -> bool:
    if getattr(actions, "standby", False):
        return False
    player = getattr(actions, "player", None)
    try:
        return player is not None and player.current() is not None
    except Exception:
        return False


def _casting(actions: Any) -> bool:
    fn = getattr(actions, "phone_has_it", None)
    try:
        return bool(fn()) if callable(fn) else False
    except Exception:
        return False


def _recent_events(watch: Any) -> list:
    fn = getattr(watch, "recent_events", None)
    try:
        return list(fn()) if callable(fn) else []
    except Exception:
        return []


def health_report(app: Any, now: float | None = None) -> dict:
    """The /api/health document for ``app`` (a WebApp, or anything shaped like one)."""
    now = time.monotonic() if now is None else now
    actions = getattr(app, "actions", None)
    player = getattr(actions, "player", None)
    standby = bool(getattr(actions, "standby", False))
    watch = getattr(app, "stream_watch", None)     # player/watchdog.py
    stalled = bool(watch is not None and getattr(watch, "silent", False))
    playing = _playing(actions) and not stalled
    casting = _casting(actions)
    active = playing or casting or not standby

    seen = getattr(app, "_health_last_active", None)
    if active:
        seen = now
    explicit = getattr(actions, "last_active_at", None)
    last = explicit if isinstance(explicit, (int, float)) else seen
    if last is None:
        state_path = getattr(getattr(actions, "state", None), "path", None)
        try:
            age = time.time() - os.stat(state_path).st_mtime if state_path else _uptime_s()
        except OSError:
            age = _uptime_s()
        last = now - max(0.0, age)
    try:
        app._health_last_active = last
    except AttributeError:
        pass
    last_play_s = max(0.0, now - last)

    checks = {
        "mpd": _mpd_ok(player),
        "audio_card": _audio_card(),
        "panel": _panel_state(getattr(app, "panel", None)),
        "lamp": getattr(app, "lamp", True) is not None,
        "web": True,
    }
    ok = all(v for k, v in checks.items() if k != "panel") and checks["panel"] != "absent"
    return {
        "version": _version(),
        "ok": ok,
        "uptime_s": int(_uptime_s()),
        "checks": checks,
        "idle": standby and not playing and not casting and last_play_s >= IDLE_AFTER_S,
        "standby": standby,
        "playing": playing or casting,
        # MPD stopped a preset by itself and the radio is bringing it back.
        "recovering": bool(watch is not None and getattr(watch, "recovering", False)),
        "stalled_s": watch.status_doc()["stalled_s"] if watch is not None else None,
        # The last few MPD stops/errors and recovery attempts (stream-events.jsonl).
        "stream_events_recent": _recent_events(watch),
        "last_play_s": int(last_play_s),
    }
