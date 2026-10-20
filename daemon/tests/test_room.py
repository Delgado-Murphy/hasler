"""The room name (web/room.py): validation, defaults, /api/info, the multiroom
identity, and the casting names applied in the background (fakes, no moOde)."""

from __future__ import annotations

import json

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.player.renderers import RendererError, spotify_device_name
from haslerd.variant import Panel, load_variant
from haslerd.web import room as room_mod
from haslerd.web.room import Room, casting_name, room_name_from, set_device_name
from haslerd.web.server import WebApp, load_settings
from haslerd.web.sources_api import SourcesApi

SPOTIFY_YML = """# go-librespot
device_name: Hasler
device_type: speaker
zeroconf_enabled: true   # owner
server:
  enabled: true
"""


class FakeRenderers:
    def __init__(self):
        self.names = {"airplay": "Hasler", "bluetooth": "Hasler", "upnp": "Hasler"}
        self.available = {"airplay": True, "bluetooth": True, "upnp": True}
        self.calls = []
        self.busy = 0                 # this many "busy" answers before taking a job
        self.fail: dict[str, Exception] = {}

    def describe(self):
        return [{"id": t, "label": t, "available": self.available[t], "on": True, "name": n,
                 "running": True, "managed_by": "moode", "reason": None if self.available[t] else "not installed"}
                for t, n in self.names.items()]

    def set(self, target, on=None, name=None):
        self.calls.append((target, on, name))
        if target in self.fail:
            raise self.fail[target]
        if self.busy:
            self.busy -= 1
            raise RendererError("moOde is busy with another job - try again in a few seconds")
        self.names[target] = name


class Player(FakePlayer):
    def __init__(self):
        super().__init__()
        self.renderers = FakeRenderers()


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = Player()
    actions = Actions(variant, player)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    spotify = tmp_path / "go-librespot.yml"
    spotify.write_text(SPOTIFY_YML)
    spotify.chmod(0o600)
    restarts = []
    app.room.spotify_config = spotify
    app.room.restart_spotify = lambda: restarts.append(1)
    app.room.avahi_host = lambda: "hasler-2.local"
    app.room.default_name = lambda: "hasler"
    app.room.sleep = lambda s: None
    app.machine_id_file = tmp_path / "machine-id"
    app.machine_id_file.write_text("0123456789abcdef0123456789abcdef\n")
    return app, player, spotify, restarts


def put(app, room):
    doc = app.put_settings({"room": room})
    app.room.wait()
    return doc


# -- validation and defaults ------------------------------------------------------


class TestNames:
    @pytest.mark.parametrize("raw,want", [("Kitchen", "Kitchen"), ("  Living room ", "Living room"),
                                          ("Küche 2", "Küche 2"), ("x" * 32, "x" * 32), ("", None),
                                          ("   ", None), (None, None)])
    def test_valid(self, raw, want):
        assert room_name_from(raw) == want

    @pytest.mark.parametrize("raw", ['Kid"s', "Kid's", "x" * 33, "-dash", "two  spaces", 5, True, ["a"]])
    def test_refused(self, raw):
        with pytest.raises(ValueError, match="room name"):
            room_name_from(raw)

    @pytest.mark.parametrize("room,want", [(None, "Hasler"), ("Kitchen", "Hasler Kitchen"),
                                           ("Hasler Attic", "Hasler Attic"), ("hasler", "hasler"),
                                           ("A very long room name of 30 ch", "Hasler A very long room name of")])
    def test_casting_name(self, room, want):
        got = casting_name(room)
        assert got == want and len(got) <= 32

    def test_defaults(self, rig):
        app, *_ = rig
        doc = app.settings()["room"]
        assert doc["name"] == "hasler" and doc["custom"] is False and doc["default"] == "hasler"
        assert doc["casting_names"] is False and doc["casting_name"] == "Hasler"
        assert doc["address"] == "hasler-2.local"
        assert doc["apply"]["state"] == "idle"

    def test_address_falls_back_to_the_host_name(self, rig, monkeypatch):
        app, *_ = rig
        app.room.avahi_host = lambda: None
        monkeypatch.setattr(room_mod.socket, "gethostname", lambda: "hasler.lan")
        assert app.room.address() == "hasler.local"

    def test_bad_saved_value_is_ignored(self, rig):
        app, *_ = rig
        app.room.load({"room": {"name": 'Bad"name', "casting_names": "yes"}})
        assert app.room.name() == "hasler" and not app.room.casting_on()


# -- saving -----------------------------------------------------------------------


class TestSettings:
    def test_name_saved_and_read_back(self, rig, tmp_path):
        app, player, _, restarts = rig
        doc = put(app, {"name": "Kitchen"})
        assert doc["room"]["name"] == "Kitchen" and doc["room"]["custom"]
        assert load_settings(tmp_path / "settings.json")["room"] == {"name": "Kitchen"}
        # The switch is off: casting names are not touched.
        assert player.renderers.calls == [] and restarts == []
        fresh = WebApp(app.actions, app.panel, None, {}, ha=False, settings_file=tmp_path / "settings.json")
        assert fresh.room.name() == "Kitchen"

    def test_empty_name_goes_back_to_the_default(self, rig, tmp_path):
        app, *_ = rig
        put(app, {"name": "Kitchen"})
        doc = put(app, {"name": ""})
        assert doc["room"]["name"] == "hasler" and not doc["room"]["custom"]
        assert "name" not in load_settings(tmp_path / "settings.json")["room"]

    @pytest.mark.parametrize("room", [{}, "Kitchen", {"name": "Bad'one"}, {"casting_names": "on"}])
    def test_refused_and_nothing_saved(self, rig, tmp_path, room):
        app, player, *_ = rig
        with pytest.raises(ValueError):
            app.put_settings({"room": room})
        assert not (tmp_path / "settings.json").exists() and player.renderers.calls == []

    def test_other_settings_keep_the_room(self, rig, tmp_path):
        app, *_ = rig
        put(app, {"name": "Kitchen", "casting_names": True})
        app.put_settings({"night": "off"})
        saved = load_settings(tmp_path / "settings.json")
        assert saved["room"] == {"name": "Kitchen", "casting_names": True} and saved["night"] == "off"

    def test_without_a_settings_file(self, rig):
        app, player, *_ = rig
        mem = WebApp(app.actions, app.panel, None, {}, ha=False)
        mem.room.default_name = lambda: "hasler"
        mem.put_settings({"room": {"name": "Den"}})
        assert mem.room.name() == "Den"


# -- who this radio is ------------------------------------------------------------


class TestIdentity:
    def test_info_name_is_the_room(self, rig):
        app, *_ = rig
        assert app.info()["name"] == "hasler"
        put(app, {"name": "Kitchen"})
        info = app.info()
        assert info["name"] == "Kitchen" and info["id"] == app.info()["id"]

    def test_group_identity_follows_the_room(self, rig):
        """main.build_group's identity reads the room live: the room list renames at once."""
        from haslerd.web import integration
        app, *_ = rig
        identity = lambda: (integration.radio_id(app.machine_id_file), app.room.name())  # as main.py

        class Group:
            def identity(self):
                return identity()

            def info_doc(self):
                return {"enabled": True}
        app.group = Group()
        put(app, {"name": "Attic"})
        assert app.info()["name"] == "Attic"


# -- casting names ----------------------------------------------------------------


class TestCasting:
    def test_switch_on_renames_everything_in_order(self, rig, spotify=None):
        app, player, spotify, restarts = rig
        put(app, {"name": "Kitchen"})
        doc = put(app, {"casting_names": True})
        assert doc["room"]["casting_names"] is True
        assert [c[0] for c in player.renderers.calls] == ["airplay", "upnp", "bluetooth"]
        assert all(c[2] == "Hasler Kitchen" and c[1] is None for c in player.renderers.calls)
        assert spotify_device_name(spotify) == "Hasler Kitchen" and restarts == [1]
        text = spotify.read_text()
        assert "zeroconf_enabled: true   # owner" in text and text.count("device_name") == 1
        assert spotify.stat().st_mode & 0o777 == 0o600
        apply = app.settings()["room"]["apply"]
        assert apply["state"] == "done" and apply["name"] == "Hasler Kitchen"
        assert [s["state"] for s in apply["steps"]] == ["done"] * 4

    def test_switch_off_sets_hasler_back(self, rig):
        app, player, spotify, restarts = rig
        put(app, {"name": "Kitchen", "casting_names": True})
        put(app, {"casting_names": False})
        assert set(player.renderers.names.values()) == {"Hasler"}
        assert spotify_device_name(spotify) == "Hasler" and restarts == [1, 1]

    def test_rename_while_on_reapplies(self, rig):
        app, player, spotify, _ = rig
        put(app, {"name": "Kitchen", "casting_names": True})
        player.renderers.calls.clear()
        put(app, {"name": "Attic"})
        assert [c[2] for c in player.renderers.calls] == ["Hasler Attic"] * 3
        assert spotify_device_name(spotify) == "Hasler Attic"

    def test_rename_while_off_touches_nothing(self, rig):
        app, player, _, restarts = rig
        put(app, {"name": "Attic"})
        assert player.renderers.calls == [] and restarts == []

    def test_already_named_is_not_reapplied(self, rig):
        app, player, _, restarts = rig
        put(app, {"casting_names": True})          # no room name: "Hasler", as they are
        assert player.renderers.calls == [] and restarts == []
        assert app.room.doc()["apply"]["state"] == "done"

    def test_busy_moode_is_waited_for(self, rig):
        app, player, *_ = rig
        player.renderers.busy = 2
        put(app, {"name": "Kitchen", "casting_names": True})
        assert player.renderers.names["airplay"] == "Hasler Kitchen"
        assert [c[0] for c in player.renderers.calls].count("airplay") == 3

    def test_one_failure_is_reported_and_the_rest_carry_on(self, rig):
        app, player, spotify, _ = rig
        player.renderers.fail["upnp"] = RendererError("moOde refused the change (HTTP 500)")
        player.renderers.available["bluetooth"] = False
        put(app, {"name": "Kitchen", "casting_names": True})
        apply = app.room.doc()["apply"]
        steps = {s["id"]: s for s in apply["steps"]}
        assert apply["state"] == "failed"
        assert steps["upnp"]["state"] == "failed" and "HTTP 500" in steps["upnp"]["note"]
        assert steps["bluetooth"]["state"] == "skipped"
        assert steps["airplay"]["state"] == steps["spotify"]["state"] == "done"

    def test_no_spotify_is_skipped(self, rig):
        app, _, spotify, restarts = rig
        spotify.unlink()
        put(app, {"name": "Kitchen", "casting_names": True})
        steps = {s["id"]: s for s in app.room.doc()["apply"]["steps"]}
        assert steps["spotify"]["state"] == "skipped" and restarts == []

    def test_spotify_restart_failure_says_so(self, rig):
        app, _, spotify, _ = rig

        def broken():
            raise RuntimeError("sudo: a password is required")
        app.room.restart_spotify = broken
        put(app, {"name": "Kitchen", "casting_names": True})
        steps = {s["id"]: s for s in app.room.doc()["apply"]["steps"]}
        assert steps["spotify"]["state"] == "failed" and "restart" in steps["spotify"]["note"]
        assert spotify_device_name(spotify) == "Hasler Kitchen"

    def test_no_moode(self, rig):
        app, player, *_ = rig
        del player.renderers
        put(app, {"name": "Kitchen", "casting_names": True})
        steps = {s["id"]: s["state"] for s in app.room.doc()["apply"]["steps"]}
        assert steps == {"airplay": "skipped", "upnp": "skipped", "bluetooth": "skipped", "spotify": "done"}

    def test_the_request_never_waits_for_moode(self, rig):
        import threading
        app, player, *_ = rig
        gate = threading.Event()
        slow = player.renderers.set

        def held(*a, **kw):
            gate.wait(5)
            return slow(*a, **kw)
        player.renderers.set = held
        doc = app.put_settings({"room": {"name": "Kitchen", "casting_names": True}})
        assert doc["room"]["apply"]["state"] == "applying"
        gate.set()
        app.room.wait()
        assert app.room.doc()["apply"]["state"] == "done"

    def test_shares_the_casting_lock(self, rig):
        app, *_ = rig
        assert SourcesApi(app)._renderer_lock is app.room.renderer_lock


class TestSpotifyFile:
    def test_quotes_and_keeps_the_rest(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text(SPOTIFY_YML)
        assert set_device_name(p, "Hasler On") is True
        assert 'device_name: "Hasler On"' in p.read_text()
        assert spotify_device_name(p) == "Hasler On"
        assert set_device_name(p, "Hasler On") is False

    def test_adds_a_missing_name(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text("device_type: speaker\nserver:\n  device_name: nested\n")
        set_device_name(p, "Hasler")
        assert p.read_text().splitlines()[0] == 'device_name: "Hasler"'
        assert "  device_name: nested" in p.read_text()

    def test_sudoers_allows_exactly_the_restart(self):
        from conftest import ROOT
        line = [l for l in (ROOT / "config/sudoers/haslerd").read_text().splitlines() if l.startswith("pi ")][0]
        assert "/usr/bin/systemctl try-restart go-librespot.service" in line
        assert room_mod.SPOTIFY_RESTART[2:] == ["/usr/bin/systemctl", "try-restart", "go-librespot.service"]


def test_settings_doc_is_json(rig):
    app, *_ = rig
    json.dumps(app.settings())
