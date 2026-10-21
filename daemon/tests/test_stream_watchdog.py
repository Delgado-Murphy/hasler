"""The stream watchdog (player/watchdog.py): MPD stopped by itself, radio said "playing".

The 2026-10-21 incident: 21 h up, /api/status and /api/health said preset 2
was playing, the lamp was amber - and MPD sat in "stop" with the stream still
queued. ``mpc play`` brought it back. Here, against a fake MPD behind the real
router (MoodePlayer) and the real Actions table.
"""

import pytest
from conftest import VARIANTS

import haslerd.player.watchdog as wd_mod
from haslerd.actions import Actions
from haslerd.health import health_report
from haslerd.main import LampKeeper
from haslerd.panel.controls import Event
from haslerd.player.base import Preset
from haslerd.player.moode import MoodeConfig, MoodePlayer
from haslerd.player.sources.radio import RadioSource
from haslerd.player.watchdog import StreamWatchdog
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp


SJ = "http://stream.srg-ssr.ch/m/rsj/mp3_128"


class FakeMpd:
    """Just enough MPD: a queue, a state, an elapsed that moves while it flows."""

    def __init__(self):
        self.state = "stop"
        self.queue = []
        self.song = None
        self.elapsed = 0.0
        self.error = None
        self.flowing = True       # False: "play" but no audio arrives (elapsed frozen)
        self.plays = True         # False: play() is accepted and nothing happens
        self.calls = []

    def ping(self):
        pass

    def clear(self):
        self.calls.append("clear")
        self.queue, self.song, self.state = [], None, "stop"

    def random(self, _on):
        pass

    def add(self, uri):
        self.queue.append(uri)

    def play(self, pos=None):
        self.calls.append("play")
        if self.plays and self.queue:
            self.state = "play"
            self.song = pos if pos is not None else (self.song or 0)
            self.elapsed = 0.0
            self.error = None

    def stop(self):
        self.calls.append("stop")
        self.state = "stop"

    def next(self):
        pass

    def status(self):
        if self.state == "play" and self.flowing:
            self.elapsed += 2.0
        doc = {"state": self.state, "playlistlength": str(len(self.queue))}
        if self.state != "stop" or self.song is not None:
            if self.song is not None:
                doc["song"] = str(self.song)
        if self.state in ("play", "pause"):
            doc["elapsed"] = f"{self.elapsed:.3f}"
        if self.error:
            doc["error"] = self.error
        return doc

    def currentsong(self):
        if self.song is None or not self.queue:
            return {}
        return {"file": self.queue[self.song]}


class FakeYtm:
    """A ytm source queueing three tracks into the same fake MPD."""

    name = "ytm"
    backend = "mpd"

    def __init__(self, mpd):
        self.mpd = mpd

    def play(self, preset):
        self.mpd.clear()
        for vid in ("a", "b", "c"):
            self.mpd.add(f"http://127.0.0.1:8099/ytm/{vid}")
        self.mpd.play()

    def stop(self):
        self.mpd.stop()

    def next_track(self):
        pass

    def is_active(self):
        return False

    def close(self):
        pass


@pytest.fixture
def rig():
    mpd = FakeMpd()
    radio = RadioSource(mpd=lambda: mpd)
    player = MoodePlayer(MoodeConfig(source_flags={}),
                         sources={"radio": radio, "ytm": FakeYtm(mpd)})
    player.set_mute = lambda muted: None
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    variant.presets = [
        {"n": 1, "name": "Radio Swiss Classic", "radio": "http://stream.srg-ssr.ch/m/rsc_de/mp3_128"},
        {"n": 2, "name": "Radio Swiss Jazz", "radio": SJ},
        {"n": 3, "name": "A playlist", "ytm": "https://music.youtube.com/playlist?list=PLx"},
    ]
    actions = Actions(variant, player)
    replies = []
    actions.on_reply = replies.append
    night = {"on": False}
    watch = StreamWatchdog(actions, night=lambda: night["on"])
    actions.stalled = watch.stalled_on
    return Rig(actions, player, mpd, watch, replies, night)


class Rig:
    def __init__(self, actions, player, mpd, watch, replies, night):
        self.actions, self.player, self.mpd, self.watch = actions, player, mpd, watch
        self.replies, self.night = replies, night
        self.t = 1000.0
        self.resumes = []         # clock times of each `play` the watchdog asked for
        real = player.resume_mpd

        def resume(gen):
            self.resumes.append(self.t)
            return real(gen)

        player.resume_mpd = resume

    def settle(self):
        assert self.player.wait_idle(3), "the player's worker did not settle"

    def press(self, key):
        self.actions.dispatch(Event(key, "tap"))
        self.settle()

    def run(self, seconds, step=1.0):
        end = self.t + seconds
        while self.t < end:
            self.t += step
            self.watch.check(self.t)
            self.settle()

    def playing_now(self):
        """Started and heard: the watchdog has armed."""
        self.run(6)
        assert self.watch._armed


def test_mpd_stopping_by_itself_is_played_again(rig):
    rig.press("k2")
    rig.playing_now()
    gen = rig.player.route_gen
    rig.mpd.state = "stop"                       # nobody asked
    rig.run(wd_mod.STOP_CONFIRM_S + 1)
    assert rig.resumes, "the watchdog did not play MPD again"
    rig.run(6)
    assert not rig.watch.recovering
    assert rig.mpd.state == "play"
    assert rig.player.route_gen == gen           # `play` was enough: no re-start
    assert rig.player.current().n == 2 and "fail" not in rig.replies


def test_while_stalled_status_and_health_tell_the_truth(rig, tmp_path):
    rig.press("k2")
    rig.playing_now()
    rig.mpd.plays = False
    rig.mpd.state = "stop"
    rig.run(wd_mod.STOP_CONFIRM_S + 2)
    assert rig.watch.recovering and rig.watch.stalled_s() > 0
    app = WebApp(rig.actions, Panel.from_variant(rig.actions.variant, fake=True), tmp_path / "keys.yaml",
                 {"radio": True, "spotify": True, "ytm": True}, ha=False)
    app.stream_watch = rig.watch
    rig.player.get_volume = lambda: 40.0
    st = app.status()
    assert st["playing"] is None and st["recovering"] is True and st["stalled_s"] is not None
    health = health_report(app)
    assert health["playing"] is False and health["recovering"] is True


def test_play_that_does_not_help_restarts_the_preset(rig):
    rig.press("k2")
    rig.playing_now()
    gen = rig.player.route_gen
    rig.mpd.state = "stop"
    rig.mpd.queue = []                           # `play` on an empty queue does nothing
    rig.run(wd_mod.STOP_CONFIRM_S + 1 + wd_mod.VERIFY_S + 1)
    assert rig.player.route_gen != gen           # started again, through the router
    rig.run(6)
    assert not rig.watch.recovering
    assert rig.mpd.state == "play" and rig.mpd.queue == [SJ]
    assert rig.player.current().n == 2


def test_elapsed_frozen_counts_as_stalled(rig):
    rig.press("k2")
    rig.playing_now()
    rig.mpd.flowing = False
    rig.run(wd_mod.STALL_S + 3)
    assert rig.watch.recovering or rig.resumes
    rig.mpd.flowing = True
    rig.run(wd_mod.VERIFY_S + 4)
    assert not rig.watch.recovering


def test_repeated_failure_backs_off_and_pulses_red_once(rig):
    rig.press("k2")
    rig.playing_now()
    rig.mpd.plays = False                        # nothing brings it back
    rig.mpd.state = "stop"
    rig.run(600)
    assert rig.watch.recovering
    assert rig.replies.count("fail") == 1
    gaps = [b - a for a, b in zip(rig.resumes, rig.resumes[1:])]
    cycle = wd_mod.VERIFY_S + wd_mod.RESTART_VERIFY_S
    assert gaps[:4] == [cycle + b for b in wd_mod.BACKOFF_S]
    # Still the owner's preset: the next press plays it, not "again" (standby).
    rig.mpd.plays = True
    rig.press("k2")
    assert rig.actions.standby is False
    rig.run(6)
    assert not rig.watch.recovering and rig.mpd.state == "play"


def test_no_red_pulses_at_night(rig):
    rig.night["on"] = True
    rig.press("k2")
    rig.playing_now()
    rig.mpd.plays = False
    rig.mpd.state = "stop"
    rig.run(300)
    assert rig.watch.recovering and "fail" not in rig.replies


def test_same_key_again_is_a_deliberate_stop(rig):
    rig.press("k2")
    rig.playing_now()
    rig.press("k2")                              # radio again: standby
    assert rig.actions.standby
    rig.run(60)
    assert rig.resumes == [] and not rig.watch.recovering
    assert rig.player.current() is None


def test_page_stop_and_standby_mid_recovery_stand_it_down(rig):
    rig.press("k2")
    rig.playing_now()
    rig.mpd.plays = False
    rig.mpd.state = "stop"
    rig.run(wd_mod.STOP_CONFIRM_S + 2)
    assert rig.watch.recovering
    rig.player.stop()                            # the page's stop
    rig.settle()
    count = len(rig.resumes)
    rig.run(400)
    assert len(rig.resumes) == count and not rig.watch.recovering


def test_a_phone_taking_over_is_left_alone(rig):
    rig.press("k2")
    rig.playing_now()
    rig.player.active_source = lambda: "bluetooth"    # moOde stopped MPD for a phone
    rig.mpd.state = "stop"
    rig.run(60)
    assert rig.resumes == [] and not rig.watch.recovering
    rig.player.active_source = lambda: "radio"        # the phone left; MPD still stopped
    rig.run(60)
    assert rig.resumes == []                          # not ours to undo (resume_after_cast is)


def test_a_start_that_never_sounded_is_not_the_watchdogs(rig):
    rig.mpd.plays = False
    rig.press("k2")                              # MPD never starts: the loading lamp's job
    rig.run(60)
    assert rig.resumes == [] and not rig.watch.recovering


def test_pause_is_somebodys_choice(rig):
    rig.press("k2")
    rig.playing_now()
    rig.mpd.state = "pause"
    rig.run(60)
    assert rig.resumes == [] and not rig.watch.recovering


def test_mpd_error_is_logged(rig, caplog):
    rig.press("k2")
    rig.playing_now()
    rig.mpd.state, rig.mpd.error = "stop", "Failed to decode http://stream: connection reset"
    with caplog.at_level("INFO", logger="haslerd.player.watchdog"):
        rig.run(wd_mod.STOP_CONFIRM_S + 1)
    text = caplog.text
    assert "play -> stop" in text and "connection reset" in text and "recovering" in text


def test_a_ytm_playlist_that_ended_is_not_restarted(rig):
    rig.press("k3")
    rig.playing_now()
    rig.mpd.song = 2                             # the last of three tracks
    rig.run(4)
    rig.mpd.state, rig.mpd.song = "stop", None   # MPD ran off the end of the queue
    rig.run(30)
    assert rig.resumes == [] and not rig.watch.recovering
    assert rig.watch.silent                      # but it no longer claims to play


def test_lamp_leaves_playing_while_recovering(rig):
    class Driver:
        def __init__(self):
            self.looks = []

        def show(self, look, fade):
            self.looks.append(look)

        def reply(self, r):
            pass

        def set_night(self, n):
            pass

        def countdown(self, p):
            pass

        def close(self, halt_dark=True):
            pass

    keeper = LampKeeper(Driver(), rig.actions, clock=lambda: rig.t, night=lambda *_: False)
    keeper.watchdog = rig.watch
    rig.press("k2")
    rig.playing_now()
    keeper._loading = None
    keeper._started -= 1000                      # past the boot warm-up
    keeper._resume_done = True
    assert keeper.gather(rig.t).playing is True
    rig.mpd.plays = False
    rig.mpd.state = "stop"
    rig.run(wd_mod.STOP_CONFIRM_S + 2)
    state = keeper.gather(rig.t)
    assert state.playing is False and state.loading is True


def test_events_go_on_the_card_and_into_health(rig, tmp_path):
    from haslerd.player.watchdog import StreamEvents
    rig.watch.events = StreamEvents(tmp_path / "stream-events.jsonl")
    rig.press("k2")
    rig.playing_now()
    rig.mpd.state, rig.mpd.error = "stop", "connection reset"
    rig.run(wd_mod.STOP_CONFIRM_S + 1)
    rig.run(6)
    import json
    lines = [json.loads(x) for x in (tmp_path / "stream-events.jsonl").read_text().splitlines()]
    actions = [e["action"] for e in lines]
    assert actions[:4] == ["mpd stopped", "stalled", "play", "recovered"]
    assert lines[0]["error"] == "connection reset" and lines[0]["preset"]["n"] == 2
    assert lines[0]["mpd_state"] == "stop" and lines[0]["t"]

    class App:
        pass
    app = App()
    app.actions, app.stream_watch, app.panel, app.lamp = rig.actions, rig.watch, None, True
    recent = health_report(app)["stream_events_recent"]
    assert [e["action"] for e in recent][-1] == "recovered"
    # Read back at the next start.
    assert StreamEvents(tmp_path / "stream-events.jsonl").recent()[-1]["action"] == "recovered"


def test_events_file_stays_small(tmp_path):
    from haslerd.player.watchdog import StreamEvents
    path = tmp_path / "e.jsonl"
    ev = StreamEvents(path, keep=20, max_bytes=4096)
    for i in range(500):
        ev.add("play", Preset(2, "Jazz", SJ), "stop", "x" * 50, attempt=i)
    lines = path.read_text().splitlines()
    assert len(lines) <= 20 and path.stat().st_size <= 4096
    import json
    assert json.loads(lines[-1])["attempt"] == 499


def test_events_never_raise_on_a_bad_card(tmp_path):
    from haslerd.player.watchdog import StreamEvents
    ev = StreamEvents(tmp_path / "missing-dir" / "e.jsonl")
    ev.add("play", None, "stop", None)
    assert ev.recent()[-1]["action"] == "play"
