"""The source router inside MoodePlayer, and the radio source, with no MPD/HTTP.

Fake sources stand in for go-librespot and the YTM proxy; a fake MPD client
stands in for MPD. What is under test is the arbitration and bookkeeping.
"""

import subprocess

import pytest

from haslerd.player.base import Player, Preset, Source
from haslerd.player.moode import (
    MoodeConfig,
    MoodePlayer,
    SpotifyConfig,
    YtmConfig,
    bluetooth_pair_script,
    parse_amixer_percent,
    scale_volume,
    unscale_volume,
)
from haslerd.player.sources.radio import MpdConnection, RadioSource, is_proxy_url

RADIO = Preset(1, "Radio", "http://example.invalid/r")
SPOTIFY = Preset(2, "Spotify", "spotify:playlist:abc", source="spotify")
YTM = Preset(3, "YTM", "https://music.youtube.com/playlist?list=xyz", source="ytm")


class FakeSource:
    def __init__(self, name, log):
        self.name = name
        self.log = log
        self.active = False
        self.fail = None
        self.status_reads = 0

    def play(self, preset):
        self.log.append((self.name, "play", preset.n))
        if self.fail:
            raise self.fail
        self.active = True

    def stop(self):
        self.log.append((self.name, "stop"))
        self.active = False

    def next_track(self):
        self.log.append((self.name, "next"))

    def is_active(self):
        self.status_reads += 1
        return self.active

    def close(self):
        self.log.append((self.name, "close"))


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def router():
    log = []
    sources = {n: FakeSource(n, log) for n in ("radio", "spotify", "ytm")}
    clock = Clock()
    # No moOde flags: the fallback must not shell out during tests.
    player = MoodePlayer(MoodeConfig(source_flags={}), sources=sources, clock=clock)
    return player, sources, log, clock


class TestProtocols:
    def test_fake_source_and_radio_satisfy_source(self, router):
        _, sources, _, _ = router
        assert isinstance(sources["spotify"], Source)
        assert isinstance(RadioSource(mpd=lambda: None), Source)

    def test_moode_player_satisfies_player(self, router):
        assert isinstance(router[0], Player)


class TestArbitration:
    @pytest.mark.parametrize("preset", [RADIO, SPOTIFY, YTM])
    def test_preset_goes_to_its_source_after_all_others_stop(self, router, preset):
        player, _, log, _ = router
        player.play_preset(preset)
        others = {n for n in ("radio", "spotify", "ytm") if n != preset.source}
        stops = [entry for entry in log if entry[1] == "stop"]
        assert {s[0] for s in stops} == others
        assert log[-1] == (preset.source, "play", preset.n), "play comes after every stop"
        assert player.current() == preset

    def test_the_chosen_source_is_not_stopped_before_it_plays(self, router):
        player, _, log, _ = router
        player.play_preset(SPOTIFY)
        assert ("spotify", "stop") not in log

    def test_a_source_that_fails_to_stop_does_not_block_the_next(self, router):
        player, sources, log, _ = router

        def boom():
            raise ConnectionError("go-librespot down")

        sources["spotify"].stop = boom
        player.play_preset(RADIO)
        assert log[-1] == ("radio", "play", 1)

    def test_failed_play_raises_and_leaves_no_current(self, router):
        player, sources, _, _ = router
        player.play_preset(RADIO)
        sources["spotify"].fail = RuntimeError("not logged in")
        with pytest.raises(RuntimeError):
            player.play_preset(SPOTIFY)
        assert player.current() is None

    def test_unknown_or_missing_source_raises(self, router):
        player, sources, _, _ = router
        del sources["ytm"]
        with pytest.raises(LookupError):
            player.play_preset(YTM)

    def test_stop_stops_everything_and_clears_current(self, router):
        player, _, log, _ = router
        player.play_preset(YTM)
        log.clear()
        player.stop()
        assert {e[0] for e in log if e[1] == "stop"} == {"radio", "spotify", "ytm"}
        assert player.current() is None

    def test_next_track_goes_to_the_current_source_only(self, router):
        player, _, log, _ = router
        player.next_track()                       # nothing playing: no-op
        player.play_preset(SPOTIFY)
        player.next_track()
        assert [e for e in log if e[1] == "next"] == [("spotify", "next")]

    def test_close_closes_radio_last(self, router):
        player, _, log, _ = router
        player.close()
        assert [e[0] for e in log if e[1] == "close"][-1] == "radio"


class TestOpsIntegration:
    def test_moodes_spotify_renderer_is_not_consulted(self):
        config = MoodeConfig()
        assert "spotify" not in config.source_flags
        assert "spotify" not in config.renderer_units

    def test_taking_the_path_back_for_mpd_pauses_go_librespot(self, router):
        player, _, log, _ = router
        player.play_preset(SPOTIFY)
        player.config.renderer_units = {}
        player.select_source("radio")
        assert log[-1] == ("spotify", "stop")
        assert player.current() is None

    def test_mpd_failing_after_a_spotify_pause_says_why(self, router, caplog):
        player, sources, _, _ = router
        player.play_preset(SPOTIFY)
        sources["radio"].fail = RuntimeError("device busy")
        with pytest.raises(RuntimeError):
            player.play_preset(RADIO)
        assert "holding ALSA" in caplog.text


class TestActiveSource:
    def test_reports_spotify_and_ytm_from_the_sources_themselves(self, router):
        player, sources, _, clock = router
        assert player.active_source() == "radio"
        sources["spotify"].active = True
        clock.t += 2
        assert player.active_source() == "spotify"
        sources["spotify"].active = False
        sources["ytm"].active = True
        clock.t += 2
        assert player.active_source() == "ytm"

    def test_cached_for_about_a_second(self, router):
        player, sources, _, clock = router
        player.active_source()
        reads = sources["spotify"].status_reads
        clock.t += 0.5
        player.active_source()
        assert sources["spotify"].status_reads == reads
        clock.t += 0.6
        player.active_source()
        assert sources["spotify"].status_reads == reads + 1

    def test_playing_something_drops_the_cache(self, router):
        player, _, _, _ = router
        assert player.active_source() == "radio"
        player.play_preset(SPOTIFY)
        assert player.active_source() == "spotify"

    def test_a_source_whose_status_raises_reads_as_inactive(self, router):
        player, sources, _, _ = router

        def boom():
            raise OSError("refused")

        sources["spotify"].is_active = boom
        assert player.active_source() == "radio"


class TestConfig:
    def test_defaults(self):
        config = MoodeConfig()
        assert config.spotify == SpotifyConfig(True, "http://127.0.0.1:3678", 3.0)
        assert config.ytm == YtmConfig(True, 8099, "yt-dlp", None)

    def test_yaml_blocks_become_dataclasses(self):
        config = MoodeConfig(spotify={"timeout": 1.5}, ytm={"port": 9000, "cookies": "/c.txt"})
        assert config.spotify.timeout == 1.5
        assert config.ytm.port == 9000 and config.ytm.cookies == "/c.txt"

    def test_unknown_keys_fail_at_startup(self):
        with pytest.raises(TypeError):
            MoodeConfig(spotify={"bogus": 1})


class TestBluetoothPair:
    def test_window_is_honoured(self):
        script = bluetooth_pair_script(90)
        assert "discoverable-timeout 90\n" in script
        assert "pairable-timeout 90\n" in script
        # Timeouts first: BlueZ applies them when discoverable turns on.
        assert script.index("discoverable-timeout") < script.index("discoverable on")


    def _player(self, monkeypatch, bt_active, hang=False):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "systemctl":
                return subprocess.CompletedProcess(cmd, 0 if bt_active else 3)
            if hang:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("haslerd.player.moode.shutil.which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr("haslerd.player.moode.subprocess.run", run)
        player = MoodePlayer(MoodeConfig(source_flags={}), sources={})
        return player, calls

    def test_bluetooth_off_returns_at_once_without_bluetoothctl(self, monkeypatch, caplog):
        player, calls = self._player(monkeypatch, bt_active=False)
        player.bluetooth_pair(120)
        assert [c[0] for c in calls] == ["systemctl"]
        assert "bluetooth is off" in caplog.text

    def test_bluetooth_on_runs_the_script(self, monkeypatch):
        player, calls = self._player(monkeypatch, bt_active=True)
        player.bluetooth_pair(120)
        assert [c[0] for c in calls] == ["systemctl", "bluetoothctl"]

    def test_a_hung_bluetoothctl_is_logged_not_raised(self, monkeypatch, caplog):
        player, _ = self._player(monkeypatch, bt_active=True, hang=True)
        player.bluetooth_pair(120)
        assert "did not answer" in caplog.text


class TestVolumeReadback:
    def test_amixer_percent_is_parsed(self):
        out = "Simple mixer control 'Digital',0\n  Front Left: Playback 154 [75%] [-25.50dB] [on]\n"
        assert parse_amixer_percent(out) == 75
        assert parse_amixer_percent("garbage") is None

    def test_unscale_inverts_the_ceiling(self):
        assert unscale_volume(35, 70) == 50.0
        assert unscale_volume(scale_volume(80, 70), 70) == pytest.approx(80, abs=1)

    def test_mixer_above_the_ceiling_reads_as_knob_max(self):
        assert unscale_volume(100, 70) == 100.0

    def test_unreadable_mixer_falls_back_to_last_set(self, router):
        player, _, _, _ = router
        player.config.amixer = "/nonexistent/amixer"
        assert player.get_volume() is None
        player._knob = 42.0
        assert player.get_volume() == 42.0


class FakeMpd:
    def __init__(self, state="play", file="http://example.invalid/r"):
        self.state = state
        self.file = file
        self.calls = []
        self.connected = False
        self.timeout = None
        self.ping_fails = False

    def connect(self, host, port):
        self.connected = True
        self.calls.append(("connect", host, port))

    def disconnect(self):
        self.connected = False

    def ping(self):
        if self.ping_fails:
            raise ConnectionError("gone")

    def status(self):
        return {"state": self.state}

    def currentsong(self):
        return {"file": self.file}

    def __getattr__(self, name):
        if name in ("clear", "add", "play", "stop", "random", "next"):
            return lambda *a: self.calls.append((name, *a))
        raise AttributeError(name)


class TestRadioSource:
    def test_play_queues_exactly_the_stream(self):
        mpd = FakeMpd()
        RadioSource(mpd=lambda: mpd).play(RADIO)
        assert [c[0] for c in mpd.calls] == ["clear", "random", "add", "play"]
        assert ("add", RADIO.uri) in mpd.calls

    def test_active_only_for_a_non_proxy_url(self):
        mpd = FakeMpd()
        radio = RadioSource(mpd=lambda: mpd, ytm_port=8099)
        assert radio.is_active() is True
        mpd.file = "http://127.0.0.1:8099/ytm/dQw4w9WgXcQ"
        assert radio.is_active() is False
        mpd.file = "http://example.invalid/r"
        mpd.state = "stop"
        assert radio.is_active() is False

    def test_next_track_is_a_no_op(self):
        mpd = FakeMpd()
        RadioSource(mpd=lambda: mpd).next_track()
        assert mpd.calls == []

    def test_mpd_down_reads_as_inactive_and_stop_does_not_raise(self):
        def down():
            raise ConnectionRefusedError()

        radio = RadioSource(mpd=down)
        assert radio.is_active() is False
        radio.stop()

    def test_proxy_url_detection(self):
        assert is_proxy_url("http://127.0.0.1:8099/ytm/abc", 8099)
        assert is_proxy_url("http://localhost:8099/ytm/abc", 8099)
        assert not is_proxy_url("http://127.0.0.1:8098/ytm/abc", 8099)
        assert not is_proxy_url("http://stream.example:8099/live", 8099)


class TestMpdConnection:
    def test_connects_lazily_and_reuses(self):
        made = []

        def factory():
            made.append(FakeMpd())
            return made[-1]

        conn = MpdConnection("h", 1234, client_factory=factory)
        assert made == []
        conn().stop()
        conn().stop()
        assert len(made) == 1
        assert made[0].calls[0] == ("connect", "h", 1234)

    def test_reconnects_after_mpd_restarts(self):
        made = []

        def factory():
            made.append(FakeMpd())
            return made[-1]

        conn = MpdConnection(client_factory=factory)
        conn()
        made[0].ping_fails = True
        conn().play()
        assert len(made) == 2
        assert ("play",) in made[1].calls

    def test_shared_factory_is_what_ytm_gets(self):
        radio = RadioSource()
        assert isinstance(radio.mpd, MpdConnection)


class TestPrefetch:
    def test_prefetch_hands_presets_to_sources_that_can_warm(self, router):
        player, sources, log, _ = router
        warmed = []
        sources["ytm"].warm = lambda presets: warmed.append([p.n for p in presets])
        player.prefetch([RADIO, SPOTIFY, YTM])
        assert warmed == [[RADIO.n, SPOTIFY.n, YTM.n]]  # the source filters its own

    def test_a_failing_warm_up_does_not_escape(self, router):
        player, sources, _, _ = router

        def boom(presets):
            raise RuntimeError("no network")

        sources["ytm"].warm = boom
        player.prefetch([YTM])  # logged, not raised
