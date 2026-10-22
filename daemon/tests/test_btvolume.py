"""The phone's Bluetooth volume is the radio's (player/btvolume.py), with a fake
BlueALSA behind busctl and a radio that rounds like the real mixer."""

from __future__ import annotations

import json

import pytest

from haslerd.player.btvolume import (ECHO_S, TICK_S, PhoneVolume, knob_to_phone, pack, phone_pcm,
                                     phone_to_knob, unpack)
from haslerd.player.moode import scale_volume, unscale_volume

PCM = "/org/bluealsa/hci0/dev_B0_9C_B2_EB_15_F9/a2dpsnk/source"
SINK = "/org/bluealsa/hci0/dev_B0_9C_B2_EB_15_F9/a2dpsnk/sink"


class FakeBlueALSA:
    """GetManagedObjects and set-property on one phone's PCM, as busctl answers."""

    def __init__(self, volume: int = 36, soft: bool = True) -> None:
        self.volume = pack(volume)
        self.soft = soft
        self.present = True
        self.calls: list[list[str]] = []
        self.fail_set = False

    def phone_moves(self, level: int) -> None:
        self.volume = pack(level)

    def __call__(self, args: list[str], timeout: float):
        self.calls.append(args)
        if args[:2] == ["--json=short", "call"]:
            objs = {}
            if self.present:
                objs[PCM] = {"org.bluealsa.PCM1": {
                    "Transport": {"type": "s", "data": "A2DP-sink"},
                    "Mode": {"type": "s", "data": "source"},
                    "Running": {"type": "b", "data": True},
                    "SoftVolume": {"type": "b", "data": self.soft},
                    "Volume": {"type": "q", "data": self.volume}}}
            return 0, json.dumps({"type": "a{oa{sa{sv}}}", "data": [objs]}), ""
        if args[0] == "set-property":
            if self.fail_set:
                return 1, "", "Access denied"
            _, _, path, _, name, _, value = args
            assert path == PCM
            if name == "Volume":
                self.volume = int(value)
            elif name == "SoftVolume":
                self.soft = value == "true"
            return 0, "", ""
        raise AssertionError(args)

    def sets(self) -> list[tuple[str, str]]:
        return [(a[4], a[6]) for a in self.calls if a[0] == "set-property"]


class Radio:
    """The player's volume: knob scale in, mixer percent kept, read back rounded."""

    def __init__(self, knob: float = 50.0, limit: int = 70, floor: int = 28) -> None:
        self.limit, self.floor = limit, floor
        self.percent = scale_volume(knob, limit, floor)
        self.set_calls: list[float] = []

    def get(self) -> float:
        return unscale_volume(self.percent, self.limit, self.floor)

    def set(self, knob: float) -> None:
        self.set_calls.append(knob)
        self.percent = scale_volume(knob, self.limit, self.floor)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rig():
    bus, radio, clock = FakeBlueALSA(), Radio(), Clock()
    state = {"on": True}
    pv = PhoneVolume(radio.get, radio.set, lambda: state["on"], run=bus, clock=clock,
                     spawn=lambda fn, name: fn())
    return pv, bus, radio, clock, state


def test_scales():
    assert knob_to_phone(0) == 0 and knob_to_phone(100) == 127 and knob_to_phone(50) == 64
    assert phone_to_knob(0) == 0 and phone_to_knob(127) == 100 and phone_to_knob(64) == 50.4
    assert unpack(9252) == 36 and unpack(0x2424 | 0x8080) == 36 and unpack(True) is None
    assert pack(200) == 127 << 8 | 127


def test_phone_pcm_is_the_a2dp_sinks_source_end():
    objs = {SINK: {"org.bluealsa.PCM1": {"Transport": "A2DP-sink", "Mode": "sink"}},
            PCM: {"org.bluealsa.PCM1": {"Transport": "A2DP-sink", "Mode": "source"}},
            "/x/a2dpsrc/sink": {"org.bluealsa.PCM1": {"Transport": "A2DP-source", "Mode": "sink"}}}
    assert phone_pcm(objs)[0] == PCM
    assert phone_pcm({}) is None


def test_start_the_radio_wins_then_soft_volume_off(rig):
    pv, bus, radio, _, _ = rig
    pv.step()
    # The phone's slider first (to the radio's 50 -> 64/127), then BlueALSA stops scaling.
    assert bus.sets() == [("Volume", str(pack(knob_to_phone(radio.get())))), ("SoftVolume", "false")]
    assert radio.set_calls == []            # a connect never changes how loud the radio is
    assert bus.soft is False


def test_phone_moves_the_radio(rig):
    pv, bus, radio, clock, _ = rig
    pv.step()
    clock.t += ECHO_S + 0.1
    bus.phone_moves(20)
    pv.step()
    assert radio.set_calls == [phone_to_knob(20)]
    n = len(bus.calls)
    clock.t += TICK_S
    pv.step()                               # the radio's re-read is not taken as a knob turn
    assert radio.set_calls == [phone_to_knob(20)] and not bus.sets()[2:]
    assert len(bus.calls) == n + 1


def test_radio_moves_the_phone(rig):
    pv, bus, radio, clock, _ = rig
    pv.step()
    clock.t += 5
    radio.set(80)                           # the knob, a key or the page
    pv.step()
    assert unpack(bus.volume) == knob_to_phone(radio.get())
    assert radio.set_calls == [80]          # nothing echoed back onto the radio


def test_the_phones_rounded_answer_is_not_a_move(rig):
    pv, bus, radio, clock, _ = rig
    pv.step()
    clock.t += 5
    radio.set(80)
    pv.step()
    clock.t += 0.3
    bus.phone_moves(unpack(bus.volume) - 2)   # an iPhone's own step, answering our write
    pv.step()
    clock.t += ECHO_S + 1
    pv.step()
    assert radio.set_calls == [80]            # still the knob's level


def test_a_real_phone_move_after_the_echo_window(rig):
    pv, bus, radio, clock, _ = rig
    pv.step()
    clock.t += 5
    radio.set(80)
    pv.step()
    clock.t += ECHO_S + 0.1
    bus.phone_moves(10)
    pv.step()
    assert radio.set_calls == [80, phone_to_knob(10)]


def test_knob_wins_on_movement_after_the_phone(rig):
    pv, bus, radio, clock, _ = rig
    pv.step()
    clock.t += 5
    bus.phone_moves(100)
    pv.step()
    clock.t += 5
    radio.set(30)                           # the knob turns: the volume is the knob's
    pv.step()
    assert unpack(bus.volume) == knob_to_phone(radio.get())


def test_nothing_written_when_nothing_moved(rig):
    pv, bus, _, clock, _ = rig
    pv.step()
    n = len(bus.sets())
    for _ in range(5):
        clock.t += TICK_S
        pv.step()
    assert len(bus.sets()) == n


def test_off_gives_blue_alsa_its_volume_back_once(rig):
    pv, bus, radio, clock, state = rig
    pv.step()
    assert bus.soft is False
    state["on"] = False
    clock.t += 5
    bus.phone_moves(5)
    pv.step()
    pv.step()
    assert bus.soft is True and bus.sets()[-1] == ("SoftVolume", "true")
    assert [s for s in bus.sets() if s[0] == "SoftVolume"] == [("SoftVolume", "false"), ("SoftVolume", "true")]
    assert radio.set_calls == []            # off: the phone no longer moves the radio
    state["on"] = True
    pv.step()                               # on again: in step from the radio, soft volume off
    assert bus.soft is False and unpack(bus.volume) == knob_to_phone(radio.get())


def test_off_leaves_a_soft_pcm_alone(rig):
    pv, bus, _, _, state = rig
    state["on"] = False
    pv.step()
    assert bus.sets() == []


def test_a_new_phone_starts_in_step_again(rig):
    pv, bus, radio, clock, _ = rig
    pv.step()
    bus.present = False
    pv.step()
    bus.present, bus.soft = True, True
    bus.phone_moves(120)                    # the next phone arrives loud
    clock.t += 5
    pv.step()
    assert radio.set_calls == [] and bus.soft is False
    assert unpack(bus.volume) == knob_to_phone(radio.get())


def test_tick_only_while_bluetooth_plays_and_not_too_often(rig):
    pv, bus, _, clock, _ = rig
    pv.tick(clock.t, active=False)
    assert bus.calls == []
    pv.tick(clock.t, active=True)
    pv.tick(clock.t + 0.1, active=True)
    assert len([c for c in bus.calls if c[1] == "call"]) == 1
    pv.tick(clock.t + TICK_S, active=True)
    assert len([c for c in bus.calls if c[1] == "call"]) == 2


def test_leaving_bluetooth_resyncs_from_the_radio_next_time(rig):
    pv, bus, radio, clock, _ = rig
    pv.tick(clock.t, active=True)
    pv.tick(clock.t + 1, active=False)      # another source: no reads
    radio.set(10)
    bus.phone_moves(90)
    clock.t += 10
    pv.tick(clock.t, active=True)
    assert radio.set_calls == [10] and unpack(bus.volume) == knob_to_phone(radio.get())


def test_blue_alsa_saying_no_is_retried_not_fatal(rig):
    pv, bus, radio, clock, _ = rig
    bus.fail_set = True
    pv.step()
    assert bus.soft is True
    bus.fail_set = False
    clock.t += 1
    pv.step()
    assert bus.soft is False and radio.set_calls == []


def test_unreadable_blue_alsa_does_nothing(rig):
    pv, _, radio, _, _ = rig
    pv._run = lambda args, timeout: (1, "", "org.bluealsa not found")
    pv.step()
    assert radio.set_calls == []
