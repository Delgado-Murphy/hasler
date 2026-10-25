"""HLS stations reach MPD as plain audio through the local ffmpeg proxy (player/sources/hls.py)."""

from __future__ import annotations

import os
import socket
import sys
import textwrap
import time
from pathlib import Path

import pytest

from haslerd.player.base import Preset
from haslerd.player.sources import hls
from haslerd.player.sources.hls import HlsProxy
from haslerd.player.sources.radio import RadioSource

RNE = "https://rtvelivestream.rtve.es/rtvesec/rne/rne_r3_main.m3u8"

# Stands in for ffmpeg: argv = mode, muxer. Ignores SIGTERM like ffmpeg's HLS
# demuxer does on a live edge, so every stop has to fall through to SIGKILL.
FAKE = textwrap.dedent("""
    import signal, sys, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    mode, muxer = sys.argv[1], sys.argv[2]
    out = sys.stdout.buffer
    if mode == "fail":
        sys.stderr.write("Connection refused\\n"); sys.exit(1)
    if mode == "aac-only" and muxer == "adts":
        sys.stderr.write("[adts @ 0x1] adts muxer supports only codec aac for type audio\\n"); sys.exit(234)
    if mode == "silent":
        time.sleep(60); sys.exit(0)
    out.write(muxer.encode() + b":"); out.flush()
    if mode == "end":
        out.write(b"x" * 1000); out.flush(); sys.exit(0)
    if mode == "stall":
        time.sleep(60); sys.exit(0)
    while True:
        out.write(b"a" * 512); out.flush(); time.sleep(0.02)
""")


@pytest.fixture
def make_proxy(tmp_path: Path):
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(FAKE)
    made: list[HlsProxy] = []

    def make(mode: str = "stream", **kw) -> HlsProxy:
        kw.setdefault("sniffer", None)
        proxy = HlsProxy(port=0, command=lambda url, muxer: [sys.executable, str(script), mode, muxer], **kw)
        proxy.start()
        made.append(proxy)
        return proxy

    yield make
    for p in made:
        p.stop()


def _get(proxy: HlsProxy, url: str, timeout: float = 10) -> tuple[socket.socket, bytes, bytes]:
    """GET the local URL; (socket, header block, first body bytes)."""
    local = proxy.register(url)
    path = local.split(str(proxy.port), 1)[1]
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=timeout)
    sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nIcy-MetaData: 1\r\n\r\n".encode())
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return sock, head, body


def _read_all(sock: socket.socket) -> bytes:
    out = b""
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            return out
        out += chunk


def _gone(pid: int, within: float = 2.5) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _pids(proxy: HlsProxy) -> list[int]:
    with proxy._lock:
        return [s.proc.pid for s in proxy._streams]


# -- detection and tokens ---------------------------------------------------

class TestDetection:
    def test_m3u8_path_with_or_without_query(self):
        assert hls.looks_hls(RNE)
        assert hls.looks_hls("https://x/live/PLAYLIST.M3U8?token=abc&x=1")
        assert not hls.looks_hls("https://x/stream.mp3")
        assert not hls.looks_hls("https://x/live?format=m3u8")

    def test_remembered_and_sniffed_urls(self):
        calls = []

        def sniffer(url):
            calls.append(url)
            return url.endswith("/hls")

        p = HlsProxy(sniffer=sniffer)
        assert p.is_hls("https://a/hls") and p.is_hls("https://a/hls")
        assert not p.is_hls("https://a/icecast") and not p.is_hls("https://a/icecast")
        assert calls == ["https://a/hls", "https://a/icecast"]          # cached both ways
        assert not p.is_hls("https://a/plain.mp3") and "https://a/plain.mp3" not in calls
        hls.remember("https://probed/live")
        assert HlsProxy(sniffer=None).is_hls("https://probed/live")
        assert not HlsProxy(sniffer=None).is_hls("https://other/live")

    def test_body_sniff_needs_hls_tags_not_just_extm3u(self):
        assert hls.is_hls_body(b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:5,\nseg.ts\n")
        assert not hls.is_hls_body(b"#EXTM3U\n#EXTINF:-1,Radio\nhttp://x/stream\n")
        assert not hls.is_hls_body(b"\xff\xf1audio")

    def test_only_http_urls_register(self):
        p = HlsProxy()
        for bad in ("file:///etc/passwd.m3u8", "ftp://x/a.m3u8", "https://x/a b.m3u8", "http://x/\n.m3u8",
                    "https:///nohost.m3u8", "", None, "https://x/" + "a" * 3000 + ".m3u8"):
            assert not hls.valid_url(bad)
            assert not p.is_hls(bad)
            with pytest.raises(ValueError):
                p.register(bad)

    def test_token_is_stable_and_lookup_is_strict(self):
        p = HlsProxy(port=8098)
        local = p.register(RNE)
        assert local == p.register(RNE) == f"http://127.0.0.1:8098/hls/{hls.token_for(RNE)}"
        path = local.split("8098", 1)[1]
        assert p.lookup(path) == RNE and p.lookup(path + "?x=1") == RNE
        assert p.lookup("/hls/" + "0" * 20) is None                     # well-formed, never registered
        assert p.lookup("/hls/../etc/passwd") is None
        assert p.lookup("/hls/" + RNE) is None
        assert p.lookup("/ytm/dQw4w9WgXcQ") is None

    def test_token_map_is_bounded(self, monkeypatch):
        monkeypatch.setattr(hls, "MAX_TOKENS", 3)
        p = HlsProxy()
        paths = [p.register(f"https://x/{i}.m3u8").split("/hls", 1)[1] for i in range(5)]
        assert p.lookup("/hls" + paths[0]) is None and p.lookup("/hls" + paths[4]) == "https://x/4.m3u8"


# -- the radio source -------------------------------------------------------

class _Mpd:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return lambda *a: self.calls.append((name, *a))


class TestRadioRewrite:
    def test_hls_preset_queues_the_proxy_url_and_keeps_its_own(self, monkeypatch):
        proxy = HlsProxy(port=0, sniffer=None)
        monkeypatch.setattr(proxy, "available", lambda: True)
        mpd = _Mpd()
        src = RadioSource(mpd=lambda: mpd, hls=proxy)
        seen = []
        src.on_play = seen.append
        preset = Preset(1, "RNE 3", RNE)
        try:
            src.play(preset)
            queued = [c[1] for c in mpd.calls if c[0] == "add"]
            assert queued == [f"http://127.0.0.1:{proxy.port}/hls/{hls.token_for(RNE)}"]
            assert src.queued_uri(RNE) == queued[0]
            assert seen == [preset] and seen[0].uri == RNE                  # everyone else sees the original
            mpd.calls.clear()
            src.play(Preset(2, "Icecast", "http://ice/stream.mp3"))
            assert ("add", "http://ice/stream.mp3") in mpd.calls
            assert src.queued_uri("http://ice/stream.mp3") == "http://ice/stream.mp3"
        finally:
            proxy.stop()

    def test_without_ffmpeg_mpd_gets_the_original(self, monkeypatch):
        proxy = HlsProxy(port=0, sniffer=None, ffmpeg="no-such-ffmpeg-binary")
        mpd = _Mpd()
        RadioSource(mpd=lambda: mpd, hls=proxy).play(Preset(1, "RNE 3", RNE))
        assert ("add", RNE) in mpd.calls and proxy._server is None

    def test_no_proxy_configured_is_unchanged(self):
        mpd = _Mpd()
        src = RadioSource(mpd=lambda: mpd)
        src.play(Preset(1, "RNE 3", RNE))
        assert ("add", RNE) in mpd.calls and src.queued_uri(RNE) == RNE


# -- the proxy, with a fake ffmpeg -------------------------------------------

class TestProxy:
    def test_streams_aac_and_kills_ffmpeg_when_the_client_leaves(self, make_proxy):
        proxy = make_proxy("stream")
        sock, head, body = _get(proxy, RNE)
        assert head.startswith(b"HTTP/1.0 200") and b"Content-Type: audio/aac" in head
        assert b"Content-Length" not in head
        while len(body) < 4096:
            body += sock.recv(4096)
        assert body.startswith(b"adts:a")
        [pid] = _pids(proxy)
        sock.close()
        assert _gone(pid), "ffmpeg still running after MPD hung up"
        assert proxy.active() == 0 and _pids(proxy) == []

    def test_client_leaving_while_the_station_stalls_is_noticed(self, make_proxy):
        proxy = make_proxy("stall")                 # a frozen playlist: nothing to write
        sock, head, body = _get(proxy, RNE)
        assert head.startswith(b"HTTP/1.0 200")
        [pid] = _pids(proxy)
        sock.close()
        assert _gone(pid)

    def test_ffmpeg_ending_closes_the_response(self, make_proxy):
        proxy = make_proxy("end")
        sock, head, body = _get(proxy, RNE)
        body += _read_all(sock)
        assert body == b"adts:" + b"x" * 1000
        sock.close()
        deadline = time.monotonic() + 2
        while _pids(proxy) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _pids(proxy) == []

    def test_ffmpeg_failure_is_a_502(self, make_proxy):
        proxy = make_proxy("fail")
        sock, head, _ = _get(proxy, RNE)
        assert head.startswith(b"HTTP/1.0 502")
        sock.close()
        assert _pids(proxy) == []

    def test_ffmpeg_missing_is_a_502(self, make_proxy):
        proxy = make_proxy("stream")
        proxy._command = lambda url, muxer: ["/nonexistent/ffmpeg", url]
        sock, head, _ = _get(proxy, RNE)
        assert head.startswith(b"HTTP/1.0 502")
        sock.close()

    def test_no_first_bytes_in_time_is_a_502(self, make_proxy):
        proxy = make_proxy("silent", first_byte_s=0.5)
        sock, head, _ = _get(proxy, RNE)
        assert head.startswith(b"HTTP/1.0 502")
        sock.close()
        assert _pids(proxy) == []

    def test_mp3_station_falls_back_and_is_remembered(self, make_proxy):
        proxy = make_proxy("aac-only")
        sock, head, body = _get(proxy, RNE)
        assert head.startswith(b"HTTP/1.0 200") and b"Content-Type: audio/mpeg" in head
        while len(body) < 4:
            body += sock.recv(4096)
        assert body.startswith(b"mp3:")
        sock.close()
        assert proxy._formats[RNE] == 1             # next time straight to mp3

    def test_unknown_token_is_404(self, make_proxy):
        proxy = make_proxy("stream")
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=5)
        sock.sendall(b"GET /hls/" + b"0" * 20 + b" HTTP/1.0\r\n\r\n")
        assert _read_all(sock).startswith(b"HTTP/1.0 404")
        sock.close()

    def test_concurrency_cap_stops_the_oldest(self, make_proxy):
        proxy = make_proxy("stream", max_streams=2)
        first = _get(proxy, "https://a/1.m3u8")[0]
        second = _get(proxy, "https://a/2.m3u8")[0]
        [p1, p2] = _pids(proxy)
        third = _get(proxy, "https://a/3.m3u8")[0]
        assert _gone(p1), "oldest ffmpeg not stopped at the cap"
        first.settimeout(3)
        _read_all(first)                            # its response ended (EOF to MPD)
        deadline = time.monotonic() + 2
        while len(_pids(proxy)) > 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(_pids(proxy)) == 2 and p2 in _pids(proxy) and proxy.active() == 2
        for s in (first, second, third):
            s.close()

    def test_stop_kills_everything(self, make_proxy):
        proxy = make_proxy("stream")
        sock = _get(proxy, RNE)[0]
        [pid] = _pids(proxy)
        proxy.stop()
        assert _gone(pid)
        sock.close()


def test_try_remembers_a_probed_hls_link(tmp_path):
    from haslerd.web import stations_api
    from haslerd.web.stations_api import StationsApi

    class App:
        played = []

        def play(self, preset):
            self.played.append(preset)

    url = "https://cdn.example/live/radio"            # no .m3u8 in sight
    api = StationsApi(App(), tmp_path / "state", collection=tmp_path / "none.json",
                      probe=lambda u: {"ok": True, "codec": "hls", "bitrate": None, "error": ""})
    status, doc = api.route("POST", "/api/stations/try", {"url": url})
    assert status == 200 and doc["ok"]
    assert HlsProxy(sniffer=None).is_hls(url)
    assert stations_api.hls is hls
