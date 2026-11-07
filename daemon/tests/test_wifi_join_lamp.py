"""After a Wi-Fi join's restart: amber while it connects, then the green OK - or
the HAL eye and CQ - and never a red that nothing clears (the bench, 2026-11-07:
the password of the same network re-typed, a restart, then a red lamp until the
plug was pulled). The lamp, the hotspot watchdog's no-network fallback and the
link probe, with fakes."""

from __future__ import annotations

import pytest
from conftest import VARIANTS

from haslerd import main as main_mod
from haslerd import net as net_mod
from haslerd import wifiwatch
from haslerd.actions import Actions
from haslerd.lamp import policy
from haslerd.lamp.contract import BoxState
from haslerd.main import JOIN_MAX_S, LampKeeper
from haslerd.player.base import FakePlayer
from haslerd.variant import load_variant
from haslerd.wifiwatch import HotspotWatchdog
from test_lamp_policy import Clock, FakeDriver, FakePanel

REFUSALS = ("refuse", "refuse1", "fail")      # the HAL eye, in whichever word the engine knows


class Net:
    """Connectivity as a just-restarted radio sees it: optimistic, then the truth."""

    def __init__(self):
        self.online, self.hotspot, self.seen_up, self.linked = True, False, False, False


def booting(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    clock = Clock()
    actions = Actions(variant, FakePlayer(), clock=clock)
    driver, net = FakeDriver(), Net()
    keeper = LampKeeper(driver, actions, FakePanel(), net, None, clock=clock,
                        night=lambda when, settings: False)
    keeper.run_dir = tmp_path
    keeper.arm()
    return clock, driver, net, keeper


def run(clock, keeper, seconds, step=1.0):
    seen = []
    end = clock.t + seconds
    while clock.t < end:
        keeper.tick(clock())
        seen.append(keeper.look)
        clock.t += step
    return seen


def test_joining_is_busy_until_online_then_the_green_ok(tmp_path):
    clock, driver, net, keeper = booting(tmp_path)
    keeper.joining()
    keeper.resume_started()
    net.online = False                       # NetworkManager still associating
    seen = run(clock, keeper, 200)           # well past the 60 s red alert and the boot's 155 s
    keeper.boot_done("failed")               # the resume gave up meanwhile
    seen += run(clock, keeper, 30)
    assert policy.NO_INTERNET not in seen and keeper.look == policy.BUSY
    assert keeper.meaning() == "starting"
    net.online = net.seen_up = net.linked = True     # on the network at last
    keeper.tick(clock())
    keeper.celebrate()                       # main.consume_wifi_joined
    run(clock, keeper, 2)
    assert "update_ok" in driver.replies     # the green bloom and OK
    assert keeper.state.joining is False
    assert keeper.look != policy.BUSY and keeper.look != policy.NO_INTERNET


def test_a_join_that_did_not_work_is_the_hal_eye_then_cq(tmp_path):
    clock, driver, net, keeper = booting(tmp_path)
    keeper.joining()
    keeper.boot_done("nothing")
    net.online = False
    seen = run(clock, keeper, 110)
    assert policy.NO_INTERNET not in seen and keeper.look == policy.BUSY
    net.hotspot = True                       # moOde's (or the watchdog's) setup hotspot
    seen = run(clock, keeper, 30)
    assert sum(driver.replies.count(w) for w in REFUSALS) == 1      # the HAL eye, once
    assert keeper.look == policy.SETUP and policy.NO_INTERNET not in seen
    assert keeper.meaning() == "setup"


def test_joining_gives_way_to_the_usual_rules_and_that_red_clears(tmp_path):
    clock, driver, net, keeper = booting(tmp_path)
    keeper.joining()
    keeper.boot_done("nothing")
    net.online = False
    run(clock, keeper, JOIN_MAX_S + 5)
    assert keeper.look == policy.NO_INTERNET     # 5 min on: the usual rules say so...
    net.online = True
    run(clock, keeper, 2)
    assert keeper.look == policy.READY           # ...and it clears by itself


def test_joining_never_hides_a_phone_or_music():
    state = BoxState(joining=True, casting=True)
    assert policy.look_for(state)[0] == policy.CASTING
    assert policy.look_for(BoxState(joining=True))[0] == policy.BUSY
    assert policy.look_for(BoxState(joining=True, hotspot=True))[0] == policy.SETUP


def test_main_starts_joining_only_when_a_join_note_waits(tmp_path):
    marker = tmp_path / "wifi-joined"
    app = type("App", (), {})()
    app.wifi = type("W", (), {"joined_pending": lambda self: marker.is_file()})()
    assert main_mod.wifi_join_pending(app) is False
    marker.write_text("Casa\nfrom=Vieja\n")
    assert main_mod.wifi_join_pending(app) is True
    assert main_mod.wifi_join_pending(type("Bare", (), {})()) is False


# -- the watchdog: no network and no hotspot at boot -> the hotspot ---------------------------


class Wifi:
    def __init__(self, result="up"):
        self.calls, self.result = [], result

    def hotspot_now(self):
        self.calls.append("hotspot-now")
        if self.result == "error":
            raise ValueError("the setup network did not come up")
        return {"ok": True, "result": self.result}

    def refresh(self):
        pass

    def ensure_hotspot(self):
        pass


def watch(linked, offline=True, result="up"):
    clock = Clock()
    wifi = Wifi(result)
    state = {"linked": linked, "offline": offline, "hotspot": False}
    w = HotspotWatchdog(wifi, hotspot=lambda: state["hotspot"], offline=lambda: state["offline"],
                        linked=lambda: state["linked"], threaded=False, clock=clock)
    return clock, wifi, state, w


def tick_for(clock, w, seconds, step=5.0):
    end = clock.t + seconds
    while clock.t < end:
        w.poll(clock())
        clock.t += step


def test_no_network_and_no_hotspot_at_boot_brings_the_hotspot_up_once():
    clock, wifi, state, w = watch(linked=False)
    tick_for(clock, w, wifiwatch.NO_WIFI_S - 10)
    assert wifi.calls == []                  # moOde's worker gets its go first
    tick_for(clock, w, 30)
    assert wifi.calls == ["hotspot-now"] and w.status()["fallback"] == "up"
    tick_for(clock, w, 600)
    assert wifi.calls == ["hotspot-now"]     # once per start


@pytest.mark.parametrize("linked, offline", [(None, True), (True, True), (False, False)])
def test_the_fallback_waits_when_it_is_not_needed_or_not_known(linked, offline):
    clock, wifi, _, w = watch(linked=linked, offline=offline)
    tick_for(clock, w, 600)
    assert wifi.calls == []


def test_a_radio_that_was_on_a_network_never_drops_into_setup_mode():
    clock, wifi, state, w = watch(linked=True)
    tick_for(clock, w, 60)
    state["linked"] = False                  # the router reboots at night
    tick_for(clock, w, 900)
    assert wifi.calls == []


def test_a_hotspot_already_up_is_no_reason():
    clock, wifi, state, w = watch(linked=False)
    state["hotspot"] = True
    tick_for(clock, w, 150)
    state["hotspot"] = False
    tick_for(clock, w, 300)
    assert wifi.calls == []


def test_a_hotspot_that_will_not_start_is_said_once():
    clock, wifi, _, w = watch(linked=False, result="error")
    tick_for(clock, w, 600)
    assert wifi.calls == ["hotspot-now"] and w.fallback == "failed"


# -- the link probe ----------------------------------------------------------------------------


def test_link_probe_counts_a_real_address_not_the_hotspots(monkeypatch):
    monkeypatch.setattr(net_mod.sys, "platform", "linux")
    addrs = {"wlan0": "", "eth0": ""}
    monkeypatch.setattr(net_mod, "iface_ipv4", lambda i: addrs[i])
    assert net_mod.link_probe() is False
    addrs["wlan0"] = "172.24.1.1"            # the setup hotspot is not a network
    assert net_mod.link_probe() is False
    addrs["wlan0"] = "192.168.1.40"
    assert net_mod.link_probe() is True
    monkeypatch.setattr(net_mod.sys, "platform", "darwin")
    assert net_mod.link_probe() is None


def test_connectivity_carries_the_link():
    conn = net_mod.Connectivity(probe=lambda: False, hotspot=lambda: False, link=lambda: False, threaded=False)
    conn.poll(0.0)
    assert conn.online is False and conn.linked is False
    conn = net_mod.Connectivity(probe=lambda: True, hotspot=lambda: False, link=lambda: None, threaded=False)
    conn.poll(0.0)
    assert conn.linked is None
