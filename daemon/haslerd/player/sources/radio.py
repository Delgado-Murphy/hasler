"""The ``radio`` source: an HTTP stream URL, played by MPD.

MPD is shared. The ``ytm`` source queues its proxy URLs into the same MPD, so
there is exactly one connection, owned here and handed out as a factory
(``RadioSource.mpd``). Two consequences shape this file:

* **"MPD is playing" does not mean "radio is playing".** ``is_active`` only
  counts a song that is *not* one of the YTM proxy's loopback URLs, otherwise
  the router would think radio owns the box while a YouTube playlist runs.
* **The client is not thread-safe, and YTM's proxy talks to MPD from a handler
  thread.** Every command through the shared factory is serialised by one lock
  here, so neither source has to trust the other's locking.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable
from urllib.parse import urlparse

from ..base import Preset
from .hls import HlsProxy

log = logging.getLogger(__name__)

DEFAULT_YTM_PORT = 8099
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def is_proxy_url(url: str, port: int = DEFAULT_YTM_PORT) -> bool:
    """True for a URL served by the local YTM proxy (``http://127.0.0.1:<port>/...``).

    Matched on loopback host + port rather than a string prefix, so
    ``localhost`` and ``127.0.0.1`` spellings both count.
    """
    try:
        parsed = urlparse(str(url))
        return parsed.hostname in _LOOPBACK and parsed.port == int(port)
    except ValueError:
        return False


class MpdUnresponsive(ConnectionError):
    """MPD did not answer in time a moment ago: failing fast instead of waiting again.

    2026-10-25: MPD 0.24 deadlocked (its ffmpeg decoder stuck in an HLS retry
    loop, the main thread waiting on it) with its port still open. Every
    caller waited a full timeout in turn behind the shared lock - the main
    loop among them, so systemd's watchdog killed haslerd. Now the first
    timeout marks MPD unresponsive and everyone else gets this at once, until
    MPD answers a probe again (``MpdConnection.probe``).
    """


def _timed_out(exc: BaseException) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout))


_local = threading.local()


@contextmanager
def mpd_patience(seconds: float):
    """MPD calls on this thread wait at most ``seconds`` (for the lock and for
    the reply) - for *questions* asked where waiting hurts (what plays? the
    panel loop's lamp, the web page), never for commands. A reply that misses
    it drops the connection (its answer is still on the way) and marks MPD
    *suspect*: the stream watchdog's thread then gives it a full-length probe,
    and only that marks it unresponsive (MPD busy stopping a stream is not a hang)."""
    previous = getattr(_local, "patience", None)
    _local.patience = seconds if previous is None else min(previous, seconds)
    try:
        yield
    finally:
        _local.patience = previous


def patience() -> float | None:
    """This thread's ``mpd_patience``, or None (the full timeout)."""
    return getattr(_local, "patience", None)


def mpd_probe(host: str, port: int, timeout: float = 2.0) -> str:
    """Does MPD answer a new client? ``"ok"``, ``"timeout"`` or ``"refused"``.

    A connection of its own (never the shared one, never its lock): greeting
    and a ``ping``, all within ``timeout``. "refused" = nothing listening (MPD
    stopped or restarting); "timeout" = something holds the port and says
    nothing (the 2026-10-25 hang: listen queue full, no greeting).
    """
    deadline = time.monotonic() + timeout

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("probe deadline")
        return remaining

    try:
        with socket.create_connection((host, port), timeout=left()) as sock:
            buf = b""
            sent = False
            while True:
                sock.settimeout(left())
                chunk = sock.recv(256)
                if not chunk:
                    return "refused"        # closed on us: not a working MPD either
                buf += chunk
                if not sent and b"\n" in buf:
                    if not buf.startswith(b"OK MPD"):
                        return "refused"
                    buf = buf.split(b"\n", 1)[1]
                    sock.settimeout(left())
                    sock.sendall(b"ping\n")
                    sent = True
                if sent and (b"OK\n" in buf or b"ACK" in buf):
                    return "ok"
    except (TimeoutError, socket.timeout):
        return "timeout"
    except OSError:
        return "refused"


class _LockedClient:
    """Wraps an MPDClient so every command runs under the shared lock.

    MPD commands are individually atomic on the wire; what breaks is two
    threads interleaving bytes on one socket. A per-call lock is enough -
    with one rule: a command that died mid-reply (a socket timeout while MPD
    was busy) leaves its answer still on the way, and the next caller would
    read it as its own. So anything but MPD's own refusal drops the
    connection, and the next command gets a fresh one. A timeout also marks
    MPD unresponsive (see ``MpdUnresponsive``).
    """

    def __init__(self, client: Any, lock: threading.RLock, owner: "MpdConnection | None" = None) -> None:
        self._client = client
        self._lock = lock
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            owner = self._owner
            if owner is not None:
                owner._acquire()
            else:
                self._lock.acquire()
            budget = owner._budget() if owner is not None else None
            try:
                if budget is not None:
                    self._client.timeout = budget
                result = attr(*args, **kwargs)
            except Exception as exc:
                if owner is not None and not _is_refusal(exc):
                    if _timed_out(exc):
                        owner._missed(f"{name}: {exc}", budget)
                    owner.forget(self._client)
                raise
            finally:
                if budget is not None and owner is not None:
                    try:
                        self._client.timeout = owner.timeout
                    except Exception:
                        pass
                self._lock.release()
            if owner is not None:
                owner._answered()
            return result

        return call


def _is_refusal(exc: BaseException) -> bool:
    """MPD answered ``ACK ...``: the connection is fine, the command was not."""
    try:
        from mpd import CommandError
    except ImportError:          # tests without python-mpd2
        return False
    return isinstance(exc, CommandError)


class MpdConnection:
    """Lazy, self-healing MPD connection. Call it to get a ready client.

    Connect on first use and reconnect silently - MPD restarts happen (moOde
    restarts it on every audio-config change), and a dead socket must cost one
    reconnect on the next key press, not a dead key.

    A *hung* MPD (port open, nothing answers) costs one timeout, once: that
    marks it unresponsive, and from then on every call - and every thread
    waiting for the lock - raises ``MpdUnresponsive`` at once, until a probe
    on a connection of its own (``probe``, the stream watchdog's thread every
    two seconds) hears MPD again, or ``retry_s`` passed with nobody probing.
    The panel loop, the web page and systemd's heartbeat never queue behind it.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6600,
        # MPD answers nobody while it stops a stream: 0.5-4 s on the bench.
        # Also the connect's (with MPD's greeting). Twice the worst seen, and
        # paid once per hang (above).
        timeout: float = 8,
        client_factory: Callable[[], Any] | None = None,
        retry_s: float = 5.0,
        prober: Callable[[str, int, float], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry_s = retry_s
        self._factory = client_factory
        self._prober = prober or mpd_probe
        self._clock = clock
        self._client: Any = None
        self._lock = threading.RLock()
        # Unresponsive since (monotonic), and when it was last tried meanwhile.
        self._down_since: float | None = None
        self._tried_at = 0.0
        self.down_reason = ""
        self._ok_at: float | None = None        # the last command MPD answered
        self._suspect_at: float | None = None   # a patient question timed out (mpd_patience)

    def _new_client(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from mpd import MPDClient

        return MPDClient()

    # -- unresponsive -----------------------------------------------------

    def unresponsive_s(self) -> float | None:
        """For how long MPD has not been answering (None = it answers, as far as we know)."""
        down = self._down_since
        return None if down is None else max(0.0, self._clock() - down)

    def answered_within(self, seconds: float) -> bool:
        """MPD answered a command in the last ``seconds`` (and is not marked unresponsive)."""
        ok = self._ok_at
        return self._down_since is None and ok is not None and self._clock() - ok <= seconds

    def trip(self, why: str) -> None:
        """MPD timed out: unresponsive from now (if it wasn't already)."""
        now = self._clock()
        self._tried_at = now
        if self._down_since is None:
            self._down_since = now
            self.down_reason = why
            log.warning("MPD is not answering (%s); failing fast until it does", why)

    def suspected(self) -> bool:
        """A question under ``mpd_patience`` timed out since MPD last answered."""
        return self._suspect_at is not None and self._down_since is None

    def _budget(self) -> float | None:
        """This thread's patience, if shorter than the full timeout."""
        budget = patience()
        return budget if budget is not None and budget < self.timeout else None

    def _missed(self, why: str, budget: float | None) -> None:
        if budget is None:
            self.trip(why)
        elif self._suspect_at is None:
            self._suspect_at = self._clock()
            log.info("MPD slow to answer (%s, %.1f s); having it checked", why, budget)

    def _answered(self) -> None:
        self._ok_at = self._clock()
        self._suspect_at = None
        if self._down_since is not None:
            log.info("MPD answering again after %.0f s", self._clock() - self._down_since)
            self._down_since = None
            self.down_reason = ""

    def _fail_fast(self) -> bool:
        return self._down_since is not None and self._clock() - self._tried_at < self.retry_s

    def _acquire(self) -> None:
        """The shared lock - but give up as soon as MPD is marked unresponsive
        (the holder is the one finding out), or after a generous wait."""
        if self._fail_fast():
            raise MpdUnresponsive(f"MPD not answering ({self.down_reason})")
        budget = self._budget()
        limit = time.monotonic() + (2 * self.timeout + 1 if budget is None else budget)
        while not self._lock.acquire(timeout=0.2):
            if self._fail_fast():
                raise MpdUnresponsive(f"MPD not answering ({self.down_reason})")
            if time.monotonic() >= limit:
                raise MpdUnresponsive("MPD busy: the connection was held too long")
        if self._fail_fast():                   # the holder found out while we waited
            self._lock.release()
            raise MpdUnresponsive(f"MPD not answering ({self.down_reason})")

    def probe(self, timeout: float = 2.0, trip: bool = True) -> str:
        """Ask MPD on a connection of its own (``mpd_probe``). "ok" clears the
        unresponsive mark; "timeout" sets it (unless ``trip`` is False)."""
        try:
            result = self._prober(self.host, self.port, timeout)
        except Exception:
            log.debug("MPD probe failed", exc_info=True)
            result = "refused"
        if result == "ok":
            self._answered()
        elif result == "refused":
            self._suspect_at = None             # not running (restarting): connects fail fast
        elif result == "timeout" and trip:
            self.trip("no answer to a probe")
        elif result == "timeout":
            self._missed("no answer to a quick probe", timeout)
        elif self._down_since is not None:
            self._tried_at = self._clock()      # still not answering: nobody else need try
        return result

    # -- the client -------------------------------------------------------

    def __call__(self) -> _LockedClient:
        self._acquire()
        try:
            budget = self._budget()
            if self._down_since is not None:
                self._tried_at = self._clock()  # this caller's try; the rest fail fast meanwhile
            if self._client is not None:
                try:
                    if budget is not None:
                        self._client.timeout = budget
                    self._client.ping()
                    if budget is not None:
                        self._client.timeout = self.timeout
                    self._answered()
                    return _LockedClient(self._client, self._lock, self)
                except Exception as exc:
                    self._drop()
                    if _timed_out(exc):
                        # Not answering, as opposed to gone (a restart): a
                        # fresh connect would only wait as long again.
                        self._missed(f"ping: {exc}", budget)
                        raise MpdUnresponsive(f"MPD not answering ({exc})") from exc
            client = self._new_client()
            client.timeout = self.timeout if budget is None else budget
            try:
                client.connect(self.host, self.port)
            except Exception as exc:
                if _timed_out(exc):
                    self._missed(f"connect: {exc}", budget)
                try:
                    client.disconnect()
                except Exception:
                    pass
                raise
            if budget is not None:
                client.timeout = self.timeout
            self._client = client
            self._answered()
            return _LockedClient(client, self._lock, self)
        finally:
            self._lock.release()

    def _drop(self) -> None:
        try:
            self._client.disconnect()
        except Exception:
            pass
        self._client = None

    def forget(self, client: Any) -> None:
        """Drop ``client`` if it is still the current one (a command on it failed)."""
        with self._lock:
            if self._client is client:
                log.info("MPD connection dropped after a failed command; reconnecting on next use")
                self._drop()

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._drop()


class RadioSource:
    """Implements ``Source`` for internet radio through MPD.

    ``play`` raises on failure so the router can flash the lamp; ``stop`` and
    ``is_active`` never do, because the panel loop calls them unconditionally.
    """

    name = "radio"
    # The router stops one source per backend, and none of the one it plays to.
    backend = "mpd"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6600,
        ytm_port: int = DEFAULT_YTM_PORT,
        mpd: Callable[[], Any] | None = None,
        hls: HlsProxy | None = None,
    ) -> None:
        self._owns_connection = mpd is None
        # HLS (.m3u8) stations go to MPD through this local proxy (hls.py):
        # MPD's own HLS demuxer can deadlock MPD on a station switch. None = as is.
        self.hls = hls
        # The factory YtmSource shares. Callable, returns a connected client.
        self.mpd: Callable[[], Any] = mpd or MpdConnection(host, port)
        self.ytm_port = ytm_port
        # Called with the preset after a stream starts (web/stations_api.py
        # keeps "recently played" with it). Must not raise; errors are logged.
        self.on_play: Callable[[Preset], None] | None = None

    def queued_uri(self, uri: str) -> str:
        """What MPD's queue holds when ``uri`` plays: itself, or its HLS proxy URL."""
        return self.hls.queued_uri(uri) if self.hls is not None else uri

    def play(self, preset: Preset) -> None:
        # Before MPD is touched: deciding may read the stream's first bytes.
        uri = self.hls.rewrite(preset.uri) if self.hls is not None else preset.uri
        client = self.mpd()
        client.clear()
        # YTM may have left random mode on for a shuffled playlist. Harmless
        # with one stream queued, but it would confuse anyone reading moOde.
        try:
            client.random(0)
        except Exception:
            log.debug("could not clear MPD random mode", exc_info=True)
        client.add(uri)
        client.play()
        log.info("playing radio preset %s (%s)", preset.n, preset.name)
        if self.on_play is not None:
            try:
                self.on_play(preset)
            except Exception:
                log.warning("radio on_play hook failed", exc_info=True)

    def stop(self) -> None:
        try:
            self.mpd().stop()
        except Exception:
            log.exception("could not stop MPD")

    def next_track(self) -> None:
        """A live stream has no next track. Deliberately nothing."""

    def is_active(self) -> bool:
        try:
            client = self.mpd()
            if client.status().get("state") != "play":
                return False
            song = str(client.currentsong().get("file", ""))
        except Exception:
            log.debug("radio is_active check failed", exc_info=True)
            return False
        return bool(song) and not is_proxy_url(song, self.ytm_port)

    def close(self) -> None:
        if self._owns_connection and hasattr(self.mpd, "close"):
            self.mpd.close()
