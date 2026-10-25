"""Quick preset presses against a slow MPD (the 2026-10-21 bench log).

MPD answers no client at all while it stops a stream - 0.5-4 s on the
bench, its output closing through moOde's CamillaDSP plugin. Each press used
to stop MPD twice (ytm's stop, then radio's clear) on the press's own thread,
and the next press queued behind it until the socket timed out. Here: presses
return at once, a newer press supersedes a pending one, radio and YTM do not
stop each other, and a timed-out MPD command never leaves its late answer for
the next caller.
"""

import threading
import time

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.main import preset_sounding
from haslerd.panel.controls import Event
from haslerd.player.base import Preset
from haslerd.player.moode import MoodeConfig, MoodePlayer
from haslerd.player.sources.radio import MpdConnection, MpdUnresponsive
from haslerd.variant import load_variant

R1 = Preset(1, "Swiss Jazz", "http://example.invalid/sj")
R2 = Preset(2, "RNE 3", "http://example.invalid/r3.m3u8")
R3 = Preset(3, "Classic", "http://example.invalid/sc")
SPOT = Preset(4, "Spotify", "spotify:playlist:abc", source="spotify")
YTM = Preset(5, "YTM", "https://music.youtube.com/playlist?list=xyz", source="ytm")


class SlowSource:
    """A source whose play/stop can be made to hang, like MPD mid-stop."""

    def __init__(self, name, log, backend=None):
        self.name = name
        self.log = log
        if backend:
            self.backend = backend
        self.gate = threading.Event()
        self.gate.set()
        self.entered = threading.Event()
        self.fail = None
        self.active = False

    def play(self, preset):
        self.log.append((self.name, "play", preset.n))
        self.entered.set()
        assert self.gate.wait(5)
        if self.fail:
            raise self.fail
        self.active = True

    def stop(self):
        self.log.append((self.name, "stop"))
        self.active = False

    def next_track(self):
        pass

    def is_active(self):
        self.log.append((self.name, "is_active"))
        return self.active

    def close(self):
        pass


@pytest.fixture
def box():
    log = []
    sources = {
        "radio": SlowSource("radio", log, backend="mpd"),
        "spotify": SlowSource("spotify", log),
        "ytm": SlowSource("ytm", log, backend="mpd"),
    }
    player = MoodePlayer(MoodeConfig(source_flags={}), sources=sources)
    return player, sources, log


def settle(player, timeout=3.0):
    assert player.wait_idle(timeout), "the player's worker did not settle"


class TestSharedBackend:
    def test_radio_does_not_stop_ytm_and_back(self, box):
        player, _, log = box
        player.play_preset(R1)
        assert ("ytm", "stop") not in log
        assert ("radio", "stop") not in log
        assert ("spotify", "stop") in log
        log.clear()
        player.play_preset(YTM)
        assert ("radio", "stop") not in log and ("ytm", "stop") not in log

    def test_spotify_stops_mpd_once(self, box):
        player, _, log = box
        player.play_preset(SPOT)
        stops = [e for e in log if e[1] == "stop"]
        assert len(stops) == 1 and stops[0][0] in ("radio", "ytm")

    def test_stop_stops_each_backend_once(self, box):
        player, _, log = box
        player.stop()
        assert sorted(e[0] for e in log if e[1] == "stop") in (["radio", "spotify"], ["spotify", "ytm"])


class TestStartInTheBackground:
    def test_start_returns_at_once_while_the_source_hangs(self, box):
        player, sources, log = box
        sources["radio"].gate.clear()
        t = time.monotonic()
        player.start_preset(R1)
        assert time.monotonic() - t < 0.2
        assert player.current() == R1
        assert sources["radio"].entered.wait(2)
        # The router answers from what it asked for: no MPD question meanwhile.
        log.clear()
        assert player.active_source() == "radio"
        assert not [e for e in log if e[1] == "is_active"]
        assert player.still_playing(R1) is False
        sources["radio"].gate.set()
        settle(player)
        assert player.starting is None and player.current() == R1

    def test_a_newer_press_supersedes_the_pending_ones(self, box):
        player, sources, log = box
        sources["radio"].gate.clear()
        player.start_preset(R1)
        assert sources["radio"].entered.wait(2)
        for preset in (R2, R3, R2, R3):         # pressed while R1 is still starting
            player.start_preset(preset)
        sources["radio"].gate.set()
        settle(player)
        plays = [e[2] for e in log if e[1] == "play"]
        assert plays == [1, 3], "the one in flight, then only the newest"
        assert player.current() == R3

    def test_stop_soon_supersedes_a_pending_start(self, box):
        player, sources, log = box
        sources["radio"].gate.clear()
        player.start_preset(R1)
        assert sources["radio"].entered.wait(2)
        player.start_preset(R2)
        player.stop_soon()
        assert player.current() is None and player.starting is None
        sources["radio"].gate.set()
        settle(player)
        assert [e[2] for e in log if e[1] == "play"] == [1]
        assert player.current() is None

    def test_a_failure_reaches_on_error_only_if_still_wanted(self, box):
        player, sources, _ = box
        errors = []
        sources["radio"].fail = RuntimeError("MPD timed out")
        player.start_preset(R1, on_error=errors.append)
        settle(player)
        assert len(errors) == 1 and player.current() is None
        # Superseded while failing: nobody is told, the newer one stands.
        sources["radio"].gate.clear()
        player.start_preset(R1, on_error=errors.append)
        assert sources["radio"].entered.wait(2)
        sources["radio"].fail = None
        sources["ytm"].fail = None
        player.start_preset(YTM, on_error=errors.append)
        sources["radio"].fail = RuntimeError("late")
        sources["radio"].gate.set()
        settle(player)
        assert len(errors) == 1
        assert player.current() == YTM

    def test_unknown_source_raises_at_once(self, box):
        player, sources, _ = box
        del sources["ytm"]
        with pytest.raises(LookupError):
            player.start_preset(YTM)

    def test_loading_lamp_does_not_ask_mpd_while_starting(self, box):
        player, sources, _ = box
        asked = []
        sources["radio"].mpd = lambda: asked.append(1)
        sources["radio"].gate.clear()
        player.start_preset(R1)
        assert preset_sounding(player, R1) is False
        assert asked == []
        sources["radio"].gate.set()
        settle(player)


class TestPressesThroughActions:
    @pytest.fixture
    def radio(self, box):
        player, sources, log = box
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        variant.presets = [
            {"n": 1, "name": "Swiss Jazz", "radio": R1.uri},
            {"n": 2, "name": "RNE 3", "radio": R2.uri},
            {"n": 3, "name": "Classic", "radio": R3.uri},
        ]
        actions = Actions(variant, player)
        player.set_mute = lambda muted: None
        replies = []
        actions.on_reply = replies.append
        return actions, player, sources, log, replies

    def test_quick_presses_never_wait_for_mpd(self, radio):
        actions, player, sources, log, _ = radio
        sources["radio"].gate.clear()          # MPD busy stopping the last stream
        t = time.monotonic()
        for key in ("k3", "k2", "k3", "k2"):
            actions.dispatch(Event(key, "tap"))
        assert time.monotonic() - t < 0.5
        sources["radio"].gate.set()
        settle(player)
        assert player.current().n == 2
        assert [e[2] for e in log if e[1] == "play"][-1] == 2
        assert ("ytm", "stop") not in log

    def test_same_key_again_while_starting_is_still_again(self, radio):
        actions, player, sources, log, replies = radio
        actions.clock = lambda: 1000.0
        sources["radio"].gate.clear()
        actions.dispatch(Event("k2", "tap"))
        actions.clock = lambda: 1002.0          # past the bounce guard
        actions.dispatch(Event("k2", "tap"))   # radio again: standby
        assert actions.standby is True and "stop" in replies
        sources["radio"].gate.set()
        settle(player)
        assert player.current() is None

    def test_a_background_failure_turns_the_lamp_red(self, radio):
        actions, player, sources, _, replies = radio
        errors = []
        actions.on_play_error = errors.append
        sources["radio"].fail = ConnectionError("MPD timed out")
        actions.dispatch(Event("k1", "tap"))
        settle(player)
        assert len(errors) == 1 and "fail" in replies


class TimingOutMpd:
    """MPD that times out once, then answers the *late* reply first."""

    def __init__(self):
        self.timeout = None
        self.calls = []
        self.time_out_next = False

    def connect(self, host, port):
        self.calls.append("connect")

    def disconnect(self):
        self.calls.append("disconnect")

    def ping(self):
        pass

    def stop(self):
        if self.time_out_next:
            self.time_out_next = False
            raise TimeoutError("timed out")

    def status(self):
        return {"state": "play"}


class TestMpdConnectionAfterATimeout:
    def test_a_timed_out_command_drops_the_connection(self):
        made = []

        def factory():
            made.append(TimingOutMpd())
            return made[-1]

        conn = MpdConnection(client_factory=factory, prober=lambda h, p, t: "ok")
        conn()
        made[0].time_out_next = True
        with pytest.raises(TimeoutError):
            conn().stop()
        assert "disconnect" in made[0].calls
        # MPD is marked unresponsive: the next caller fails fast (2026-10-25)...
        with pytest.raises(MpdUnresponsive):
            conn().status()
        assert len(made) == 1
        # ...until a probe of its own hears MPD again: then a fresh connection.
        assert conn.probe() == "ok"
        conn().status()
        assert len(made) == 2, "the next command gets a fresh connection"

    def test_an_mpd_refusal_keeps_the_connection(self):
        mpd = pytest.importorskip("mpd")

        class Refusing(TimingOutMpd):
            def stop(self):
                raise mpd.CommandError("[50@0] {stop} nope")

        made = []
        conn = MpdConnection(client_factory=lambda: made.append(Refusing()) or made[-1])
        with pytest.raises(mpd.CommandError):
            conn().stop()
        conn().status()
        assert len(made) == 1

    def test_default_timeout_outlasts_mpds_slow_stop(self):
        assert MpdConnection().timeout >= 8
