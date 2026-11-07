"""Clock and place: the radio's time zone, for the key page.

Raspberry Pi Imager set the zone once; nothing on the radio changed it
afterwards. Night dimming by fixed hours, "asleep if the power returns at
night" and the 02:00-05:00 update window all follow the clock, so a radio
that moves abroad wants it changed once (docs/using.md "Moving the radio").
The place (``settings.json`` ``location``) already has a row on Pilot lamp;
this adds the zone beside it:

    GET  /api/settings            -> "clock": {"timezone": "Europe/Madrid", "zones": [...]}
    PUT  /api/settings {"timezone": "Europe/Zurich"}

The change goes through ``scripts/hasler-timezone`` (sudoers allows exactly
that file; it checks the name against /usr/share/zoneinfo itself), so the
page can only ever name a zone the system knows. Home Assistant knows the
home's zone and coordinates (ha/config.fingerprint reads them): when it is
connected the page offers, once, to take both.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-timezone"
ZONEINFO = Path("/usr/share/zoneinfo")
ETC_TIMEZONE = Path("/etc/timezone")
# Area/City, one or two slashes: letters, digits, _ + - only (never . or a path).
ZONE_RE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")
# The zones the page lists first (a radio travels mostly between these homes);
# the field takes any Area/City the system knows.
COMMON_ZONES = (
    "Europe/Madrid", "Europe/Lisbon", "Europe/Zurich", "Europe/Paris", "Europe/Berlin", "Europe/Rome",
    "Europe/Amsterdam", "Europe/Brussels", "Europe/Vienna", "Europe/London", "Europe/Dublin",
    "Atlantic/Canary", "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Mexico_City", "America/Bogota", "America/Buenos_Aires", "America/Sao_Paulo", "Asia/Tokyo",
    "Australia/Sydney", "UTC",
)


def timezone_from(raw: object, zoneinfo: Path = ZONEINFO) -> str:
    """A submitted zone name -> itself, or ValueError saying why not."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("timezone must be a name like Europe/Madrid")
    zone = raw.strip()
    if len(zone) > 64 or not ZONE_RE.match(zone):
        raise ValueError(f"not a time zone name: {zone[:40]}")
    if zoneinfo.is_dir() and not (zoneinfo / zone).is_file():
        raise ValueError(f"the radio doesn't know a time zone called {zone}")
    return zone


def current_timezone(runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                     etc_timezone: Path = ETC_TIMEZONE) -> str:
    """The zone in force: timedatectl's word, else /etc/timezone, else ""."""
    try:
        out = runner(["timedatectl", "show", "-p", "Timezone", "--value"],
                     capture_output=True, text=True, timeout=3)
        zone = (out.stdout or "").strip()
        if out.returncode == 0 and zone and ZONE_RE.match(zone):
            return zone
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        zone = etc_timezone.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return zone if ZONE_RE.match(zone) else ""


def set_timezone(zone: str, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 helper: str = HELPER) -> None:
    """``sudo hasler-timezone <zone>``; raises ValueError with the helper's reason."""
    try:
        out = runner(["sudo", "-n", helper, zone], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not change the time zone: {exc}") from exc
    if out.returncode != 0:
        why = (out.stderr or out.stdout or "").strip().splitlines()
        raise ValueError(why[-1] if why else "the time zone helper failed")
    log.info("time zone set to %s from the web page", zone)


def doc(timezone: str) -> dict:
    """The ``clock`` block of GET /api/settings."""
    zones = list(COMMON_ZONES)
    if timezone and timezone not in zones:
        zones.insert(0, timezone)
    return {"timezone": timezone, "zones": zones}
