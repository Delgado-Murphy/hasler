"""HLS radio, served to MPD as a plain HTTP audio stream.

Why: MPD 0.24 plays an ``.m3u8`` through its ffmpeg decoder, whose HLS
demuxer does its own network I/O in a loop MPD cannot interrupt. Switch
station while it waits for the next segment and ``DecoderControl::Stop``
deadlocks MPD's player and main threads: MPD stops answering anyone until it
is restarted. Upstream fixed it only in MPD 0.25.

So MPD never sees the playlist. ``RadioSource`` queues
``http://127.0.0.1:<port>/hls/<token>`` instead, and this proxy answers each
request with its own ``ffmpeg`` that reads the HLS stream and copies the
audio (no re-encode) out as ADTS (AAC) or MP3. MPD reads that through its
curl input, which it *can* interrupt; when it stops or switches, curl closes
the socket, and the ffmpeg behind it is killed at once.

Things that bite (measured on the bench Pi, ffmpeg 7.1):

* **ffmpeg ignores SIGTERM** while its HLS demuxer waits on a live edge or a
  frozen playlist. Every stop is terminate, wait 1 s, kill, reap.
* **A stalled upstream writes nothing**, so a client that went away is not
  noticed by a failed write. The pump watches the client socket for EOF too.
* The first bytes take 0.5-2.5 s (playlist + first segment). Response headers
  wait for them, so a station that never starts is a 502 MPD can report,
  rather than a 200 with silence behind it.
* The container is guessed: ADTS first (almost all HLS radio is AAC), MP3 if
  ffmpeg says ``adts muxer supports only codec aac``. What worked is
  remembered per station.

Tokens are a hash of the station URL, so the same station always gets the
same local URL (``queued_uri`` and moOde's queue stay comparable). Only
registered http(s) URLs are ever handed to ffmpeg; the path carries a token,
never a URL.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import select
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_PORT = 8098
MAX_STREAMS = 2                 # one playing, one being switched to
MAX_TOKENS = 512
FIRST_BYTE_S = 15.0             # playlist + first segment; a Pi on Wi-Fi takes 0.5-2.5 s
TERM_GRACE_S = 1.0
SNIFF_TIMEOUT_S = 2.0
SNIFF_NEGATIVE_S = 6 * 3600
TOKEN = re.compile(r"^/hls/([0-9a-f]{20})/?$")

# (muxer, Content-Type), in the order tried.
FORMATS: tuple[tuple[str, str], ...] = (("adts", "audio/aac"), ("mp3", "audio/mpeg"))
# Extensions that plainly are not HLS: never sniffed.
_NOT_HLS_EXT = (".mp3", ".aac", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".mp4", ".pls",
                ".m3u", ".asx", ".xspf", ".wav")

# URLs known to be HLS without looking at their path (a probe said so).
_known: set[str] = set()
_known_lock = threading.Lock()


def valid_url(url: Any) -> bool:
    """An http(s) URL safe to hand to ffmpeg as one argv entry."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return False
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def looks_hls(url: str) -> bool:
    """The URL's path ends ``.m3u8`` (query and fragment ignored)."""
    try:
        return urlparse(url).path.lower().endswith(".m3u8")
    except ValueError:
        return False


def remember(url: str) -> None:
    """Note that ``url`` is HLS (a probe saw ``#EXTM3U`` + ``#EXT-X-``)."""
    if valid_url(url):
        with _known_lock:
            _known.add(url)


def is_hls_body(body: bytes) -> bool:
    head = body[:4096].lstrip()
    return head.startswith(b"#EXTM3U") and b"#EXT-X-" in head


def sniff(url: str, timeout: float = SNIFF_TIMEOUT_S) -> bool:
    """Fetch the first bytes of ``url``: is it an HLS playlist? Never raises."""
    request = urllib.request.Request(url, headers={"User-Agent": "Music Player Daemon 0.24",
                                                   "Icy-MetaData": "0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            ctype = (response.headers.get("Content-Type") or "").lower()
            if ctype.startswith(("audio/mpeg", "audio/aac", "audio/ogg", "audio/flac", "application/ogg")):
                return False
            reader = getattr(response, "read1", response.read)
            return is_hls_body(reader(4096))
    except Exception:  # noqa: BLE001 - unreachable reads as "not HLS"; MPD will say why
        return False


def token_for(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


class _Stream:
    """One ffmpeg feeding one client."""

    def __init__(self, proc: subprocess.Popen, url: str) -> None:
        self.proc = proc
        self.url = url
        self.started = time.monotonic()
        self.evicted = threading.Event()
        self.stderr = b""
        self._drain = threading.Thread(target=self._read_stderr, name="hls-stderr", daemon=True)
        self._drain.start()

    def _read_stderr(self) -> None:
        try:
            for line in iter(self.proc.stderr.readline, b""):
                self.stderr = (self.stderr + line)[-2048:]
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        """Terminate, kill after ``TERM_GRACE_S``, reap. Idempotent, never raises."""
        proc = self.proc
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(TERM_GRACE_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
        except Exception:  # noqa: BLE001
            log.debug("could not stop ffmpeg %s", getattr(proc, "pid", "?"), exc_info=True)
        for f in (proc.stdout, proc.stderr):
            try:
                if f is not None:
                    f.close()
            except OSError:
                pass

    def error_text(self) -> str:
        self._drain.join(0.5)
        return self.stderr.decode("utf-8", "replace").strip()


class HlsProxy:
    """Local HTTP endpoint turning registered HLS URLs into plain audio streams."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        ffmpeg: str = "ffmpeg",
        max_streams: int = MAX_STREAMS,
        first_byte_s: float = FIRST_BYTE_S,
        sniffer: Callable[[str], bool] | None = sniff,
        command: Callable[[str, str], list[str]] | None = None,
    ) -> None:
        self.port = port
        self.ffmpeg = ffmpeg
        self.max_streams = max(1, int(max_streams))
        self.first_byte_s = first_byte_s
        self._sniffer = sniffer
        self._command = command or self.ffmpeg_command
        self._tokens: OrderedDict[str, str] = OrderedDict()
        self._formats: dict[str, int] = {}          # url -> index into FORMATS that worked
        self._sniffed: dict[str, tuple[bool, float]] = {}
        self._streams: list[_Stream] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._server_lock = threading.Lock()

    # -- URLs ---------------------------------------------------------------

    def available(self) -> bool:
        return shutil.which(self.ffmpeg) is not None

    def is_hls(self, url: str) -> bool:
        """``.m3u8`` path, a probe said so, or (once, cached) its first bytes say so."""
        if not valid_url(url):
            return False
        if looks_hls(url):
            return True
        with _known_lock:
            if url in _known:
                return True
        if self._sniffer is None:
            return False
        try:
            path = urlparse(url).path.lower()
        except ValueError:
            return False
        if path.endswith(_NOT_HLS_EXT):
            return False
        now = time.monotonic()
        with self._lock:
            hit = self._sniffed.get(url)
        if hit is not None and (hit[0] or now - hit[1] < SNIFF_NEGATIVE_S):
            return hit[0]
        found = bool(self._sniffer(url))
        with self._lock:
            self._sniffed[url] = (found, now)
            while len(self._sniffed) > MAX_TOKENS:
                self._sniffed.pop(next(iter(self._sniffed)))
        if found:
            log.info("%s is an HLS stream; it will play through the local proxy", url)
        return found

    def register(self, url: str) -> str:
        """The local URL that streams ``url``. Raises ValueError for a non-http(s) URL."""
        if not valid_url(url):
            raise ValueError("not an http(s) URL")
        token = token_for(url)
        with self._lock:
            self._tokens[token] = url
            self._tokens.move_to_end(token)
            while len(self._tokens) > MAX_TOKENS:
                self._tokens.popitem(last=False)
        return self.local_url(token)

    def local_url(self, token: str) -> str:
        return f"http://127.0.0.1:{self.port}/hls/{token}"

    def queued_uri(self, url: str) -> str:
        """What MPD's queue holds for ``url`` if it was rewritten, else ``url``."""
        if not valid_url(url):
            return url
        token = token_for(url)
        with self._lock:
            known = token in self._tokens
        return self.local_url(token) if known and self._server is not None else url

    def rewrite(self, url: str) -> str:
        """``url``, or the local proxy URL if it is HLS and the proxy can serve it. Never raises."""
        try:
            if not self.is_hls(url):
                return url
            if not self.available():
                log.warning("no %s on this system; HLS stream %s goes to MPD as is", self.ffmpeg, url)
                return url
            self.start()
            return self.register(url)
        except Exception:  # noqa: BLE001 - worst case MPD gets the original URL
            log.warning("could not route %s through the HLS proxy", url, exc_info=True)
            return url

    def lookup(self, path: str) -> str | None:
        match = TOKEN.match(path.split("?", 1)[0])
        if not match:
            return None
        with self._lock:
            return self._tokens.get(match.group(1))

    # -- ffmpeg -------------------------------------------------------------

    def ffmpeg_command(self, url: str, muxer: str) -> list[str]:
        return [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "15000000",
                "-i", url, "-vn", "-map", "0:a:0", "-c:a", "copy", "-f", muxer, "pipe:1"]

    def _spawn(self, url: str, muxer: str) -> _Stream:
        proc = subprocess.Popen(self._command(url, muxer), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
        stream = _Stream(proc, url)
        with self._lock:
            self._streams.append(stream)
            evict = self._streams[:-self.max_streams] if len(self._streams) > self.max_streams else []
        for old in evict:
            log.info("HLS proxy: more than %d streams; stopping the oldest (%s)", self.max_streams, old.url)
            old.evicted.set()
            try:
                old.proc.kill()
            except OSError:
                pass
        return stream

    def _forget(self, stream: _Stream) -> None:
        stream.stop()
        with self._lock:
            if stream in self._streams:
                self._streams.remove(stream)

    def active(self) -> int:
        with self._lock:
            return sum(1 for s in self._streams if s.proc.poll() is None)

    def _first_bytes(self, stream: _Stream, client: socket.socket) -> bytes | None:
        """The first stdout chunk, or None (ffmpeg ended, timed out, or the client left)."""
        fd = stream.proc.stdout.fileno()
        deadline = time.monotonic() + self.first_byte_s
        while time.monotonic() < deadline and not stream.evicted.is_set():
            ready, _, _ = select.select([fd, client], [], [], 0.25)
            if client in ready and _client_gone(client):
                stream.evicted.set()        # nobody to answer: no retry either
                return None
            if fd in ready:
                chunk = os.read(fd, 65536)
                return chunk or None
        return None

    def open(self, url: str, client: socket.socket) -> tuple[_Stream, bytes, str] | None:
        """Start ffmpeg for ``url`` and wait for audio: (stream, first chunk, content type)."""
        with self._lock:
            first = self._formats.get(url, 0)
        order = [first] + [i for i in range(len(FORMATS)) if i != first]
        for i in order:
            muxer, ctype = FORMATS[i]
            try:
                stream = self._spawn(url, muxer)
            except OSError as exc:
                log.warning("HLS proxy: could not start %s: %s", self.ffmpeg, exc)
                return None
            chunk = self._first_bytes(stream, client)
            if chunk:
                with self._lock:
                    self._formats[url] = i
                return stream, chunk, ctype
            try:
                stream.proc.wait(0.5)       # stdout closed: it is exiting, let it say why
            except subprocess.TimeoutExpired:
                pass
            err = stream.error_text() if stream.proc.poll() is not None else ""
            self._forget(stream)
            if stream.evicted.is_set():
                return None
            if "supports only codec" in err or "Could not write header" in err:
                log.info("HLS proxy: %s is not %s (%s); trying the next format", url, muxer, err[-160:])
                continue
            log.warning("HLS proxy: no audio from %s%s", url, f": {err[-300:]}" if err else "")
            return None
        return None

    def pump(self, stream: _Stream, client: socket.socket, write: Callable[[bytes], None]) -> None:
        """Copy ffmpeg's stdout to the client until either side ends."""
        fd = stream.proc.stdout.fileno()
        while not stream.evicted.is_set():
            ready, _, _ = select.select([fd, client], [], [], 0.5)
            if client in ready and _client_gone(client):
                return
            if fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return                  # ffmpeg ended: EOF to MPD, its watchdog takes over
                write(chunk)

    # -- server -------------------------------------------------------------

    def start(self) -> None:
        """Bind 127.0.0.1 and serve. Idempotent. ``port=0`` picks a free port."""
        with self._server_lock:
            if self._server is not None:
                return
            server = ThreadingHTTPServer(("127.0.0.1", self.port), _make_handler(self))
            server.daemon_threads = True
            self.port = server.server_address[1]
            threading.Thread(target=server.serve_forever, name="hls-proxy", daemon=True).start()
            self._server = server
            log.info("HLS proxy listening on 127.0.0.1:%s", self.port)

    def stop(self) -> None:
        with self._server_lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        with self._lock:
            streams, self._streams = list(self._streams), []
        for s in streams:
            s.evicted.set()
            s.stop()


def _client_gone(client: socket.socket) -> bool:
    """The client socket is readable: EOF (gone) or stray bytes (drained, still here)."""
    try:
        data = client.recv(4096)
    except BlockingIOError:
        return False
    except OSError:
        return True
    return not data


def _make_handler(proxy: HlsProxy) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "haslerd-hls/1"
        protocol_version = "HTTP/1.0"

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            url = proxy.lookup(self.path)
            if url is None:
                self.send_error(404)
                return
            opened = proxy.open(url, self.connection)
            if opened is None:
                try:
                    self.send_error(502, "the station sent no audio")
                except OSError:
                    pass                    # MPD already gave up
                return
            stream, chunk, ctype = opened
            log.info("HLS proxy: streaming %s as %s (ffmpeg %s)", url, ctype, stream.proc.pid)
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def write(data: bytes) -> None:
                    self.wfile.write(data)
                    self.wfile.flush()

                write(chunk)
                proxy.pump(stream, self.connection, write)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass                        # MPD stopped or switched
            finally:
                proxy._forget(stream)
                log.info("HLS proxy: stopped %s (ffmpeg %s)", url, stream.proc.pid)

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: we log what matters
            log.debug("hls proxy: " + fmt, *args)

    return Handler
