"""SpotifySource against a stub go-librespot. Loopback only, no real network.

The stub speaks just enough of go-librespot v0.10.3's API to check routes,
bodies and ordering - including its habit of answering 204 for "no session",
which must count as failure, not success.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from haslerd.player.base import Preset, Source
from haslerd.player.sources.spotify import SpotifyError, SpotifySource

PLAYING = {"stopped": False, "paused": False, "buffering": False, "track": {"uri": "spotify:track:x"}}
PAUSED = {"stopped": False, "paused": True, "buffering": False, "track": {"uri": "spotify:track:x"}}
STOPPED = {"stopped": True, "paused": False, "buffering": False, "track": None}


class StubLibrespot:
    """Records ``(method, path, body)`` and serves a settable ``/status``."""

    def __init__(self):
        self.requests: list[tuple[str, str, object]] = []
        self.status_body: dict | None = PLAYING
        self.status_code = 200
        self.post_code = 200
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body=None):
                payload = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                if payload:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                stub.requests.append(("GET", self.path, None))
                if self.path == "/status" and stub.status_code == 200:
                    self._reply(200, stub.status_body)
                else:
                    self._reply(stub.status_code)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                stub.requests.append(("POST", self.path, json.loads(raw) if raw else None))
                self._reply(stub.post_code)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self.thread.start()

    def posts(self):
        return [(path, body) for method, path, body in self.requests if method == "POST"]

    def status_hits(self):
        return sum(1 for method, path, _ in self.requests if (method, path) == ("GET", "/status"))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = StubLibrespot()
    yield s
    s.close()


@pytest.fixture
def source(stub):
    return SpotifySource(base_url=stub.url, timeout=2.0)


def preset(shuffle=False):
    return Preset(n=3, name="Morning", uri="spotify:playlist:37i9dQZF1DX0XUsuxWHRQd",
                  source="spotify", shuffle=shuffle)


def dead_url():
    """A loopback port with nothing listening: connection refused, instantly."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_implements_source_protocol(source):
    assert isinstance(source, Source)
    assert source.name == "spotify"


def test_play_sends_uri_to_player_play(source, stub):
    source.play(preset())
    assert stub.posts()[-1] == (
        "/player/play",
        {"uri": "spotify:playlist:37i9dQZF1DX0XUsuxWHRQd", "paused": False},
    )


def test_shuffle_is_set_before_play(source, stub):
    source.play(preset(shuffle=True))
    assert [p for p, _ in stub.posts()] == ["/player/shuffle_context", "/player/play"]
    assert stub.posts()[0][1] == {"shuffle_context": True}


def test_unshuffled_preset_turns_shuffle_off(source, stub):
    # go-librespot remembers shuffle across plays; an album must not inherit it.
    source.play(preset(shuffle=False))
    assert stub.posts()[0] == ("/player/shuffle_context", {"shuffle_context": False})
    assert stub.posts()[1][0] == "/player/play"


def test_play_raises_when_unreachable():
    with pytest.raises(SpotifyError):
        SpotifySource(base_url=dead_url(), timeout=1.0).play(preset())


def test_play_raises_on_204_no_session(source, stub):
    stub.post_code = 204
    with pytest.raises(SpotifyError):
        source.play(preset())


def test_play_raises_on_http_error(source, stub):
    stub.post_code = 500
    with pytest.raises(SpotifyError):
        source.play(preset())


def test_stop_pauses(source, stub):
    source.stop()
    assert stub.posts() == [("/player/pause", None)]


def test_next_track(source, stub):
    source.next_track()
    assert stub.posts() == [("/player/next", None)]


def test_stop_and_next_never_raise():
    dead = SpotifySource(base_url=dead_url(), timeout=1.0)
    dead.stop()
    dead.next_track()


def test_stop_swallows_http_errors(source, stub):
    stub.post_code = 500
    source.stop()
    source.next_track()


@pytest.mark.parametrize(
    "body, expected",
    [(PLAYING, True), (PAUSED, False), (STOPPED, False), ({}, False), ([1, 2], False)],
)
def test_is_active_reads_status(source, stub, body, expected):
    stub.status_body = body
    assert source.is_active() is expected


def test_is_active_false_when_no_session(source, stub):
    stub.status_code = 204
    assert source.is_active() is False


def test_is_active_false_on_server_error(source, stub):
    stub.status_code = 500
    assert source.is_active() is False


def test_is_active_false_when_unreachable():
    assert SpotifySource(base_url=dead_url()).is_active() is False


def test_is_active_is_cached(stub, monkeypatch):
    import haslerd.player.sources.spotify as mod

    clock = [100.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: clock[0])
    src = SpotifySource(base_url=stub.url, cache_seconds=1.0)
    assert src.is_active() is True
    stub.status_body = PAUSED
    for _ in range(50):  # one second of 50 Hz panel loop
        assert src.is_active() is True
    assert stub.status_hits() == 1
    clock[0] += 1.01
    assert src.is_active() is False
    assert stub.status_hits() == 2


def test_play_and_stop_update_cache_without_polling(source, stub):
    stub.status_body = STOPPED
    source.play(preset())
    assert source.is_active() is True  # optimistic across the buffering gap
    source.stop()
    assert source.is_active() is False
    assert stub.status_hits() == 0


def test_close_is_noop(source, stub):
    source.close()
    assert stub.requests == []
