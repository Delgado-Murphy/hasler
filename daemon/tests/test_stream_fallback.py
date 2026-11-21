"""YouTube Music that won't play (player/watchdog.py): skip, count relapses, fall back.

The 2026-11-21 power cut left two empty track files; each was a 40 s silence
the watchdog "recovered" by starting the preset again - which opens on a kept
track, so it counted as fixed and looped ~20 times in an hour. Here, against
the same fake MPD, real router and real Actions as test_stream_watchdog.py.
"""

import json

import pytest

import haslerd.player.watchdog as wd_mod
from haslerd.player.base import Preset
from haslerd.player.watchdog import StreamEvents, fallback_target
from haslerd.state import StateStore
from haslerd.variant import Panel
from haslerd.web.server import WebApp

from test_stream_watchdog import FakeMpd, rig  # noqa: F401 - the fixture


@pytest.fixture
def ytm(rig):  # noqa: F811
    """rig, plus the ytm source's forget(), settings and the internet probe."""
    source = rig.player.sources["ytm"]
    source.forgotten = []
    source.forget = source.forgotten.append
    rig.settings = {}
    rig.online = {"up": True}
    rig.watch.settings = lambda: rig.settings
    rig.watch.online = lambda: rig.online["up"]
    return rig


def broken(rig, everything=False):
    """MPD plays nothing from our YTM proxy (or nothing at all): play() leaves it stopped."""
    mpd = rig.mpd
    real = FakeMpd.play.__get__(mpd)

    def play(pos=None):
        at = pos if pos is not None else (mpd.song or 0)
        if everything or (mpd.queue and at < len(mpd.queue) and "/ytm/" in mpd.queue[at]):
            mpd.calls.append("play")
            mpd.song, mpd.state = at, "stop"
            return
        real(pos)

    mpd.play = play


def stall_once(rig):
    """elapsed freezes long enough to count as a stall, then the music comes back."""
    rig.mpd.flowing = False
    rig.run(wd_mod.STALL_S + 3)
    rig.mpd.flowing = True
    rig.run(wd_mod.SKIP_VERIFY_S + 4)


def ytm_fails(rig, seconds=240):
    rig.press("k3")
    rig.playing_now()
    broken(rig)
    rig.mpd.state = "stop"
    rig.run(seconds)


def test_ytm_mid_playlist_stall_skips_track(ytm):
    ytm.press("k3")
    ytm.playing_now()
    gen = ytm.player.route_gen
    ytm.mpd.flowing = False                       # track "a" never delivers its audio
    ytm.run(wd_mod.STALL_S + 3)
    assert ytm.mpd.song == 1                      # the next track, not the preset again
    assert ytm.player.sources["ytm"].forgotten == ["a"]
    ytm.mpd.flowing = True
    ytm.run(6)
    assert not ytm.watch.recovering
    assert ytm.player.route_gen == gen and ytm.player.current().n == 3


def test_ytm_relapses_counted_across_recoveries(ytm):
    ytm.press("k3")
    ytm.playing_now()
    stall_once(ytm)
    stall_once(ytm)
    assert ytm.player.current().n == 3 and ytm.watch.status_doc()["fallback"] is None
    ytm.mpd.flowing = False                       # the third in 20 minutes
    ytm.run(wd_mod.STALL_S + 3)
    ytm.mpd.flowing = True
    ytm.run(6)
    fb = ytm.watch.status_doc()["fallback"]
    assert fb is not None and fb["why"].startswith("3 stalls")
    assert ytm.player.current().source == "radio"


def test_ytm_relapses_outside_the_window_do_not_add_up(ytm):
    ytm.press("k3")
    ytm.playing_now()
    stall_once(ytm)
    stall_once(ytm)
    ytm.run(wd_mod.RELAPSE_WINDOW_S)
    stall_once(ytm)
    assert ytm.player.current().n == 3 and ytm.watch.status_doc()["fallback"] is None


def test_ytm_fallback_after_failed_attempts(ytm):
    ytm_fails(ytm)
    fb = ytm.watch.status_doc()["fallback"]
    assert fb["why"] == f"{wd_mod.FALLBACK_AFTER} failed attempts"
    assert fb["from"] == {"n": 3, "name": "A playlist", "source": "ytm"} and fb["to"]["n"] == 1
    assert ytm.player.current().n == 1 and ytm.mpd.state == "play"
    assert ytm.replies.count("fail") == 1         # one red pulse, by day
    assert not ytm.watch.recovering and not ytm.actions.standby


def test_fallback_prefers_setting_then_recent_radio_then_lowest(ytm):
    actions = ytm.actions
    assert fallback_target(actions, {}).n == 1                       # the lowest-numbered
    ytm.press("k2")
    ytm.press("k3")
    assert fallback_target(actions, {}).n == 2                       # the one played last
    assert fallback_target(actions, {"fallback_preset": 1}).n == 1   # the owner's choice
    assert fallback_target(actions, {"fallback_preset": 3}).n == 2   # not a radio preset: ignored
    assert fallback_target(actions, {"fallback_preset": 9}).n == 2   # no such preset
    ytm.settings = {"fallback_preset": 1}
    ytm_fails(ytm)
    assert ytm.player.current().n == 1


def test_fallback_standby_when_offline(ytm):
    ytm.online["up"] = False
    ytm_fails(ytm)
    assert ytm.actions.standby and ytm.player.current() is None
    assert ytm.watch.status_doc()["fallback"]["to"] is None


def test_fallback_standby_when_set(ytm):
    ytm.settings = {"when_music_fails": "standby"}
    ytm_fails(ytm)
    assert ytm.actions.standby and ytm.watch.status_doc()["fallback"]["to"] is None


def test_fallback_standby_at_night(ytm):
    ytm.night["on"] = True
    ytm_fails(ytm)
    assert ytm.actions.standby and ytm.watch.status_doc()["fallback"]["to"] is None
    assert "fail" not in ytm.replies              # no red at night


def test_fallback_standby_when_no_radio_preset(ytm):
    for n in (1, 2):
        del ytm.actions.presets[n]
    ytm_fails(ytm)
    assert ytm.actions.standby and ytm.watch.status_doc()["fallback"]["to"] is None


def test_fallback_once_then_standby(ytm):
    ytm.press("k3")
    ytm.playing_now()
    broken(ytm, everything=True)                  # the fallback station won't play either
    ytm.mpd.state = "stop"
    ytm.run(240)
    assert ytm.actions.standby and ytm.player.current() is None
    fb = ytm.watch.status_doc()["fallback"]
    assert fb["to"] is None and fb["from"]["n"] == 3
    starts = [c for c in ytm.mpd.calls if c == "clear"]
    ytm.run(600)
    assert [c for c in ytm.mpd.calls if c == "clear"] == starts   # no chain, nothing after


def test_hand_routing_clears_fallback(ytm):
    ytm_fails(ytm)
    assert ytm.watch.status_doc()["fallback"] is not None
    broken_play = ytm.mpd.play
    ytm.press("k2")                               # another station: the fallback is over...
    assert ytm.watch.status_doc()["fallback"] is None
    ytm.run(2)
    doc = ytm.watch.status_doc()
    assert doc["fallback"] is None and doc["fallback_notice"]["from"]["n"] == 3   # ...the note stays
    del ytm.mpd.play                              # YouTube Music works again
    assert broken_play is not ytm.mpd.play
    ytm.press("k3")                               # that preset again: tried, and the note goes
    ytm.run(6)
    doc = ytm.watch.status_doc()
    assert doc["fallback"] is None and doc["fallback_notice"] is None
    assert ytm.player.current().n == 3 and ytm.mpd.state == "play"


def test_hand_routing_gives_ytm_its_chances_again(ytm):
    ytm.press("k3")
    ytm.playing_now()
    stall_once(ytm)
    stall_once(ytm)
    ytm.press("k3")                               # the same key: the next track (a hand, no routing)...
    ytm.press("k1")
    ytm.press("k3")                               # ...a new routing of it starts the count again
    ytm.playing_now()
    stall_once(ytm)
    assert ytm.player.current().n == 3 and ytm.watch.status_doc()["fallback"] is None


def test_radio_preset_never_falls_back(ytm):
    ytm.press("k2")
    ytm.playing_now()
    ytm.mpd.plays = False
    ytm.mpd.state = "stop"
    ytm.run(900)
    assert ytm.player.current().n == 2 and ytm.watch.recovering
    assert ytm.watch.status_doc()["fallback"] is None


def test_keep_trying_disables_fallback(ytm):
    ytm.settings = {"when_music_fails": "keep_trying"}
    ytm_fails(ytm, 900)
    assert ytm.player.current().n == 3 and ytm.watch.recovering
    assert ytm.watch.status_doc()["fallback"] is None
    assert ytm.replies.count("fail") == 1         # today's one pulse after three attempts


def test_fallback_event_recorded(ytm, tmp_path):
    ytm.watch.events = StreamEvents(tmp_path / "stream-events.jsonl")
    ytm_fails(ytm)
    lines = [json.loads(x) for x in (tmp_path / "stream-events.jsonl").read_text().splitlines()]
    acts = [e["action"] for e in lines]
    assert acts[acts.index("stalled") + 1] == "skip track"
    fb = next(e for e in lines if e["action"] == "fallback")
    assert fb["preset"]["n"] == 3 and fb["to"] == 1 and fb["reason"] == "3 failed attempts"
    assert fb["quiet"] is None and fb["lamp_fail"] is True


def test_fallback_keeps_last_preset(ytm, tmp_path):
    store = StateStore(tmp_path / "state.json")
    ytm.actions.state = store
    ytm_fails(ytm)
    assert ytm.player.current().n == 1
    assert store.load()["last_preset"] == 3 and ytm.actions.resume_preset().n == 3


def test_fallback_to_standby_is_not_remembered(ytm, tmp_path):
    store = StateStore(tmp_path / "state.json")
    ytm.actions.state = store
    ytm.online["up"] = False
    ytm_fails(ytm)
    assert ytm.actions.standby
    saved = store.load()
    assert saved["last_preset"] == 3 and saved["standby"] is False   # the next boot tries YTM again


def test_status_doc_reports_fallback(ytm, tmp_path):
    ytm_fails(ytm)
    app = WebApp(ytm.actions, Panel.from_variant(ytm.actions.variant, fake=True), tmp_path / "keys.yaml",
                 {"radio": True, "spotify": True, "ytm": True}, ha=False)
    app.stream_watch = ytm.watch
    ytm.player.get_volume = lambda: 40.0
    st = app.status()
    assert st["fallback"]["from"]["n"] == 3 and st["fallback"]["to"] == {"n": 1, "name": "Radio Swiss Classic"}
    assert st["fallback"]["at"] and st["fallback_notice"] == st["fallback"]


def test_without_forget_the_skip_still_happens(ytm):
    del ytm.player.sources["ytm"].forget         # an older ytm.py
    ytm.press("k3")
    ytm.playing_now()
    ytm.mpd.flowing = False
    ytm.run(wd_mod.STALL_S + 3)
    assert ytm.mpd.song == 1


def test_fallback_target_none_without_radio():
    class A:
        presets = {3: Preset(3, "List", "https://music.youtube.com/playlist?list=PLx", source="ytm")}

        def _recent_preset(self, sources):
            return None

    assert fallback_target(A(), {}) is None
