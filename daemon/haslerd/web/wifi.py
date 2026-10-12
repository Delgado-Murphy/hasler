"""Wi-Fi setup for the captive portal: list networks, join one.

When moOde can't find a known network at boot it starts the "Hasler"
hotspot; a phone joining it is redirected here (config/captive/). Joining is
done by a small root helper (scripts/hasler-wifi-join) that writes moOde's own
network tables and reboots - haslerd itself never runs as root. The SSID and
password go to it on stdin, never on a command line.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from typing import Callable

from ..net import hotspot_probe
from .stations import MOODE_DB

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-wifi-join"
HOTSPOT_SSID = "Hasler"


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


class Wifi:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        hotspot: Callable[[], bool] = hotspot_probe,
        db: str = MOODE_DB,
        helper: str = HELPER,
    ) -> None:
        self._run = runner
        self._hotspot = hotspot
        self.db = db
        self.helper = helper

    def status(self) -> dict:
        return {"hotspot": self._hotspot(), "current": self.current_ssid(), "networks": self.networks()}

    def networks(self) -> list[dict]:
        """What the radio can see. In hotspot mode this is the scan cached from
        before the hotspot started - the radio can't scan while it is one."""
        try:
            out = self._run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                capture_output=True, text=True, timeout=10,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            log.debug("wifi scan failed", exc_info=True)
            return []
        return parse_scan(out or "")

    def current_ssid(self) -> str:
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=2)
            try:
                row = con.execute("SELECT wlanssid FROM cfg_network WHERE iface='wlan0'").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return ""
        ssid = (row[0] if row else "") or ""
        return "" if ssid in ("None", "Activate Hotspot") else ssid

    def join(self, ssid: str, password: str, security: str = "auto") -> None:
        """Hand the network to the root helper. Raises ValueError with its reason."""
        ssid = (ssid or "").strip()
        if not 1 <= len(ssid.encode()) <= 32:
            raise ValueError("network name must be 1–32 characters")
        if security not in ("auto", "none", "wpa-psk", "sae"):
            raise ValueError("unknown security type")
        if security in ("wpa-psk", "sae") and not 8 <= len(password) <= 64:
            raise ValueError("a Wi-Fi password is 8–63 characters")
        payload = json.dumps({"ssid": ssid, "password": password, "security": security})
        try:
            result = self._run(
                ["sudo", "-n", self.helper], input=payload, capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"could not run the Wi-Fi helper: {exc}") from exc
        try:
            reply = json.loads((result.stdout or "").strip().splitlines()[-1])
        except (IndexError, ValueError):
            reply = {"ok": False, "error": (result.stderr or "the Wi-Fi helper failed").strip()[-200:]}
        if not reply.get("ok"):
            raise ValueError(reply.get("error") or "the Wi-Fi helper failed")
        log.warning("joining Wi-Fi %r - restarting", ssid)
