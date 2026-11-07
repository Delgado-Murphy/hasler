"""Faults that need a maker - the lamp's SOS (BoxState.fault), docs/using.md.

Checked every FAULT_POLL_S by the LampKeeper (main.py), never on a page
request. Each check is cheap, never raises, and answers a short reason word;
the lamp only says "something needs a maker", the page and the journal say
what:

``update``   the updater could not roll a failed update back (its state file
             stays in trial/rollback for over 10 min) - the radio runs on luck
``storage``  the SD card is read-only, or nearly full (state directory)
``sound``    no sound card at all (on a Pi: /proc/asound has none)
``panel``    the panel board is missing (the keys can't work)
``heat``     the CPU is hot enough to be throttled and shut down soon

Off a Pi (no /proc/asound, no thermal zone) the hardware checks stay quiet.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FAULT_POLL_S = 30.0
UPDATE_STATE = Path("/var/lib/hasler-apply/state.json")
ROLLBACK_STUCK_S = 600.0    # a rollback still not done after 10 min keeps failing
CPU_TEMP = Path("/sys/class/thermal/thermal_zone0/temp")
HOT_C = 80.0                # the Pi throttles at 80-85 degC, and shuts down at its limit
ASOUND = Path("/proc/asound")
FULL_FREE_BYTES = 20 * 1024 * 1024      # less than this free on the state disk: full
FULL_FREE_SHARE = 0.01


def update_phase(state_file: Path | None = UPDATE_STATE) -> str | None:
    """The updater's phase (trial, committed, probation, rolled_back, ...), or None."""
    try:
        doc = json.loads(Path(state_file).read_text()) if state_file else None
    except (OSError, ValueError):
        return None
    phase = doc.get("phase") if isinstance(doc, dict) else None
    return phase if isinstance(phase, str) else None


def _when(stamp: Any) -> float | None:
    """An updater ``iso()`` time -> epoch seconds, or None."""
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def update_broken(state_file: Path | None = UPDATE_STATE, now: float | None = None,
                  stuck_s: float = ROLLBACK_STUCK_S) -> bool:
    """The updater could not roll a failed update back.

    Its phases are trial, probation, committed and rolled_back
    (update/hasler_update/apply.py). A rollback sets ``phase: trial, step:
    rollback`` and, once done, ``rolled_back``; it is resumed at the next boot
    and haslerd start. So a ``trial``/``rollback`` state that has lasted more
    than ``stuck_s`` (since the history's last "rollback" entry, else the file's
    time) is a rollback that keeps failing - the radio runs on whatever is left.
    """
    if state_file is None:
        return False
    try:
        path = Path(state_file)
        doc = json.loads(path.read_text())
        mtime = path.stat().st_mtime
    except (OSError, ValueError):
        return False
    if not isinstance(doc, dict) or doc.get("phase") != "trial" or doc.get("step") != "rollback":
        return False
    history = [h for h in (doc.get("history") or []) if isinstance(h, dict)]
    since = next((_when(h.get("at")) for h in reversed(history) if h.get("event") == "rollback"), None)
    if since is None:
        since = mtime
    return (time.time() if now is None else now) - since > stuck_s


def storage_bad(path: Path | None) -> bool:
    """Read-only, or less than 20 MB / 1 % free, where haslerd keeps its state."""
    if path is None:
        return False
    try:
        st = os.statvfs(path)
    except OSError:
        return False
    if st.f_flag & getattr(os, "ST_RDONLY", 1):
        return True
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return total > 0 and (free < FULL_FREE_BYTES or free < FULL_FREE_SHARE * total)


def no_sound_card(asound: Path = ASOUND) -> bool:
    if not asound.is_dir():
        return False            # not a Pi (or no ALSA at all): not ours to judge
    try:
        return not (asound / "cards").read_text().strip() or "no soundcards" in (asound / "cards").read_text()
    except OSError:
        return False


def too_hot(temp_file: Path = CPU_TEMP, hot_c: float = HOT_C) -> bool:
    try:
        return int(temp_file.read_text().strip()) / 1000.0 >= hot_c
    except (OSError, ValueError):
        return False


# The firmware's throttle word, readable without root (vcgencmd get_throttled
# says the same). Measured and shown (web/diagnostics.py), never an SOS of
# its own: heat already is, and the amp's heat is in the Amp4 Pro, not the Pi.
THROTTLED_FILE = Path("/sys/devices/platform/soc/soc:firmware/get_throttled")
THROTTLED_BITS = {
    0: "under_voltage_now", 1: "freq_capped_now", 2: "throttled_now", 3: "soft_temp_limit_now",
    16: "under_voltage_seen", 17: "freq_capped_seen", 18: "throttled_seen", 19: "soft_temp_limit_seen",
}


def throttled(path: Path = THROTTLED_FILE) -> dict | None:
    """The Pi's throttle bits, decoded: ``{"raw": "0x50000", "under_voltage_now":
    bool, ..., "any_now": bool, "any_seen": bool}``, or None off a Pi.
    ``*_now`` is this moment; ``*_seen`` since boot (a power supply that
    sagged once at 3 a.m.). Never raises."""
    try:
        text = path.read_text().strip()
        word = int(text, 16) if text.lower().startswith("0x") else int(text, 16)
    except (OSError, ValueError):
        return None
    doc: dict = {"raw": f"0x{word:x}"}
    for bit, name in THROTTLED_BITS.items():
        doc[name] = bool(word & (1 << bit))
    doc["any_now"] = any(doc[n] for n in THROTTLED_BITS.values() if n.endswith("_now"))
    doc["any_seen"] = any(doc[n] for n in THROTTLED_BITS.values() if n.endswith("_seen"))
    return doc


def cpu_temp_c(temp_file: Path = CPU_TEMP) -> float | None:
    """The SoC's temperature in degC (one decimal), or None off a Pi."""
    try:
        return round(int(temp_file.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def check(state_dir: Path | None = None, update_state: Path | None = UPDATE_STATE, panel: Any = None,
          asound: Path = ASOUND, temp_file: Path = CPU_TEMP) -> list[str]:
    """The reasons for an SOS now, in that order; [] when all is well."""
    out = []
    for word, bad in (
        ("update", lambda: update_broken(update_state)),
        ("storage", lambda: storage_bad(state_dir)),
        ("sound", lambda: no_sound_card(asound)),
        ("panel", lambda: bool(panel is not None and getattr(panel, "absent", False))),
        ("heat", lambda: too_hot(temp_file)),
    ):
        try:
            if bad():
                out.append(word)
        except Exception:
            log.debug("fault check %s failed", word, exc_info=True)
    return out


def version_changed(path: Path | None, version: str) -> bool:
    """True once, on the first start of a new version (an update installed).

    Remembers ``version`` in ``path``; a first start ever (no file) is not an
    update. Never raises."""
    if path is None:
        return False
    try:
        before = path.read_text().strip()
    except OSError:
        before = None
    if before == version:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(version + "\n")
        os.replace(tmp, path)
    except OSError:
        return False
    return before is not None and before != ""
