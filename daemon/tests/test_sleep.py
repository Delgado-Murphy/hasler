"""The sleep timer (haslerd/sleep.py, web/sleep_api.py): set, cancel, the fade,
standby at the end with the level put back, any hand on the radio cancelling
it, the phones it sleeps through, the lamp's dim and the HTTP API."""

import json
import logging
import subprocess
import threading
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.lamp import policy
from haslerd.lamp.contract import BoxState, Look
from haslerd.lamp.policy import look_for
from haslerd.main import LampKeeper
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.player.moode import MoodeConfig, MoodePlayer
from haslerd.sleep import FADE_S, FADE_STEP_S, LatestWriter, SleepError, SleepTimer, minutes_from
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, WebServer
from haslerd.web.sleep_api import SleepApi


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakePhones:
    def __init__(self):
        self.calls = []

    def control_async(self, command):
        self.calls.append(command)


@pytest.fixture
def rig():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    player = FakePlayer()
    player.volume = 50.0
    actions = Actions(variant, player)
    clock = Clock()
    timer = SleepTimer(actions, clock=clock, write=lambda job: job())    # writes inline
    actions.on_hand = timer.hand
    actions.dispatch(Event("k1", "tap"))         # a station plays
    player.calls.clear()
    return timer, actions, player, clock


def run_to(timer, clock, t, step=0.1):
    """Tick the timer from now to ``t`` the way the main loop would."""
    while clock.t < t:
        clock.t = min(t, clock.t + step)
        timer.tick(clock.t)


# -- setting and cancelling ------------------------------------------------------


class TestSetCancel:
    def test_set_reports_what_is_left(self, rig, caplog):
        timer, _, _, clock = rig
        with caplog.at_level(logging.INFO, logger="haslerd.sleep"):
            doc = timer.set(30)
        assert doc == {"ends_in_s": 1800, "fading": False, "minutes": 30}
        clock.t += 61.5
        assert timer.remaining() == pytest.approx(1738.5)
        assert timer.status()["ends_in_s"] == 1739       # rounded up: never 0 while it plays
        assert "sleep timer: set for 30 min" in caplog.text

    @pytest.mark.parametrize("raw", [0, -5, 241, "15", True, None, float("nan"), [15]])
    def test_bad_minutes_are_refused(self, raw):
        with pytest.raises(SleepError) as exc:
            minutes_from(raw)
        assert exc.value.status == 400

    def test_fractions_are_fine_for_the_undo(self):
        assert minutes_from(0.5) == 0.5 and minutes_from(240) == 240.0

    def test_not_in_standby(self, rig):
        timer, actions, _, _ = rig
        actions.enter_standby()
        with pytest.raises(SleepError) as exc:
            timer.set(15)
        assert exc.value.status == 409 and timer.status() is None

    def test_cancel(self, rig, caplog):
        timer, _, player, _ = rig
        timer.set(15)
        with caplog.at_level(logging.INFO, logger="haslerd.sleep"):
            assert timer.cancel() is True
        assert timer.status() is None and timer.remaining() is None
        assert timer.cancel() is False
        assert "sleep timer: cancelled (the page)" in caplog.text
        assert "end_fade" not in player.verbs(), "nothing to put back before the fade"

    def test_set_again_restarts(self, rig):
        timer, _, _, clock = rig
        timer.set(15)
        clock.t += 300
        assert timer.set(60)["ends_in_s"] == 3600


# -- the fade and the end ----------------------------------------------------------


class TestFade:
    def test_nothing_happens_before_the_last_minute(self, rig):
        timer, actions, player, clock = rig
        timer.set(2)
        run_to(timer, clock, clock.t + 120 - FADE_S - 0.5, step=1.0)
        assert not timer.fading and player.calls == [] and not actions.standby

    def test_the_last_minute_fades_down_without_touching_the_volume(self, rig):
        timer, _, player, clock = rig
        start = clock.t
        timer.set(2)
        run_to(timer, clock, start + 120 - FADE_S / 2)          # half way through the fade
        assert timer.fading and timer.status()["fading"] is True
        fades = [c[1] for c in player.calls if c[0] == "fade_level"]
        assert fades[0] == pytest.approx(1.0, abs=0.01)
        assert fades == sorted(fades, reverse=True), "only ever down"
        assert player.faded == pytest.approx(0.5, abs=0.01)
        assert player.volume == 50.0 and player.get_volume() == 50.0, "the owner's level stands"
        assert "set_volume" not in player.verbs()

    def test_mixer_writes_are_paced(self, rig):
        timer, _, player, clock = rig
        start = clock.t
        timer.set(1)                                             # all fade
        run_to(timer, clock, start + 10, step=0.02)              # the 50 Hz loop
        writes = player.verbs().count("fade_level")
        assert writes == pytest.approx(10 / FADE_STEP_S, abs=1)

    def test_the_end_is_standby_and_the_level_comes_back(self, rig, caplog):
        timer, actions, player, clock = rig
        start = clock.t
        timer.set(1)
        with caplog.at_level(logging.INFO, logger="haslerd.sleep"):
            run_to(timer, clock, start + 60.05)
        assert actions.standby and player.muted
        verbs = player.verbs()
        assert verbs.index("set_mute") < verbs.index("end_fade"), "silent first, then the level back"
        assert player.faded is None and player.volume == 50.0
        assert timer.status() is None and not timer.fading
        assert "time's up - standby" in caplog.text
        before = list(player.calls)
        run_to(timer, clock, clock.t + 5)
        assert player.calls == before, "fired once"

    def test_what_standby_does_is_what_the_button_does(self, rig):
        timer, actions, player, clock = rig
        timer.set(1)
        run_to(timer, clock, clock.t + 61)
        # Actions.enter_standby: muted, stopped, persisted the same way.
        assert actions.standby and ("set_mute", True) in player.calls and player.playing is None

    def test_the_next_wake_plays_at_the_owners_level(self, rig):
        timer, actions, player, clock = rig
        timer.set(1)
        run_to(timer, clock, clock.t + 61)
        player.calls.clear()
        actions.dispatch(Event("power", "on"))
        assert not actions.standby and not player.muted and player.faded is None
        assert player.get_volume() == 50.0 and player.playing is not None

    def test_set_again_during_the_fade_comes_back_up(self, rig):
        timer, _, player, clock = rig
        timer.set(1)
        run_to(timer, clock, clock.t + 40)
        assert timer.fading
        timer.set(15)
        assert not timer.fading and player.faded is None and player.calls[-1] == ("end_fade",)

    def test_a_player_without_a_fade_just_sleeps(self, rig):
        timer, actions, player, clock = rig

        class Plain(FakePlayer):
            fade_level = end_fade = None
        plain = Plain()
        actions.player = plain
        timer.set(1)
        run_to(timer, clock, clock.t + 61)
        assert actions.standby and "fade_level" not in plain.verbs()


# -- a hand on the radio -------------------------------------------------------------


class TestCancelledByAHand:
    def test_a_key(self, rig, caplog):
        timer, actions, player, _ = rig
        timer.set(30)
        with caplog.at_level(logging.INFO, logger="haslerd.sleep"):
            actions.dispatch(Event("k3", "tap"))
        assert timer.status() is None
        assert "cancelled (a hand on the radio: k3.tap)" in caplog.text
        assert player.current().n == 3, "and the key still does its thing"

    def test_the_tone_knob(self, rig):
        timer, actions, _, _ = rig
        timer.set(30)
        actions.dispatch(Event("tone", "change", 40.0))
        assert timer.status() is None

    def test_the_volume_knob_during_the_fade_cancels_and_its_level_stands(self, rig):
        timer, actions, player, clock = rig
        timer.set(1)
        run_to(timer, clock, clock.t + 45)
        actions.dispatch(Event("volume", "change", 30.0))
        assert timer.status() is None
        verbs = player.verbs()
        assert verbs.index("end_fade") < verbs.index("set_volume"), "back up, then the knob's level"
        assert player.volume == 30.0 and player.faded is None
        run_to(timer, clock, clock.t + 30)
        assert not actions.standby

    def test_standby_from_the_knob(self, rig):
        timer, actions, _, _ = rig
        timer.set(30)
        actions.dispatch(Event("power", "off"))
        assert actions.standby and timer.status() is None

    def test_standby_from_the_page_during_the_fade_puts_the_level_back(self, rig, tmp_path):
        timer, actions, player, clock = rig
        app = WebApp(actions, Panel.from_variant(actions.variant, fake=True), tmp_path / "keys.yaml",
                     {"radio": True}, ha=False)
        timer.set(1)
        run_to(timer, clock, clock.t + 30)
        app.set_standby({"on": True})                # not a panel event: the tick notices
        timer.tick(clock.t)
        assert timer.status() is None and player.faded is None and actions.standby

    def test_the_page_volume_cancels_only_the_fade(self, rig, tmp_path):
        timer, actions, player, clock = rig
        app = WebApp(actions, Panel.from_variant(actions.variant, fake=True), tmp_path / "keys.yaml",
                     {"radio": True}, ha=False)
        app.sleep = timer
        timer.set(2)
        app.volume({"value": 40})                    # in bed, a little quieter: the timer stays
        assert timer.status() is not None
        run_to(timer, clock, clock.t + 90)
        assert timer.fading
        app.volume({"value": 35})
        assert timer.status() is None and player.volume == 35 and player.faded is None

    def test_page_remote_keys_count_too(self, rig, tmp_path):
        timer, actions, _, _ = rig
        app = WebApp(actions, Panel.from_variant(actions.variant, fake=True), tmp_path / "keys.yaml",
                     {"radio": True}, ha=False)
        timer.set(30)
        app.press({"key": "k2"})
        assert timer.status() is None


# -- phones ----------------------------------------------------------------------------


class TestPhones:
    def test_a_bluetooth_phone_is_asked_to_pause(self, rig):
        timer, actions, player, clock = rig
        phones = player.bluetooth = FakePhones()
        timer.set(1)
        player.source = "bluetooth"
        run_to(timer, clock, clock.t + 61)
        assert phones.calls == ["pause"] and actions.standby

    def test_the_phone_it_slept_through_does_not_wake_it(self, rig):
        timer, actions, player, clock = rig
        timer.set(1)
        player.source = "airplay"                    # AirPlay can't be stopped from here
        run_to(timer, clock, clock.t + 61)
        assert actions.standby and timer.keeps_asleep("airplay")
        assert not timer.keeps_asleep("bluetooth")
        player.source = "radio"                      # the phone stopped
        timer.tick(clock.t)
        assert not timer.keeps_asleep("airplay"), "a new cast wakes it, as always"

    def test_waking_forgets_the_phone(self, rig):
        timer, actions, player, clock = rig
        timer.set(1)
        player.source = "airplay"
        run_to(timer, clock, clock.t + 61)
        actions.dispatch(Event("power", "on"))
        timer.tick(clock.t)
        assert not timer.keeps_asleep("airplay")

    def test_no_phone_nothing_kept(self, rig):
        timer, _, _, clock = rig
        timer.set(1)
        run_to(timer, clock, clock.t + 61)
        assert not timer.keeps_asleep("radio")


# -- the lamp ---------------------------------------------------------------------------


class FakeDriver:
    def __init__(self):
        self.shows = []

    def show(self, look, fade_s=0.0):
        self.shows.append((look, fade_s))

    def reply(self, name):
        pass

    def countdown(self, progress):
        pass

    def set_night(self, night):
        pass

    def close(self, halt_dark=True):
        pass


class TestLamp:
    def test_playing_dims_slowly_in_the_last_minute(self):
        look, fade = look_for(BoxState(playing=True, source="ytm", sleeping=True))
        assert look == Look("amber", policy.SLEEP_LEVEL, night=policy.SLEEP_LEVEL)
        assert fade == policy.FADE_SLEEP
        look, fade = look_for(BoxState(sleeping=True))
        assert (look.colour, look.level, fade) == ("amber", policy.SLEEP_LEVEL, policy.FADE_SLEEP)

    def test_standby_and_alerts_are_not_dimmed(self):
        assert look_for(BoxState(standby=True, sleeping=True))[0] == policy.STANDBY
        assert look_for(BoxState(loading=True, sleeping=True))[0] == policy.LOADING

    def test_the_sleep_level_dims_under_the_night_cap_too(self):
        from haslerd.lamp.engine import NIGHT_CAP
        assert policy.SLEEP_LEVEL < NIGHT_CAP

    def test_keeper_dims_and_keeps_a_slept_phone_ember(self, rig):
        timer, actions, player, clock = rig
        keeper = LampKeeper(FakeDriver(), actions, None, None, None, clock=clock,
                            night=lambda when, settings: False)
        keeper.sleep = timer
        keeper.boot_done()
        timer.set(1)
        run_to(timer, clock, clock.t + 30)
        assert keeper.gather(clock.t).sleeping
        player.source = "airplay"
        keeper.source = "airplay"
        run_to(timer, clock, clock.t + 31)
        state = keeper.gather(clock.t)
        assert state.standby and not state.casting and not state.sleeping
        assert look_for(state)[0] == policy.STANDBY


# -- the real player's mixer -------------------------------------------------------------


@pytest.fixture
def amixer(monkeypatch):
    calls = []
    mixer = {"level": "50", "mute_ok": True}

    def run(cmd, **kwargs):
        calls.append(cmd)
        if "sget" in cmd:
            return subprocess.CompletedProcess(cmd, 0, f"Mono: Playback [{mixer['level']}%] [on]", "")
        if not mixer["mute_ok"] and cmd[-1] in ("mute", "unmute"):
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("haslerd.player.moode.subprocess.run", run)
    return calls, mixer


def ssets(calls):
    return [c[5:] for c in calls if "sset" in c]


class TestMoodeFade:
    @pytest.fixture
    def player(self, amixer):
        player = MoodePlayer(MoodeConfig(source_flags={}, volume_limit=70, volume_floor=28), sources={})
        player.set_volume(50)                        # mixer 49%
        amixer[0].clear()
        return player

    def test_fades_under_the_knob_and_reports_the_knob(self, player, amixer):
        calls, mixer = amixer
        player.fade_level(0.5)
        player.fade_level(0.0)
        assert ssets(calls) == [["24%"], ["0%"]], "no unmute, no mute: just the level"
        mixer["level"] = "0"
        assert player.get_volume() == 50.0, "the owner's level, not the fade's"

    def test_end_fade_puts_the_level_back_under_the_mute(self, player, amixer):
        calls, _ = amixer
        player.fade_level(0.2)
        player.set_mute(True)
        calls.clear()
        player.end_fade()
        assert ssets(calls) == [["49%"]], "the level, and the mute switch left alone"
        player.end_fade()
        assert len(ssets(calls)) == 1, "once"

    def test_the_knob_ends_the_fade(self, player, amixer):
        calls, _ = amixer
        player.fade_level(0.5)
        player.set_volume(30)
        calls.clear()
        player.end_fade()
        assert ssets(calls) == [], "the knob's new level stands"

    def test_a_mixer_that_mutes_by_level_stays_silent(self, player, amixer):
        calls, mixer = amixer
        mixer["mute_ok"] = False
        player.fade_level(0.5)
        player.set_mute(True)                         # falls back to 0%
        calls.clear()
        player.end_fade()
        assert ssets(calls) == [], "its unmute sets the level"

    def test_no_knob_yet_fades_from_the_mixer(self, amixer):
        calls, _ = amixer
        player = MoodePlayer(MoodeConfig(source_flags={}, volume_limit=70, volume_floor=28), sources={})
        player.fade_level(0.5)
        assert ssets(calls)[-1] == ["25%"]


class TestLatestWriter:
    def test_only_the_newest_job_runs_after_a_slow_one(self):
        write = LatestWriter(idle_s=0.2)
        gate, started, done, ran = threading.Event(), threading.Event(), threading.Event(), []

        def slow():
            started.set()
            gate.wait(2)
            ran.append("slow")
        write(slow)
        assert started.wait(2)
        for n in range(5):
            write(lambda n=n: ran.append(n))
        write(lambda: (ran.append("last"), done.set()))
        gate.set()
        assert done.wait(2)
        assert ran == ["slow", "last"]


# -- HTTP ----------------------------------------------------------------------------------


class TestHttp:
    @pytest.fixture
    def base(self, rig, tmp_path):
        timer, actions, player, clock = rig
        app = WebApp(actions, Panel.from_variant(actions.variant, fake=True), tmp_path / "keys.yaml",
                     {"radio": True}, ha=False)
        app.sleep = timer
        app.extra_routes.append(SleepApi(app, timer))
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        yield f"http://127.0.0.1:{server.port}", app, timer
        server.close()

    def call(self, url, method="GET", body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {"Content-Type": "application/json"} if headers is None else headers
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"null")

    def test_set_status_cancel(self, base):
        url, _, _ = base
        status, doc = self.call(url + "/api/sleep", "POST", {"minutes": 15})
        assert status == 200 and doc["ok"] and doc["sleep"]["ends_in_s"] == 900
        assert self.call(url + "/api/status")[1]["sleep"] == {"ends_in_s": 900, "fading": False, "minutes": 15}
        assert self.call(url + "/api/sleep")[1]["sleep"]["minutes"] == 15
        status, doc = self.call(url + "/api/sleep", "DELETE")
        assert status == 200 and doc == {"ok": True, "cancelled": True, "sleep": None}
        assert self.call(url + "/api/status")[1]["sleep"] is None

    @pytest.mark.parametrize("body", [{}, {"minutes": 0}, {"minutes": "30"}, {"minutes": 999}, []])
    def test_validation(self, base, body):
        url, _, timer = base
        status, doc = self.call(url + "/api/sleep", "POST", body)
        assert status == 400 and doc["ok"] is False and doc["error"]
        assert timer.status() is None

    def test_standby_is_a_409(self, base):
        url, app, _ = base
        app.actions.enter_standby()
        status, doc = self.call(url + "/api/sleep", "POST", {"minutes": 15})
        assert status == 409 and "standby" in doc["error"]

    def test_behind_the_guard(self, base):
        url, _, timer = base
        status, _ = self.call(url + "/api/sleep", "POST", {"minutes": 15}, headers={"Content-Type": "text/plain"})
        assert status == 403 and timer.status() is None
        evil = {"Content-Type": "application/json", "Origin": "https://evil.example"}
        assert self.call(url + "/api/sleep", "POST", {"minutes": 15}, headers=evil)[0] == 403
        timer.set(15)
        assert self.call(url + "/api/sleep", "DELETE", headers=evil)[0] == 403
        assert timer.status() is not None

    def test_status_without_a_timer_is_null(self, rig, tmp_path):
        _, actions, _, _ = rig
        app = WebApp(actions, Panel.from_variant(actions.variant, fake=True), tmp_path / "keys.yaml",
                     {"radio": True}, ha=False)
        assert app.status()["sleep"] is None


def test_main_wires_the_timer(tmp_path):
    """build_web gives the page its timer and Actions its hand hook."""
    from haslerd import main
    from haslerd.state import StateStore
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    actions = Actions(variant, FakePlayer())
    panel = Panel.from_variant(variant, fake=True)
    _, app = main.build_web({"web": {"enabled": False}}, actions, panel, StateStore(None), False)
    assert isinstance(app.sleep, SleepTimer) and actions.on_hand == app.sleep.hand
    assert app.route_extra("GET", "/api/sleep", None) == (200, {"sleep": None})
