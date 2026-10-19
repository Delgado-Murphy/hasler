"""Night for the pilot lamp: NOAA sunrise/sunset, polar days, the settings' modes."""

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from haslerd.lamp import night
from haslerd.lamp.night import (
    is_night, location_from_settings, night_from, night_mode_active, polar, sun_times, tonight,
)

MADRID = ZoneInfo("Europe/Madrid")
MAD = (40.4168, -3.7038)
SVALBARD = (78.2232, 15.6267)


def near(at, hh, mm, tol_min=3):
    want = at.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return abs((at - want).total_seconds()) <= tol_min * 60


# Almanac values (USNO / timeanddate), local clock time.
@pytest.mark.parametrize("day, rise, set_", [
    (date(2026, 6, 21), (6, 44), (21, 48)),     # CEST, longest day
    (date(2026, 12, 21), (8, 33), (17, 51)),    # CET, shortest day
    (date(2026, 3, 20), (7, 18), (19, 27)),     # equinox, CET
    (date(2026, 10, 19), (8, 30), (19, 29)),    # CEST
])
def test_madrid_matches_the_almanac(day, rise, set_):
    sunrise, sunset = sun_times(day, *MAD, MADRID)
    assert sunrise.date() == day == sunset.date()
    assert sunrise.tzinfo is MADRID and sunset.tzinfo is MADRID
    assert near(sunrise, *rise), sunrise
    assert near(sunset, *set_), sunset


@pytest.mark.parametrize("lat, lon, zone, day, rise, set_", [
    (40.7128, -74.006, "America/New_York", date(2026, 6, 21), (5, 25), (20, 31)),
    (-33.8688, 151.2093, "Australia/Sydney", date(2026, 6, 21), (7, 0), (16, 54)),
    (47.3769, 8.5417, "Europe/Zurich", date(2026, 12, 21), (8, 13), (16, 37)),
])
def test_elsewhere_too(lat, lon, zone, day, rise, set_):
    sunrise, sunset = sun_times(day, lat, lon, ZoneInfo(zone))
    assert near(sunrise, *rise) and near(sunset, *set_), (sunrise, sunset)


def test_tz_defaults_to_the_system_zone(monkeypatch):
    monkeypatch.setattr(night, "local_tz", lambda: MADRID)
    sunrise, _ = sun_times(date(2026, 12, 21), *MAD)
    assert sunrise.utcoffset() == timedelta(hours=1)


def test_polar_day_and_night():
    tz = ZoneInfo("Arctic/Longyearbyen")
    for day, kind in ((date(2026, 6, 21), "day"), (date(2026, 12, 21), "night")):
        assert sun_times(day, *SVALBARD, tz) == (None, None)
        assert polar(day, *SVALBARD, tz) == kind
        for hour in range(0, 24, 3):
            when = datetime(day.year, day.month, day.day, hour, tzinfo=tz)
            assert is_night(when, *SVALBARD) is (kind == "night")
    assert polar(date(2026, 6, 21), *MAD, MADRID) is None
    # the pole itself, and the south in June
    assert sun_times(date(2026, 6, 21), 90.0, 0.0, timezone.utc) == (None, None)
    assert polar(date(2026, 6, 21), -90.0, 0.0, timezone.utc) == "night"


def test_is_night_flips_at_sunset_and_sunrise():
    sunrise, sunset = sun_times(date(2026, 6, 21), *MAD, MADRID)
    minute = timedelta(minutes=1)
    assert is_night(sunrise - minute, *MAD) and not is_night(sunrise + minute, *MAD)
    assert not is_night(sunset - minute, *MAD) and is_night(sunset + minute, *MAD)
    assert not is_night(datetime(2026, 6, 21, 14, tzinfo=MADRID), *MAD)
    assert is_night(datetime(2026, 6, 21, 2, tzinfo=MADRID), *MAD)
    # Any zone, same instant: the answer is about the sun, not the clock.
    assert is_night((sunset + minute).astimezone(timezone.utc), *MAD)


def test_location_from_settings_defaults_to_madrid():
    assert location_from_settings({}) == MAD
    assert location_from_settings({"location": {"lat": 47.37, "lon": 8.54}}) == (47.37, 8.54)
    for bad in ({"location": "Bern"}, {"location": {"lat": 91, "lon": 0}},
                {"location": {"lat": "47", "lon": 8}}, {"location": None}):
        assert location_from_settings(bad) == MAD
    assert location_from_settings(None) == MAD


def test_validators():
    assert night.location_from({"name": "  Bern ", "lat": 46.948, "lon": 7.4474}) == \
        {"name": "Bern", "lat": 46.948, "lon": 7.4474}
    for bad in ({"lat": 90.1, "lon": 0}, {"lat": 0, "lon": -180.5}, {"lat": True, "lon": 0},
                {"lat": float("nan"), "lon": 0}, {"lon": 0}, {"lat": 0, "lon": 0, "name": 5}, [1, 2]):
        with pytest.raises(ValueError):
            night.location_from(bad)
    assert night_from("sun") == "sun" and night_from("off") == "off"
    assert night_from("7:05-23:30") == "07:05-23:30"
    for bad in ("24:00-07:00", "22:00-22:00", "22:00", "dusk", None, 1, "22:60-07:00"):
        with pytest.raises(ValueError):
            night_from(bad)


def test_modes(monkeypatch):
    monkeypatch.setattr(night, "local_tz", lambda: MADRID)
    noon = datetime(2026, 6, 21, 12, tzinfo=MADRID)
    late = datetime(2026, 6, 21, 23, 30, tzinfo=MADRID)
    assert not night_mode_active(noon, {}) and night_mode_active(late, {})
    assert not night_mode_active(late, {"night": "off"})
    fixed = {"night": "22:00-07:00"}
    assert night_mode_active(datetime(2026, 6, 21, 22, 0, tzinfo=MADRID), fixed)
    assert night_mode_active(datetime(2026, 6, 22, 6, 59, tzinfo=MADRID), fixed)
    assert not night_mode_active(datetime(2026, 6, 22, 7, 0, tzinfo=MADRID), fixed)
    assert not night_mode_active(datetime(2026, 6, 21, 21, 59, tzinfo=MADRID), fixed)
    # the same instant in UTC is read on the local clock
    assert night_mode_active(datetime(2026, 6, 21, 20, 30, tzinfo=timezone.utc), fixed)
    day_window = {"night": "13:00-15:00"}
    assert night_mode_active(datetime(2026, 6, 21, 14, tzinfo=MADRID), day_window)
    assert not night_mode_active(late, day_window)
    assert night_mode_active(late, {"night": "bogus"})   # bad mode -> "sun"


def test_tonight_for_the_page():
    evening = datetime(2026, 10, 19, 18, 0, tzinfo=MADRID)
    doc = tonight({}, evening, MADRID)
    assert doc["mode"] == "sun" and doc["polar"] is None and doc["night_now"] is False
    assert doc["from"] == doc["sunset"] and doc["to"] != doc["sunrise"]   # tomorrow's dawn
    dawn = tonight({}, datetime(2026, 10, 19, 3, 0, tzinfo=MADRID), MADRID)
    assert dawn["night_now"] is True and dawn["to"] == dawn["sunrise"]
    assert tonight({"night": "off"}, evening, MADRID)["from"] is None
    fixed = tonight({"night": "22:00-07:00"}, evening, MADRID)
    assert (fixed["from"], fixed["to"]) == ("22:00", "07:00")
    arctic = tonight({"location": {"lat": SVALBARD[0], "lon": SVALBARD[1]}},
                     datetime(2026, 12, 21, 12, tzinfo=MADRID), MADRID)
    assert arctic["polar"] == "night" and arctic["sunrise"] is None and arctic["night_now"]
