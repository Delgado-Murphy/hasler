"""Night for the pilot lamp: sunset to sunrise where the radio stands.

docs/lamp.md: "Respect the dark room" - from sunset to sunrise the lamp is
dimmer and slower. Night is computed here, locally and offline, with NOAA's
solar position equations (the ones behind their sunrise/sunset spreadsheet),
good to about a minute at the radio's latitudes. Sunrise and sunset are the
moments the sun's centre is 90.833 degrees from the zenith (refraction plus
the sun's radius), the standard almanac definition.

Where the radio stands comes from the web page's ``settings.json``::

    "location": {"name": "Madrid", "lat": 40.4168, "lon": -3.7038},
    "night": "sun"            # sunset..sunrise (default)
             "off"            # never dim for the night
             "22:00-07:00"    # fixed local hours, may wrap past midnight

Standard library only. Everything here is pure and cheap (a few dozen trig
calls), so the policy can ask every tick.
"""

from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[assignment,misc]
    ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

# The owner's home; used whenever settings.json says nothing usable.
DEFAULT_LOCATION = {"name": "Madrid", "lat": 40.4168, "lon": -3.7038}
DEFAULT_MODE = "sun"

ZENITH = 90.833          # degrees: sunrise/sunset, refraction + solar radius
_FIXED = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")
_EPOCH_JD = 2440587.5    # Julian day of 1970-01-01T00:00Z


# -- the sun -----------------------------------------------------------------


def _sun(when: datetime) -> tuple[float, float]:
    """(declination in degrees, equation of time in minutes) at ``when``."""
    jd = when.timestamp() / 86400.0 + _EPOCH_JD
    t = (jd - 2451545.0) / 36525.0                   # Julian centuries since J2000
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    mr = math.radians(m)
    c = (math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
         + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
         + math.sin(3 * mr) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * t)
    apparent = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(omega))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(omega))
    decl = math.degrees(math.asin(math.sin(eps) * math.sin(apparent)))
    y = math.tan(eps / 2) ** 2
    l0r = math.radians(l0)
    eqtime = 4.0 * math.degrees(
        y * math.sin(2 * l0r)
        - 2 * e * math.sin(mr)
        + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
        - 0.5 * y * y * math.sin(4 * l0r)
        - 1.25 * e * e * math.sin(2 * mr))
    return decl, eqtime


def _hour_angle(when: datetime, lon: float, eqtime: float) -> float:
    """The sun's hour angle at ``when``, degrees in (-180, 180]; 0 = solar noon."""
    utc = when.astimezone(timezone.utc)
    minutes = utc.hour * 60 + utc.minute + utc.second / 60 + utc.microsecond / 6e7
    ha = ((minutes + eqtime + 4 * lon) / 4.0 - 180.0) % 360.0
    return ha - 360.0 if ha > 180.0 else ha


def _rise_set_angle(lat: float, decl: float) -> float | None:
    """Hour angle of sunrise/sunset in degrees; 0 = polar night, 180 = polar day."""
    la, de = math.radians(lat), math.radians(decl)
    denom = math.cos(la) * math.cos(de)
    if abs(denom) < 1e-12:                          # at the pole itself
        return 180.0 if lat * decl > 0 else 0.0
    cos_h = (math.cos(math.radians(ZENITH)) - math.sin(la) * math.sin(de)) / denom
    if cos_h >= 1.0:
        return 0.0
    if cos_h <= -1.0:
        return 180.0
    return math.degrees(math.acos(cos_h))


def zenith(when: datetime, lat: float, lon: float) -> float:
    """The sun's distance from the zenith at ``when``, degrees (no refraction)."""
    decl, eqtime = _sun(when)
    ha = math.radians(_hour_angle(when, lon, eqtime))
    la, de = math.radians(lat), math.radians(decl)
    cos_z = math.sin(la) * math.sin(de) + math.cos(la) * math.cos(de) * math.cos(ha)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_z))))


def sun_times(
    day: date, lat: float, lon: float, tz: tzinfo | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Sunrise and sunset on local ``day`` as aware datetimes in ``tz``.

    ``tz`` defaults to the system's zone. Polar night or polar day: the sun
    neither rises nor sets that day, and both are None - ask ``is_night`` (or
    ``polar``) which of the two it is.
    """
    tz = tz or local_tz()
    noon = datetime.combine(day, time(12), tzinfo=tz)
    # Solar noon nearest local noon, refined once for the moved instant.
    for _ in range(2):
        decl, eqtime = _sun(noon)
        noon -= timedelta(minutes=4 * _hour_angle(noon, lon, eqtime))
    decl, _ = _sun(noon)
    h0 = _rise_set_angle(lat, decl)
    if h0 in (0.0, 180.0):
        return None, None
    events = []
    for sign in (-1, 1):
        at = noon + timedelta(minutes=4 * sign * h0)
        for _ in range(3):                          # converges to seconds
            decl, eqtime = _sun(at)
            h = _rise_set_angle(lat, decl)
            if h in (0.0, 180.0):
                break
            at += timedelta(minutes=4 * (sign * h - _hour_angle(at, lon, eqtime)))
        events.append(at.astimezone(tz))
    return events[0], events[1]


def polar(day: date, lat: float, lon: float, tz: tzinfo | None = None) -> str | None:
    """"night" or "day" when the sun stays down or up all ``day``, else None."""
    tz = tz or local_tz()
    noon = datetime.combine(day, time(12), tzinfo=tz)
    decl, _ = _sun(noon)
    h0 = _rise_set_angle(lat, decl)
    return "night" if h0 == 0.0 else "day" if h0 == 180.0 else None


def is_night(when: datetime, lat: float, lon: float) -> bool:
    """True between sunset and sunrise at (lat, lon). Naive ``when`` is local time.

    Decided from where the sun is at that instant, so polar night (always
    True) and polar day (always False) need no special case, and the edges
    agree with ``sun_times`` to within seconds.
    """
    if when.tzinfo is None:
        when = when.astimezone()
    return zenith(when, lat, lon) > ZENITH


# -- settings ----------------------------------------------------------------


def local_tz() -> tzinfo:
    """The system's time zone, with its DST rules when they can be found.

    ``datetime.now().astimezone().tzinfo`` is only today's fixed offset - in
    July it would put December's sunset an hour late - so the zone's name is
    looked up first (TZ, /etc/localtime's link, /etc/timezone).
    """
    if ZoneInfo is not None:
        names = [os.environ.get("TZ", "").lstrip(":")]
        try:
            link = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in link:
                names.append(link.split("zoneinfo/", 1)[1])
        except OSError:
            pass
        try:
            names.append(Path("/etc/timezone").read_text().strip())
        except OSError:
            pass
        for name in names:
            if name:
                try:
                    return ZoneInfo(name)
                except (ZoneInfoNotFoundError, ValueError, OSError):
                    continue
    return datetime.now().astimezone().tzinfo or timezone.utc


def _coordinate(raw: Any, name: str, limit: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(raw)
    if value != value or not -limit <= value <= limit:   # NaN or out of range
        raise ValueError(f"{name} must be from {-limit:g} to {limit:g}")
    return value


def location_from(raw: Any) -> dict:
    """A submitted ``{name, lat, lon}`` -> clean dict, or ValueError saying why."""
    if not isinstance(raw, dict):
        raise ValueError("location must be {\"name\": ..., \"lat\": ..., \"lon\": ...}")
    lat = _coordinate(raw.get("lat"), "latitude", 90.0)
    lon = _coordinate(raw.get("lon"), "longitude", 180.0)
    name = raw.get("name", "")
    if name is None:
        name = ""
    if not isinstance(name, str):
        raise ValueError("the place name must be text")
    name = " ".join(name.split())[:60]
    return {"name": name, "lat": round(lat, 4), "lon": round(lon, 4)}


def night_from(raw: Any) -> str:
    """A submitted night mode -> "sun" | "off" | "HH:MM-HH:MM", or ValueError."""
    if raw in ("sun", "off"):
        return raw
    if isinstance(raw, str):
        m = _FIXED.match(raw.replace(" ", ""))
        if m:
            start, end = (int(m[1]), int(m[2])), (int(m[3]), int(m[4]))
            if start == end:
                raise ValueError("night hours must start and end at different times")
            return "%02d:%02d-%02d:%02d" % (*start, *end)
    raise ValueError("night must be \"sun\", \"off\" or hours like \"22:00-07:00\"")


def location_doc(settings: dict) -> dict:
    """settings.json's location, or Madrid's when it has none usable."""
    try:
        return location_from(settings.get("location"))
    except (ValueError, AttributeError):
        return dict(DEFAULT_LOCATION)


def location_from_settings(settings: dict) -> tuple[float, float]:
    """(lat, lon) from settings.json's ``location``; Madrid when missing or bad."""
    loc = location_doc(settings if isinstance(settings, dict) else {})
    return loc["lat"], loc["lon"]


def mode_from_settings(settings: dict) -> str:
    """settings.json's ``night`` mode; "sun" when missing or bad."""
    try:
        return night_from(settings.get("night", DEFAULT_MODE))
    except (ValueError, AttributeError):
        return DEFAULT_MODE


def _fixed_hours(mode: str) -> tuple[time, time]:
    start, end = mode.split("-")
    return time.fromisoformat(start), time.fromisoformat(end)


def night_mode_active(when: datetime, settings: dict) -> bool:
    """Is the lamp in its night manner at ``when``, by the page's settings?

    "sun": sunset..sunrise at the saved location; "off": never; fixed hours:
    local wall-clock start (inclusive) to end (exclusive), wrapping midnight.
    """
    settings = settings if isinstance(settings, dict) else {}
    mode = mode_from_settings(settings)
    if mode == "off":
        return False
    if mode == "sun":
        return is_night(when, *location_from_settings(settings))
    if when.tzinfo is not None:
        when = when.astimezone(local_tz())
    start, end = _fixed_hours(mode)
    now = when.time().replace(tzinfo=None)
    return start <= now < end if start < end else now >= start or now < end


def _hhmm(at: datetime | None) -> str | None:
    return at.strftime("%H:%M") if at is not None else None


def tonight(settings: dict, now: datetime | None = None, tz: tzinfo | None = None) -> dict:
    """What the Settings page shows: today's sun and the night being or next dimmed.

    ``from``/``to`` are the dimming window that is under way or comes next
    (before dawn that is last evening's; after it, this evening's), HH:MM in
    local time; both None when nothing dims (mode "off", or polar).
    """
    tz = tz or local_tz()
    now = (now or datetime.now(tz)).astimezone(tz)
    settings = settings if isinstance(settings, dict) else {}
    mode = mode_from_settings(settings)
    lat, lon = location_from_settings(settings)
    today = now.date()
    sunrise, sunset = sun_times(today, lat, lon, tz)
    doc: dict[str, Any] = {
        "mode": mode,
        "sunrise": _hhmm(sunrise),
        "sunset": _hhmm(sunset),
        "polar": polar(today, lat, lon, tz),
        "night_now": night_mode_active(now, settings),
        "from": None,
        "to": None,
    }
    if mode == "sun" and sunrise is not None and sunset is not None:
        if now < sunrise:
            doc["from"] = _hhmm(sun_times(today - timedelta(days=1), lat, lon, tz)[1])
            doc["to"] = _hhmm(sunrise)
        else:
            doc["from"] = _hhmm(sunset)
            doc["to"] = _hhmm(sun_times(today + timedelta(days=1), lat, lon, tz)[0])
    elif mode not in ("sun", "off"):
        doc["from"], doc["to"] = mode.split("-")
    return doc
