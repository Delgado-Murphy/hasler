"""The lamp changing hands without a dark gap - docs/lamp.md "Boot and shutdown".

Two programs drive the LED: the early boot script (scripts/hasler-lamp-boot,
root, stdlib only) and haslerd's engine. They meet in ``RUN_DIR``
(/run/hasler-lamp, made by the boot script, group gpio so haslerd may write):

``boot``      the boot script's state, JSON: the breath it draws (colour,
              floor, peak, period_s, t0_monotonic), its ``pid`` while it runs,
              and ``released: true`` once it has let go of the LED.
``takeover``  haslerd asks the running boot script to let go: it notices
              within a frame (25 ms), leaves the LED exactly as it is, writes
              ``released`` and exits. haslerd then reads the LED back from
              sysfs and seeds its engine with that duty (``engine.seed``), so the
              boot breath flows on instead of jumping or going dark.
              (haslerd.service's ExecStartPost stops the unit anyway, for a
              boot script too old to know this.)
``handover``  haslerd's last word when it exits, JSON ``{"mode", "at"}``:
              ``hold`` (the box is shutting down: it left a steady ember for
              the halt hook - nothing more to draw), ``amber`` (a restart or a
              crash: breathe on until haslerd is back) or ``ember`` (an update
              is being installed: the busy ember breath, never dark). The
              unit's ExecStopPost runs ``hasler-lamp-boot --handover``, which
              starts the boot script again in that mode, continuing from
              whatever the LED shows.

Everything here is best effort and never raises: a missing directory or a
refused write only costs the seamlessness, never the radio.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger(__name__)

RUN_DIR = Path(os.environ.get("HASLER_LAMP_RUN", "/run/hasler-lamp"))
GAMMA = 2.2                 # the engine's (lamp/engine.py); the boot script's too
TAKEOVER_WAIT_S = 0.3       # the boot script answers within a frame or two
# What a shutdown leaves for the halt hook: the shutting-down breath's middle
# (0.15..0.35 perceptual), steady - never a frozen breath at a random phase.
# Pure red, as the engine draws deep ember (its green is under the floor).
HOLD_LEVEL = 0.25
HOLD_LEVEL_NIGHT = 0.17     # the night ember's share of that (0.15 / 0.22)
MINOR_SHARE = 0.2           # the engine's rule: a trace of a channel stays dark

RGB = tuple[float, float, float]


def read_boot(run_dir: Path = RUN_DIR) -> dict | None:
    """The boot script's state file, or None."""
    try:
        doc = json.loads((run_dir / "boot").read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True             # root's process, and we are not root: alive
    except (ProcessLookupError, ValueError, TypeError, OverflowError):
        return False
    except OSError:
        return False
    return True


def boot_running(doc: dict | None) -> bool:
    return bool(doc) and not doc.get("released") and _alive(doc.get("pid"))


def take_over(
    run_dir: Path = RUN_DIR,
    wait_s: float = TAKEOVER_WAIT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict | None:
    """Ask a running boot script to let go of the LED; its last state, or None.

    Returns at once when no boot script runs. Otherwise waits up to
    ``wait_s`` for it to write ``released`` (or exit).
    """
    doc = read_boot(run_dir)
    if not boot_running(doc):
        return doc
    try:
        (run_dir / "takeover").write_text(f"{os.getpid()}\n")
    except OSError:
        log.info("lamp: can't ask the boot breath to stop (%s); systemd will", run_dir)
        return doc
    deadline = clock() + wait_s
    while clock() < deadline:
        sleep(0.01)
        doc = read_boot(run_dir)
        if not boot_running(doc):
            return doc
    log.info("lamp: the boot breath didn't answer in %.1f s; taking over anyway", wait_s)
    return doc


def write_handover(mode: str, run_dir: Path = RUN_DIR) -> bool:
    """Leave ``mode`` (hold | amber | ember) for ``hasler-lamp-boot --handover``."""
    try:
        tmp = run_dir / ".handover.tmp"
        tmp.write_text(json.dumps({"mode": mode, "at": time.time()}))
        os.replace(tmp, run_dir / "handover")
        return True
    except OSError:
        log.debug("lamp: no handover note in %s", run_dir, exc_info=True)
        return False


def hold_duty(
    ratio: Sequence[float] = (1.0, 0.10, 0.0),
    night: bool = False,
    balance: Sequence[float] = (1.0, 1.0, 1.0),
) -> RGB:
    """The steady ember a shutdown leaves for the halt hook, as linear duty.

    ``ratio`` is the palette's ember (linear); a channel with less than
    MINOR_SHARE of the strongest stays dark, as the engine draws deep ember.
    """
    level = (HOLD_LEVEL_NIGHT if night else HOLD_LEVEL) ** GAMMA
    raw = [max(0.0, float(r)) * level * float(b) for r, b in zip(ratio, balance)]
    top = max(raw) or 1.0
    out = [min(1.0, d) if d >= MINOR_SHARE * top else 0.0 for d in raw]
    return (out[0], out[1], out[2])
