"""The front page tells the truth: ``/api/status`` says where the music comes
from (``played_from``), who the phone is on any renderer (``phone``), whether
PU is waiting for one (``waiting``) and whether PU chose the source
(``pu_source``) - on a Hasler HM and on an Excelsior whose key stays down."""

import json

import pytest
from conftest import VARIANTS

from haslerd.actions import CAST_END_GRACE_S, Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp

SOURCES = {"radio": True, "spotify": True, "ytm": True}
DT = 0.02
# The Excelsior's bank, by contact (config/variants/excelsior.yaml).
PINS = {"k1": "d8", "k2": "d9", "k3": "d10", "pu": "d14"}


class Radio:
    def __init__(self, tmp_path, variant="hasler-hm"):
        self.variant = load_variant(VARIANTS / f"{variant}.yaml")
        self.player = FakePlayer()
        self.actions = Actions(self.variant, self.player)
        self.panel = Panel.from_variant(self.variant, fake=True)
        self.app = WebApp(self.actions, self.panel, tmp_path / "keys.yaml", SOURCES, ha=False)
        self.app.load_saved()
        self.t = 0.0

    def status(self):
        return self.app.status()

    def tap(self, kid):
        self.actions.dispatch(Event(kid, "tap"))

    def run(self, seconds):
        end = self.t + seconds
        while self.t <= end + 1e-9:
            self.actions.dispatch_all(self.panel.poll(self.t))
            self.t = round(self.t + DT, 4)

    def latch(self, kid):
        """Press a latching key: the old one pops up first (the changeover gap)."""
        self.panel.digital.clear()
        self.run(0.02)
        self.panel.digital.set(PINS[kid], True)
        self.run(0.2)


@pytest.fixture
def hm(tmp_path):
    return Radio(tmp_path)


@pytest.fixture
def ex(tmp_path):
    return Radio(tmp_path, "excelsior")


class FakeGroup:
    following = True

    def status_doc(self):
        return {"role": "follow", "joining": False, "leader": {"id": "x", "name": "Kitchen"}, "members": 2}

    def leave(self, why):
        self.following = False


class TestHaslerHm:
    def test_a_key_preset_plays_from_its_key(self, hm):
        hm.tap("k3")
        st = hm.status()
        assert (st["played_from"], st["current_key"]) == ("key", "k3")
        assert st["phone"] is None and st["waiting"] is False and st["pu_source"] is None

    def test_a_page_play_comes_from_the_page(self, hm):
        hm.app.play({"name": "Try", "source": "radio", "uri": "http://stream.example/a"})
        st = hm.status()
        assert st["played_from"] == "page" and st["current_key"] is None
        assert st["playing"]["n"] == 0 and st["playing"]["name"] == "Try"

    def test_a_bluetooth_phone_that_took_the_speaker(self, hm):
        hm.tap("k3")
        hm.player.phone_casts("bluetooth", "Pixel 11", "Song", "Band")
        st = hm.status()
        assert st["played_from"] == "phone" and st["playing"] is None and st["current_key"] is None
        assert st["phone"] == {"device": "Pixel 11", "title": "Song", "artist": "Band", "source": "bluetooth"}
        assert st["waiting"] is False and st["pu_source"] is None     # nobody pressed PU
        assert st["renderer"]["device"] == "Pixel 11"                 # as before

    def test_airplay_from_moodes_metadata(self, hm, tmp_path):
        hm.app.aplmeta = tmp_path / "aplmeta.json"
        hm.app.aplmeta.write_text(json.dumps({"title": "Blue in Green", "artist": "Miles Davis", "album": "Kind of Blue"}))
        hm.player.phone_casts("airplay")
        st = hm.status()
        assert st["played_from"] == "phone" and st["source"] == "airplay" and st["waiting"] is False
        assert st["phone"] == {"device": "", "title": "Blue in Green", "artist": "Miles Davis", "source": "airplay"}
        assert st["renderer"] is None

    def test_airplay_without_metadata_is_still_a_phone(self, hm, tmp_path):
        hm.app.aplmeta = tmp_path / "missing.json"
        hm.player.phone_casts("airplay")
        st = hm.status()
        assert st["played_from"] == "phone"
        assert st["phone"] == {"device": "", "title": "", "artist": "", "source": "airplay"}

    def test_spotify_connect_from_go_librespots_status(self, hm):
        class Spotify:
            def now_playing(self):
                return {"device": "", "title": "So What", "artist": "Miles Davis"}

        hm.player.sources = {"spotify": Spotify()}
        hm.player.phone_casts("spotify")
        st = hm.status()
        assert st["played_from"] == "phone" and st["source"] == "spotify"
        assert st["phone"] == {"device": "", "title": "So What", "artist": "Miles Davis", "source": "spotify"}

    def test_our_own_spotify_preset_is_not_a_phone(self, hm):
        hm.tap("k4")                                  # Today's Top Hits, on go-librespot
        st = hm.status()
        assert (st["played_from"], st["phone"], st["source"]) == ("key", None, "spotify")

    def test_pu_parked_on_bluetooth_waits_for_a_phone(self, hm):
        hm.tap("k1")
        hm.tap("pu")                                  # -> bluetooth, MPD stopped
        hm.player.source = "radio"    # like moOde: btactive stays 0 until a phone connects
        st = hm.status()
        assert (st["source"], st["waiting"], st["played_from"], st["pu_source"]) == ("bluetooth", True, None, "bluetooth")
        assert st["phone"] is None and st["playing"] is None

    def test_the_phone_pu_waited_for_plays(self, hm):
        hm.tap("pu")
        hm.player.phone_casts("bluetooth", "Pixel 11", "Song", "Band")
        st = hm.status()
        assert (st["waiting"], st["played_from"], st["pu_source"]) == (False, "phone", "bluetooth")

    def test_pairing_shows_its_countdown(self, hm):
        hm.actions.clock = lambda: 1000.0
        hm.actions.dispatch(Event("pu", "hold"))      # bt_pair, 120 s
        hm.player.source = "radio"
        assert hm.status()["pairing_left_s"] == 120
        hm.actions.clock = lambda: 1050.0
        assert hm.status()["pairing_left_s"] == 70
        hm.actions.clock = lambda: 1200.0
        assert hm.status()["pairing_left_s"] is None

    def test_a_bluetooth_flag_naming_nobody_is_not_waiting(self, hm):
        hm.player.source = "bluetooth"                # moOde says connected, BlueZ names no phone
        st = hm.status()
        assert st["waiting"] is False and st["phone"] is None and st["pu_source"] is None

    def test_standby_plays_from_nowhere(self, hm):
        hm.tap("k3")
        hm.actions.enter_standby()
        st = hm.status()
        assert st["played_from"] is None and st["current_key"] is None and st["phone"] is None

    def test_following_a_room(self, hm):
        hm.app.group = FakeGroup()
        st = hm.status()
        assert st["played_from"] == "room" and st["group"]["leader"]["name"] == "Kitchen"

    def test_after_the_cast_grace_the_key_comes_back(self, hm):
        hm.tap("k3")
        hm.player.phone_casts("bluetooth", "Pixel 11")
        hm.actions.watch_cast("bluetooth", now=100.0)
        assert hm.status()["played_from"] == "phone"
        hm.player.phone_leaves()
        hm.actions.watch_cast("radio", now=101.0)
        st = hm.status()                              # the grace: quiet, nothing waiting
        assert st["played_from"] is None and st["playing"] is None and st["waiting"] is False
        assert hm.actions.watch_cast("radio", now=101.0 + CAST_END_GRACE_S + 1) is True
        st = hm.status()
        assert st["played_from"] == "key" and st["current_key"] == "k3"

    def test_every_old_field_is_still_there(self, hm):
        st = hm.status()
        assert {"pressed", "playing", "standby", "volume", "source", "waiting", "renderer", "track",
                "keys", "current_key", "latched"} <= set(st)


class TestExcelsior:
    def test_a_latched_key_plays_from_its_key(self, ex):
        ex.latch("k3")
        st = ex.status()
        assert (st["played_from"], st["current_key"], st["latched"]) == ("key", "k3", "k3")

    def test_a_page_play_leaves_key_3_down_but_not_playing(self, ex):
        ex.latch("k3")
        ex.app.play({"name": "Try", "source": "radio", "uri": "http://stream.example/a"})
        st = ex.status()
        assert (st["played_from"], st["current_key"], st["latched"]) == ("page", None, "k3")

    def test_a_phone_leaves_key_3_down(self, ex):
        ex.latch("k3")
        ex.player.phone_casts("bluetooth", "Pixel 11", "Song", "Band")
        st = ex.status()
        assert (st["played_from"], st["current_key"], st["latched"], st["pu_source"]) == ("phone", None, "k3", None)
        assert st["phone"]["device"] == "Pixel 11"

    def test_pu_latched_waits_then_plays_the_phone(self, ex):
        ex.latch("k3")
        ex.latch("pu")                                # the Excelsior's PU key: Bluetooth
        st = ex.status()
        assert (st["source"], st["waiting"], st["pu_source"], st["latched"]) == ("bluetooth", True, "bluetooth", "pu")
        ex.player.phone_casts("bluetooth", "Pixel 11", "Song", "Band")
        st = ex.status()
        assert (st["waiting"], st["played_from"], st["pu_source"], st["latched"]) == (False, "phone", "bluetooth", "pu")
