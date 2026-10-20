"""The ytm proxy's ranged streaming, against a fake googlevideo.

The fake throttles a plain GET the way googlevideo does (to a crawl) and
answers Range requests at once, so a test that finishes quickly proves the
proxy used ranges. Real sockets on ephemeral ports; no yt-dlp, no network.
"""

import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from haslerd.player.sources import ytm
from haslerd.player.sources.ytm import YtmSource

from test_ytm import IDS, FakeMPD, fetch, wait_for

CHUNK = 64 * 1024


def track(vid: str, size: int = 5 * CHUNK + 1234) -> bytes:
    """Deterministic, distinct bytes per video id."""
    seed = sum(vid.encode())
    return bytes((seed + i * 7 + (i >> 8)) % 256 for i in range(size))


class FakeGoogleVideo:
    """googlevideo, as far as the proxy can tell. ``mode``: "ok", "403" (every
    request refused) or "ignore" (Range ignored: 200 + throttled body)."""

    def __init__(self):
        self.mode = "ok"
        self.range_delay = 0.0
        self.requests = []                     # (vid, Range header or None)
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                vid = self.path.split("?", 1)[0].strip("/")
                body = track(vid)
                rng = self.headers.get("Range")
                with fake.lock:
                    fake.requests.append((vid, rng))
                if fake.mode == "403":
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if rng and fake.mode == "ok":
                    time.sleep(fake.range_delay)
                    first, last = (int(x) for x in rng.split("=", 1)[1].split("-"))
                    last = min(last, len(body) - 1)
                    self.send_response(206)
                    self.send_header("Content-Type", "audio/mp4")
                    self.send_header("Content-Range", f"bytes {first}-{last}/{len(body)}")
                    self.send_header("Content-Length", str(last - first + 1))
                    self.end_headers()
                    self.wfile.write(body[first:last + 1])
                    return
                # The throttle: a plain GET trickles out at ~32 KB/s.
                self.send_response(200)
                self.send_header("Content-Type", "audio/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for i in range(0, len(body), 4096):
                        self.wfile.write(body[i:i + 4096])
                        time.sleep(0.125)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def url(self, vid):
        return f"http://127.0.0.1:{self.port}/{vid}?itag=140&mime=audio%2Fmp4&clen={len(track(vid))}"

    def ranges(self, vid):
        return [r for v, r in self.requests if v == vid and r and r != "bytes=0-0"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Runner:
    """yt-dlp -g, answering with the fake googlevideo's URLs."""

    def __init__(self, gv):
        self.gv = gv
        self.calls = []

    def __call__(self, cmd, **kwargs):
        vid = cmd[-1].rsplit("=", 1)[1]
        self.calls.append(vid)
        return subprocess.CompletedProcess(cmd, 0, self.gv.url(vid) + "\n", "")


@pytest.fixture
def gv():
    server = FakeGoogleVideo()
    yield server
    server.close()


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(ytm, "STREAM_CHUNK", CHUNK)
    monkeypatch.setattr(ytm, "STREAM_DIRECT_GAP", 2 * CHUNK)


@pytest.fixture
def make(gv, tmp_path, small_chunks):
    made = []

    def build(mpd=None, **kwargs):
        mpd = mpd or FakeMPD()
        runner = Runner(gv)
        src = YtmSource(mpd=lambda: mpd, port=0, runner=runner,
                        stream_dir=tmp_path / "ytm-stream", **kwargs)
        src.start()
        src.runner = runner
        made.append(src)
        return src

    yield build
    for src in made:
        src.close()


def get(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def finished(src, vid):
    return any(p.name.startswith(vid) and not p.name.startswith(".") for p in src._stream_dir.iterdir())


def test_a_whole_track_comes_quickly_through_ranges(make, gv):
    src = make()
    started = time.monotonic()
    status, headers, body = get(src.prefix + IDS[0])
    took = time.monotonic() - started
    assert status == 200 and body == track(IDS[0])
    assert took < 3, f"took {took:.1f} s - the throttled path would take ~10 s"
    assert headers["Content-Type"] == "audio/mp4"
    assert headers["Content-Length"] == str(len(track(IDS[0])))
    assert headers["Accept-Ranges"] == "bytes"
    assert all(r for _, r in gv.requests)              # never a plain GET
    assert len(gv.ranges(IDS[0])) == 6                 # 5 full chunks + the tail
    assert wait_for(lambda: (src._stream_dir / f"{IDS[0]}.m4a").is_file())
    assert (src._stream_dir / f"{IDS[0]}.m4a").read_bytes() == track(IDS[0])
    # A replay is served from the card, no googlevideo at all.
    before = len(gv.requests)
    assert get(src.prefix + IDS[0])[2] == track(IDS[0])
    assert len(gv.requests) == before


def test_a_range_request_from_mpd(make, gv):
    src = make()
    data = track(IDS[0])
    status, headers, body = get(src.prefix + IDS[0], {"Range": "bytes=100-199"})
    assert (status, body) == (206, data[100:200])
    assert headers["Content-Range"] == f"bytes 100-199/{len(data)}"
    status, headers, body = get(src.prefix + IDS[0], {"Range": f"bytes={len(data) - 500}-"})
    assert (status, body) == (206, data[-500:])
    assert get(src.prefix + IDS[0], {"Range": f"bytes={len(data)}-"})[0] == 416


def test_a_seek_far_past_the_download_is_fetched_directly(make, gv):
    gv.range_delay = 0.3                               # the download crawls along
    src = make()
    data = track(IDS[0])
    started = time.monotonic()
    status, _, body = get(src.prefix + IDS[0], {"Range": f"bytes={len(data) - 1000}-"})
    assert (status, body) == (206, data[-1000:])
    assert time.monotonic() - started < 1.5            # didn't wait for 6 chunks at 0.3 s
    assert f"bytes={len(data) - 1000}-{len(data) - 1}" in gv.ranges(IDS[0])


def test_two_readers_share_one_download(make, gv):
    gv.range_delay = 0.05
    src = make()
    results = []

    def read():
        results.append(get(src.prefix + IDS[0])[2])

    threads = [threading.Thread(target=read) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == [track(IDS[0])] * 2
    counts = Counter(gv.ranges(IDS[0]))
    assert len(counts) == 6 and set(counts.values()) == {1}   # every chunk fetched once
    assert src.runner.calls == [IDS[0]]                       # and resolved once


def test_the_next_track_is_downloaded_when_one_starts(make, gv):
    mpd = FakeMPD()
    src = make(mpd)
    mpd.queue = [src.prefix + i for i in IDS]
    mpd.nextsong = 1
    assert get(src.prefix + IDS[0])[2] == track(IDS[0])
    assert wait_for(lambda: (src._stream_dir / f"{IDS[1]}.m4a").is_file())
    before = len(gv.requests)
    assert get(src.prefix + IDS[1])[2] == track(IDS[1])        # straight from the card
    assert len(gv.requests) == before


def test_the_cache_keeps_the_newest_tracks_only(make, gv, monkeypatch):
    monkeypatch.setattr(ytm, "STREAM_KEEP", 2)
    src = make()
    vids = IDS + ["ddddddddddd"]
    for vid in vids:
        assert get(src.prefix + vid)[2] == track(vid)
        assert wait_for(lambda: finished(src, vid))
    names = sorted(p.name for p in src._stream_dir.iterdir())
    assert names == [f"{IDS[2]}.m4a", "ddddddddddd.m4a"]

    # And by size: room for one track only, so the newest stays alone.
    monkeypatch.setattr(ytm, "STREAM_KEEP", 10)
    monkeypatch.setattr(ytm, "STREAM_MAX_BYTES", len(track(IDS[0])) + 10)
    assert get(src.prefix + IDS[0])[2] == track(IDS[0])
    assert wait_for(lambda: sorted(p.name for p in src._stream_dir.iterdir()) == [f"{IDS[0]}.m4a"])


def test_leftovers_from_a_previous_run(make, tmp_path):
    cache = tmp_path / "ytm-stream"
    cache.mkdir()
    (cache / f"{IDS[0]}.m4a").write_bytes(track(IDS[0]))
    (cache / f".tmp-{IDS[1]}").write_bytes(b"half")
    (cache / "junk.txt").write_bytes(b"?")
    src = make()
    assert sorted(p.name for p in cache.iterdir()) == [f"{IDS[0]}.m4a"]
    assert get(src.prefix + IDS[0])[2] == track(IDS[0])
    assert src.runner.calls == []                      # served without resolving


@pytest.mark.parametrize("mode", ["403", "ignore"])
def test_falls_back_to_the_redirect_when_ranges_fail(make, gv, mode, caplog):
    gv.mode = mode
    src = make()
    status, location = fetch(src.prefix + IDS[0])
    assert status == 302 and location == gv.url(IDS[0])
    assert "redirecting MPD to googlevideo" in caplog.text
    assert [p for p in src._stream_dir.iterdir()] == []     # no half file left
    if mode == "403":
        assert src.runner.calls.count(IDS[0]) >= 2           # re-resolved once first


def test_a_link_refused_mid_track_is_resolved_again(make, gv):
    src = make()
    real = ytm.fetch_range
    refused = []

    def flaky(url, first, last, **kw):
        if first == 2 * CHUNK and not refused:
            refused.append(url)
            raise ytm._RangeRefused(403)
        return real(url, first, last, **kw)

    ytm.fetch_range = flaky
    try:
        assert get(src.prefix + IDS[0])[2] == track(IDS[0])
    finally:
        ytm.fetch_range = real
    assert refused and src.runner.calls == [IDS[0], IDS[0]]


def test_without_a_stream_dir_the_proxy_redirects(gv, small_chunks):
    mpd = FakeMPD()
    src = YtmSource(mpd=lambda: mpd, port=0, runner=Runner(gv))
    src.start()
    try:
        assert fetch(src.prefix + IDS[0]) == (302, gv.url(IDS[0]))
        assert gv.requests == []
    finally:
        src.close()


def test_build_sources_streams_through_the_state_dir(tmp_path):
    from haslerd.player.moode import MoodeConfig, build_sources

    sources = build_sources(MoodeConfig(spotify={"enabled": False}, ytm={"catalog": False}), tmp_path)
    assert sources["ytm"]._stream_dir == tmp_path / "ytm-stream"
    assert os.path.basename(ytm.STREAM_DIR) == "ytm-stream"
