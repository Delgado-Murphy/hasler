"""The multiroom audio path: Snapcast between Haslers, behind four calls.

``SnapAudio`` is all the group logic needs to know about sound:

    lead()               this radio is the group's source
    follow(host, port)   play the leader's stream on this radio's card
    alone()              today's direct path, exactly as before
    status()             what is running now

Root work happens in ``scripts/hasler-multiroom`` (sudoers allows exactly that
file); it explains the routing. In short: every local player already plays
into moOde's ALSA name ``_audioout``. While grouped, a file in /run points that
name at the ALSA Loopback; ``snapserver-hasler`` captures the other end (lead
only) and ``snapclient-hasler`` plays the group stream into ``hasler_local`` -
this radio's card after mono and the tone knob (CamillaDSP). Volume stays on
the card's hardware mixer: snapclient runs ``--mixer none``. /run is empty
after a boot, so a radio always starts alone; haslerd restores the group.

Nothing is persisted here. Every call is idempotent: repeating ``lead()`` or
``follow()`` with the same leader changes nothing and interrupts nothing.

ALSA reads its routing when a device is opened, so a player that is already
playing keeps the old path until it reopens. After each switch ``reopen`` is
called (default: restart MPD's stream if it is playing, as the mono switch
does); go-librespot, AirPlay and Bluetooth follow at their next open - pause
and resume them if they must move at once.

``status()`` needs no root: unit state, the helper's /run files and the
kernel's TCP table (is the client connected to its server?).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-multiroom"
RUN_DIR = Path("/run/hasler-multiroom")
SERVER_UNIT = "snapserver-hasler.service"
CLIENT_UNIT = "snapclient-hasler.service"
DSP_UNIT = "camilladsp-hasler.service"     # the tone knob while grouped
DSP_MARK = Path("/etc/alsa/conf.d/.hasler-dsp")
SERVER_CONF = Path("/opt/hasler/current/app/config/snapcast/snapserver.conf")
PORT = 1704
BUFFER_MS = 1000          # snapserver.conf "buffer", if it can't be read
HELPER_TIMEOUT_S = 20.0
TCP_TABLES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))
# What "installed" means: the helper, and Debian's snapserver/snapclient.
NEEDS = (HELPER, "/usr/bin/snapserver", "/usr/bin/snapclient")

# A host name or IPv4/IPv6 address - the helper checks it again.
HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$")


class MultiroomError(RuntimeError):
    """The helper failed (the radio is back to alone); message fit to show."""


def _unit_active(unit: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", unit],
                              capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def buffer_ms(conf: str | None) -> int:
    """snapserver.conf's ``buffer`` in its [stream] section, else BUFFER_MS."""
    section = ""
    for line in (conf or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("["):
            section = line
        elif section == "[stream]" and "=" in line:
            key, _, value = line.partition("=")
            if key.strip() == "buffer" and value.strip().isdigit():
                return int(value.strip())
    return BUFFER_MS


def connected_to_port(tables: list[str | None], port: int) -> bool:
    """An ESTABLISHED TCP connection to a remote ``port`` in /proc/net/tcp{,6}."""
    want = f"{port:04X}"
    for text in tables:
        for line in (text or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "01" and parts[2].rsplit(":", 1)[-1] == want:
                return True
    return False


class SnapAudio:
    """Snapcast routing for this radio. Every outside touch is injectable."""

    def __init__(
        self,
        helper: str = HELPER,
        run: Callable[..., Any] = subprocess.run,
        unit_active: Callable[[str], bool] = _unit_active,
        read: Callable[[Path], str | None] = _read,
        reopen: Callable[[], None] | None = None,
        run_dir: Path = RUN_DIR,
        server_conf: Path = SERVER_CONF,
        tcp_tables: tuple[Path, ...] = TCP_TABLES,
        available: Callable[[str], bool] = lambda p: os.access(p, os.X_OK),
        dsp_mark: Path = DSP_MARK,
    ) -> None:
        self.helper = helper
        self._run, self._unit_active, self._read = run, unit_active, read
        self._reopen = reopen if reopen is not None else self._reopen_mpd
        self.run_dir, self.server_conf, self.tcp_tables = Path(run_dir), Path(server_conf), tcp_tables
        self._available = available
        self.dsp_mark = Path(dsp_mark)
        self._error: str | None = None

    @property
    def available(self) -> bool:
        """The helper and snapcast are installed (not on a laptop running --fake)."""
        return all(self._available(p) for p in (self.helper, *NEEDS[1:]))

    # -- switching --------------------------------------------------------

    def lead(self) -> None:
        self._switch(["lead"], "lead")

    def follow(self, host: str, port: int = PORT) -> None:
        if not isinstance(host, str) or not HOST.match(host) or host.startswith("-"):
            raise ValueError(f"not a host name or address: {host!r}")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"not a port: {port!r}")
        self._switch(["follow", host, str(port)], "follow")

    def alone(self) -> None:
        self._switch(["alone"], "alone")

    def _switch(self, args: list[str], want: str) -> None:
        before = self._mode()
        target = self._leader()
        if not self.available:
            if want == "alone":
                return            # nothing installed, so nothing to undo
            self._error = "multiroom isn't installed on this radio"
            raise MultiroomError(self._error)
        doc = self._helper(args)
        if not doc.get("ok"):
            self._error = str(doc.get("error") or "unknown error")
            log.warning("multiroom %s failed: %s", want, self._error)
            self._safe_reopen()      # the helper fell back to alone
            raise MultiroomError(f"multiroom {want} failed: {self._error}")
        self._error = None
        moved = before != doc.get("mode") or (want == "follow" and target != doc.get("leader"))
        if moved:
            log.info("multiroom: %s%s", doc.get("mode"),
                     f" ({doc.get('leader')})" if doc.get("leader") else "")
        # A player moves to the new path only when it reopens the device.
        # follow: locals are stopped by the caller, nothing to reopen.
        if moved and want != "follow":
            self._safe_reopen()

    def _helper(self, args: list[str]) -> dict:
        try:
            out = self._run(["sudo", "-n", self.helper, *args],
                            capture_output=True, text=True, timeout=HELPER_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"helper did not answer: {exc}"}
        try:
            return json.loads((out.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "error": (out.stderr or "no answer").strip()[:200]}

    def _safe_reopen(self) -> None:
        try:
            self._reopen()
        except Exception:   # noqa: BLE001 - a reopen never fails a switch
            log.debug("reopen after the multiroom switch failed", exc_info=True)

    def _reopen_mpd(self) -> None:
        """Restart MPD's stream if it is playing, so it reopens _audioout."""
        try:
            status = self._run(["mpc", "status"], capture_output=True, text=True, timeout=5)
            if "[playing]" not in (status.stdout or ""):
                return
            self._run(["mpc", "-q", "stop"], capture_output=True, timeout=5)
            self._run(["mpc", "-q", "play"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            log.debug("could not restart MPD after the multiroom switch", exc_info=True)

    # -- reading ----------------------------------------------------------

    def _mode(self) -> str:
        mode = (self._read(self.run_dir / "mode") or "").strip()
        return mode if mode in ("lead", "follow") else "alone"

    def _leader(self) -> str | None:
        if self._mode() != "follow":
            return None
        env = dict(line.split("=", 1) for line in (self._read(self.run_dir / "client.env") or "").splitlines()
                   if "=" in line)
        host, port = env.get("SNAP_HOST"), env.get("SNAP_PORT")
        return f"{host}:{port}" if host and port else None

    def status(self) -> dict:
        """{"mode", "server", "client", "connected", "leader", "latency_ms", "error"}.

        ``leader`` is "host:port" while following, else None. ``latency_ms``
        is the key-to-sound delay the group adds (snapserver's buffer), 0 alone.
        ``connected``: the client has a live connection to its server.
        ``error`` is the last failed switch, or what is wrong right now.
        """
        mode = self._mode()
        server = self._unit_active(SERVER_UNIT)
        client = self._unit_active(CLIENT_UNIT)
        connected = client and connected_to_port([self._read(p) for p in self.tcp_tables],
                                                 self._port())
        error = self._error
        if error is None:
            if mode == "lead" and not server:
                error = "snapserver isn't running"
            elif mode != "alone" and not client:
                error = "snapclient isn't running"
            elif mode == "follow" and not connected:
                error = "not connected to the leader"
            elif mode != "alone" and self.dsp_mark.exists() and not self._unit_active(DSP_UNIT):
                error = "the tone knob's CamillaDSP isn't running yet"
        return {
            "mode": mode,
            "server": server,
            "client": client,
            "connected": connected,
            "leader": self._leader(),
            "latency_ms": buffer_ms(self._read(self.server_conf)) if mode != "alone" else 0,
            "error": error,
        }

    def _port(self) -> int:
        leader = self._leader()
        if leader and leader.rsplit(":", 1)[-1].isdigit():
            return int(leader.rsplit(":", 1)[-1])
        return PORT
