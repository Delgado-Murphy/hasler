"""Moving the radio (docs/using.md "Moving the radio"): forgetting a Wi-Fi
network, the setup network named after the room, the wifi-joined marker and
what the next start does with it, Clock and place, the wifi_setup key job,
Reset keys' Undo. The root helper is stubbed; what only the radio can show
(nmcli, moOde's tables, the restart) is not here."""

from __future__ import annotations

import json
import subprocess
import time

import pytest
from conftest import VARIANTS
from test_web_confirm import Clock, call

from haslerd import main as main_mod
from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import clock as clock_mod
from haslerd.web import confirm as confirm_mod
from haslerd.web.room import Room
from haslerd.web.server import KEYS_BAK, WebApp, WebServer, load_settings
from haslerd.web.wifi import Wifi


class Helper:
    """The root helper, answering from a script: (payload, reply) pairs by action."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = replies or {}

    def __call__(self, cmd, **kw):
        if cmd[0] == "nmcli":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        payload = json.loads(kw["input"])
        self.calls.append(payload)
        reply = self.replies.get(payload.get("action", "join"), {"ok": True})
        if callable(reply):
            reply = reply(payload)
        return subprocess.CompletedProcess(cmd, 0 if reply.get("ok") else 1, json.dumps(reply) + "\n", "")


def wifi_with(tmp_path, helper, saved=("Casa Vieja", "Movil")):
    w = Wifi(runner=helper, hotspot=lambda: False, hotspot_file=tmp_path / "hotspot.txt",
             joined_file=tmp_path / "wifi-joined")
    w.saved_ssids = lambda: list(saved)
    w.current_ssid = lambda: saved[0] if saved else ""
    w.hotspot_ssid = lambda: "Hasler"
    return w


# -- forget ----------------------------------------------------------------------


def test_known_lists_saved_networks_current_first_and_flagged(tmp_path):
    w = wifi_with(tmp_path, Helper())
    assert w.known() == [{"ssid": "Casa Vieja", "current": True}, {"ssid": "Movil", "current": False}]


def test_forget_hands_the_ssid_to_the_helper_and_says_whether_it_restarts(tmp_path):
    helper = Helper({"forget": lambda p: {"ok": True, "forgotten": p["ssid"], "was_current": p["ssid"] == "Casa Vieja",
                                          "restarting": p["ssid"] == "Casa Vieja"}})
    w = wifi_with(tmp_path, helper)
    assert w.forget("Movil") == {"forgotten": "Movil", "was_current": False, "restarting": False}
    assert w.forget("Casa Vieja") == {"forgotten": "Casa Vieja", "was_current": True, "restarting": True}
    assert helper.calls == [{"action": "forget", "ssid": "Movil", "reboot": True},
                            {"action": "forget", "ssid": "Casa Vieja", "reboot": True}]


@pytest.mark.parametrize("ssid, why", [("", "1–32"), ("Nadie", "doesn't know"), ("Hasler", "setup network")])
def test_forget_refuses_what_it_should_before_sudo(tmp_path, ssid, why):
    helper = Helper()
    w = wifi_with(tmp_path, helper)
    with pytest.raises(ValueError, match=why):
        w.forget(ssid)
    assert helper.calls == []


def test_forget_all_reports_what_went_and_drops_the_marker(tmp_path):
    helper = Helper({"forget-all": {"ok": True, "forgotten": ["Casa Vieja", "Movil"]}})
    w = wifi_with(tmp_path, helper)
    w.mark_joined("Casa Vieja")
    assert w.forget_all() == {"forgotten": ["Casa Vieja", "Movil"]}
    assert helper.calls == [{"action": "forget-all"}]
    assert w.take_joined() is None


def test_forget_passes_the_helpers_refusal_on(tmp_path):
    w = wifi_with(tmp_path, Helper({"forget": {"ok": False, "error": "cannot open the moOde database"}}))
    with pytest.raises(ValueError, match="moOde database"):
        w.forget("Movil")


# -- the marker ------------------------------------------------------------------------


def test_join_and_retry_leave_the_marker_and_take_joined_reads_it_once(tmp_path):
    helper = Helper({"retry": {"ok": True, "result": "joined", "ssid": "Casa Nueva"}})
    w = wifi_with(tmp_path, helper)
    w.join("Casa Nueva", "longenough", "wpa-psk")
    assert (tmp_path / "wifi-joined").read_text() == "Casa Nueva\nfrom=Casa Vieja\n"
    assert w.joined_pending()
    assert w.take_joined() == "Casa Nueva" and w.joined_from == "Casa Vieja"
    assert not w.joined_pending()
    assert w.take_joined() is None and not (tmp_path / "wifi-joined").exists()
    w.retry()
    assert w.take_joined() == "Casa Nueva"


def test_an_old_marker_without_from_still_reads(tmp_path):
    w = wifi_with(tmp_path, Helper())
    (tmp_path / "wifi-joined").write_text("Casa Nueva\n")      # the helper before 1.6.4
    assert w.take_joined() == "Casa Nueva" and w.joined_from is None


def test_the_network_it_is_on_chosen_again_saves_without_a_restart(tmp_path):
    """The owner re-typed the password of the Wi-Fi the radio is on (Handover sent
    them to the Wi-Fi page): the helper stores the key in place - no restart, no
    marker, so no "New Wi-Fi" and no join breath at a next start."""
    helper = Helper({"join": {"ok": True, "same_network": True, "changed": False, "restarting": False}})
    w = wifi_with(tmp_path, helper)
    assert w.join("Casa Vieja", "longenough", "wpa-psk") == {
        "ssid": "Casa Vieja", "same_network": True, "changed": False, "restarting": False}
    assert not (tmp_path / "wifi-joined").exists()
    helper.replies["join"] = {"ok": True, "same_network": True, "changed": True, "restarting": False}
    assert w.join("Casa Vieja", "longenough2", "wpa-psk")["changed"] is True
    assert not (tmp_path / "wifi-joined").exists()


def test_a_new_password_the_network_refuses_is_said_and_nothing_restarts(tmp_path):
    helper = Helper({"join": {"ok": False, "same_network": True, "refused": True, "restarting": False,
                              "error": "the network refused that password - the radio kept the one that works"}})
    w = wifi_with(tmp_path, helper)
    with pytest.raises(ValueError, match="kept the one that works"):
        w.join("Casa Vieja", "wrongpassword", "wpa-psk")
    assert not (tmp_path / "wifi-joined").exists()


def test_another_network_still_restarts_and_leaves_the_marker(tmp_path):
    w = wifi_with(tmp_path, Helper())
    assert w.join("Casa Nueva", "longenough", "wpa-psk")["restarting"] is True
    assert w.joined_pending()


def test_a_failed_join_leaves_no_marker(tmp_path):
    w = wifi_with(tmp_path, Helper({"join": {"ok": False, "error": "a WPA password is 8-63 characters"}}))
    with pytest.raises(ValueError):
        w.join("Casa Nueva", "x" * 9, "wpa-psk")
    assert w.take_joined() is None


# -- the setup network's name ----------------------------------------------------------


@pytest.mark.parametrize("room, name", [(None, "Hasler"), ("", "Hasler"), ("Kitchen", "Hasler-Kitchen"),
                                        ("Hasler Study", "Hasler-Study"), ("x" * 40, "Hasler-" + "x" * 25), ("Living  room", "Hasler-Living-room")])
def test_hotspot_name_follows_the_room(room, name):
    assert Wifi.hotspot_name_for(room) == name


def test_room_name_renames_the_hotspot_only_with_a_known_password(tmp_path):
    helper = Helper({"hotspot": lambda p: {"ok": True, "ssid": p.get("ssid", "Hasler"), "renamed": True,
                                           "changed": True, "password_used": True}})
    w = wifi_with(tmp_path, helper)
    w.room_name = lambda: "Kitchen"
    assert w.apply_hotspot_name() is None and helper.calls == []       # no password known: nothing sent
    (tmp_path / "hotspot.txt").write_text("ssid=Hasler\npassword=abcd-efgh-jkmn\n")
    reply = w.apply_hotspot_name()
    assert reply["ssid"] == "Hasler-Kitchen"
    assert helper.calls == [{"action": "hotspot", "password": "abcd-efgh-jkmn", "ssid": "Hasler-Kitchen"}]
    # hotspot.txt follows, so the key page still shows the password under the new name.
    assert "ssid=Hasler-Kitchen" in (tmp_path / "hotspot.txt").read_text()
    w.hotspot_ssid = lambda: "Hasler-Kitchen"
    assert w.apply_hotspot_name() is None                               # already right: nothing sent
    assert len(helper.calls) == 1


def test_ensure_hotspot_applies_the_rooms_name_after_its_check(tmp_path):
    helper = Helper({"hotspot": lambda p: {"ok": True, "ssid": p.get("ssid", "Hasler"), "changed": bool(p.get("password")),
                                           "password_used": bool(p.get("ensure"))}})
    w = wifi_with(tmp_path, helper)
    w.room_name = lambda: "Study"
    w.ensure_hotspot()
    assert [c.get("ssid") for c in helper.calls] == [None, "Hasler-Study"]
    assert helper.calls[1]["password"] == helper.calls[0]["password"]   # the one ensure just chose


def test_a_new_setup_password_is_legible_and_kept(tmp_path):
    helper = Helper({"hotspot": lambda p: {"ok": True, "ssid": p["ssid"], "changed": True, "password_used": True}})
    w = wifi_with(tmp_path, helper)
    doc = w.set_hotspot_password(None)
    assert doc["ssid"] == "Hasler" and len(doc["password"]) == 14 and doc["password"].count("-") == 2
    assert all(c in "abcdefghjkmnpqrstuvwxyz23456789-" for c in doc["password"])
    assert f"password={doc['password']}" in (tmp_path / "hotspot.txt").read_text()


def test_hotspot_now_asks_the_helper(tmp_path):
    helper = Helper({"hotspot-now": {"ok": True, "result": "up", "ssid": "Hasler"}})
    w = wifi_with(tmp_path, helper)
    assert w.hotspot_now()["result"] == "up"
    assert helper.calls == [{"action": "hotspot-now"}]


def test_room_rename_reaches_the_wifi(tmp_path):
    seen = []
    room = Room(tmp_path / "settings.json")
    room.on_renamed = seen.append
    room.commit(room.change({"name": "Kitchen"}), {"name": "Kitchen"})
    deadline = time.monotonic() + 2
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == ["Kitchen"]


def test_room_doc_carries_the_ip_beside_the_address(tmp_path):
    room = Room(tmp_path / "settings.json", avahi_host=lambda: "hasler-ab12.local", own_ip=lambda: "192.168.1.23")
    doc = room.doc()
    assert doc["address"] == "hasler-ab12.local" and doc["ip"] == "192.168.1.23"


# -- the next start: celebrate, peers, Home Assistant, the page's note ----------------------


class Peers:
    def __init__(self):
        self.kicked = []

    def kick(self, force=False):
        self.kicked.append(force)


class Keeper:
    def __init__(self):
        self.celebrated = 0

    def celebrate(self):
        self.celebrated += 1


class HaApi:
    def __init__(self):
        self.ups = 0

    def network_up(self):
        self.ups += 1


def make_app(tmp_path, allow_unconfirmed=False):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    app.confirm.clock = Clock()
    app.confirm.allow_unconfirmed = allow_unconfirmed
    app.wifi = wifi_with(tmp_path, Helper())
    return app


def test_consume_wifi_joined_does_what_the_guide_promises(tmp_path):
    app = make_app(tmp_path)
    keeper, ha = Keeper(), HaApi()
    app.ha_api = ha
    group = type("G", (), {"enabled": True, "peers": Peers()})()
    app.group = group
    assert main_mod.consume_wifi_joined(app, keeper) is None            # nothing joined: nothing happens
    app.wifi.mark_joined("Casa Nueva")
    assert main_mod.consume_wifi_joined(app, keeper) == "Casa Nueva"
    assert keeper.celebrated == 1 and group.peers.kicked == [True] and ha.ups == 1
    note = load_settings(tmp_path / "settings.json")["wifi_joined"]
    assert note["ssid"] == "Casa Nueva" and abs(note["at"] - time.time()) < 5
    assert app.settings()["wifi_joined"]["ssid"] == "Casa Nueva"
    assert main_mod.consume_wifi_joined(app, keeper) is None            # once


def test_the_same_network_again_is_no_new_wifi(tmp_path):
    """A restart onto the network it was already on (a join from before this
    release, the watchdog back on the home Wi-Fi): the lamp still says OK, but
    Settings doesn't ask "New Wi-Fi: … Moved house?"."""
    app = make_app(tmp_path)
    keeper = Keeper()
    app.wifi.mark_joined("Casa Vieja", "Casa Vieja")
    assert main_mod.consume_wifi_joined(app, keeper) == "Casa Vieja"
    assert keeper.celebrated == 1
    assert "wifi_joined" not in load_settings(tmp_path / "settings.json")
    assert app.settings()["wifi_joined"] is None
    app.wifi.mark_joined("Casa Nueva", "Casa Vieja")               # a different one: said
    assert main_mod.consume_wifi_joined(app, keeper) == "Casa Nueva"
    assert app.settings()["wifi_joined"]["ssid"] == "Casa Nueva"


def test_consume_wifi_joined_leaves_peers_alone_when_multiroom_is_off(tmp_path):
    app = make_app(tmp_path)
    group = type("G", (), {"enabled": False, "peers": Peers()})()
    app.group = group
    app.wifi.mark_joined("Casa Nueva")
    assert main_mod.consume_wifi_joined(app, None) == "Casa Nueva"
    assert group.peers.kicked == []


def test_the_joined_note_fades_after_a_day(tmp_path):
    app = make_app(tmp_path)
    app.note_wifi_joined("Casa Nueva")
    doc = load_settings(tmp_path / "settings.json")
    doc["wifi_joined"]["at"] = time.time() - 2 * 24 * 3600
    (tmp_path / "settings.json").write_text(json.dumps(doc))
    assert app.settings()["wifi_joined"] is None


# -- the routes: a press from the home network, none from the setup hotspot --------------------


@pytest.fixture
def served(tmp_path):
    app = make_app(tmp_path)
    helper = Helper({"forget": lambda p: {"ok": True, "forgotten": p["ssid"], "was_current": False, "restarting": False}})
    app.wifi = wifi_with(tmp_path, helper)
    app.wifi.helper_calls = helper.calls
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"127.0.0.1:{server.port}", app
    server.close()


def test_forget_from_home_needs_a_press_with_the_ssid_in_the_question(served):
    hp, app = served
    status, doc = call(hp, "DELETE", "/api/wifi/networks/Movil", {}, {"Origin": f"http://{hp}"})
    assert status == 403
    status, ask = call(hp, "POST", "/api/confirm/request", {"action": "wifi", "args": {"ssid": "Movil", "forget": True}},
                       {"Origin": f"http://{hp}"})
    assert status == 200 and ask["state"] == "pending"
    app.confirm.intercept([Event("k2", "tap")])
    status, doc = call(hp, "DELETE", "/api/wifi/networks/Movil", {"confirm": ask["token"]}, {"Origin": f"http://{hp}"})
    assert status == 200 and doc["forgotten"] == "Movil" and doc["restarting"] is False
    assert app.wifi.helper_calls == [{"action": "forget", "ssid": "Movil", "reboot": True}]


def test_forget_from_the_setup_hotspot_needs_no_press(served):
    hp, app = served
    app.hotspot_addrs = {"127.0.0.1"}          # this test's socket stands in for 172.24.1.1
    status, doc = call(hp, "DELETE", "/api/wifi/networks/Casa%20Vieja", {}, {"Origin": f"http://{hp}"})
    assert status == 200 and doc["forgotten"] == "Casa Vieja"


def test_api_wifi_lists_the_known_networks(served):
    hp, _ = served
    status, doc = call(hp, "GET", "/api/wifi")
    assert status == 200 and doc["known"] == [{"ssid": "Casa Vieja", "current": True}, {"ssid": "Movil", "current": False}]


# -- Clock and place ---------------------------------------------------------------------------


def test_timezone_names_are_checked_against_the_system(tmp_path):
    zones = tmp_path / "zoneinfo"
    (zones / "Europe").mkdir(parents=True)
    (zones / "Europe" / "Madrid").write_text("")
    assert clock_mod.timezone_from("Europe/Madrid", zoneinfo=zones) == "Europe/Madrid"
    for bad in ("", "../etc/passwd", "Europe/Paris", "Europe Madrid", 5):
        with pytest.raises(ValueError):
            clock_mod.timezone_from(bad, zoneinfo=zones)


def test_set_timezone_goes_through_the_root_helper():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    clock_mod.set_timezone("Europe/Zurich", runner=run)
    assert calls == [["sudo", "-n", "/usr/local/sbin/hasler-timezone", "Europe/Zurich"]]

    def refuse(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "unknown time zone: Mars/Olympus\n")

    with pytest.raises(ValueError, match="unknown time zone"):
        clock_mod.set_timezone("Mars/Olympus", runner=refuse)


def test_current_timezone_falls_back_to_etc_timezone(tmp_path):
    etc = tmp_path / "timezone"
    etc.write_text("Europe/Lisbon\n")

    def no_timedatectl(cmd, **kw):
        raise OSError("no such file")

    assert clock_mod.current_timezone(runner=no_timedatectl, etc_timezone=etc) == "Europe/Lisbon"
    assert clock_mod.doc("Europe/Lisbon")["zones"][0] == "Europe/Madrid"
    assert clock_mod.doc("Pacific/Auckland")["zones"][0] == "Pacific/Auckland"


def test_settings_carry_the_clock_and_put_changes_the_zone(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(clock_mod, "current_timezone", lambda: "Europe/Madrid")
    monkeypatch.setattr(clock_mod, "timezone_from", lambda raw: raw)
    sets = []
    monkeypatch.setattr(clock_mod, "set_timezone", sets.append)
    assert app.settings()["clock"]["timezone"] == "Europe/Madrid"
    doc = app.put_settings({"timezone": "Europe/Zurich", "location": {"name": "Zurich", "lat": 47.3769, "lon": 8.5417}})
    assert sets == ["Europe/Zurich"] and doc["clock"]["timezone"] == "Europe/Zurich"
    assert load_settings(tmp_path / "settings.json")["location"]["name"] == "Zurich"
    app.put_settings({"timezone": "Europe/Zurich"})
    assert sets == ["Europe/Zurich"]                                    # the same zone again: no helper call
    doc = app.put_settings({"dismissed": {"clock_offer": "http://ha.local:8123"}})
    assert doc["dismissed"]["clock_offer"] == "http://ha.local:8123"


# -- the wifi_setup key job ------------------------------------------------------------------


def test_wifi_setup_is_a_verb_the_page_can_bind_and_a_key_runs_it(tmp_path):
    app = make_app(tmp_path)
    assert any(v["do"] == "wifi_setup" for v in app.config()["verbs"])
    doc = {"keys": [{"id": "k3", "tap": {"do": "preset", "n": 3}, "hold": {"do": "wifi_setup"}}], "presets": app.config()["presets"]}
    app.save({**doc, "keys": [{k: v for k, v in key.items()} for key in doc["keys"]]})
    assert app.actions.variant.actions["k3.hold"] == {"do": "wifi_setup"}
    ran = []
    app.actions.wifi_setup = lambda: ran.append(True)
    app.actions.dispatch(Event("k3", "hold"))
    deadline = time.monotonic() + 2
    while not ran and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ran == [True]


def test_wifi_setup_without_a_helper_does_nothing_quietly(tmp_path):
    app = make_app(tmp_path)
    app.actions.variant.actions["k3.hold"] = {"do": "wifi_setup"}
    app.actions.dispatch(Event("k3", "hold"))      # no hook: a log line, no exception


# -- Reset keys keeps a day's layout for Undo ---------------------------------------------------


def test_reset_keeps_the_layout_a_day_and_undo_brings_it_back(tmp_path):
    app = make_app(tmp_path, allow_unconfirmed=True)
    cfg = app.config()
    doc = {"keys": [{k: v for k, v in key.items() if k != "label"} for key in cfg["keys"]], "presets": cfg["presets"]}
    doc["keys"][0]["hold"] = {"do": "volume_up"}
    hold = lambda: next(k for k in app.config()["keys"] if k["id"] == "k1").get("hold")   # noqa: E731
    app.save(doc)
    assert hold() == {"do": "volume_up"}
    app.reset()
    assert (tmp_path / KEYS_BAK).exists() and not (tmp_path / "keys.yaml").exists()
    assert hold() != {"do": "volume_up"}
    app.reset_undo()
    assert hold() == {"do": "volume_up"}
    assert (tmp_path / "keys.yaml").exists() and not (tmp_path / KEYS_BAK).exists()
    with pytest.raises(ValueError, match="no layout to bring back"):
        app.reset_undo()


def test_undo_refuses_a_reset_older_than_a_day(tmp_path):
    import os
    app = make_app(tmp_path, allow_unconfirmed=True)
    (tmp_path / "keys.yaml").write_text("schema: 1\n")
    app.reset()
    old = time.time() - 25 * 3600
    os.utime(tmp_path / KEYS_BAK, (old, old))
    with pytest.raises(ValueError, match="more than a day"):
        app.reset_undo()
    assert not (tmp_path / KEYS_BAK).exists()


def test_handover_is_always_a_real_press():
    assert "handover" in confirm_mod.ACTIONS and "handover" in confirm_mod.ALWAYS_PRESS
