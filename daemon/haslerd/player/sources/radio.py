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
import threading
from typing import Any, Callable
from urllib.parse import urlparse

from ..base import Preset

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


class _LockedClient:
    """Wraps an MPDClient so every command runs under the shared lock.

    MPD commands are individually atomic on the wire; what breaks is two
    threads interleaving bytes on one socket. A per-call lock is enough -
    with one rule: a command that died mid-reply (a socket timeout while MPD
    was busy) leaves its answer still on the way, and the next caller would
    read it as its own. So anything but MPD's own refusal drops the
    connection, and the next command gets a fresh one.
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
            with self._lock:
                try:
                    return attr(*args, **kwargs)
                except Exception as exc:
                    if self._owner is not None and not _is_refusal(exc):
                        self._owner.forget(self._client)
                    raise

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
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6600,
        # MPD answers nobody while it stops a stream: 0.5-4 s on the bench.
        timeout: float = 10,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._factory = client_factory
        self._client: Any = None
        self._lock = threading.RLock()

    def _new_client(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from mpd import MPDClient

        return MPDClient()

    def __call__(self) -> _LockedClient:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.ping()
                    return _LockedClient(self._client, self._lock, self)
                except Exception:
                    self._drop()
            client = self._new_client()
            client.timeout = self.timeout
            client.connect(self.host, self.port)
            self._client = client
            return _LockedClient(client, self._lock, self)

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
    ) -> None:
        self._owns_connection = mpd is None
        # The factory YtmSource shares. Callable, returns a connected client.
        self.mpd: Callable[[], Any] = mpd or MpdConnection(host, port)
        self.ytm_port = ytm_port
        # Called with the preset after a stream starts (web/stations_api.py
        # keeps "recently played" with it). Must not raise; errors are logged.
        self.on_play: Callable[[Preset], None] | None = None

    def play(self, preset: Preset) -> None:
        client = self.mpd()
        client.clear()
        # YTM may have left random mode on for a shuffled playlist. Harmless
        # with one stream queued, but it would confuse anyone reading moOde.
        try:
            client.random(0)
        except Exception:
            log.debug("could not clear MPD random mode", exc_info=True)
        client.add(preset.uri)
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
