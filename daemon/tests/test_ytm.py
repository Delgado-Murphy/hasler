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
        self.song = 0
        self.tag_error = None

    def clear(self):
        self.calls.append(("clear",))
        self.queue = []

    def random(self, on):
        self.calls.append(("random", on))

    def add(self, uri):
        self.calls.append(("add", uri))
        self.queue.append(uri)

    def addid(self, uri):
        self.calls.append(("addid", uri))
        self.queue.append(uri)
        return str(len(self.queue))          # MPD's song ids are strings too

    def addtagid(self, songid, tag, value):
        if self.tag_error:
            raise self.tag_error
        self.calls.append(("addtagid", songid, tag, value))

    def delete(self, span):
        self.calls.append(("delete", span))
        start, end = span
        del self.queue[start:end]

    def play(self, *args):
        self.calls.append(("play", *args))

    def stop(self):
        self.calls.append(("stop",))

    def next(self):
        self.calls.append(("next",))

    def status(self):
        st = {"state": self.state, "playlistlength": str(len(self.queue)), "song": str(self.song)}
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


def test_warm_again_swaps_the_list_and_goes_round_at_once(mpd, runner):
    other = "https://music.youtube.com/playlist?list=PLother"
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    try:
        src.warm([Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")], interval_s=3600)
        assert wait_for(lambda: any(PLAYLIST in c for c in runner.calls))
        src.warm([Preset(n=2, name="Other", uri=other, source="ytm")], interval_s=3600)
        assert wait_for(lambda: any(other in c for c in runner.calls))   # not an hour later
    finally:
        src.close()


def test_a_removed_video_is_not_asked_about_again(mpd):
    runner = FakeRunner(fail_ids={IDS[0]})   # stderr: "Video unavailable"
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    for _ in range(5):
        assert src.resolve(IDS[0]) is None
    assert runner.resolves() == [IDS[0]]


def test_other_failures_are_only_remembered_briefly(mpd, monkeypatch):
    import haslerd.player.sources.ytm as ytm

    def flaky(cmd, **kwargs):
        flaky.n += 1
        return subprocess.CompletedProcess(cmd, 1, "", "ERROR: unable to download: timed out")

    flaky.n = 0
    src = YtmSource(mpd=lambda: mpd, port=0, runner=flaky)
    clock = [1000.0]
    monkeypatch.setattr(ytm.time, "monotonic", lambda: clock[0])
    src.resolve(IDS[0])
    src.resolve(IDS[0])
    assert flaky.n == 1
    clock[0] += ytm.DEAD_OTHER_S + 1
    src.resolve(IDS[0])
    assert flaky.n == 2


# -- the YouTube Music catalogue ------------------------------------------

RADIO = "https://music.youtube.com/watch?v=aaaaaaaaaaa&list=RDAMVMaaaaaaaaaaa"


def meta(vid, title=None):
    return {"videoId": vid, "title": title or f"Song {vid[0]}", "artist": "Band", "album": "LP",
            "thumb": f"https://t/{vid}", "duration": "3:00"}


class FakeCatalog:
    """Stands in for YtmCatalog: tracks() for playlists, radio() batches in turn."""

    def __init__(self, tracks=None, batches=None, available=True, cookies_file=None):
        self.available = available
        self.cookies_file = cookies_file
        self._tracks = [meta(i) for i in IDS] if tracks is None else tracks
        self.batches = list(batches or [])
        self.calls = []

    def tracks(self, uri):
        self.calls.append(("tracks", uri))
        return self._tracks

    def radio(self, uri, after=None):
        self.calls.append(("radio", uri, after))
        return self.batches.pop(0) if self.batches else []


def ids_of(letters):
    return [c * 11 for c in letters]


def test_catalog_expands_and_tags_each_song(mpd, runner):
    cat = FakeCatalog()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
    finally:
        src.close()
    assert runner.calls == []                                   # no yt-dlp expansion
    assert cat.calls == [("tracks", PLAYLIST)]
    assert mpd.queue == [src.prefix + i for i in IDS]
    assert ("addtagid", "1", "Title", "Song a") in mpd.calls
    assert ("addtagid", "1", "Artist", "Band") in mpd.calls
    assert ("addtagid", "3", "Album", "LP") in mpd.calls
    assert mpd.calls[-1] == ("play",)


def test_a_refused_tag_still_queues_the_song_and_stops_asking(mpd, runner):
    mpd.tag_error = RuntimeError("[2@0] {addtagid} not a remote song")
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=FakeCatalog())
    try:
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
    finally:
        src.close()
    assert mpd.queue == [src.prefix + i for i in IDS]           # all three, once each
    assert [c[0] for c in mpd.calls].count("addid") == 1         # then plain adds
    assert [c[0] for c in mpd.calls].count("add") == 2


def test_empty_catalog_answer_falls_back_to_ytdlp(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=FakeCatalog(tracks=[]))
    try:
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
    finally:
        src.close()
    assert runner.calls == [["yt-dlp", "--flat-playlist", "-J", PLAYLIST]]
    assert [c for c in mpd.calls if c[0] == "add"] == [("add", src.prefix + i) for i in IDS]


def test_unavailable_catalog_is_the_old_ytdlp_path(mpd, runner):
    cat = FakeCatalog(available=False)
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
    finally:
        src.close()
    assert cat.calls == [] and sum("-J" in c for c in runner.calls) == 1


def test_catalog_expansion_is_cached_and_warmed_like_ytdlp(mpd, runner):
    cat = FakeCatalog()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
        assert src.warm_once(preset) is True
        src.play(preset)
        src.play(preset)
    finally:
        src.close()
    assert cat.calls == [("tracks", PLAYLIST)]
    assert runner.resolves() == [IDS[0]]                        # warm resolved the opener


def test_radio_link_uses_radio_and_is_endless(mpd, runner):
    cat = FakeCatalog(batches=[[meta(i) for i in IDS]])
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        src.play(Preset(n=1, name="Band radio", uri=RADIO, source="ytm"))
        assert cat.calls == [("radio", RADIO, None)]
        assert src._endless == RADIO
        src.play(Preset(n=2, name="Jazz", uri=PLAYLIST, source="ytm"))
        assert src._endless is None                             # a playlist ends
    finally:
        src.close()


def test_top_up_near_the_end_appends_only_new_ids(mpd, runner):
    first = ids_of("abcdefgh")
    cat = FakeCatalog(batches=[[meta(i) for i in first], [meta(i) for i in ids_of("ghijk")]])
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        src.play(Preset(n=1, name="Band radio", uri=RADIO, source="ytm"))
        assert src._maybe_top_up(first[1]) is False             # 6 from the end: not yet
        assert len(cat.calls) == 1
        assert src._maybe_top_up(first[3]) is True              # 5th from the end
        assert cat.calls[-1] == ("radio", RADIO, first[-1])     # continue after the last
        assert mpd.queue == [src.prefix + i for i in first + ids_of("ijk")]
        assert src._queue == first + ids_of("ijk")
        assert ("addtagid", "9", "Title", "Song i") in mpd.calls
    finally:
        src.close()


def test_top_up_happens_on_the_prefetch_thread_when_mpd_asks(mpd, runner):
    first = ids_of("abc")
    cat = FakeCatalog(batches=[[meta(i) for i in first], [meta(i) for i in ids_of("de")]])
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        src.play(Preset(n=1, name="Band radio", uri=RADIO, source="ytm"))
        status, _ = fetch(src.prefix + first[0])
        assert status == 302
        assert wait_for(lambda: len(mpd.queue) == 5)
    finally:
        src.close()


def test_top_up_leaves_a_queue_someone_else_replaced(mpd, runner):
    first = ids_of("abc")
    cat = FakeCatalog(batches=[[meta(i) for i in first], [meta(i) for i in ids_of("de")]])
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        src.play(Preset(n=1, name="Band radio", uri=RADIO, source="ytm"))
        mpd.queue = ["http://radio.example/stream"]             # a radio key was pressed
        assert src._maybe_top_up(first[2]) is False
        assert mpd.queue == ["http://radio.example/stream"]
    finally:
        src.close()


def test_top_up_caps_the_queue_by_dropping_played_songs(mpd, runner, monkeypatch):
    import haslerd.player.sources.ytm as ytm

    monkeypatch.setattr(ytm, "QUEUE_CAP", 6)
    first = ids_of("abcde")
    cat = FakeCatalog(batches=[[meta(i) for i in first], [meta(i) for i in ids_of("fgh")]])
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    try:
        src.play(Preset(n=1, name="Band radio", uri=RADIO, source="ytm"))
        mpd.song = 3                                            # a-c played, d playing
        assert src._maybe_top_up(first[3]) is True
        assert ("delete", (0, 2)) in mpd.calls                  # only played songs go
        assert len(mpd.queue) == 6
        assert src._queue == ids_of("cdefgh")
    finally:
        src.close()


def test_signed_in_cookies_reach_ytdlp_per_call(mpd, runner, tmp_path):
    jar = tmp_path / "ytm-cookies.txt"
    cat = FakeCatalog(available=False, cookies_file=jar)
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=cat)
    src.resolve(IDS[0])
    assert "--cookies" not in runner.calls[-1]                  # not signed in yet
    jar.write_text("# Netscape HTTP Cookie File\n")
    src.resolve(IDS[1])
    assert runner.calls[-1][runner.calls[-1].index("--cookies") + 1] == str(jar)
    src._expand(PLAYLIST)
    assert "--cookies" in runner.calls[-1]


def test_configured_cookies_win_over_the_sign_in(mpd, runner, tmp_path):
    jar = tmp_path / "ytm-cookies.txt"
    jar.write_text("x")
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, cookies="/etc/haslerd/yt.txt",
                    catalog=FakeCatalog(cookies_file=jar))
    src.resolve(IDS[0])
    assert runner.calls[-1][runner.calls[-1].index("--cookies") + 1] == "/etc/haslerd/yt.txt"


def test_now_playing_names_our_current_track(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, catalog=FakeCatalog())
    try:
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
        mpd.file = src.prefix + IDS[1]
        assert src.now_playing() == {"title": "Song b", "artist": "Band", "album": "LP",
                                     "thumb": f"https://t/{IDS[1]}", "explicit": False, "videoId": IDS[1]}
        mpd.file = src.prefix + "zzzzzzzzzzz"                   # ours, but never named
        assert src.now_playing() is None
        mpd.file = "http://radio.example/stream"
        assert src.now_playing() is None
    finally:
        src.close()


def test_build_sources_gives_ytm_a_catalog_in_the_state_dir(tmp_path):
    from haslerd.player.moode import MoodeConfig, build_sources

    sources = build_sources(MoodeConfig(spotify={"enabled": False}), tmp_path)
    assert sources["ytm"].catalog.cookies_file == tmp_path / "ytm-cookies.txt"
    off = build_sources(MoodeConfig(spotify={"enabled": False}, ytm={"catalog": False}), tmp_path)
    assert off["ytm"].catalog is None


def test_a_refused_link_is_resolved_again_before_mpd_sees_it(mpd, runner):
    seen = []

    def probe(url):
        seen.append(url)
        return len(seen) > 1          # first link refused, the fresh one fine

    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, probe=probe)
    status, location = src.handle(f"/ytm/{IDS[0]}")
    assert status == 302 and location.endswith(f"/{IDS[0]}?x=1")
    assert runner.resolves() == [IDS[0], IDS[0]]      # resolved twice: cache dropped


def test_a_link_refused_twice_is_a_502_and_remembered(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, probe=lambda url: False)
    assert src.handle(f"/ytm/{IDS[0]}") == (502, None)
    assert src.handle(f"/ytm/{IDS[0]}") == (502, None)
    assert runner.resolves() == [IDS[0], IDS[0]]      # not hammered on the retry


def test_cover_prefers_the_catalogue_art(source):
    with source._cache_lock:
        source._meta[IDS[0]] = {"title": "t", "artist": "a", "album": "", "thumb": "https://lh3.example/x"}
    assert source.cover(IDS[0]) == "https://lh3.example/x"
    assert source.cover(IDS[1]) == f"https://i.ytimg.com/vi/{IDS[1]}/hqdefault.jpg"


# -- boot: warm order, retry, state across reboots, the opening track on disk --


OTHER = "https://music.youtube.com/playlist?list=PLother"
AUDIO = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 40


class DownloadRunner(FakeRunner):
    """FakeRunner that also 'downloads': writes AUDIO where ``-o`` points."""

    def __init__(self, payload=AUDIO, fail_expand=False, **kwargs):
        super().__init__(**kwargs)
        self.payload = payload
        self.fail_expand = fail_expand

    def __call__(self, cmd, **kwargs):
        if "-J" in cmd and self.fail_expand:
            self.calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "ERROR: Unable to download: no network")
        if "-o" not in cmd:
            return super().__call__(cmd, **kwargs)
        self.calls.append(cmd)
        target = cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a")
        with open(target, "wb") as fh:
            fh.write(self.payload)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def downloads(self):
        return [c[-1].rsplit("=", 1)[1] for c in self.calls if "-o" in c]


def fetch_body(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        resp = opener.open(req, timeout=5)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, b""


def test_warm_goes_last_played_first(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    try:
        src.play(Preset(n=2, name="Other", uri=OTHER, source="ytm"))
        runner.calls.clear()
        src.warm([Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"),
                  Preset(n=2, name="Other", uri=OTHER, source="ytm")], interval_s=3600)
        assert wait_for(lambda: sum("-J" in c for c in runner.calls) >= 2)
        expands = [c[-1] for c in runner.calls if "-J" in c]
        assert expands[:2] == [OTHER, PLAYLIST]
    finally:
        src.close()


def test_boot_retries_just_the_failed_presets_every_few_seconds(mpd, monkeypatch):
    monkeypatch.setattr("haslerd.player.sources.ytm.BOOT_RETRY_S", 0.05)
    runner = DownloadRunner(fail_expand=True)          # booted before the Wi-Fi
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    try:
        src.warm([Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")], interval_s=3600)
        assert wait_for(lambda: sum("-J" in c for c in runner.calls) >= 3)   # not once a minute
        assert runner.resolves() == []                 # no resolving blind
        runner.fail_expand = False                     # the Wi-Fi is up
        assert wait_for(lambda: runner.resolves() == [IDS[0]])
        n = len(runner.calls)
        time.sleep(0.3)
        assert len(runner.calls) == n                  # settled: back to the long interval
    finally:
        src.close()


def test_network_up_starts_a_round_at_once(mpd, runner):
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner)
    try:
        src.network_up()                               # no warm-up yet: harmless
        src.warm([Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")], interval_s=3600)
        assert wait_for(lambda: sum("-J" in c for c in runner.calls) == 1)
        src.network_up()
        assert wait_for(lambda: sum("-J" in c for c in runner.calls) == 2)
    finally:
        src.close()


def test_state_survives_a_reboot_and_plays_without_the_network(mpd, runner, tmp_path):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    first = YtmSource(mpd=lambda: mpd, port=0, runner=FakeRunner(fail_ids={IDS[0]}), state_dir=tmp_path)
    first.warm_once(preset)                            # opens on track 1: track 0 is dead
    first.play(preset)
    first.close()
    assert (tmp_path / "ytm-state.json").stat().st_mode & 0o777 == 0o600

    offline = DownloadRunner(fail_expand=True, fail_ids=set(IDS))
    mpd2 = FakeMPD()
    again = YtmSource(mpd=lambda: mpd2, port=0, runner=offline, state_dir=tmp_path)
    try:
        again.play(preset)
        assert mpd2.queue == [again.prefix + i for i in IDS]
        assert mpd2.calls[-1] == ("play", 1)           # the chosen opener, remembered
        assert not any("-J" in c for c in offline.calls)   # stale list used at once
        assert again.resolve(IDS[1]).endswith(f"/{IDS[1]}?x=1")   # URL cache, no yt-dlp
        assert offline.resolves() == []
    finally:
        again.close()


def test_a_stale_list_is_refreshed_by_warm_and_never_warms_blind(mpd, tmp_path):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    first = YtmSource(mpd=lambda: mpd, port=0, runner=FakeRunner(), state_dir=tmp_path)
    first.warm_once(preset)
    first.close()
    offline = DownloadRunner(fail_expand=True, entries=["ddddddddddd"])
    src = YtmSource(mpd=lambda: mpd, port=0, runner=offline, state_dir=tmp_path)
    try:
        assert src.warm_once(preset) is False          # stale list not used to resolve offline
        assert src._expand(PLAYLIST) == IDS            # but a press still gets it
        offline.fail_expand = False
        assert src.warm_once(preset) is True
        assert src._expand(PLAYLIST) == ["ddddddddddd"]
    finally:
        src.close()


def test_a_failed_expansion_falls_back_to_the_last_list(mpd):
    runner = DownloadRunner()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, playlist_ttl_s=0)
    try:
        assert src._expand(PLAYLIST) == IDS
        runner.fail_expand = True
        assert src._expand(PLAYLIST) == IDS
    finally:
        src.close()


def test_a_broken_state_file_is_a_cold_start(mpd, runner, tmp_path):
    (tmp_path / "ytm-state.json").write_text("{not json")
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, state_dir=tmp_path)
    try:
        src.play(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"))
        assert mpd.queue == [src.prefix + i for i in IDS]
    finally:
        src.close()
    assert json.loads((tmp_path / "ytm-state.json").read_text())["last"] == PLAYLIST


def test_prepare_for_boot_caches_the_opener_and_the_proxy_serves_it(mpd, tmp_path):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    runner = DownloadRunner()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, state_dir=tmp_path)
    src.warm_once(preset)
    assert src.prepare_for_boot(preset, wait_s=5) is True
    cached = tmp_path / "ytm-cache" / f"{IDS[0]}.m4a"
    assert cached.read_bytes() == AUDIO
    assert cached.stat().st_mode & 0o777 == 0o644
    assert [p.name for p in cached.parent.iterdir()] == [cached.name]   # no temp files left
    assert src.prepare_for_boot(preset, wait_s=5) is True
    assert runner.downloads() == [IDS[0]]              # already there: not again
    src.close()

    offline = DownloadRunner(fail_expand=True, fail_ids=set(IDS))
    mpd2 = FakeMPD()
    booted = YtmSource(mpd=lambda: mpd2, port=0, runner=offline, state_dir=tmp_path)
    try:
        booted.play(preset)
        status, headers, body = fetch_body(mpd2.queue[0])
        assert (status, body) == (200, AUDIO)
        assert headers["Content-Type"] == "audio/mp4"
        status, headers, body = fetch_body(mpd2.queue[0], {"Range": "bytes=10-19"})
        assert (status, body) == (206, AUDIO[10:20])
        assert headers["Content-Range"] == f"bytes 10-19/{len(AUDIO)}"
        assert fetch_body(mpd2.queue[0], {"Range": f"bytes={len(AUDIO)}-"})[0] == 416
        assert IDS[0] not in offline.resolves()        # never asked yt-dlp for the opener
        assert fetch(mpd2.queue[1])[0] == 502          # the rest wait for the network
    finally:
        booted.close()


def test_shuffle_keeps_the_cached_opener_through_warm_rounds(mpd, tmp_path, monkeypatch):
    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm", shuffle=True)
    picks = iter([[2, 0, 1], [0, 1, 2], [1, 0, 2]])
    monkeypatch.setattr("haslerd.player.sources.ytm.random.sample", lambda pop, k: next(picks)[:k])
    src = YtmSource(mpd=lambda: mpd, port=0, runner=DownloadRunner(), state_dir=tmp_path)
    try:
        src.warm_once(preset)
        assert src.prepare_for_boot(preset, wait_s=5) is True
        src.warm_once(preset)                          # a later round: same opener
        src.play(preset)
        assert ("play", 2) in mpd.calls
        src.warm_once(preset)                          # spent: a fresh pick
        assert src._starts[PLAYLIST] == IDS[1]
    finally:
        src.close()


def test_one_cached_track_per_preset_and_gone_presets_are_pruned(mpd, tmp_path):
    a = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    b = Preset(n=2, name="Other", uri=OTHER, source="ytm")
    runner = DownloadRunner()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, state_dir=tmp_path)
    cache = tmp_path / "ytm-cache"
    try:
        src.warm([a, b], interval_s=3600)
        assert wait_for(lambda: len(src._starts) == 2)
        assert src.prepare_for_boot(a, wait_s=5) and src.prepare_for_boot(b, wait_s=5)
        assert sorted(p.name for p in cache.iterdir()) == [f"{IDS[0]}.m4a"]   # both open on it
        runner.entries = ["ddddddddddd"] + IDS         # a's playlist gained a new opener
        src.warm_once(a)
        assert src.prepare_for_boot(a, wait_s=5)
        assert sorted(p.name for p in cache.iterdir()) == [f"{IDS[0]}.m4a", "ddddddddddd.m4a"]
        src.warm([a], interval_s=3600)                 # b removed from the keys
        assert sorted(p.name for p in cache.iterdir()) == ["ddddddddddd.m4a"]
    finally:
        src.close()


def test_never_caches_for_radio_and_none_means_the_last_played(mpd, tmp_path):
    runner = DownloadRunner()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, state_dir=tmp_path)
    try:
        radio = Preset(n=3, name="FIP", uri="http://radio.example/s", source="radio")
        assert src.prepare_for_boot(radio, wait_s=5) is False
        assert src.prepare_for_boot(None, wait_s=5) is False     # nothing played yet
        assert runner.downloads() == [] and not (tmp_path / "ytm-cache").exists()
        jazz = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
        src.warm([jazz], interval_s=3600)
        src.play(jazz)
        assert src.prepare_for_boot(wait_s=5) is True
        assert runner.downloads() == [IDS[0]]
    finally:
        src.close()


def test_without_a_state_dir_nothing_is_cached(source):
    assert source.prepare_for_boot(Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm"), wait_s=1) is False


def test_too_big_failed_or_full_disk_downloads_leave_nothing(mpd, tmp_path, monkeypatch):
    from collections import namedtuple

    preset = Preset(n=1, name="Jazz", uri=PLAYLIST, source="ytm")
    cache = tmp_path / "ytm-cache"

    monkeypatch.setattr("haslerd.player.sources.ytm.CACHE_MAX_BYTES", 100)
    src = YtmSource(mpd=lambda: mpd, port=0, runner=DownloadRunner(), state_dir=tmp_path)
    assert src.prepare_for_boot(preset, wait_s=5) is False          # AUDIO is > 100 bytes
    assert list(cache.iterdir()) == []
    src.close()
    monkeypatch.undo()

    class Failing(DownloadRunner):
        def __call__(self, cmd, **kwargs):
            result = super().__call__(cmd, **kwargs)
            if "-o" in cmd:
                open(cmd[cmd.index("-o") + 1].replace("%(ext)s", "m4a.part"), "wb").close()
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: HTTP Error 403")
            return result

    src = YtmSource(mpd=lambda: mpd, port=0, runner=Failing(), state_dir=tmp_path)
    assert src.prepare_for_boot(preset, wait_s=5) is False
    assert list(cache.iterdir()) == []
    src.close()

    class DiskFull(DownloadRunner):
        def __call__(self, cmd, **kwargs):
            if "-o" in cmd:
                raise OSError(28, "No space left on device")
            return super().__call__(cmd, **kwargs)

    src = YtmSource(mpd=lambda: mpd, port=0, runner=DiskFull(), state_dir=tmp_path)
    assert src.prepare_for_boot(preset, wait_s=5) is False
    src.close()

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("haslerd.player.sources.ytm.shutil.disk_usage", lambda p: usage(1, 1, 1024))
    runner = DownloadRunner()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=runner, state_dir=tmp_path)
    assert src.prepare_for_boot(preset, wait_s=5) is False
    assert runner.downloads() == []                                # did not even try
    src.close()
    assert list(cache.iterdir()) == []


def test_build_sources_gives_ytm_its_state_dir(tmp_path):
    from haslerd.player.moode import MoodeConfig, build_sources

    sources = build_sources(MoodeConfig(spotify={"enabled": False}, ytm={"catalog": False}), tmp_path)
    assert sources["ytm"]._state_file == tmp_path / "ytm-state.json"
