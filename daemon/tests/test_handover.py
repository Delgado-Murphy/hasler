"""Hand over to a new owner (web/handover.py): the allow-list (a file it
doesn't name is never touched), every step runs even when one fails, the
new setup password comes back once, and the route takes a real press."""

from __future__ import annotations

import json
import subprocess

import pytest
from conftest import VARIANTS
from test_web_confirm import Clock, call

from haslerd import boxconfig
from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.player.bluetooth import FakeBluetooth
from haslerd.variant import Panel, load_variant
from haslerd.web import handover
from haslerd.web.server import WebApp, WebServer, load_settings
from haslerd.web.wifi import Wifi
from test_moving import Helper


class Spotify:
    def __init__(self):
        self.purged = 0

    def purge(self):
        self.purged += 1
        return ["account", "state.json.before-sign-out"]


class Ytm:
    def __init__(self):
        self.out = 0

    def sign_out(self):
        self.out += 1
        return {"signed_in": False}


class Ha:
    def __init__(self, state_dir):
        self.settings = object()
        self.state_dir = state_dir
        self.disconnected = 0

    def disconnect(self):
        self.disconnected += 1
        self.settings = None
        (self.state_dir / "secrets" / "ha.json").unlink(missing_ok=True)
        return 200, {}


class Group:
    def __init__(self):
        self.enabled = True
        self.configured = []

    def configure(self, enabled=None, follower_keys=None):
        self.configured.append(enabled)
        self.enabled = enabled


def make(tmp_path, helper=None):
    etc, state = tmp_path / "etc", tmp_path / "state"
    (etc / "variant.d").mkdir(parents=True)
    (state / "secrets").mkdir(parents=True)
    (etc / "box.yaml").write_text("schema: 1\nvariant: hasler-hm\nchannel: stable\ncheckin: true\nbox_id: abc\n")
    (etc / "variant.d" / "hasler-hm.yaml").write_text("schema: 1\ncontrols: {}\n")
    # The owner's radio, file by file.
    (state / "settings.json").write_text(json.dumps({
        "schema": 1, "volume_limit": 60, "volume_floor": 25, "mono": True,
        "room": {"name": "Kitchen", "casting_names": True}, "multiroom": {"enabled": True},
        "location": {"name": "Madrid", "lat": 40.4, "lon": -3.7}, "lamp": {"geek": True}}))
    for name, text in (("stations.json", "{}"), ("reports.jsonl", "{}\n"), ("state.json", '{"last_preset": 3}'),
                       ("ha-instance.json", "{}"), ("bluetooth.json", "{}"), ("allowed-hosts.json", '{"hosts": []}'),
                       ("keys.yaml", "schema: 1\n"), ("wifi-joined", "Casa\n"), ("hotspot.txt", "password=old\n"),
                       ("lamp-version", "1.4.0"), ("yt-dlp-request.json", "{}"), ("maker-notes.jsonl",
                        '{"at": "2026-11-01", "text": "Tone pot replaced"}\n'), ("stream-events.jsonl", "")):
        (state / name).write_text(text)
    for name in ("ha.json", "ytm-browser.json", "ytm-cookies.txt"):
        (state / "secrets" / name).write_text("secret")

    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    player.bluetooth = FakeBluetooth([{"address": "AA:BB:CC:DD:EE:01", "name": "Pixel", "connected": False, "paired": True}])
    actions = Actions(variant, player)
    app = WebApp(actions, panel, state / "keys.yaml", {"radio": True}, ha=False, settings_file=state / "settings.json")
    app.confirm.clock = Clock()
    app.box_paths = boxconfig.Paths(etc=etc, state=state, release=tmp_path / "release")
    helper = helper or Helper({"forget-all": {"ok": True, "forgotten": ["Casa", "Movil"]},
                               "hotspot": lambda p: {"ok": True, "ssid": p["ssid"], "changed": True, "password_used": True}})
    app.wifi = Wifi(runner=helper, hotspot=lambda: False, hotspot_file=state / "hotspot.txt", joined_file=state / "wifi-joined")
    app.wifi.helper_calls = helper.calls
    app.spotify_account = Spotify()
    app.ytm = Ytm()
    app.ha_api = Ha(state)
    app.group = Group()
    app.hosts.add("dash.example.net")
    return app, etc, state


def vacuum_ok(cmd, **kw):
    vacuum_ok.calls.append(cmd)
    return subprocess.CompletedProcess(cmd, 0, "", "")


vacuum_ok.calls = []


def test_it_forgets_the_owner_and_keeps_the_cabinet(tmp_path):
    app, etc, state = make(tmp_path)
    doc = handover.run(app, vacuum=vacuum_ok)
    assert doc["failed"] == []
    assert [s["id"] for s in doc["steps"]] == ["spotify", "ytm", "home_assistant", "bluetooth", "hosts", "keys", "files",
                                               "settings", "casting", "multiroom", "box", "wifi", "setup_network",
                                               "secrets", "journal"]
    # Forgotten.
    for name in ("stations.json", "reports.jsonl", "state.json", "ha-instance.json", "bluetooth.json", "allowed-hosts.json",
                 "keys.yaml", "wifi-joined"):
        assert not (state / name).exists(), name
    for name in ("ha.json", "ytm-browser.json", "ytm-cookies.txt"):
        assert not (state / "secrets" / name).exists(), name
    assert app.spotify_account.purged == 1 and app.ytm.out == 1 and app.ha_api.disconnected == 1
    assert app.actions.bluetooth.devices() == [] and app.hosts.names() == set()
    assert app.group.configured == [False]
    assert load_settings(state / "settings.json") == {"schema": 1, "volume_limit": 60, "volume_floor": 25, "mono": True}
    assert app.room.custom() is None
    assert "checkin" not in boxconfig.read_box_file(app.box_paths) and boxconfig.read_box_file(app.box_paths)["box_id"] == "abc"
    assert [c["action"] for c in app.wifi.helper_calls] == ["forget-all", "hotspot"]
    assert app.wifi.helper_calls[1]["ssid"] == "Hasler" and app.wifi.helper_calls[1]["password"] == "haslerhm"
    assert vacuum_ok.calls[-1] == ["sudo", "-n", "/usr/bin/journalctl", "--vacuum-time=1s"]
    # Shown once: the new setup network.
    assert doc["setup"]["ssid"] == "Hasler" and doc["setup"]["password"] == app.wifi.helper_calls[1]["password"]
    assert "password=" + doc["setup"]["password"] in (state / "hotspot.txt").read_text()
    # Kept: what belongs to the cabinet, and anything the allow-list doesn't name.
    for name in ("lamp-version", "yt-dlp-request.json", "maker-notes.jsonl", "stream-events.jsonl"):
        assert (state / name).exists(), name
    assert (etc / "variant.d" / "hasler-hm.yaml").exists() and (etc / "box.yaml").exists()


def test_a_step_that_fails_does_not_stop_the_rest(tmp_path):
    app, etc, state = make(tmp_path)

    class BrokenSpotify:
        def purge(self):
            raise OSError("go-librespot's directory is read-only")

    app.spotify_account = BrokenSpotify()
    app.ha_api.disconnect = lambda: (_ for _ in ()).throw(RuntimeError("HA is down"))

    def vacuum_fails(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required")

    doc = handover.run(app, vacuum=vacuum_fails)
    assert doc["failed"] == ["spotify", "home_assistant", "journal"]
    failed = {s["id"]: s["note"] for s in doc["steps"] if not s["ok"]}
    assert "read-only" in failed["spotify"] and "HA is down" in failed["home_assistant"] and "password" in failed["journal"]
    # Everything after a failure still ran.
    assert not (state / "keys.yaml").exists() and not (state / "stations.json").exists()
    assert not (state / "ha-instance.json").exists()           # the file half of the HA step still went
    assert doc["setup"]["ssid"] == "Hasler"
    assert app.group.configured == [False]


def test_the_restart_cannot_write_the_old_state_back(tmp_path):
    app, etc, state = make(tmp_path)
    handover.run(app, vacuum=vacuum_ok)
    app.actions._persist()                                        # what the shutdown path does
    assert not (state / "state.json").exists()


def test_describe_lists_the_makers_notes_without_creating_the_file(tmp_path):
    app, etc, state = make(tmp_path)
    doc = handover.describe(app)
    assert doc["maker_notes"] == [{"at": "2026-11-01", "text": "Tone pot replaced"}]
    assert "the makers' notes about this cabinet" in doc["keeps"]
    (state / "maker-notes.jsonl").unlink()
    assert handover.describe(app)["maker_notes"] == [] and not (state / "maker-notes.jsonl").exists()


def test_without_the_optional_parts_it_still_runs(tmp_path):
    app, etc, state = make(tmp_path)
    app.spotify_account = None
    app.ytm = None
    app.ha_api = None
    app.group = None
    app.actions.player.bluetooth = None
    app.box_paths = None
    doc = handover.run(app, vacuum=vacuum_ok)
    assert doc["failed"] == []
    notes = {s["id"]: s["note"] for s in doc["steps"]}
    assert notes["spotify"] == "not on this radio" and notes["multiroom"] == "not on this radio" and notes["box"] == "no box.yaml"


@pytest.fixture
def served(tmp_path):
    app, etc, state = make(tmp_path)
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"127.0.0.1:{server.port}", app, state
    server.close()


def test_the_route_takes_a_real_press_even_on_a_bench_and_answers_once(served, monkeypatch):
    hp, app, state = served
    monkeypatch.setattr(handover, "run", lambda a, vacuum=None: {"setup": {"ssid": "Hasler", "password": "abcd-efgh-jkmn"},
                                                                 "steps": [], "failed": [], "at": 0})
    app.confirm.allow_unconfirmed = True
    status, doc = call(hp, "POST", "/api/handover", {}, {"Origin": f"http://{hp}"})
    assert status == 403                                                  # no token, bench or not
    status, ask = call(hp, "POST", "/api/confirm/request", {"action": "handover"}, {"Origin": f"http://{hp}"})
    assert status == 200 and ask["state"] == "pending" and ask["unconfirmed"] is False
    app.confirm.intercept([Event("k1", "tap")])
    status, doc = call(hp, "POST", "/api/handover", {"confirm": ask["token"]}, {"Origin": f"http://{hp}"})
    assert status == 200 and doc["setup"] == {"ssid": "Hasler", "password": "abcd-efgh-jkmn"} and doc["restarting"] is True
    status, doc = call(hp, "POST", "/api/handover", {"confirm": ask["token"]}, {"Origin": f"http://{hp}"})
    assert status == 403                                                  # the token is used up
    status, doc = call(hp, "GET", "/api/handover")
    assert status == 200 and doc["maker_notes"][0]["text"] == "Tone pot replaced"
