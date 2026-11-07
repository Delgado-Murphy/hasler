"""The check-in's 24 h counters (haslerd/daycounters.py) and the throttle word's
read order on kernels without the sysfs file (lamp/faults.throttled)."""

from __future__ import annotations

import json
import subprocess

from haslerd import daycounters, health
from haslerd.daycounters import DayCounters
from haslerd.lamp import faults
from haslerd.player.watchdog import StreamEvents

T0 = 1_794_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def test_counts_roll_over_24_hours_whatever_hour_the_check_in_asks(tmp_path):
    clock = Clock()
    c = DayCounters(tmp_path / "counters.json", wall=clock)
    c.event("stalled")
    c.event("stalled")
    c.event("recovered")                                   # not counted
    c.event("mpd unresponsive -> restarted")
    c.add("daemon_starts")
    c.add("ytdlp_failures", 2)
    c.sample(["heat"], 81.4)
    c.sample(["heat", "storage"], 62.0)
    s = c.sums()
    assert s["stalls"] == 2 and s["mpd_restarts"] == 1 and s["daemon_starts"] == 1 and s["ytdlp_failures"] == 2
    assert s["faults"] == ["heat", "storage"] and s["temp_max"] == 81 and s["undervolt"] is False and s["hours"] == 1
    clock.t += 23 * 3600                                     # still inside the 24 h
    assert c.sums()["stalls"] == 2
    clock.t += 3600                                          # the first hour has left the window
    s = c.sums()
    assert s["stalls"] == 0 and s["faults"] == [] and s["temp_max"] is None


def test_written_at_most_every_five_minutes_and_read_back_at_start(tmp_path):
    clock = Clock()
    path = tmp_path / "counters.json"
    c = DayCounters(path, wall=clock)
    c.event("stalled")
    assert not path.exists()                                # under FLUSH_S since start: not yet
    clock.t += daycounters.FLUSH_S
    c.event("stalled")
    assert json.loads(path.read_text())["hours"]
    c.add("daemon_starts", flush=True)                       # a restart is written at once
    again = DayCounters(path, wall=clock)
    assert again.sums()["stalls"] == 2 and again.sums()["daemon_starts"] == 1
    mtime = path.stat().st_mtime_ns
    clock.t += 3 * daycounters.FLUSH_S
    again.maybe_flush()                                     # nothing changed: nothing written
    assert path.stat().st_mtime_ns == mtime
    DayCounters(None).add("stalls")                         # no card: counts in memory, never raises
    (tmp_path / "bad.json").write_text("{nope")
    assert DayCounters(tmp_path / "bad.json").sums()["stalls"] == 0


def test_power_flags_count_now_and_a_seen_bit_that_came_on():
    c = DayCounters(None, wall=Clock())
    c.sample(power={"under_voltage_now": False, "under_voltage_seen": True})   # since boot: when? not counted
    assert c.sums()["undervolt"] is False
    c.sample(power={"under_voltage_now": False, "under_voltage_seen": True, "throttled_seen": True})
    assert c.sums()["throttled"] is True and c.sums()["undervolt"] is False
    c.sample(power={"under_voltage_now": True})
    assert c.sums()["undervolt"] is True
    assert c.power_due() is True and c.power_due() is False


def test_stream_events_and_the_health_document_carry_the_day(tmp_path):
    c = DayCounters(None, wall=Clock())
    ev = StreamEvents(tmp_path / "events.jsonl")
    ev.on_add = c.event
    ev.add("stalled", None, "stop", None)
    assert c.sums()["stalls"] == 1

    class App:
        day_counters = c
    assert health._day(App())["stalls"] == 1
    assert health._day(object()) is None


def test_throttled_falls_back_to_vcgencmd_then_hwmon(tmp_path, monkeypatch):
    monkeypatch.setattr(faults, "THROTTLED_FILE", tmp_path / "gone")
    monkeypatch.setattr(faults, "THROTTLED_GLOBS", ())
    calls = []

    def vcgencmd(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "throttled=0x50005\n", "")
    doc = faults.throttled(run=vcgencmd, found={})
    assert doc["source"] == "vcgencmd" and doc["raw"] == "0x50005" and doc["under_voltage_now"] and doc["under_voltage_seen"]
    assert calls == [["vcgencmd", "get_throttled"]]
    # no vcgencmd: the hwmon rpi_volt alarm, undervoltage only
    hw = tmp_path / "hwmon"
    (hw / "hwmon0").mkdir(parents=True)
    (hw / "hwmon0/name").write_text("cpu_thermal\n")
    (hw / "hwmon1").mkdir()
    (hw / "hwmon1/name").write_text("rpi_volt\n")
    (hw / "hwmon1/in0_lcrit_alarm").write_text("1\n")

    def missing(argv, **kw):
        raise FileNotFoundError(argv[0])
    found: dict = {}
    doc = faults.throttled(run=missing, hwmon=hw, found=found)
    assert doc["source"] == "hwmon" and doc["raw"] is None and doc["under_voltage_now"] is True and doc["any_seen"] is False
    (hw / "hwmon1/in0_lcrit_alarm").write_text("0\n")
    assert faults.throttled(run=missing, hwmon=hw, found=found)["under_voltage_now"] is False
    assert faults.throttled(run=missing, hwmon=tmp_path / "none", found={}) is None
    # the glob finds the firmware node where the kernel moved it
    node = tmp_path / "platform/fw"
    node.mkdir(parents=True)
    (node / "get_throttled").write_text("0x0\n")
    monkeypatch.setattr(faults, "THROTTLED_GLOBS", (str(tmp_path / "platform/*/get_throttled"),))
    assert faults.throttled(run=missing, found={})["source"] == "sysfs"
