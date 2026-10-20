"""The lamp's policy (pure) and the main loop's LampKeeper - docs/lamp.md."""

from dataclasses import replace
from datetime import datetime

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.lamp import policy
from haslerd.lamp.contract import BoxState, signature
from haslerd.lamp.policy import look_for
from haslerd.main import LampKeeper
from haslerd.panel.controls import EndSwitch, Event
from haslerd.player.base import FakePlayer
from haslerd.variant import load_variant

IDLE = BoxState()   # boot done, on, silent, online


def look(**kw):
    return look_for(replace(IDLE, **kw))[0]


# -- the pure policy, one case per row of "Priority" -----------------------


class TestPriority:
    def test_1_shutting_down_beats_everything(self):
        assert look(shutting_down=True, hotspot=True, casting=True, standby=True) == policy.SHUTTING_DOWN
        assert policy.SHUTTING_DOWN.colour == "ember" and policy.SHUTTING_DOWN.motion == "breath"

    def test_1_installing_an_update_is_the_ember_breath_never_solid(self):
        # Installs happen in standby: busy, don't unplug - not "asleep".
        assert look(updating=True, standby=True) == policy.UPDATING == policy.SHUTTING_DOWN
        assert look(updating=True, hotspot=True, casting=True, playing=True) == policy.UPDATING
        assert policy.UPDATING.motion == "breath" and policy.UPDATING.colour == "ember"

    def test_magenta_is_never_a_look(self):
        flags = ("standby", "shutting_down", "updating", "hotspot", "pairing", "casting",
                 "cast_paused", "loading", "playing", "panel_missing", "night")
        for i in range(1 << len(flags)):
            on = {f: bool(i >> k & 1) for k, f in enumerate(flags)}
            for group in ("", "joining", "following"):
                state = replace(IDLE, **on, group=group, group_playing=True, offline_s=120.0)
                assert look_for(state)[0].colour != "magenta"

    def test_3_hotspot_beats_a_phone_and_standby(self):
        assert look(hotspot=True, casting=True, standby=True) == policy.HOTSPOT
        assert policy.HOTSPOT.colour == "white"

    def test_4_a_phone_beats_standby(self):
        assert look(casting=True, standby=True) == policy.CASTING

    def test_4_casting_is_blue_80_and_30_paused(self):
        assert look(casting=True) == policy.CASTING and policy.CASTING.level == 0.8
        assert look(casting=True, cast_paused=True).level == 0.3
        assert look_for(replace(IDLE, casting=True))[1] == 1.5

    def test_4_a_phone_beats_offline(self):
        assert look(casting=True, offline_s=600, source="airplay") == policy.CASTING

    def test_5_standby_is_ember_6_percent_and_never_red(self):
        assert look(standby=True, offline_s=600, panel_missing=True) == policy.STANDBY
        assert (policy.STANDBY.colour, policy.STANDBY.level) == ("ember", 0.06)

    def test_6_boot_warm_up_beats_pairing_loading_and_offline(self):
        for stage in ("daemon", "network"):
            got = look(boot=stage, pairing=True, loading=True, offline_s=600)
            assert got == policy.BOOT[stage] and got.colour == "amber"
        assert policy.BOOT["network"].floor > policy.BOOT["daemon"].floor > policy.BOOT["early"].floor

    def test_6_boot_with_the_knob_off_is_an_ember_breath_then_solid_ember(self):
        assert look(boot="daemon", standby=True) == policy.BOOT_STANDBY
        assert policy.BOOT_STANDBY.colour == "ember"
        assert look(boot="done", standby=True) == policy.STANDBY

    def test_7_pairing_beats_loading(self):
        assert look(pairing=True, loading=True) == policy.PAIRING
        assert policy.PAIRING.colour == "blue" and policy.PAIRING.motion == "breath"

    def test_pairing_to_casting_is_a_one_second_fade(self):
        assert look_for(replace(IDLE, pairing=True, casting=True)) == (policy.CASTING, 1.0)

    def test_8_loading_beats_offline(self):
        assert look(loading=True, offline_s=600) == policy.LOADING
        assert policy.LOADING.period_s == 1.2

    def test_9_offline_only_after_60s_idle_and_not_at_night(self):
        assert look(offline_s=59) == policy.READY
        assert look(offline_s=61) == policy.OFFLINE
        assert look(offline_s=61, night=True) == policy.READY
        assert look(offline_s=61, playing=True) == policy.playing_look("radio")

    def test_9_panel_missing_is_a_slow_red_breath(self):
        got = look(panel_missing=True)
        assert got.colour == "red" and got.motion == "breath" and got.period_s > policy.OFFLINE.period_s

    def test_10_playing_is_full_and_ready_is_35(self):
        assert look_for(replace(IDLE, playing=True)) == (policy.playing_look("radio"), 1.5)
        assert look() == policy.READY and policy.READY.level == 0.35

    def test_playlists_wear_the_pale_tint(self):
        assert look(playing=True, source="radio").colour == "amber"
        assert look(playing=True, source="ytm").colour == "amber_pale"
        assert look(playing=True, source="spotify").colour == "amber_pale"


class TestReplies:
    def test_pu_signature_is_the_destination(self):
        assert policy.reply_for("source:radio:radio") == signature("amber")
        assert policy.reply_for("source:radio:ytm") == signature("amber_pale")   # a YTM preset
        assert policy.reply_for("source:spotify:spotify") == signature("amber_pale")
        assert policy.reply_for("source:radio") == signature("amber")
        assert policy.reply_for("source:ytm") == signature("amber_pale")
        # A phone's stop: dim blue while no phone is there, blue once one is.
        assert policy.reply_for("source:bluetooth") == signature("blue", 0.3)
        assert policy.reply_for("source:airplay", phone_there=True) == signature("blue")
        assert policy.reply_for("source:spotify") == signature("blue", 0.3)

    def test_group_is_the_magenta_signature(self):
        assert policy.reply_for("group") == signature("magenta") == "signature:magenta"

    def test_everything_else_passes_through(self):
        for name in ("ack", "next", "stop", "fail", "ha", "swell", "nudge"):
            assert policy.reply_for(name) == name


# -- the keeper ------------------------------------------------------------


class FakeDriver:
    def __init__(self):
        self.shows, self.replies, self.counts, self.nights = [], [], [], []
        self.closed = None

    def show(self, look, fade_s=0.0):
        self.shows.append((look, fade_s))

    def reply(self, name):
        self.replies.append(name)

    def countdown(self, progress):
        self.counts.append(progress)

    def set_night(self, night):
        self.nights.append(night)

    def close(self, halt_dark=True):
        self.closed = halt_dark


class FakeNet:
    def __init__(self):
        self.online, self.hotspot, self.seen_up = True, False, True


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class FakePanel:
    def __init__(self, controls=(), absent=False):
        self.controls, self.absent = list(controls), absent


@pytest.fixture
def box():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    clock = Clock()
    player = FakePlayer()
    actions = Actions(variant, player, clock=clock)
    driver, net = FakeDriver(), FakeNet()
    switch = EndSwitch("power", "x", off_hold_s=5.0, bounce_s=0.0)
    panel = FakePanel([switch])
    keeper = LampKeeper(driver, actions, panel, net, None, clock=clock,
                        night=lambda when, settings: False)
    keeper.boot_done()
    keeper.arm()
    return clock, player, actions, driver, net, keeper, switch


def last(driver):
    return driver.shows[-1][0]


class TestKeeper:
    def test_show_only_when_the_look_changes(self, box):
        clock, _, _, driver, _, keeper, _ = box
        for _ in range(5):
            keeper.tick(clock())
            clock.t += 0.05
        assert driver.shows == [(policy.READY, 1.5)]

    def test_airplay_offline_shows_blue_not_red(self, box):
        clock, player, _, driver, net, keeper, _ = box
        player.source = "airplay"
        net.online = False
        for _ in range(70):
            keeper.tick(clock())
            clock.t += 1.0
        assert last(driver) == policy.CASTING
        assert all(look.colour != "red" for look, _ in driver.shows)

    def test_radio_offline_goes_red_only_after_60s(self, box):
        clock, _, _, driver, net, keeper, _ = box
        net.online = False
        keeper.tick(clock())
        clock.t += 59
        keeper.tick(clock())
        assert last(driver) == policy.READY
        clock.t += 2
        keeper.tick(clock())
        assert last(driver) == policy.OFFLINE

    def test_a_phone_wakes_standby_without_resuming(self, box):
        clock, player, actions, driver, _, keeper, _ = box
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("power", "off"))
        keeper.tick(clock())
        assert actions.standby and last(driver) == policy.STANDBY
        player.calls.clear()
        player.source = "bluetooth"
        assert actions.phone_has_it("bluetooth")
        actions.wake_for_phone("bluetooth")     # what the main loop does
        clock.t += 2.1
        keeper.tick(clock())
        assert actions.standby is False
        assert ("set_mute", False) in player.calls
        assert "play_preset" not in player.verbs()
        assert driver.shows[-1] == (policy.CASTING, 1.5)

    def test_knob_on_after_a_phone_woke_it_leaves_the_phone_alone(self, box):
        _, player, actions, _, _, _, _ = box
        actions.dispatch(Event("power", "off"))
        player.source = "airplay"
        actions.wake_for_phone("airplay")
        actions.dispatch(Event("power", "on"))
        assert "play_preset" not in player.verbs()

    def test_spotify_connect_from_a_phone_is_casting_our_preset_is_not(self, box):
        _, player, actions, _, _, _, _ = box
        assert actions.phone_has_it("spotify")          # go-librespot, no preset of ours
        actions.dispatch(Event("k4", "tap"))            # the Spotify preset
        assert not actions.phone_has_it("spotify")

    def test_blue_lingers_3s_after_the_phone_lets_go(self, box):
        clock, player, _, driver, _, keeper, _ = box
        player.source = "bluetooth"
        keeper.tick(clock())
        player.source = "radio"
        clock.t += 2.1
        keeper.tick(clock())
        assert last(driver) == policy.CASTING
        clock.t += 2.1
        keeper.tick(clock())
        assert last(driver) == policy.READY

    def test_countdown_progress_while_the_knob_is_held_off(self, box):
        clock, _, actions, driver, _, keeper, switch = box
        switch.update(clock(), {"x": False}, {})
        switch.update(clock(), {"x": False}, {})
        clock.t += 2.5
        keeper.tick(clock())
        assert driver.counts[-1] == pytest.approx(0.5)
        switch.update(clock(), {"x": True}, {})      # knob back on
        switch.update(clock(), {"x": True}, {})
        keeper.tick(clock())
        assert driver.counts[-1] is None
        n = len(driver.counts)
        keeper.tick(clock())
        assert len(driver.counts) == n, "None once, not every tick"

    def test_a_knob_off_from_before_boot_never_counts_down(self, box):
        clock, _, _, driver, _, keeper, switch = box
        switch.update(clock() - 1, {"x": False}, {})
        switch.update(clock() - 1, {"x": False}, {})
        clock.t += 2
        keeper.tick(clock())
        assert driver.counts == []

    def test_shutdown_shows_the_ember_breath_before_powering_off(self, box):
        clock, player, actions, driver, _, keeper, _ = box
        seen = []
        player.shutdown = lambda: seen.append(last(driver))
        actions.dispatch(Event("power", "off_held"))
        assert seen == [policy.SHUTTING_DOWN]
        keeper.close()
        # The ember stays through the halt; the shutdown hook darkens it.
        assert driver.closed is False

    def test_installing_an_update_breathes_ember_and_never_goes_dark(self, box):
        clock, _, actions, driver, _, keeper, _ = box
        installing = [False]
        keeper.updating = lambda: installing[0]
        actions.dispatch(Event("power", "off"))
        keeper.tick(clock())
        assert last(driver) == policy.STANDBY
        installing[0] = True
        clock.t += 5.1
        keeper.tick(clock())
        assert last(driver) == policy.UPDATING
        # The updater restarts haslerd mid-trial: the lamp holds, it doesn't go dark.
        keeper.close()
        assert driver.closed is False
        installing[0] = False
        clock.t += 5.1
        keeper.tick(clock())
        assert last(driver) == policy.STANDBY

    def test_an_unreadable_update_phase_is_no_update(self, box):
        clock, _, _, driver, _, keeper, _ = box
        keeper.updating = lambda: 1 / 0
        keeper.tick(clock())
        keeper.close()
        assert last(driver) == policy.READY and driver.closed is True

    def test_pu_key_replies_a_signature_of_where_it_went(self, box):
        clock, player, actions, driver, _, keeper, _ = box
        player.select_source = lambda source: player.calls.append(("select_source", source))
        actions.dispatch(Event("pu", "tap"))         # radio -> bluetooth: no phone there yet
        assert driver.replies == [signature("blue", 0.3)]   # the signature, not a dip
        player.source = "airplay"                    # a phone is casting already
        actions.dispatch(Event("pu", "tap"))         # -> airplay
        assert driver.replies[-1] == signature("blue")
        actions.dispatch(Event("k1", "tap"))         # a radio preset, remembered
        player.stop()
        player.source = "radio"
        actions.source_cycle, actions._cycle_at = ("radio", "bluetooth"), "bluetooth"
        actions.dispatch(Event("pu", "tap"))         # -> radio: resumes it
        assert driver.replies[-1] == signature("amber")

    def test_a_phone_pairing_swells_once_then_casting_blue(self, box):
        clock, player, actions, driver, _, keeper, _ = box
        actions.dispatch(Event("pu", "hold"))
        player.source = "bluetooth"
        clock.t += 2.1
        keeper.tick(clock())
        actions.end_pairing(paired=True)
        assert driver.replies == ["swell"] and last(driver) == policy.CASTING
        actions.end_pairing(paired=True)             # already closed: nothing more
        assert driver.replies == ["swell"]

    def test_the_window_ending_otherwise_does_not_swell(self, box):
        _, _, actions, driver, _, _, _ = box
        actions.dispatch(Event("pu", "hold"))
        actions.end_pairing()
        assert driver.replies == []

    def test_volume_keys_nudge_once_at_the_ends(self, box):
        clock, player, actions, driver, _, _, _ = box
        player.volume = 96.0
        for _ in range(5):                           # held: 98, 100, 100, 100, 100
            actions.dispatch(Event("k6", "hold"))
            clock.t += 0.15
        assert driver.replies == ["nudge"] and player.volume == 100.0
        clock.t += 1.0                               # let go, press again
        actions.dispatch(Event("k6", "hold"))
        assert driver.replies == ["nudge", "nudge"]
        actions.dispatch(Event("k5", "hold"))        # down, in range: silent
        assert driver.replies == ["nudge", "nudge"]
        player.volume = 1.0
        actions.dispatch(Event("k5", "hold"))
        actions.dispatch(Event("k5", "hold"))
        assert driver.replies == ["nudge", "nudge", "nudge"] and player.volume == 0.0

    def test_any_other_exit_goes_dark_at_once(self, box):
        _, _, _, driver, _, keeper, _ = box
        keeper.close()
        assert driver.closed is True

    def test_loading_shows_before_the_start_and_ends_with_sound(self, box):
        clock, player, actions, driver, _, keeper, _ = box
        sounding = []
        keeper.sounding = lambda p, preset: bool(sounding)
        seen = []
        real = player.play_preset
        player.play_preset = lambda preset: (seen.append(last(driver)), real(preset))
        actions.dispatch(Event("k5", "tap"))
        assert seen == [policy.LOADING], "breathing before the blocking start"
        assert driver.replies == ["ack"]
        clock.t += 1
        keeper.tick(clock())
        assert last(driver) == policy.LOADING
        sounding.append(True)
        clock.t += 1
        keeper.tick(clock())
        assert driver.shows[-1] == (policy.playing_look("ytm"), 1.5)

    def test_loading_with_no_sound_for_20s_fails(self, box):
        clock, _, actions, driver, _, keeper, _ = box
        keeper.sounding = lambda p, preset: False
        actions.dispatch(Event("k1", "tap"))
        for _ in range(21):
            clock.t += 1
            keeper.tick(clock())
        assert driver.replies[-1] == "fail"
        assert last(driver) == policy.READY

    def test_a_failed_start_pulses_red(self, box):
        _, player, actions, driver, _, _, _ = box
        player.fail_play = ConnectionError("down")
        actions.dispatch(Event("k2", "tap"))
        assert driver.replies == ["ack", "fail"]
        assert last(driver) == policy.READY

    def test_again_replies_next_for_playlists_and_stop_for_radio(self, box):
        _, _, actions, driver, _, _, _ = box
        actions.dispatch(Event("k5", "tap"))
        actions.dispatch(Event("k5", "tap"))
        assert driver.replies == ["ack", "next"]
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("k1", "tap"))
        assert driver.replies[-2:] == ["ack", "stop"]
        assert actions.standby and last(driver) == policy.STANDBY

    def test_wheels_and_the_power_knob_get_no_reply(self, box):
        _, _, actions, driver, _, _, _ = box
        actions.dispatch(Event("volume", "change", 50.0))
        actions.dispatch(Event("power", "off"))
        assert driver.replies == []

    def test_ha_hold_replies_ha_and_fails_without_ha(self, box):
        _, _, actions, driver, _, _, _ = box
        actions.variant.actions["k1.hold"] = {"do": "ha_scene", "entity": "scene.relax"}
        actions.dispatch(Event("k1", "hold"))
        assert driver.replies == ["fail"]
        calls = []
        actions.ha = lambda kind, payload: calls.append(kind)
        actions.dispatch(Event("k1", "hold"))
        assert driver.replies[-1] == "ha" and calls == ["scene"]

    def test_pairing_window_breathes_blue_until_it_closes(self, box):
        clock, player, actions, driver, _, keeper, _ = box
        actions.dispatch(Event("pu", "hold"))
        assert actions.pairing and last(driver) == policy.PAIRING
        assert ("bluetooth_pair", 120) in player.calls
        clock.t += 121
        keeper.tick(clock())
        assert not actions.pairing and last(driver) == policy.READY

    def test_a_phone_connecting_ends_pairing_in_one_second(self, box):
        clock, player, actions, driver, _, keeper, _ = box
        actions.dispatch(Event("pu", "hold"))
        player.source = "bluetooth"
        clock.t += 2.1
        keeper.tick(clock())
        assert driver.shows[-1] == (policy.CASTING, 1.0)
        actions.end_pairing()
        assert not actions.pairing


class TestBoot:
    def make(self, net):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        clock = Clock()
        actions = Actions(variant, FakePlayer(), clock=clock)
        keeper = LampKeeper(FakeDriver(), actions, FakePanel(), net, None, clock=clock,
                            night=lambda when, settings: False)
        return clock, keeper

    def test_daemon_then_network_then_done(self):
        net = FakeNet()
        net.seen_up = False
        clock, keeper = self.make(net)
        assert keeper.boot_stage(clock()) == "daemon"
        net.seen_up = True
        keeper.tick(clock())
        assert keeper.boot_stage(clock()) == "network"
        assert keeper.driver.shows[-1][0] == policy.BOOT["network"]
        keeper.boot_done()
        keeper.tick(clock())
        assert keeper.boot_stage(clock()) == "done"
        assert keeper.driver.shows[-1][0] == policy.READY

    def test_45s_ends_the_warm_up_whatever_happens(self):
        net = FakeNet()
        net.seen_up = False
        clock, keeper = self.make(net)
        clock.t += 45
        assert keeper.boot_stage(clock()) == "done"


class TestNight:
    def test_night_is_asked_once_a_minute_and_flips_the_engine(self):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        clock = Clock()
        asked, answer = [], [False]

        def night(when, settings):
            asked.append(settings)
            return answer[0]

        driver = FakeDriver()
        keeper = LampKeeper(driver, Actions(variant, FakePlayer(), clock=clock), FakePanel(),
                            FakeNet(), None, clock=clock, night=night,
                            wall=lambda: datetime(2026, 10, 19, 22, 0))
        for _ in range(100):           # 5 s of ticks
            keeper.tick(clock())
            clock.t += 0.05
        assert len(asked) == 1 and driver.nights == []
        answer[0] = True
        clock.t += 60
        keeper.tick(clock())
        assert len(asked) == 2 and driver.nights == [True]
        keeper.tick(clock())
        assert len(asked) == 2

    def test_a_settings_change_is_applied_at_the_next_tick(self):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        clock = Clock()
        asked = []
        keeper = LampKeeper(FakeDriver(), Actions(variant, FakePlayer(), clock=clock), FakePanel(),
                            FakeNet(), None, clock=clock,
                            night=lambda when, settings: asked.append(settings) or False)
        keeper.tick(clock())
        keeper.settings_changed({"night": "off"})
        keeper.tick(clock())
        assert asked[-1] == {"night": "off"}
