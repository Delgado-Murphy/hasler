"""The boot resume (bootresume.py): waits for the player, swells, sticks, sleeps at night."""

import logging
import socket
import sqlite3
import subprocess
import threading

import pytest
from conftest import VARIANTS

from haslerd import bootresume
from haslerd.actions import Actions
from haslerd.bootresume import (
    BootResume, FirstSound, Probe, clock_synced, mpd_answers, night_hold, shutdown_kind,
)
from haslerd.main import BOOT_MAX_S, LampKeeper
from haslerd.lamp import policy
from haslerd.player.base import FakePlayer
from haslerd.player.moode import MoodeConfig, MoodePlayer
from haslerd.state import StateStore
from haslerd.variant import load_variant


def inline(fn):
    fn()


class Clock:
    def __init__(self):
        self.t = 10.0

    def __call__(self):
        return self.t


class Player(FakePlayer):
    """FakePlayer plus the boot resume's hooks, all recorded."""

    def __init__(self):
        super().__init__()
        self.answers = False
        self.sounds = True
        self.still = True
        self.wrk = None
        self.holds = 0
        self.releases = 0
        self.swells = 0
        self.reasserts = 0

    def answering(self, source):
        return self.answers

    def still_playing(self, preset):
        return self.still

    def wrkready(self):
        return self.wrk

    def hold_silent(self):
        self.holds += 1

    def release_hold(self):
        self.releases += 1

    def swell(self):
        self.swells += 1

    def reassert_volume(self):
        self.reasserts += 1


@pytest.fixture
def box(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    clock = Clock()
    player = Player()
    state = StateStore(tmp_path / "state.json")
    state.save({"last_preset": 2, "standby": False})
    actions = Actions(variant, player, state=state, clock=clock)
    replies = []
    actions.on_reply = replies.append
    actions.restore()
    outcomes = []
    resume = BootResume(
        actions, sounding=lambda pl, p: pl.sounds and pl.current() == p,
        on_done=outcomes.append, clock=clock, spawn=inline,
    )
    return clock, player, actions, resume, replies, outcomes


def run(resume, clock, seconds, step=0.25):
    end = clock.t + seconds
    while clock.t < end:
        clock.t += step
        resume.tick(clock.t)


def plays(player):
    return [c for c in player.calls if c[0] == "play_preset"]


class TestWaitsForThePlayer:
    def test_nothing_starts_until_the_player_answers_then_at_once(self, box):
        clock, player, _, resume, replies, outcomes = box
        run(resume, clock, 80)                  # MPD late: longer than the old 6 x 10 s
        assert plays(player) == [] and resume.pending and replies == []
        player.answers = True
        run(resume, clock, 2.0)
        assert plays(player) == [("play_preset", 2, "radio")]
        assert player.holds == 1                # silent first, for the swell
        assert outcomes == ["playing"] and player.swells == 1

    def test_polls_about_twice_a_second(self, box):
        clock, player, actions, resume, _, _ = box
        asked = []
        resume.ready = lambda p: asked.append(clock.t) or False
        run(resume, clock, 5.0, step=0.02)
        assert 9 <= len(asked) <= 11

    def test_a_start_that_raises_is_retried_quietly_then_fails_after_the_window(self, box):
        clock, player, _, resume, replies, outcomes = box
        player.answers = True
        player.fail_play = RuntimeError("no network yet")
        run(resume, clock, 60)
        attempts = len(plays(player))
        assert 10 <= attempts <= 14             # every RESUME_RETRY_S
        assert player.releases >= attempts     # the mixer never left at 0
        assert replies.count("fail") == 0 and outcomes == []
        run(resume, clock, 100)
        assert replies.count("fail") == 1 and outcomes == ["failed"]
        assert not resume.pending

    def test_works_after_a_slow_network_within_the_window(self, box):
        clock, player, _, resume, replies, outcomes = box
        player.answers = True
        player.fail_play = RuntimeError("offline")
        run(resume, clock, 100)
        player.fail_play = None
        run(resume, clock, 6)
        assert outcomes == ["playing"] and "fail" not in replies

    def test_started_but_silent_is_started_again(self, box):
        clock, player, _, resume, _, outcomes = box
        player.answers, player.sounds = True, False
        run(resume, clock, 1)
        assert len(plays(player)) == 1
        run(resume, clock, bootresume.RESUME_SOUND_S + 1)
        assert len(plays(player)) == 2 and outcomes == []
        player.sounds = True
        run(resume, clock, 1)
        assert outcomes == ["playing"]

    def test_a_key_press_cancels_quietly(self, box):
        clock, player, _, resume, replies, outcomes = box
        run(resume, clock, 3)
        resume.cancel("a hand on the radio")
        player.answers = True
        run(resume, clock, 200)
        assert plays(player) == [] and replies == [] and outcomes == ["cancelled"]

    def test_standby_cancels(self, box):
        clock, player, actions, resume, _, outcomes = box
        actions.enter_standby()
        player.answers = True
        run(resume, clock, 2)
        assert plays(player) == [] and outcomes == ["cancelled"]

    def test_nothing_to_resume_finishes_at_once(self, tmp_path):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        player = Player()
        outcomes = []
        resume = BootResume(Actions(variant, player), on_done=outcomes.append, spawn=inline)
        resume.tick(1.0)
        assert outcomes == ["nothing"] and not resume.pending

    def test_another_start_meanwhile_ends_it(self, box):
        clock, player, actions, resume, _, outcomes = box
        player.answers, player.sounds = True, False
        run(resume, clock, 1)
        actions.presets[1] and player.play_preset(actions.presets[1])    # the web page
        run(resume, clock, 1)
        assert outcomes == ["cancelled"]


class TestProbe:
    def test_one_at_a_time_and_tagged(self):
        held = []
        probe = Probe(spawn=held.append)
        probe.ask("a", lambda: 1)
        probe.ask("a", lambda: 2)               # still busy: ignored
        assert len(held) == 1
        held[0]()
        assert probe.take("b") is None          # wrong question: dropped
        probe.ask("a", lambda: 3)
        held[1]()
        assert probe.take("a") == 3 and probe.take("a") is None

    def test_never_blocks_the_caller(self):
        gate = threading.Event()
        probe = Probe()
        probe.ask("slow", lambda: gate.wait(5))
        assert probe.take("slow") is None       # returned at once
        gate.set()


class TestGuard:
    def playing(self, box):
        clock, player, _, resume, _, outcomes = box
        player.answers = True
        run(resume, clock, 2)
        assert outcomes == ["playing"] and resume.phase == "guard"
        return clock, player, resume

    def test_restarts_once_if_moode_stops_it(self, box):
        clock, player, resume = self.playing(box)
        player.still = False
        run(resume, clock, 3)
        assert len(plays(player)) == 2 and player.reasserts == 1
        run(resume, clock, 10)
        assert len(plays(player)) == 2          # once only

    def test_one_odd_read_is_not_enough(self, box):
        clock, player, resume = self.playing(box)
        player.still = False
        run(resume, clock, 1.0)
        player.still = True
        run(resume, clock, 5)
        assert len(plays(player)) == 1

    def test_moode_ready_reasserts_the_volume_and_ends_the_guard(self, box):
        clock, player, resume = self.playing(box)
        player.wrk = 0
        run(resume, clock, 2)
        player.wrk = 1
        run(resume, clock, 2)
        assert player.reasserts == 1 and resume.phase == "done"

    def test_a_stale_wrkready_from_last_boot_is_not_trusted(self, box):
        clock, player, resume = self.playing(box)
        player.wrk = 1
        run(resume, clock, 5)
        assert player.reasserts == 0 and resume.phase == "guard"
        run(resume, clock, bootresume.GUARD_S)
        assert resume.phase == "done"

    def test_moode_ready_before_we_played_means_no_guard(self, box):
        clock, player, _, resume, _, outcomes = box
        player.wrk = 0
        run(resume, clock, 2)
        player.wrk, player.answers, player.sounds = 1, True, False
        run(resume, clock, 1)
        player.sounds = True
        run(resume, clock, 1)
        assert outcomes == ["playing"] and resume.phase == "done"

    def test_a_key_press_ends_the_guard(self, box):
        clock, player, resume = self.playing(box)
        resume.cancel("a hand on the radio")
        player.still = False
        run(resume, clock, 5)
        assert len(plays(player)) == 1


class TestNight:
    def make(self, box, hold):
        clock, player, actions, _, replies, _ = box
        outcomes = []
        resume = BootResume(actions, night_hold=hold, on_done=outcomes.append,
                            clock=clock, spawn=inline)
        player.answers = True
        return clock, player, actions, resume, outcomes

    def test_power_cut_at_night_stays_asleep_and_does_not_remember_it(self, box, tmp_path):
        clock, player, actions, resume, outcomes = self.make(
            box, night_hold(True, lambda: True, synced=lambda: True))
        run(resume, clock, 2)
        assert actions.standby and plays(player) == [] and outcomes == ["night"]
        assert player.muted
        assert StateStore(tmp_path / "state.json").load()["standby"] is False

    def test_power_cut_by_day_resumes(self, box):
        clock, player, _, resume, outcomes = self.make(
            box, night_hold(True, lambda: False, synced=lambda: True))
        run(resume, clock, 2)
        assert outcomes == ["playing"]

    def test_clean_shutdown_at_night_resumes(self, box):
        clock, player, _, resume, outcomes = self.make(
            box, night_hold(False, lambda: True, synced=lambda: True))
        run(resume, clock, 2)
        assert outcomes == ["playing"]

    def test_waits_for_the_clock_then_decides(self, box):
        clock = box[0]
        synced = [False]
        hold = night_hold(True, lambda: True, synced=lambda: synced[0], clock=clock)
        clock, player, actions, resume, outcomes = self.make(box, hold)
        run(resume, clock, 10)
        assert outcomes == [] and plays(player) == []
        synced[0] = True
        run(resume, clock, 1)
        assert outcomes == ["night"]

    def test_gives_up_waiting_for_the_clock(self):
        clock = Clock()
        hold = night_hold(True, lambda: True, synced=lambda: False, clock=clock)
        assert hold() is None
        clock.t += bootresume.CLOCK_WAIT_S
        assert hold() is True

    def test_clock_synced(self, tmp_path):
        assert clock_synced(tmp_path / "absent") is True
        (tmp_path / "ts").mkdir()
        assert clock_synced(tmp_path / "ts") is False
        (tmp_path / "ts" / "synchronized").touch()
        assert clock_synced(tmp_path / "ts") is True


class TestShutdownKind:
    def boot(self, tmp_path, boot_id):
        f = tmp_path / "boot_id"
        f.write_text(boot_id + "\n")
        return f

    def test_marker_means_clean_and_is_consumed(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        (state / "clean-shutdown").touch()
        assert shutdown_kind(state, self.boot(tmp_path, "a")) == "clean"
        assert not (state / "clean-shutdown").exists()

    def test_no_marker_means_power_cut(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        assert shutdown_kind(state, self.boot(tmp_path, "a")) == "power-cut"

    def test_a_restart_within_one_boot_is_neither(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        boot = self.boot(tmp_path, "a")
        shutdown_kind(state, boot)
        (state / "clean-shutdown").touch()      # whatever is there is left alone
        assert shutdown_kind(state, boot) == "restart"
        assert (state / "clean-shutdown").exists()
        assert shutdown_kind(state, self.boot(tmp_path, "b")) == "clean"

    def test_unknown_without_a_boot_id(self, tmp_path):
        assert shutdown_kind(tmp_path, tmp_path / "nope") == "unknown"
        assert shutdown_kind(None) == "unknown"


class TestFirstSound:
    def tree(self, tmp_path, state="closed"):
        (tmp_path / "cards").write_text(
            " 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0\n"
            "                      vc4-hdmi-0\n"
            " 2 [sndrpihifiberry]: HifiberryDacp - snd_rpi_hifiberry_dacplus\n"
            "                      snd_rpi_hifiberry_dacplus\n"
        )
        status = tmp_path / "card2" / "pcm0p" / "sub0"
        status.mkdir(parents=True)
        (status / "status").write_text(state + "\n")
        return status / "status"

    def test_logs_once_when_the_pcm_first_runs(self, tmp_path, caplog):
        status = self.tree(tmp_path)
        clock = Clock()
        fs = FirstSound(root=tmp_path, clock=clock)
        with caplog.at_level(logging.INFO, logger="haslerd.bootresume"):
            fs.tick(clock.t)
            assert not fs.fired
            status.write_text("state: RUNNING\nowner_pid   : 812\n")
            clock.t = 23.46
            fs.tick(clock.t)
            assert fs.fired and fs.at == 23.46
            fs.tick(clock.t + 10)
        assert [r.getMessage() for r in caplog.records] == ["first sound at 23.5 s after boot"]

    def test_card_by_index_and_cheap_polling(self, tmp_path):
        status = self.tree(tmp_path, "state: RUNNING")
        clock = Clock()
        fs = FirstSound(card=2, root=tmp_path, clock=clock)
        status.write_text("closed")
        fs.tick(clock.t)
        status.write_text("state: RUNNING")
        fs.tick(clock.t + 0.1)                  # too soon: not read
        assert not fs.fired
        fs.tick(clock.t + 0.5)
        assert fs.fired

    def test_no_amp_card_is_quiet(self, tmp_path):
        (tmp_path / "cards").write_text(" 0 [vc4hdmi0       ]: vc4-hdmi\n")
        fs = FirstSound(root=tmp_path)
        fs.tick(1.0)
        assert not fs.fired


class TestSwell:
    def player(self, monkeypatch, limit=70, floor=0):
        calls = []

        def run(cmd, **kwargs):
            if cmd[3] == "sget":
                return subprocess.CompletedProcess(cmd, 0, "  Front Left: Playback 180 [87%]\n", "")
            calls.append(cmd[5:])
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("haslerd.player.moode.subprocess.run", run)
        player = MoodePlayer(MoodeConfig(source_flags={}, volume_limit=limit, volume_floor=floor), sources={})
        return player, calls

    def levels(self, calls):
        return [int(c[0].rstrip("%")) for c in calls if c and c[0].endswith("%")]

    def test_silent_then_up_to_the_knob_in_twenty_steps_over_three_seconds(self, monkeypatch):
        player, calls = self.player(monkeypatch)
        player.set_volume(50)                   # knob -> 35%
        calls.clear()
        player.hold_silent()
        assert calls == [["0%"]]
        slept = []
        player.swell(sleep=slept.append, spawn=inline)
        levels = self.levels(calls[1:])
        assert len(levels) == 20 and levels[-1] == 35
        assert levels == sorted(levels) and max(levels) <= 35
        assert sum(slept) == pytest.approx(3.0 * 19 / 20)
        assert player._hold is None

    def test_starts_from_the_volume_floor(self, monkeypatch):
        player, calls = self.player(monkeypatch, limit=90, floor=40)
        player.set_volume(50)                   # -> 65%
        calls.clear()
        player.hold_silent()
        player.swell(sleep=lambda s: None, spawn=inline)
        levels = self.levels(calls[1:])
        assert levels[0] > 40 and levels[-1] == 65

    def test_never_above_volume_limit_without_a_knob(self, monkeypatch):
        player, calls = self.player(monkeypatch, limit=70)
        player.hold_silent()                    # mixer reads 87%: capped
        player.swell(sleep=lambda s: None, spawn=inline)
        assert max(self.levels(calls)) == 70

    def test_the_knob_wins(self, monkeypatch):
        player, calls = self.player(monkeypatch)
        player.set_volume(100)                  # -> 70%
        player.hold_silent()
        calls.clear()
        steps = []

        def sleep(s):
            steps.append(s)
            if len(steps) == 5:
                player.set_volume(20)           # -> 14%, mid-swell

        player.swell(sleep=sleep, spawn=inline)
        levels = self.levels(calls)
        assert levels[-1] == 14 and len(levels) == 6

    def test_release_restores_the_level_when_no_swell_comes(self, monkeypatch):
        player, calls = self.player(monkeypatch)
        player.set_volume(50)
        player.hold_silent()
        calls.clear()
        player.release_hold()
        assert self.levels(calls) == [35]
        player.release_hold()
        assert len(calls) == 1

    def test_reassert_puts_the_knob_back(self, monkeypatch):
        player, calls = self.player(monkeypatch)
        player.set_volume(50)
        calls.clear()
        player.reassert_volume()
        assert self.levels(calls) == [35]


class TestPlayerProbes:
    def test_mpd_answers_needs_the_greeting(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(2)
        port = server.getsockname()[1]

        def serve(greeting):
            conn, _ = server.accept()
            conn.sendall(greeting)
            conn.close()

        try:
            t = threading.Thread(target=serve, args=(b"OK MPD 0.24.0\n",))
            t.start()
            assert mpd_answers("127.0.0.1", port) is True
            t.join()
            t = threading.Thread(target=serve, args=(b"",))
            t.start()
            assert mpd_answers("127.0.0.1", port) is False
            t.join()
        finally:
            server.close()
        assert mpd_answers("127.0.0.1", port) is False      # nobody listening

    def test_wrkready_from_moodes_db(self, tmp_path):
        db = tmp_path / "moode.db"
        con = sqlite3.connect(db)
        con.execute("create table cfg_system (id integer, param text, value text)")
        con.execute("insert into cfg_system values (1, 'wrkready', '0')")
        con.commit()
        player = MoodePlayer(MoodeConfig(source_flags={}, moodeutl="/nonexistent"), sources={})
        assert player.wrkready(db) == 0
        con.execute("update cfg_system set value='1' where param='wrkready'")
        con.commit()
        con.close()
        assert player.wrkready(db) == 1
        assert player.wrkready(tmp_path / "absent.db") is None


class TestLamp:
    def keeper(self):
        from test_lamp_policy import Clock as LClock, FakeDriver, FakeNet, FakePanel
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        clock = LClock()
        actions = Actions(variant, FakePlayer(), clock=clock)
        net = FakeNet()
        net.seen_up = False
        keeper = LampKeeper(FakeDriver(), actions, FakePanel(), net, None, clock=clock,
                            night=lambda when, settings: False)
        return clock, actions, keeper

    def test_warm_up_holds_while_the_resume_is_on_its_way(self):
        clock, _, keeper = self.keeper()
        keeper.resume_started()
        clock.t += BOOT_MAX_S + 30
        assert keeper.boot_stage(clock()) == "daemon"
        clock.t += bootresume.RESUME_WINDOW_S
        assert keeper.boot_stage(clock()) == "done"

    def test_music_ends_the_warm_up_straight_to_playing(self):
        clock, actions, keeper = self.keeper()
        keeper.resume_started()
        actions.resume_last()                   # nothing remembered: no-op
        preset = actions.presets[1]
        keeper.loading_started(preset)
        actions.player.play_preset(preset)
        keeper.boot_done("playing")
        keeper.tick(clock())
        assert keeper.boot_stage(clock()) == "done"
        assert keeper.driver.shows[-1][0] == policy.PLAYING if hasattr(policy, "PLAYING") else True
        assert keeper.gather(clock()).playing

    def test_no_red_while_the_resume_retries(self):
        clock, actions, keeper = self.keeper()
        keeper.resume_started()
        keeper.loading_started(actions.presets[1])
        clock.t += 25
        keeper.tick(clock())
        assert "fail" not in keeper.driver.replies

    def test_failed_resume_does_not_look_like_music(self):
        clock, actions, keeper = self.keeper()
        keeper.resume_started()
        preset = actions.presets[1]
        keeper.loading_started(preset)
        actions.player.play_preset(preset)
        keeper.boot_done("failed")
        assert not keeper.gather(clock()).playing


class TestYtmHooks:
    class Ytm:
        def __init__(self, fail=False):
            self.calls, self.fail, self.ups = [], fail, 0

        def prepare_for_boot(self, preset=None, wait_s=0.0):
            self.calls.append((preset.n if preset else None, wait_s))
            if self.fail:
                raise RuntimeError("disk full")
            return True

        def network_up(self):
            self.ups += 1

    def actions(self, ytm, last=5):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        player = FakePlayer()
        player.sources = {"ytm": ytm}
        actions = Actions(variant, player)
        actions._last_preset = last
        return actions

    def test_standby_prepares_the_boot_preset_without_waiting(self):
        ytm = self.Ytm()
        self.actions(ytm).enter_standby()
        assert ytm.calls == [(5, 0.0)]

    def test_shutdown_waits_for_it_before_poweroff(self):
        ytm = self.Ytm()
        actions = self.actions(ytm, last=1)
        actions._shutdown(None, {})
        assert ytm.calls == [(1, 20.0)] and actions.player.shutdown_called

    def test_failures_and_absence_are_harmless(self):
        actions = self.actions(self.Ytm(fail=True))
        actions._shutdown(None, {})
        assert actions.player.shutdown_called
        assert self.actions(None).prepare_for_boot() is False

    def test_network_up_reaches_ytm(self):
        from haslerd.main import network_up
        ytm = self.Ytm()
        network_up(self.actions(ytm))
        assert ytm.ups == 1
        network_up(self.actions(None))      # no ytm: nothing
