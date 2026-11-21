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


def test_every_minute_for_ten_minutes_then_every_five_for_an_hour_then_every_ten():
    dog, wifi, state = make()
    run(dog, state, wifiwatch.FIRST_S - 1)
    assert wifi.calls == []
    run(dog, state, wifiwatch.FIRST_S + 1)
    assert wifi.calls == [False]                  # a minute in
    run(dog, state, 600)
    assert 9 <= len(wifi.calls) <= 10             # every minute for the first ten
    n = len(wifi.calls)
    run(dog, state, 600 + 3600)
    assert 11 <= len(wifi.calls) - n <= 13        # every five for the hour after
    n = len(wifi.calls)
    run(dog, state, 600 + 3600 + 3600)
    assert 5 <= len(wifi.calls) - n <= 7          # then every ten
    assert dog.status()["last"]["result"] == "not-visible" and dog.status()["active"]


def test_retry_delay():
    assert [wifiwatch.retry_delay(t) for t in (0, 599, 600, 4199, 4200, 86400)] == [60, 60, 300, 300, 600, 600]


def test_keeps_trying_for_as_long_as_it_takes():
    dog, wifi, state = make()
    run(dog, state, 6 * 3600, step=10.0)          # a router that stays off for six hours
    assert len(wifi.calls) >= 6 * 3600 / wifiwatch.LATER_S


def test_a_phone_on_the_hotspot_or_music_puts_it_off_without_backing_off():
    dog, wifi, state = make({"ok": True, "result": "busy", "stations": 1})
    run(dog, state, wifiwatch.FIRST_S + 1)
    assert wifi.calls == [False]
    run(dog, state, state["t"] + wifiwatch.BUSY_S + 1)
    assert len(wifi.calls) == 2 and dog.attempts == 1     # busy is not an attempt
    state["idle"] = False                         # Bluetooth playing: not now
    run(dog, state, state["t"] + 300 + 5)
    assert len(wifi.calls) == 2


def test_wrong_password_waits_for_a_person_and_nothing_saved_stops():
    dog, wifi, state = make({"ok": True, "result": "failed", "reason": "secrets"})
    run(dog, state, wifiwatch.FIRST_S + 1)
    run(dog, state, state["t"] + wifiwatch.LATER_S + 1)
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


def test_being_put_off_is_logged_once_per_reason(caplog):
    import logging
    dog, _wifi, state = make(*[{"ok": True, "result": "busy", "stations": 1}] * 5)
    with caplog.at_level(logging.INFO, logger="haslerd.wifiwatch"):
        run(dog, state, wifiwatch.FIRST_S + 5 * wifiwatch.BUSY_S + 5)
    put_off = [r for r in caplog.messages if "not looking" in r]
    assert len(put_off) == 1 and "setup network" in put_off[0]
    assert any("looking for the saved Wi-Fi again" in r for r in caplog.messages)
