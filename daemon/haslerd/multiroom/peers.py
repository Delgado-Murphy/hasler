"""Other Haslers on the LAN, and how one radio talks to another.

* **Finding them.** Every radio announces ``_hasler._tcp`` (config/avahi/
  hasler.service: port 8080, TXT ``api=1``, ``path=/api/info``).
  ``avahi-browse -rtp _hasler._tcp`` resolves and dumps the records, then
  exits (the same tool and format as ha/discover.py). Each address found is
  asked ``GET /api/info`` for its ``id``, ``name`` and ``multiroom`` block; the
  record whose ``id`` is this radio's own is left out. IPv4 is preferred: an
  IPv6 link-local address needs a scope id nobody wants to carry around.
* **Caching.** A browse costs up to BROWSE_TIMEOUT_S, so ``Peers`` keeps the
  last answer and refreshes it in the background when it is older than
  CACHE_S (``kick``). A radio missing from a browse is shown offline, and
  forgotten after FORGET_S.
* **Talking.** ``http_json`` is the one radio-to-radio call: JSON in and out,
  ``Content-Type: application/json``, no Origin, short timeouts. The other
  radio's ``request_refusal`` (web/server.py) accepts it because the Host
  header is the very address the request was sent to. There are no tokens
  between radios: the LAN is trusted, as the key page trusts it.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from ..ha.discover import _unescape, parse_txt

log = logging.getLogger(__name__)

AVAHI_CMD = ["avahi-browse", "-rtp", "_hasler._tcp"]
BROWSE_TIMEOUT_S = 4.0
INFO_TIMEOUT_S = 2.0
CACHE_S = 30.0          # a browse older than this is redone on the next look
FORGET_S = 600.0        # not seen for this long: no longer listed
API_PORT = 8080

# fetch(method, url, body, timeout) -> (status, parsed JSON doc). Raises
# PeerUnreachable when nothing answered.
Fetch = Callable[[str, str, Any, float], tuple[int, Any]]
Runner = Callable[..., subprocess.CompletedProcess]


class PeerUnreachable(Exception):
    """Nothing answered at that address (refused, timed out, no route)."""


def url_for(host: str, port: int, path: str) -> str:
    hostpart = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{hostpart}:{int(port)}{path}"


def http_json(method: str, url: str, body: Any = None, timeout: float = INFO_TIMEOUT_S) -> tuple[int, Any]:
    """One radio-to-radio call. (status, doc); an HTTP error status is an answer."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "haslerd-multiroom"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(64 * 1024) if exc.fp is not None else b""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PeerUnreachable(f"{url}: {getattr(exc, 'reason', exc)}") from exc
    try:
        doc = json.loads(raw or b"{}")
    except ValueError:
        doc = {}
    return status, doc


def address_towards(host: str) -> str:
    """This radio's own address as ``host`` sees it (no packet is sent).

    Connecting a UDP socket only picks the route and the source address.
    Falls back to ``<hostname>.local`` when there is no route.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect((host.strip("[]"), 9))
            return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostname().split(".")[0] + ".local"
        except OSError:
            return "hasler.local"


def parse_avahi(output: str) -> list[tuple[str, int]]:
    """``avahi-browse -rtp`` output -> [(address, port)], one per radio, IPv4 first.

    Resolved lines are ``=;iface;proto;name;type;domain;host;address;port;txt``.
    A radio announces itself on every interface and protocol; one address per
    host name is enough.
    """
    by_host: dict[str, tuple[str, str, int]] = {}
    order: list[str] = []
    for line in output.splitlines():
        parts = line.split(";", 9)
        if len(parts) < 9 or parts[0] != "=":
            continue
        _, _iface, proto, _name, _stype, _domain, host, address, port = parts[:9]
        txt = parse_txt(parts[9]) if len(parts) > 9 else {}
        if txt.get("api") not in (None, "1"):
            continue                  # a radio speaking an API we don't know
        try:
            port_n = int(port)
        except ValueError:
            continue
        if not address:
            continue
        key = _unescape(host or address).lower()
        seen = by_host.get(key)
        if seen is None:
            by_host[key] = (proto, address, port_n)
            order.append(key)
        elif seen[0] != "IPv4" and proto == "IPv4":
            by_host[key] = (proto, address, port_n)
    return [(by_host[k][1], by_host[k][2]) for k in order]


@dataclass
class Peer:
    """Another Hasler, as last heard from."""

    id: str
    name: str
    host: str
    port: int = API_PORT
    multiroom: dict = field(default_factory=dict)
    seen_at: float = 0.0
    online: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.multiroom.get("enabled"))

    @property
    def role(self) -> str:
        return str(self.multiroom.get("role") or "alone")

    def doc(self) -> dict:
        mr = self.multiroom
        return {
            "id": self.id, "name": self.name, "host": self.host, "port": self.port,
            "online": self.online, "enabled": self.enabled, "role": self.role,
            "leader": mr.get("leader"), "playing": bool(mr.get("playing")),
            "source": mr.get("source"),
        }


def peer_from_info(doc: Any, host: str, port: int, now: float) -> Peer | None:
    """An /api/info answer -> Peer, or None if it isn't a Hasler's."""
    if not isinstance(doc, dict) or not isinstance(doc.get("id"), str) or not doc["id"]:
        return None
    mr = doc.get("multiroom") if isinstance(doc.get("multiroom"), dict) else {}
    return Peer(id=doc["id"], name=str(doc.get("name") or doc["id"]), host=host, port=int(port),
                multiroom=dict(mr), seen_at=now)


class Peers:
    """The Haslers found on the LAN, cached. Thread-safe; never raises."""

    def __init__(
        self,
        self_id: Callable[[], str],
        fetch: Fetch = http_json,
        runner: Runner = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.self_id = self_id
        self.fetch = fetch
        self.runner = runner
        self.clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, name="peers", daemon=True).start())
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {}
        self._browsed_at: float | None = None
        self._busy = False
        self.note: str | None = None     # why discovery found nothing, for the page

    # -- reading ------------------------------------------------------------

    def list(self) -> list[Peer]:
        now = self.clock()
        with self._lock:
            peers = [p for p in self._peers.values() if now - p.seen_at < FORGET_S]
        return sorted(peers, key=lambda p: (p.name.lower(), p.id))

    def get(self, peer_id: str) -> Peer | None:
        with self._lock:
            return self._peers.get(peer_id)

    @property
    def searching(self) -> bool:
        return self._busy

    def stale(self) -> bool:
        return self._browsed_at is None or self.clock() - self._browsed_at >= CACHE_S

    # -- updating -------------------------------------------------------------

    def remember(self, peer: Peer) -> None:
        """What a radio said about itself just now (an /api/info answer)."""
        with self._lock:
            self._peers[peer.id] = peer

    def seen_offline(self, peer_id: str) -> None:
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is not None:
                peer.online = False

    def kick(self, force: bool = False) -> None:
        """Refresh in the background if the cache is stale (or ``force``)."""
        if self._busy or not (force or self.stale()):
            return
        self._busy = True

        def run() -> None:
            try:
                self.refresh()
            finally:
                self._busy = False
        try:
            self._spawn(run)
        except Exception:
            self._busy = False
            log.exception("could not start peer discovery")

    def info(self, host: str, port: int, timeout: float = INFO_TIMEOUT_S) -> Peer | None:
        """Ask one address who it is. None if it didn't answer as a Hasler."""
        try:
            status, doc = self.fetch("GET", url_for(host, port, "/api/info"), None, timeout)
        except PeerUnreachable:
            return None
        except Exception:
            log.debug("asking %s:%s for /api/info failed", host, port, exc_info=True)
            return None
        if status != 200:
            return None
        return peer_from_info(doc, host, port, self.clock())

    def refresh(self) -> list[Peer]:
        """Browse now (blocks up to ~BROWSE_TIMEOUT_S + INFO_TIMEOUT_S). Never raises."""
        addresses, note = self._browse()
        found: dict[str, Peer] = {}
        lock = threading.Lock()

        def ask(host: str, port: int) -> None:
            peer = self.info(host, port)
            if peer is not None:
                with lock:
                    found.setdefault(peer.id, peer)

        workers = [threading.Thread(target=ask, args=a, daemon=True) for a in addresses]
        for w in workers:
            w.start()
        for w in workers:
            w.join(INFO_TIMEOUT_S + 0.5)
        try:
            me = self.self_id()
        except Exception:
            me = None
        with lock:
            found.pop(me, None)
            fresh = dict(found)
        with self._lock:
            for peer in self._peers.values():
                if peer.id not in fresh:
                    peer.online = False
            self._peers.update(fresh)
            self._browsed_at = self.clock()
        self.note = note if note or fresh else "No other Hasler found on this network"
        return self.list()

    def _browse(self) -> tuple[list[tuple[str, int]], str | None]:
        try:
            done = self.runner(AVAHI_CMD, capture_output=True, text=True, timeout=BROWSE_TIMEOUT_S)
            output = done.stdout or ""
        except FileNotFoundError:
            return [], "avahi-browse isn't installed on the radio (apt install avahi-utils)"
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or b""
            output = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        except Exception as exc:
            log.info("avahi-browse failed: %s", exc)
            return [], "Network discovery failed on the radio"
        return parse_avahi(output), None
