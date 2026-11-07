"""Faults that need a maker - the lamp's SOS (BoxState.fault), docs/using.md.

Checked every FAULT_POLL_S by the LampKeeper (main.py), never on a page
request. Each check is cheap, never raises, and answers a short reason word;
the lamp only says "something needs a maker", the page and the journal say
what:

``update``   the updater could not roll a failed update back (its state file
             says ``rollback_failed``/``broken``) - the radio runs on luck
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
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FAULT_POLL_S = 30.0
UPDATE_STATE = Path("/var/lib/hasler-apply/state.json")
ROLLBACK_FAILED = ("rollback_failed", "broken", "stuck")
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


def update_broken(state_file: Path | None = UPDATE_STATE) -> bool:
    return update_phase(state_file) in ROLLBACK_FAILED


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
