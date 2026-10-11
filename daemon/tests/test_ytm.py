"""YtmSource against a fake yt-dlp and a fake MPD. No network, no subprocesses.

The proxy tests do run a real ThreadingHTTPServer on an ephemeral port, because
the 302/404/502 contract with MPD is the part most worth pinning down.
"""

import json
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from haslerd.player.base import Preset
from haslerd.player.sources.ytm import YtmError, YtmSource

IDS = ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"]
PLAYLIST = "https://music.youtube.com/playlist?list=PLxyz"


class FakeRunner:
    """Stands in for subprocess.run; answers -J with JSON and -g with a URL."""

    def __init__(self, entries=IDS, fail_ids=()):
        self.calls = []
        self.entries = entries
        self.fail_ids = set(fail_ids)

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "-J" in cmd:
            body = {"_type": "playlist", "entries": [{"id": i, "title": i} for i in self.entries]}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(body), "")
        vid = cmd[-1].rsplit("=", 1)[1]
        if vid in self.fail_ids:
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Video unavailable")
        return subprocess.CompletedProcess(cmd, 0, f"https://rr1.googlevideo.com/{vid}?x=1\n", "")

    def resolves(self):
        return [c[-1].rsplit("=", 1)[1] for c in self.calls if "-g" in c]


class FakeMPD:
    def __init__(self):
        self.calls = []
        self.state = "stop"
        self.file = ""
        self.nextsong = None
        self.queue = []

    def clear(self):
        self.calls.append(("clear",))
        self.queue = []

    def random(self, on):
        self.calls.append(("random", on))

    def add(self, uri):
        self.calls.append(("add", uri))
        self.queue.append(uri)

    def play(self, *args):
        self.calls.append(("play", *args))

    def stop(self):
        self.calls.append(("stop",))

    def next(self):
        self.calls.append(("next",))

    def status(self):
        st = {"state": self.state}
        if self.nextsong is not None:
            st["nextsong"] = str(self.nextsong)
        return st

    def currentsong(self):
        return {"file": self.file} if self.file else {}

    def playlistinfo(self, pos):
        return [{"file": self.queue[int(pos)]}]


@pytest.fixture
def mpd():
    return FakeMPD()


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def source(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    yield src
    src.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url):
    """GET without following redirects -> (status, Location)."""
    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(url, timeout=5)
        return resp.status, resp.headers.get("Location")
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Location")


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- play -----------------------------------------------------------------


def test_playlist_expands_into_mpd_queue_in_order(source, mpd, runner):
    source.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
    prefix = source.prefix
    assert source.port != 0
    assert mpd.calls == [
        ("clear",),
        ("random", 0),
        *[("add", prefix + i) for i in IDS],
        ("play",),
    ]
    assert runner.calls == [["yt-dlp", "--flat-playlist", "-J", PLAYLIST]]


def test_shuffle_turns_on_random(source, mpd):
    source.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm", shuffle=True))
    assert ("random", 1) in mpd.calls
    assert mpd.calls[-1][0] == "play"


def test_single_watch_url_queues_one_track_without_expansion(source, mpd, runner):
    uri = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
    source.play(Preset(n=2, name="One", uri=uri, source="ytm"))
    adds = [c for c in mpd.calls if c[0] == "add"]
    assert adds == [("add", source.prefix + "dQw4w9WgXcQ")]
    assert runner.calls == []


def test_playlist_expansion_is_cached_for_a_repress(source, mpd, runner):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    source.play(preset)
    source.play(preset)
    assert sum("-J" in c for c in runner.calls) == 1
    assert sum(c == ("play",) for c in mpd.calls) == 2


def test_cookies_are_passed_to_ytdlp(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, cookies="/etc/haslerd/yt.txt")
    try:
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
        assert src.resolve(IDS[0])
    finally:
        src.close()
    assert all(c[c.index("--cookies") + 1] == "/etc/haslerd/yt.txt" for c in runner.calls)


def test_empty_expansion_raises_and_leaves_mpd_alone(mpd):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=FakeRunner(entries=[]))
    try:
        with pytest.raises(YtmError):
            src.play(Preset(n=1, name="Empty", uri=PLAYLIST, source="ytm"))
    finally:
        src.close()
    assert mpd.calls == []


def test_play_raises_on_mpd_failure_but_the_rest_swallow(runner):
    def broken():
        raise ConnectionRefusedError

    src = YtmSource(mpd=broken, port=0, runner=runner)
    try:
        # play raises so the router can flash the lamp; nothing else may.
        with pytest.raises(YtmError):
            src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
        src.stop()
        src.next_track()
        assert src.is_active() is False
    finally:
        src.close()


# -- proxy ----------------------------------------------------------------


def test_proxy_redirects_to_resolved_url(source, runner):
    source.start()
    status, location = fetch(source.prefix + IDS[0])
    assert status == 302
    assert location == f"https://rr1.googlevideo.com/{IDS[0]}?x=1"
    cmd = next(c for c in runner.calls if "-g" in c)
    assert cmd == [
        "yt-dlp", "-f", "bestaudio[ext=m4a]/bestaudio", "-g",
        f"https://music.youtube.com/watch?v={IDS[0]}",
    ]


def test_cache_hit_does_not_rerun_ytdlp(source, runner):
    source.start()
    fetch(source.prefix + IDS[2])  # last in no queue -> nothing to prefetch
    fetch(source.prefix + IDS[2])
    assert runner.resolves() == [IDS[2]]


def test_expired_cache_resolves_again(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, cache_ttl_s=0)
    src.resolve(IDS[0])
    src.resolve(IDS[0])
    assert runner.resolves() == [IDS[0], IDS[0]]


@pytest.mark.parametrize("path", ["/ytm/short", "/ytm/aaaaaaaaaa!", "/other/aaaaaaaaaaa", "/ytm/", "/"])
def test_invalid_path_is_404(source, runner, path):
    source.start()
    status, _ = fetch(f"http://127.0.0.1:{source.port}{path}")
    assert status == 404
    assert runner.calls == []


def test_resolver_failure_is_502(mpd):
    runner = FakeRunner(fail_ids={IDS[0]})
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    try:
        src.start()
        status, location = fetch(src.prefix + IDS[0])
    finally:
        src.close()
    assert status == 502
    assert location is None


def test_prefetch_resolves_next_queued_id_from_mpd(source, mpd, runner):
    source.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
    mpd.nextsong = 2  # e.g. shuffle picked track 3 to follow track 1
    status, _ = fetch(source.prefix + IDS[0])
    assert status == 302
    assert wait_for(lambda: IDS[2] in runner.resolves())
    assert IDS[1] not in runner.resolves()


def test_prefetch_falls_back_to_queue_order(source, mpd, runner):
    source.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
    mpd.nextsong = None
    fetch(source.prefix + IDS[0])
    assert wait_for(lambda: IDS[1] in runner.resolves())
    # And the following request is served from cache.
    status, location = fetch(source.prefix + IDS[1])
    assert status == 302 and location.endswith(f"/{IDS[1]}?x=1")
    assert runner.resolves().count(IDS[1]) == 1


def test_cache_respects_url_expire_stamp(mpd):
    soon = int(time.time()) + 60  # inside the safety margin -> must not be cached

    def runner(cmd, **kwargs):
        runner.n += 1
        return subprocess.CompletedProcess(cmd, 0, f"https://g.example/x?expire={soon}\n", "")

    runner.n = 0
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    src.resolve(IDS[0])
    src.resolve(IDS[0])
    assert runner.n == 2


# -- transport and state --------------------------------------------------


def test_stop_and_next_go_to_mpd(source, mpd):
    source.stop()
    source.next_track()
    assert mpd.calls == [("stop",), ("next",)]


def test_is_active_only_for_proxy_urls_while_playing(source, mpd):
    source.start()
    mpd.state, mpd.file = "play", source.prefix + IDS[0]
    assert source.is_active() is True
    mpd.state = "pause"
    assert source.is_active() is False
    mpd.state, mpd.file = "play", "http://radio.example/stream.mp3"
    assert source.is_active() is False


def test_close_stops_the_server(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    src.start()
    url = src.prefix + IDS[0]
    src.close()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(url, timeout=2)
    src.close()  # idempotent


# -- warm-up --------------------------------------------------------------


def test_warm_once_expands_and_resolves_the_opening_track(source, runner):
    assert source.warm_once(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")) is True
    assert sum("-J" in c for c in runner.calls) == 1
    assert runner.resolves() == [IDS[0]]


def test_press_after_warm_needs_no_ytdlp(source, mpd, runner):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    source.warm_once(preset)
    runner.calls.clear()
    source.play(preset)
    status, _ = fetch(mpd.queue[0])
    assert status == 302
    assert runner.calls == [] or all("-g" in c for c in runner.calls)  # only prefetch of track 2
    assert IDS[0] not in runner.resolves()


def test_warmed_playlist_outlives_the_short_press_cache(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, playlist_ttl_s=0)
    try:
        src._warm_interval_s = 600
        preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
        src.warm_once(preset)
        src.play(preset)
        assert sum("-J" in c for c in runner.calls) == 1
    finally:
        src.close()


def test_warm_refreshes_the_playlist_each_round(source, runner):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    source.warm_once(preset)
    source.warm_once(preset)
    assert sum("-J" in c for c in runner.calls) == 2


def test_shuffle_press_opens_on_the_warmed_track(source, mpd, runner, monkeypatch):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm", shuffle=True)
    monkeypatch.setattr("haslerd.player.sources.ytm.random.sample", lambda pop, k: [2, 0, 1][:k])
    source.warm_once(preset)
    assert runner.resolves() == [IDS[2]]
    monkeypatch.setattr("haslerd.player.sources.ytm.random.randrange", lambda n: 1)
    source.play(preset)
    assert ("play", 2) in mpd.calls


def test_warm_once_reports_failure(mpd):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=FakeRunner(fail_ids=set(IDS)))
    try:
        assert src.warm_once(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")) is False
        empty = YtmSource(mpd=lambda: mpd, port=0, runner=FakeRunner(entries=[]))
        assert empty.warm_once(Preset(n=1, name="Empty", uri=PLAYLIST, source="ytm")) is False
    finally:
        src.close()


def test_warm_runs_in_the_background_and_close_stops_it(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    presets = [
        Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"),
        Preset(n=2, name="Radio", uri="http://radio.example/s", source="radio"),
    ]
    src.warm(presets, interval_s=0.05)
    try:
        assert wait_for(lambda: sum("-J" in c for c in runner.calls) >= 2)
        assert all(PLAYLIST in c or "watch?v=" in c[-1] for c in runner.calls)  # radio ignored
    finally:
        src.close()
    src._warm_thread.join(timeout=2)
    assert not src._warm_thread.is_alive()


def test_warm_with_no_ytm_presets_starts_nothing(source):
    source.warm([Preset(n=2, name="Radio", uri="http://radio.example/s", source="radio")])
    assert source._warm_thread is None


def test_warm_skips_a_dead_opening_track_and_the_press_starts_past_it(mpd):
    runner = FakeRunner(fail_ids={IDS[0]})
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    try:
        preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
        assert src.warm_once(preset) is True
        assert runner.resolves() == [IDS[0], IDS[1]]
        src.play(preset)
        assert mpd.calls[-1] == ("play", 1)
        src.play(preset)                      # in order: the start is kept
        assert mpd.calls[-1] == ("play", 1)
    finally:
        src.close()


def test_shuffle_warm_tries_another_random_start_when_its_pick_is_dead(mpd, monkeypatch):
    runner = FakeRunner(fail_ids={IDS[2]})
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    monkeypatch.setattr("haslerd.player.sources.ytm.random.sample", lambda pop, k: [2, 1, 0][:k])
    try:
        preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm", shuffle=True)
        assert src.warm_once(preset) is True
        assert runner.resolves() == [IDS[2], IDS[1]]
        src.play(preset)
        assert ("play", 1) in mpd.calls
    finally:
        src.close()


def test_warm_gives_up_after_three_dead_openers(mpd):
    runner = FakeRunner(entries=IDS + ["ddddddddddd"], fail_ids=set(IDS))
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    try:
        assert src.warm_once(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")) is False
        assert runner.resolves() == IDS             # 3 tries, not the 4th
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
        assert mpd.calls[-1] == ("play",)            # no known-good start: from the top
    finally:
        src.close()
