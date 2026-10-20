"""Phones over Bluetooth: BlueZ/BlueALSA through busctl (faked), the
Bluetooth key (again = next, hold = pause/play), reconnecting the last phone
on select and on wake, and /api/bluetooth. No Pi, no D-Bus."""

import json

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions, bt_settings_from
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.player.bluetooth import (Bluetooth, BluetoothError, FakeBluetooth, codec_label,
                                      parse_objects, valid_address)
from haslerd.state import StateStore
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.bluetooth_api import BluetoothApi, apply_saved
from haslerd.web.server import Unavailable, WebApp

PHONE = "B0:9C:B2:EB:15:F9"
TABLET = "11:22:33:44:55:66"
PHONE_PATH = "/org/bluez/hci0/dev_B0_9C_B2_EB_15_F9"
TABLET_PATH = "/org/bluez/hci0/dev_11_22_33_44_55_66"


def v(t, data):
    return {"type": t, "data": data}


def device(address, name, connected=False, paired=True, icon="phone", uuids=None):
    return {
        "org.freedesktop.DBus.Introspectable": {},
        "org.bluez.Device1": {
            "Address": v("s", address), "Name": v("s", name), "Alias": v("s", name),
            "Icon": v("s", icon), "Paired": v("b", paired), "Bonded": v("b", paired),
            "Trusted": v("b", False), "Connected": v("b", connected),
            "UUIDs": v("as", uuids if uuids is not None else ["0000110a-0000-1000-8000-00805f9b34fb"]),
        },
    }


def bluez_json(phone_connected=False, status="playing", battery=None, tablet=True, player=True):
    objects = {
        "/org/bluez": {"org.bluez.AgentManager1": {}},
        "/org/bluez/hci0": {"org.bluez.Adapter1": {
            "Address": v("s", "D8:3A:DD:46:12:8A"), "Name": v("s", "Hasler"),
            "Alias": v("s", "Hasler"), "Powered": v("b", True)}},
        PHONE_PATH: device(PHONE, "Pixel 11 Pro XL", connected=phone_connected),
        "/org/bluez/hci0/dev_AA_AA_AA_AA_AA_AA": device("AA:AA:AA:AA:AA:AA", "Stranger", paired=False),
    }
    if tablet:
        objects[TABLET_PATH] = device(TABLET, "Tablet", icon="computer")
    if battery is not None:
        objects[PHONE_PATH]["org.bluez.Battery1"] = {"Percentage": v("y", battery)}
    if phone_connected and player:
        objects[PHONE_PATH + "/player0"] = {"org.bluez.MediaPlayer1": {
            "Status": v("s", status),
            "Track": v("a{sv}", {"Title": v("s", "Song"), "Artist": v("s", "Band")}),
        }}
    return json.dumps({"type": "a{oa{sa{sv}}}", "data": [objects]})


def bluealsa_json(codec="aptX-HD", rate=48000):
    return json.dumps({"type": "a{oa{sa{sv}}}", "data": [{
        PHONE_PATH.replace("/org/bluez", "/org/bluealsa") + "/a2dpsnk/source": {"org.bluealsa.PCM1": {
            "Device": v("o", PHONE_PATH), "Transport": v("s", "A2DP-sink"), "Mode": v("s", "source"),
            "Running": v("b", True), "Codec": v("s", codec), "Rate": v("u", rate)}},
    }]})


class Bus:
    """busctl, faked: answers by the call's shape, records every call."""

    def __init__(self, **kw):
        self.kw = kw
        self.calls = []
        self.fail = {}        # method name -> (rc, stderr)
        self.on_connect = None

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if "GetManagedObjects" in args:
            if "org.bluealsa" in args:
                return 0, bluealsa_json(), ""
            if self.kw.get("down"):
                return 1, "", "Could not activate remote peer"
            return 0, bluez_json(**self.kw), ""
        method = args[-1] if args[-2] != "o" else args[-3]
        if method in self.fail:
            rc, err = self.fail[method]
            return rc, "", err
        if method == "Connect":
            if self.on_connect:
                self.on_connect(args)
            self.kw["phone_connected"] = args[2] == PHONE_PATH or self.kw.get("phone_connected", False)
        return 0, "", ""

    def methods(self):
        return [c for c in self.calls if "GetManagedObjects" not in c]


def make(tmp_path=None, **kw):
    bus = Bus(**kw)
    clock = [1000.0]
    wall = [1_760_000_000.0]
    bt = Bluetooth(state_dir=tmp_path, run=bus, clock=lambda: clock[0], wall=lambda: wall[0],
                   spawn=lambda fn, name: fn())
    return bt, bus, clock, wall


# -- parsing ----------------------------------------------------------------


class TestParsing:
    def test_objects_unwrap_variants(self):
        objects = parse_objects(bluez_json(phone_connected=True))
        assert objects[PHONE_PATH]["org.bluez.Device1"]["Name"] == "Pixel 11 Pro XL"
        track = objects[PHONE_PATH + "/player0"]["org.bluez.MediaPlayer1"]["Track"]
        assert track == {"Title": "Song", "Artist": "Band"}

    @pytest.mark.parametrize("raw", ["", "nope", "{}", '{"data": 3}'])
    def test_garbage_is_empty(self, raw):
        assert parse_objects(raw) == {}

    @pytest.mark.parametrize("raw,label", [("SBC", "SBC"), ("aptX", "aptX"), ("aptX-HD", "aptX HD"),
                                           ("LDAC", "LDAC"), ("AAC", "AAC"), ("Weird", "Weird"), ("", None)])
    def test_codec_labels(self, raw, label):
        assert codec_label(raw) == label

    @pytest.mark.parametrize("raw", ["b0:9c:b2:eb:15:f9", " B0:9C:B2:EB:15:F9 "])
    def test_addresses_normalised(self, raw):
        assert valid_address(raw) == PHONE

    @pytest.mark.parametrize("raw", [None, 3, "", "B0:9C:B2:EB:15", "B0:9C:B2:EB:15:F9;reboot",
                                     "B0-9C-B2-EB-15-F9", "/org/bluez/hci0", "G0:9C:B2:EB:15:F9"])
    def test_bad_addresses_refused(self, raw):
        with pytest.raises(ValueError):
            valid_address(raw)


# -- reading BlueZ ------------------------------------------------------------


class TestDevices:
    def test_paired_devices_only(self):
        bt, _, _, _ = make(battery=80)
        devices = [bt.public(d) for d in bt.devices()]
        assert [d["address"] for d in devices] == [TABLET, PHONE], "stranger dropped; BlueZ order"
        phone = devices[1]
        assert phone == {"address": PHONE, "name": "Pixel 11 Pro XL", "icon": "phone", "paired": True,
                         "trusted": False, "connected": False, "last_used": None, "battery": 80}

    def test_most_recently_used_first_and_remembered(self, tmp_path):
        bt, _, _, wall = make(tmp_path)
        bt.note(PHONE)
        assert [d["address"] for d in bt.devices()] == [PHONE, TABLET]
        assert bt.devices()[0]["last_used"] == "2025-10-09T08:53:20+00:00"
        again, _, _, _ = make(tmp_path)
        assert again.devices()[0]["address"] == PHONE, "kept in bluetooth.json"
        assert json.loads((tmp_path / "bluetooth.json").read_text())["last_used"] == {PHONE: wall[0]}

    def test_a_connected_phone_is_noted(self, tmp_path):
        bt, _, _, _ = make(tmp_path, phone_connected=True)
        bt.devices()
        assert bt.devices()[0]["last_used"] is not None
        assert PHONE in json.loads((tmp_path / "bluetooth.json").read_text())["last_used"]

    def test_bluez_down(self):
        bt, _, _, _ = make(down=True)
        assert bt.adapter() is None and bt.devices() == [] and bt.playing() is None

    def test_adapter(self):
        bt, _, _, _ = make()
        assert bt.adapter() == {"path": "/org/bluez/hci0", "name": "Hasler", "powered": True}

    def test_reads_are_cached(self):
        bt, bus, clock, _ = make()
        bt.devices(), bt.adapter(), bt.devices()
        assert len(bus.calls) == 1
        clock[0] += 5
        bt.devices()
        assert len(bus.calls) == 2


class TestPlaying:
    def test_nobody_connected(self):
        bt, _, _, _ = make()
        assert bt.playing() is None

    def test_phone_track_and_codec(self):
        bt, _, _, _ = make(phone_connected=True)
        assert bt.playing() == {"address": PHONE, "name": "Pixel 11 Pro XL", "codec": "aptX HD",
                                "rate": 48000, "title": "Song", "artist": "Band", "paused": False}

    def test_paused(self):
        bt, _, _, _ = make(phone_connected=True, status="paused")
        assert bt.playing()["paused"] is True and bt.paused() is True

    def test_paused_soon_never_blocks_and_refreshes(self):
        bus = Bus(phone_connected=True, status="paused")
        later = []
        bt = Bluetooth(run=bus, spawn=lambda fn, name: later.append(fn))
        assert bt.paused_soon() is False and len(later) == 1
        assert bt.paused_soon() is False and len(later) == 1, "one refresh at a time"
        later[0]()
        assert bt.paused_soon() is True


# -- doing ------------------------------------------------------------------


class TestActions:
    def test_connect(self):
        bt, bus, _, _ = make()
        bt.connect(PHONE.lower())
        assert bus.methods() == [["call", "org.bluez", PHONE_PATH, "org.bluez.Device1", "Connect"]]

    def test_connect_already_connected_does_nothing(self):
        bt, bus, _, _ = make(phone_connected=True)
        bt.connect(PHONE)
        assert bus.methods() == []

    def test_connect_failure_is_409(self):
        bt, bus, _, _ = make()
        bus.fail["Connect"] = (1, "Call failed: Page Timeout\n")
        with pytest.raises(BluetoothError) as exc:
            bt.connect(PHONE)
        assert exc.value.status == 409 and "Page Timeout" in str(exc.value)

    def test_unknown_device_is_400(self):
        bt, _, _, _ = make()
        with pytest.raises(BluetoothError) as exc:
            bt.connect("AA:AA:AA:AA:AA:AA")    # seen, never paired
        assert exc.value.status == 400

    def test_bluez_down_is_503(self):
        bt, _, _, _ = make(down=True)
        with pytest.raises(BluetoothError) as exc:
            bt.connect(PHONE)
        assert exc.value.status == 503

    def test_disconnect_all_connected(self):
        bt, bus, _, _ = make(phone_connected=True)
        assert bt.disconnect() == ["Pixel 11 Pro XL"]
        assert bus.methods() == [["call", "org.bluez", PHONE_PATH, "org.bluez.Device1", "Disconnect"]]

    def test_forget(self, tmp_path):
        bt, bus, _, _ = make(tmp_path)
        bt.note(PHONE)
        assert bt.forget(PHONE) == "Pixel 11 Pro XL"
        assert bus.methods() == [["call", "org.bluez", "/org/bluez/hci0", "org.bluez.Adapter1",
                                  "RemoveDevice", "o", PHONE_PATH]]
        assert json.loads((tmp_path / "bluetooth.json").read_text())["last_used"] == {}

    @pytest.mark.parametrize("status,method", [("playing", "Pause"), ("paused", "Play")])
    def test_toggle(self, status, method):
        bt, bus, _, _ = make(phone_connected=True, status=status)
        assert bt.control("toggle")
        assert bus.methods() == [["call", "org.bluez", PHONE_PATH + "/player0", "org.bluez.MediaPlayer1", method]]

    def test_next_without_a_player_object_uses_media_control(self):
        bt, bus, _, _ = make(phone_connected=True, player=False)
        assert bt.control("next")
        assert bus.methods() == [["call", "org.bluez", PHONE_PATH, "org.bluez.MediaControl1", "Next"]]

    def test_control_with_nobody_connected(self):
        bt, bus, _, _ = make()
        assert bt.control("next") is False and bus.methods() == []

    def test_control_failure_is_logged_not_raised(self):
        bt, bus, _, _ = make(phone_connected=True)
        bus.fail["Next"] = (1, "Call failed: Not supported")
        assert bt.control("next") is False


class TestReconnect:
    def test_most_recent_first_stops_at_the_first_that_answers(self, tmp_path):
        bt, bus, _, wall = make(tmp_path)
        bt.note(TABLET)
        wall[0] += 100
        bt.note(PHONE)
        assert bt.reconnect() == "Pixel 11 Pro XL"
        assert [c[2] for c in bus.methods()] == [PHONE_PATH]

    def test_next_phone_when_the_first_is_away(self, tmp_path):
        bt, bus, _, _ = make(tmp_path)
        bt.note(PHONE)
        bus.fail["Connect"] = (1, "Page Timeout")
        assert bt.reconnect() is None
        assert [c[2] for c in bus.methods()] == [PHONE_PATH, TABLET_PATH]

    def test_already_connected(self):
        bt, bus, _, _ = make(phone_connected=True)
        assert bt.reconnect() == "Pixel 11 Pro XL" and bus.methods() == []

    def test_cancelled_before_trying(self):
        bt, bus, _, _ = make()
        assert bt.reconnect(cancelled=lambda: True) is None and bus.methods() == []

    def test_a_late_answer_is_let_go(self, tmp_path):
        bt, bus, _, _ = make(tmp_path)
        bt.note(PHONE)
        state = {"cancel": False}
        bus.on_connect = lambda args: state.update(cancel=True)   # a preset key meanwhile
        assert bt.reconnect(cancelled=lambda: state["cancel"]) is None
        assert [c[-1] for c in bus.methods()] == ["Connect", "Disconnect"]

    def test_devices_that_cannot_send_audio_are_skipped(self):
        bus = Bus()
        bt = Bluetooth(run=bus, spawn=lambda fn, name: fn())
        orig = bus.__call__

        def run(args, timeout):
            rc, out, err = orig(args, timeout)
            if "GetManagedObjects" in args and "org.bluez" in args:
                doc = json.loads(out)
                doc["data"][0][TABLET_PATH]["org.bluez.Device1"]["UUIDs"] = v("as", ["0000110b-0000-1000-8000-00805f9b34fb"])
                out = json.dumps(doc)
            return rc, out, err
        bt._run = run
        bus.fail["Connect"] = (1, "Page Timeout")
        bt.reconnect()
        assert [c[2] for c in bus.methods()] == [PHONE_PATH], "a speaker is not a phone"

    def test_one_at_a_time(self):
        bus = Bus()
        later = []
        bt = Bluetooth(run=bus, spawn=lambda fn, name: later.append(fn))
        assert bt.reconnect_async() is True and bt.reconnecting
        assert bt.reconnect_async() is False
        later[0]()
        assert not bt.reconnecting


# -- the Bluetooth key, select and wake --------------------------------------


class BtPlayer(FakePlayer):
    def __init__(self):
        super().__init__()
        self.bluetooth = FakeBluetooth([{"address": PHONE, "name": "Pixel", "icon": "phone",
                                         "paired": True, "trusted": False, "connected": False,
                                         "last_used": None, "battery": None}])

    def select_source(self, source):
        super().select_source(source)
        if source == "bluetooth":
            self.source = "radio"     # as on the box: nothing reports until a phone casts


@pytest.fixture
def rig():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    variant.actions = dict(variant.actions)
    variant.actions["k6.tap"] = {"do": "bluetooth"}
    variant.actions["k6.hold"] = {"do": "volume_step", "delta": 2}
    player = BtPlayer()
    replies = []
    actions = Actions(variant, player, on_reply=replies.append)
    return actions, player, player.bluetooth, replies


class TestBluetoothKey:
    def test_selects_bluetooth_and_calls_the_last_phone(self, rig):
        actions, player, bt, replies = rig
        actions.dispatch(Event("k6", "tap"))
        assert ("select_source", "bluetooth") in player.calls
        assert actions.cycle_position == "bluetooth"
        assert bt.calls == [("reconnect",)] and replies == ["source:bluetooth"]

    def test_no_reconnect_when_switched_off(self, rig):
        actions, player, bt, _ = rig
        actions.bt_reconnect = {"on_select": False, "on_wake": True}
        actions.dispatch(Event("k6", "tap"))
        assert ("select_source", "bluetooth") in player.calls and bt.calls == []

    def test_again_while_a_phone_plays_is_next_track(self, rig):
        actions, player, bt, replies = rig
        player.source = "bluetooth"
        actions.dispatch(Event("k6", "tap"))
        assert bt.calls == [("control", "next")] and replies == ["next"]
        assert ("select_source", "bluetooth") not in player.calls

    @pytest.mark.parametrize("keys", [{"enabled": False, "again": "next", "hold": "pause"},
                                      {"enabled": True, "again": "none", "hold": "pause"}])
    def test_again_off_selects_again(self, rig, keys):
        actions, player, bt, _ = rig
        actions.bt_keys = keys
        player.source = "bluetooth"
        actions.dispatch(Event("k6", "tap"))
        assert ("control", "next") not in bt.calls and ("select_source", "bluetooth") in player.calls

    def test_previous(self, rig):
        actions, player, bt, _ = rig
        actions.bt_keys = {"enabled": True, "again": "previous", "hold": "none"}
        player.source = "bluetooth"
        actions.dispatch(Event("k6", "tap"))
        assert bt.calls == [("control", "previous")]

    def test_hold_while_a_phone_plays_is_pause_play(self, rig):
        actions, player, bt, _ = rig
        player.source = "bluetooth"
        actions.dispatch(Event("k6", "hold"))
        assert bt.calls == [("control", "toggle")]
        assert not any(c[0] == "set_volume" for c in player.calls), "the hold's own binding waits"

    def test_hold_does_its_own_thing_otherwise(self, rig):
        actions, player, bt, _ = rig
        player.volume = 40
        actions.dispatch(Event("k6", "hold"))
        assert bt.calls == [] and player.volume == 42

    def test_hold_none_keeps_the_binding(self, rig):
        actions, player, bt, _ = rig
        actions.bt_keys = {"enabled": True, "again": "next", "hold": "none"}
        player.source, player.volume = "bluetooth", 40
        actions.dispatch(Event("k6", "hold"))
        assert bt.calls == [] and player.volume == 42

    def test_only_the_bluetooth_keys_hold(self, rig):
        actions, player, bt, _ = rig
        player.source = "bluetooth"
        actions.dispatch(Event("k5", "hold"))
        assert bt.calls == []

    def test_cycle_landing_on_bluetooth_reconnects(self, rig):
        actions, player, bt, _ = rig
        actions.source_cycle = ("radio", "bluetooth")
        actions.dispatch(Event("pu", "tap"))
        assert ("select_source", "bluetooth") in player.calls and bt.calls == [("reconnect",)]

    def test_switched_off_in_moode_fails(self, rig):
        actions, player, bt, replies = rig

        class Off:
            def enabled(self):
                return {"bluetooth": False, "airplay": True}
        player.renderers = Off()
        actions.dispatch(Event("k6", "tap"))
        assert replies == ["fail"] and ("select_source", "bluetooth") not in player.calls

    def test_reconnect_is_cancelled_by_a_preset(self, rig):
        actions, player, _, _ = rig
        seen = {}

        class Slow(FakeBluetooth):
            def reconnect_async(self, cancelled=lambda: False, done=None):
                seen["cancelled"] = cancelled
                return True
        player.bluetooth = Slow()
        actions.dispatch(Event("k6", "tap"))
        assert seen["cancelled"]() is False
        actions.dispatch(Event("k1", "tap"))
        assert seen["cancelled"]() is True


class TestWake:
    def test_slept_on_bluetooth_wakes_into_it(self, rig):
        actions, player, bt, _ = rig
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("k6", "tap"))
        player.source = "bluetooth"                  # the phone plays
        actions.dispatch(Event("power", "off"))
        assert actions.standby and actions.standby_source == "bluetooth"
        player.source = "radio"
        bt.calls.clear()
        actions.dispatch(Event("power", "on"))
        assert not actions.standby and bt.calls == [("reconnect",)]
        assert actions.cycle_position == "bluetooth"
        assert player.calls.count(("play_preset", 1, "radio")) == 1, "no preset on top of it"

    def test_on_wake_off_resumes_the_preset(self, rig):
        actions, player, bt, _ = rig
        actions.bt_reconnect = {"on_select": True, "on_wake": False}
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("k6", "tap"))
        actions.dispatch(Event("power", "off"))
        bt.calls.clear()
        actions.dispatch(Event("power", "on"))
        assert bt.calls == [] and player.calls.count(("play_preset", 1, "radio")) == 2

    def test_slept_on_radio_resumes_radio(self, rig):
        actions, player, bt, _ = rig
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("power", "off"))
        actions.dispatch(Event("power", "on"))
        assert actions.standby_source is None and bt.calls == []
        assert player.calls.count(("play_preset", 1, "radio")) == 2

    def test_kept_across_a_restart(self, rig, tmp_path):
        actions, player, _, _ = rig
        actions.state = StateStore.at(tmp_path / "state.json")
        actions.dispatch(Event("k6", "tap"))
        actions.dispatch(Event("power", "off"))
        assert json.loads((tmp_path / "state.json").read_text())["standby_source"] == "bluetooth"
        fresh = BtPlayer()
        again = Actions(actions.variant, fresh, state=StateStore.at(tmp_path / "state.json"))
        again.restore()
        assert again.standby and again.standby_source == "bluetooth"
        again.dispatch(Event("power", "on"))
        assert fresh.bluetooth.calls == [("reconnect",)]


# -- settings and the API -----------------------------------------------------


class TestSettings:
    def test_partial_merge(self):
        doc = bt_settings_from({"keys": {"again": "none"}})
        assert doc == {"keys": {"enabled": True, "again": "none", "hold": "pause"},
                       "reconnect": {"on_select": True, "on_wake": True}}

    @pytest.mark.parametrize("raw", [{}, None, {"keys": {"again": "skip"}}, {"keys": {"enabled": 1}},
                                     {"keys": {"volume": "up"}}, {"reconnect": {"on_select": "yes"}},
                                     {"reconnect": {"always": True}}, {"other": {}}, {"keys": []}])
    def test_refused(self, raw):
        with pytest.raises(ValueError):
            bt_settings_from(raw)

    def test_apply_saved(self, rig):
        actions = rig[0]
        apply_saved(actions, {"bluetooth": {"keys": {"hold": "none"}, "reconnect": {"on_wake": False}}})
        assert actions.bt_keys["hold"] == "none" and actions.bt_reconnect == {"on_select": True, "on_wake": False}

    def test_apply_saved_ignores_a_bad_value(self, rig):
        actions = rig[0]
        apply_saved(actions, {"bluetooth": {"keys": {"hold": "explode"}}})
        assert actions.bt_keys["hold"] == "pause"

    def test_the_verb_is_on_the_key_page(self):
        assert "bluetooth" in keymap.VERB_NAMES
        assert keymap.to_page({"do": "bluetooth"}) == {"do": "bluetooth"}
        assert keymap.action_from_page({"do": "bluetooth"}, "k6.tap", set(), None, []) == {"do": "bluetooth"}


@pytest.fixture
def api(rig, tmp_path):
    actions, player, bt, _ = rig
    panel = Panel.from_variant(actions.variant, fake=True)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    return BluetoothApi(app), actions, player, bt, tmp_path


class TestApi:
    def test_get(self, api):
        bluetooth, actions, player, bt, _ = api
        bt.list[0]["connected"] = True
        status, doc = bluetooth.route("GET", "/api/bluetooth", None)
        assert status == 200
        assert doc["available"] and doc["on"] and doc["name"] == "Hasler"
        assert doc["devices"] == [{"address": PHONE, "name": "Pixel", "icon": "phone", "paired": True,
                                   "trusted": False, "connected": True, "last_used": None, "battery": None}]
        assert doc["playing"] == {"address": PHONE, "name": "Pixel", "codec": "SBC", "rate": 44100,
                                  "title": "", "artist": "", "paused": False}
        assert doc["settings"] == {"keys": {"enabled": True, "again": "next", "hold": "pause"},
                                   "reconnect": {"on_select": True, "on_wake": True}}
        assert doc["pairing"] == {"open": False, "seconds_left": 0}
        assert doc["bound_to"] == ["k6.tap"]

    def test_no_bluetooth(self, api):
        bluetooth, _, player, _, _ = api
        player.bluetooth = None
        status, doc = bluetooth.route("GET", "/api/bluetooth", None)
        assert status == 200 and doc["available"] is False and doc["devices"] == [] and doc["playing"] is None
        with pytest.raises(Unavailable):
            bluetooth.route("POST", "/api/bluetooth/connect", {"address": PHONE})

    def test_connect_disconnect_forget(self, api):
        bluetooth, _, _, bt, _ = api
        status, doc = bluetooth.route("POST", "/api/bluetooth/connect", {"address": PHONE.lower()})
        assert status == 200 and doc["ok"] and doc["devices"][0]["connected"]
        status, doc = bluetooth.route("POST", "/api/bluetooth/disconnect", {})
        assert status == 200 and not doc["devices"][0]["connected"]
        status, doc = bluetooth.route("POST", "/api/bluetooth/forget", {"address": PHONE})
        assert status == 200 and doc["devices"] == []
        assert bt.calls == [("connect", PHONE), ("disconnect", None), ("forget", PHONE)]

    @pytest.mark.parametrize("body", [{}, {"address": "nope"}, {"address": "B0:9C:B2:EB:15:F9 ; x"}, None])
    def test_bad_address_is_400(self, api, body):
        with pytest.raises(ValueError):
            api[0].route("POST", "/api/bluetooth/connect", body)

    def test_unknown_device_is_400(self, api):
        status, doc = api[0].route("POST", "/api/bluetooth/forget", {"address": TABLET})
        assert status == 400 and doc["ok"] is False and TABLET in doc["error"]

    def test_bluez_refusal_is_409(self, api):
        bluetooth, _, _, bt, _ = api

        def refuse(address, timeout=8):
            raise BluetoothError("couldn't connect Pixel - is it near, with Bluetooth on?")
        bt.connect = refuse
        status, doc = bluetooth.route("POST", "/api/bluetooth/connect", {"address": PHONE})
        assert status == 409 and "near" in doc["error"]

    def test_connect_when_switched_off_is_409(self, api):
        bluetooth, _, player, _, _ = api

        class Off:
            def enabled(self):
                return {"bluetooth": False}
        player.renderers = Off()
        status, doc = bluetooth.route("POST", "/api/bluetooth/connect", {"address": PHONE})
        assert status == 409

    def test_put_settings_persists_and_applies(self, api):
        bluetooth, actions, _, _, tmp = api
        status, doc = bluetooth.route("PUT", "/api/bluetooth/settings",
                                      {"keys": {"hold": "none"}, "reconnect": {"on_select": False}})
        assert status == 200 and doc["ok"] and doc["settings"]["keys"]["hold"] == "none"
        assert actions.bt_reconnect["on_select"] is False
        saved = json.loads((tmp / "settings.json").read_text())["bluetooth"]
        assert saved == {"keys": {"enabled": True, "again": "next", "hold": "none"},
                         "reconnect": {"on_select": False, "on_wake": True}}

    def test_put_settings_bad_is_400(self, api):
        with pytest.raises(ValueError):
            api[0].route("PUT", "/api/bluetooth/settings", {"keys": {"again": "louder"}})

    def test_not_ours_and_wrong_method(self, api):
        assert api[0].route("GET", "/api/casting", None) is None
        assert api[0].route("DELETE", "/api/bluetooth", None)[0] == 405


class TestWiring:
    def test_served_by_the_web_server(self):
        from haslerd.web.server import EXTRA_PREFIXES
        assert "/api/bluetooth" in EXTRA_PREFIXES

    def test_renderer_info_carries_the_codec(self):
        from haslerd.player.moode import MoodePlayer
        player = MoodePlayer(sources={})
        player.bluetooth = FakeBluetooth([{"address": PHONE, "name": "Pixel", "connected": True}])
        player.bluetooth.codec_doc = {"codec": "aptX HD", "rate": 48000}
        info = player.renderer_info()
        assert info["device"] == "Pixel" and info["codec"] == "aptX HD" and info["rate"] == 48000
