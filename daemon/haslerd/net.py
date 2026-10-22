"""Is the internet there? Asked cheaply, and never on the panel's time.

Radio, Spotify and YouTube all need the network; with it gone the lamp should
say so rather than leaving a key press to fail in silence. The probe is one TCP
connect to a public DNS resolver - no DNS lookup of our own (which could hang
for the resolver timeout), no HTTP.

The connect runs on a background thread. Even a short timeout is too long for a
50 Hz loop: a tap shorter than the stall would be missed outright, because the
key's press and release would both happen while nobody was polling.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable

log = logging.getLogger(__name__)

DEFAULT_HOST = "1.1.1.1"
DEFAULT_PORT = 53
DEFAULT_INTERVAL_S = 10.0
DEFAULT_TIMEOUT_S = 2.0
# moOde's fallback hotspot ("Hasler", started when no known Wi-Fi answers at
# boot) puts the Pi at this address (cfg_system ap_network_addr). A hotspot
# profile written without it (scripts/hasler-wifi-join before 2026-10-21, or
# moOde's cfgNetworks run outside moOde) lands on NetworkManager's default for
# shared connections instead: still setup mode, so both count.
HOTSPOT_ADDR = "172.24.1.1"
NM_SHARED_ADDR = "10.42.0.1"
HOTSPOT_ADDRS = (HOTSPOT_ADDR, NM_SHARED_ADDR)


def tcp_probe(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = DEFAULT_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def hotspot_address(addrs: str | tuple[str, ...] = HOTSPOT_ADDRS) -> str:
    """The hotspot address the Pi itself owns right now, or "".

    Binding to an address only works if one of our interfaces has it, so this
    answers without shelling out to nmcli or ip.
    """
    for addr in (addrs,) if isinstance(addrs, str) else addrs:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind((addr, 0))
            return addr
        except OSError:
            continue
    return ""


def hotspot_probe(addrs: str | tuple[str, ...] = HOTSPOT_ADDRS) -> bool:
    """True while the Pi is moOde's setup hotspot."""
    return bool(hotspot_address(addrs))


class Connectivity:
    """Polled from the main loop; probes at most every ``interval_s``.

    Starts optimistic (``online=True``) so a box that boots faster than its
    Wi-Fi does not greet you with a flashing red lamp for the first ten
    seconds. ``threaded=False`` probes inline, for tests.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        interval_s: float = DEFAULT_INTERVAL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        probe: Callable[[], bool] | None = None,
        threaded: bool = True,
        hotspot: Callable[[], bool] | None = None,
    ) -> None:
        self.interval_s = interval_s
        self.online = True
        self.hotspot = False
        self._hotspot = hotspot or hotspot_probe
        self._probe = probe or (lambda: tcp_probe(host, port, timeout_s))
        self._threaded = threaded
        self._next = 0.0
        self._busy = threading.Lock()

    def poll(self, now: float) -> bool:
        if now >= self._next:
            self._next = now + self.interval_s
            if self._threaded:
                if self._busy.acquire(blocking=False):
                    threading.Thread(target=self._run, name="connectivity", daemon=True).start()
            else:
                self._busy.acquire()
                self._run()
        return self.online

    def _run(self) -> None:
        try:
            try:
                online = bool(self._probe())
            except Exception:
                online = False
            if online != self.online:
                log.info("network %s", "back" if online else "down")
            self.online = online
            try:
                hotspot = bool(self._hotspot())
            except Exception:
                hotspot = False
            if hotspot != self.hotspot:
                log.info("Wi-Fi hotspot %s", "up - setup mode: join its Wi-Fi, the page is http://172.24.1.1:8080/wifi" if hotspot else "down")
            self.hotspot = hotspot
        finally:
            self._busy.release()
