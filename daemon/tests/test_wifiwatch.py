"""The hotspot watchdog: out of setup mode by itself when the Wi-Fi is back."""

from haslerd import wifiwatch
from haslerd.net import HOTSPOT_ADDRS, hotspot_address
from haslerd.wifiwatch import HotspotWatchdog


class FakeWifi:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []
        self.refreshed = 0

    def retry(self, force=False):
        self.calls.append(force)
        return self.replies.pop(0) if self.replies else {"ok": True, "result": "not-visible"}

    def refresh(self):
        self.refreshed += 1


def make(*replies, hotspot=True, idle=True, offline=False):
    state = {"hotspot": hotspot, "idle": idle, "offline": offline, "t": 0.0}
    wifi = FakeWifi(*replies)
    dog = HotspotWatchdog(wifi, hotspot=lambda: state["hotspot"], idle=lambda: state["idle"],
                          offline=lambda: state["offline"], threaded=False, clock=lambda: state["t"])
    return dog, wifi, state


def run(dog, state, until, step=1.0):
    while state["t"] < until:
        dog.poll(state["t"])
        state["t"] += step


def test_waits_before_the_first_try_then_backs_off():
    dog, wifi, state = make()
    run(dog, state, wifiwatch.FIRST_S - 1)
    assert wifi.calls == []                       # a phone may be on its way
    run(dog, state, wifiwatch.FIRST_S + 1)
    assert wifi.calls == [False]
    t1 = state["t"]
    run(dog, state, t1 + wifiwatch.EVERY_S[0] + 1)
    assert len(wifi.calls) == 2
    t2 = state["t"]
    run(dog, state, t2 + wifiwatch.EVERY_S[0] + 1)
    assert len(wifi.calls) == 2                   # the second wait is longer
    run(dog, state, t2 + wifiwatch.EVERY_S[1] + 1)
    assert len(wifi.calls) == 3
    assert dog.status()["last"]["result"] == "not-visible" and dog.status()["active"]


def test_keeps_trying_for_as_long_as_it_takes():
    dog, wifi, state = make()
    run(dog, state, 6 * 3600, step=10.0)          # a router that stays off for six hours
    assert len(wifi.calls) >= 6 * 3600 / wifiwatch.EVERY_S[-1] - 1


def test_a_phone_on_the_hotspot_or_music_puts_it_off_without_backing_off():
    dog, wifi, state = make({"ok": True, "result": "busy", "stations": 1})
    run(dog, state, wifiwatch.FIRST_S + 1)
    assert wifi.calls == [False]
    run(dog, state, state["t"] + wifiwatch.BUSY_S + 1)
    assert len(wifi.calls) == 2 and dog.attempts == 1     # busy is not an attempt
    state["idle"] = False                         # Bluetooth playing: not now
    run(dog, state, state["t"] + wifiwatch.EVERY_S[0] + 5)
    assert len(wifi.calls) == 2


def test_wrong_password_waits_for_a_person_and_nothing_saved_stops():
    dog, wifi, state = make({"ok": True, "result": "failed", "reason": "secrets"})
    run(dog, state, wifiwatch.FIRST_S + 1)
    run(dog, state, state["t"] + wifiwatch.EVERY_S[-1] + 1)
    assert len(wifi.calls) == 1
    assert 0 < dog.status()["next_in"] <= wifiwatch.WRONG_PASSWORD_S

    dog, wifi, state = make({"ok": True, "result": "no-network"})
    run(dog, state, 4 * 3600, step=10.0)
    assert wifi.calls == [False] and dog.status()["stopped"] == "no-network"
    assert dog.status()["next_in"] is None


def test_a_new_hotspot_session_starts_over_and_while_offline_it_keeps_the_scan_fresh():
    dog, wifi, state = make({"ok": True, "result": "no-network"})
    run(dog, state, wifiwatch.FIRST_S + 1)
    state["hotspot"], state["offline"] = False, True
    run(dog, state, state["t"] + 61)
    assert wifi.refreshed >= 2 and not dog.status()["active"] and dog.status()["stopped"] == ""
    state["hotspot"] = True
    start = state["t"]
    run(dog, state, start + wifiwatch.FIRST_S + 1)
    assert len(wifi.calls) == 2


def test_try_now_is_the_pages_button_and_forces():
    dog, wifi, state = make({"ok": True, "result": "joined", "ssid": "Home"})
    assert dog.try_now(force=True)["result"] == "joined"
    assert wifi.calls == [True]


def test_errors_are_retried_later_not_in_a_loop():
    dog, wifi, state = make({"ok": False, "error": "could not run the Wi-Fi helper"})
    run(dog, state, wifiwatch.FIRST_S + 60)
    assert len(wifi.calls) == 1 and dog.status()["last"]["result"] == "error"


def test_hotspot_address_knows_both_addresses():
    assert HOTSPOT_ADDRS == ("172.24.1.1", "10.42.0.1")
    assert hotspot_address(("127.0.0.1",)) == "127.0.0.1"
    assert hotspot_address(("192.0.2.123",)) == ""        # TEST-NET: never ours


def test_the_hotspot_check_runs_once_soon_after_start():
    dog, wifi, state = make(hotspot=False)
    wifi.ensured = 0

    def ensure():
        wifi.ensured += 1
    wifi.ensure_hotspot = ensure
    run(dog, state, wifiwatch.ENSURE_AFTER_S - 1)
    assert wifi.ensured == 0
    run(dog, state, 3600, step=5.0)
    assert wifi.ensured == 1
