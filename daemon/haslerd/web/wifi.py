"""Wi-Fi setup for the captive portal: list networks, join one.

When moOde can't find a known network at boot it starts the "Hasler"
hotspot; a phone joining it is redirected here (config/captive/). Joining is
done by a small root helper (scripts/hasler-wifi-join) that writes moOde's own
network tables and reboots - haslerd itself never runs as root. The SSID and
password go to it on stdin, never on a command line.

The same helper scans while the radio is a hotspot (NetworkManager can't) and
tries the saved networks again for the watchdog (haslerd/wifiwatch.py), so a
power cut where the router comes back slower than the radio does not leave it
in setup mode until somebody pulls the plug.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..net import hotspot_probe
from .stations import MOODE_DB

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-wifi-join"
HOTSPOT_SSID = "Hasler"
# Where haslerd keeps the setup hotspot's password when it chose it
# (ensure_hotspot): ssid=... and password=... lines, 0600. main.py points this
# into the state directory.
HOTSPOT_FILE = "/var/lib/haslerd/hotspot.txt"
# Hotspot passwords haslerd chooses: letters and digits nobody misreads.
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RESERVED = ("None", "Activate Hotspot")
# A scan from the hotspot takes the radio off its channel for a moment (the
# phone on it notices): at most this often, however often the page asks.
HOTSPOT_SCAN_EVERY_S = 20.0


def split_terse(line: str) -> list[str]:
    """nmcli -t fields: ':'-separated, with ':' and '\\' inside values escaped."""
    fields, cur, escaped = [], [], False
    for ch in line:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def security_for(nm_security: str) -> str:
    """nmcli's SECURITY column -> what moOde stores: none, wpa-psk or sae."""
    s = nm_security.upper()
    if not s or s == "--":
        return "none"
    if "WPA3" in s and "WPA2" not in s and "WPA1" not in s:
        return "sae"            # WPA3-only: needs SAE
    return "wpa-psk"


def parse_scan(output: str) -> list[dict]:
    """`nmcli -t -f SSID,SIGNAL,SECURITY dev wifi list` -> one entry per SSID, strongest first."""
    best: dict[str, dict] = {}
    for line in output.splitlines():
        parts = split_terse(line)
        if len(parts) < 3:
            continue
        ssid, signal, sec = parts[0], parts[1], parts[2]
        if not ssid or ssid == HOTSPOT_SSID:
            continue
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        if ssid not in best or strength > best[ssid]["signal"]:
            best[ssid] = {"ssid": ssid, "signal": strength, "security": security_for(sec)}
    return sorted(best.values(), key=lambda n: -n["signal"])


def _iw_unescape(s: str) -> str:
    """iw prints non-printable SSID bytes as \\xNN."""
    try:
        return re.sub(rb"\\x([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]),
                      s.encode("utf-8", "surrogateescape")).decode("utf-8", "replace")
    except Exception:
        return s


def parse_iw_scan(output: str, skip: str = HOTSPOT_SSID) -> list[dict]:
    """`iw dev wlan0 scan` -> the same shape as parse_scan(), strongest first.

    Signal in dBm becomes nmcli's 0-100 scale (-100 dBm = 0, -50 dBm = 100);
    security from the RSN/WPA elements and, for WPA3-only, the SAE suite.
    """
    best: dict[str, dict] = {}
    cur: dict[str, Any] | None = None

    def flush() -> None:
        if not cur or not cur.get("ssid") or cur["ssid"] == skip:
            return
        if not cur["rsn"] and not cur["wpa"]:
            sec = "none" if not cur["privacy"] else "wpa-psk"
        elif cur["sae"] and not cur["psk"]:
            sec = "sae"
        else:
            sec = "wpa-psk"
        entry = {"ssid": cur["ssid"], "signal": cur["signal"], "security": sec}
        if cur["ssid"] not in best or entry["signal"] > best[cur["ssid"]]["signal"]:
            best[cur["ssid"]] = entry

    for raw in output.splitlines():
        line = raw.strip()
        if raw.startswith("BSS "):
            flush()
            cur = {"ssid": "", "signal": 0, "rsn": False, "wpa": False, "privacy": False,
                   "sae": False, "psk": False}
            continue
        if cur is None:
            continue
        if line.startswith("SSID:"):
            cur["ssid"] = _iw_unescape(line[5:].strip())
        elif line.startswith("signal:"):
            try:
                dbm = float(line.split()[1])
                cur["signal"] = max(0, min(100, int(round(2 * (dbm + 100)))))
            except (IndexError, ValueError):
                pass
        elif line.startswith("RSN:"):
            cur["rsn"] = True
        elif line.startswith("WPA:"):
            cur["wpa"] = True
        elif line.startswith("capability:") and "Privacy" in line:
            cur["privacy"] = True
        elif "Authentication suites:" in line:
            suites = line.split(":", 1)[1].split()
            cur["sae"] = cur["sae"] or "SAE" in suites
            cur["psk"] = cur["psk"] or any(x in ("PSK", "PSK/SHA-256") for x in suites)
    flush()
    return sorted(best.values(), key=lambda n: -n["signal"])


def read_hotspot_file(path: str | Path = HOTSPOT_FILE) -> dict:
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


class Wifi:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        hotspot: Callable[[], bool] = hotspot_probe,
        db: str = MOODE_DB,
        helper: str = HELPER,
        hotspot_file: str | Path = HOTSPOT_FILE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run = runner
        self._hotspot = hotspot
        self.db = db
        self.helper = helper
        self.hotspot_file = hotspot_file
        # The last scan that found something: in hotspot mode NetworkManager
        # lists nothing, and the helper's scan does not work on every chip.
        self._last_scan: list[dict] = []
        self._scan_lock = threading.Lock()
        self._clock = clock
        self._next_helper_scan = 0.0
        # The watchdog (haslerd/wifiwatch.py) sets this; its status() joins ours.
        self.watch: Any = None

    def status(self) -> dict:
        hotspot = bool(self._hotspot())
        doc = {"hotspot": hotspot, "current": self.current_ssid(), "networks": self.networks(hotspot),
               "saved": self.saved_ssids(), "setup": self.hotspot_info()}
        if self.watch is not None:
            try:
                doc["retry"] = self.watch.status()
            except Exception:
                log.debug("wifi watch status failed", exc_info=True)
        return doc

    def networks(self, hotspot: bool | None = None) -> list[dict]:
        """What the radio can see. NetworkManager can't scan while the radio is
        a hotspot: then the helper's scan, or the last list that had anything."""
        if hotspot is None:
            hotspot = bool(self._hotspot())
        if hotspot:
            now = self._clock()
            with self._scan_lock:
                due = now >= self._next_helper_scan
                if due:
                    self._next_helper_scan = now + HOTSPOT_SCAN_EVERY_S
            nets = self._helper_scan() if due else []
            if not nets and not self._last_scan:
                nets = self._nm_scan()      # whatever NetworkManager still remembers
        else:
            nets = self._nm_scan()
        with self._scan_lock:
            if nets:
                self._last_scan = nets
            return nets or list(self._last_scan)

    def refresh(self) -> None:
        """Scan now, to have a list ready for when the hotspot starts (the
        watchdog calls this while the radio has no network)."""
        self.networks(False)

    def _nm_scan(self) -> list[dict]:
        try:
            out = self._run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            log.debug("wifi scan failed", exc_info=True)
            return []
        return parse_scan(out or "")

    def _helper_scan(self) -> list[dict]:
        reply = self._helper({"action": "scan"}, timeout=30)
        return parse_iw_scan(str(reply.get("iw") or ""), skip=self.hotspot_ssid())

    def _helper(self, payload: dict, timeout: float = 30) -> dict:
        try:
            result = self._run(["sudo", "-n", self.helper], input=json.dumps(payload),
                               capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"could not run the Wi-Fi helper: {exc}"}
        try:
            reply = json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, ValueError):
            reply = {"ok": False, "error": (result.stderr or "the Wi-Fi helper failed").strip()[-200:]}
        return reply if isinstance(reply, dict) else {"ok": False, "error": "the Wi-Fi helper failed"}

    def ensure_hotspot(self) -> dict:
        """Make sure the setup hotspot can start (once, at haslerd's start).

        moOde's import of Raspberry Pi Imager's Wi-Fi leaves the hotspot
        without a password, and a profile without one never comes up; a
        profile written by moOde's cfgNetworks outside moOde has no address.
        The helper fixes whichever is the case and nothing else; a password
        it used is kept here for the key page (``setup.password``).
        """
        password = "-".join("".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(4)) for _ in range(3))
        reply = self._helper({"action": "hotspot", "ensure": True, "password": password}, timeout=60)
        if reply.get("ok") and reply.get("password_used"):
            ssid = str(reply.get("ssid") or self.hotspot_ssid())
            path = Path(self.hotspot_file)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"# haslerd: the setup hotspot (moOde -> Network -> Access Point)\n"
                            f"ssid={ssid}\npassword={password}\n")
            except OSError:
                log.warning("could not keep the setup hotspot's password in %s", path, exc_info=True)
            log.warning("setup hotspot %r had no password - it has one now (key page, /api/wifi)", ssid)
        elif reply.get("ok") and reply.get("changed"):
            log.warning("setup hotspot's profile repaired (it had no address)")
        elif not reply.get("ok"):
            log.info("setup hotspot check: %s", reply.get("error"))
        return reply

    def retry(self, force: bool = False) -> dict:
        """In setup mode: try the saved networks again (scripts/hasler-wifi-join
        "retry"). The helper restarts the radio if one answers."""
        reply = self._helper({"action": "retry", "force": bool(force)}, timeout=180)
        if not reply.get("result"):
            reply["result"] = "error"
        return reply

    def _query(self, sql: str) -> list[tuple]:
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=2)
            try:
                return con.execute(sql).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            return []

    def saved_ssids(self) -> list[str]:
        """moOde's network and its saved ones (cfg_ssid): what the watchdog tries."""
        out = []
        cur = self.current_ssid()
        if cur:
            out.append(cur)
        for (ssid,) in self._query("SELECT ssid FROM cfg_ssid"):
            if ssid and ssid not in RESERVED and ssid not in out and ssid != self.hotspot_ssid():
                out.append(ssid)
        return out

    def hotspot_ssid(self) -> str:
        rows = self._query("SELECT wlanssid FROM cfg_network WHERE iface='apd0'")
        return (rows[0][0] if rows and rows[0][0] else "") or HOTSPOT_SSID

    def hotspot_info(self) -> dict:
        """The setup hotspot's name, and its password where Hasler set it
        (/etc/hasler/hotspot.txt; a password set on moOde's page is stored only
        as a key, so then it is not known here)."""
        rows = self._query("SELECT wlanssid, wlanpsk FROM cfg_network WHERE iface='apd0'")
        ssid = (rows[0][0] if rows else "") or HOTSPOT_SSID
        info = {"ssid": ssid, "password_set": bool(rows and rows[0][1]), "address": "172.24.1.1",
                "page": "http://172.24.1.1:8080/wifi"}
        saved = read_hotspot_file(self.hotspot_file)
        if saved.get("password") and saved.get("ssid", ssid) == ssid:
            info["password"] = saved["password"]
        return info

    def current_ssid(self) -> str:
        rows = self._query("SELECT wlanssid FROM cfg_network WHERE iface='wlan0'")
        ssid = (rows[0][0] if rows else "") or ""
        return "" if ssid in RESERVED else ssid

    def join(self, ssid: str, password: str, security: str = "auto") -> None:
        """Hand the network to the root helper. Raises ValueError with its reason."""
        ssid = (ssid or "").strip()
        if not 1 <= len(ssid.encode()) <= 32:
            raise ValueError("network name must be 1–32 characters")
        if security not in ("auto", "none", "wpa-psk", "sae"):
            raise ValueError("unknown security type")
        if security in ("wpa-psk", "sae") and not 8 <= len(password) <= 64:
            raise ValueError("a Wi-Fi password is 8–63 characters")
        reply = self._helper({"ssid": ssid, "password": password, "security": security}, timeout=30)
        if not reply.get("ok"):
            raise ValueError(reply.get("error") or "the Wi-Fi helper failed")
        log.warning("joining Wi-Fi %r - restarting", ssid)
