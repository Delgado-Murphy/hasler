"""Other addresses the radio takes changes from, allowed by a press on the radio.

The key page only accepts changes through the radio's own names (hasler.local,
its IP, the setup hotspot): that is what keeps another web site, or a page
reached through someone else's name for this IP, from changing keys and
settings (server.py ``request_refusal``). An owner who reaches the radio
through a name of their own - a home dashboard, Home Assistant's remote
access, Tailscale, a domain - gets this refusal with the address in it, and
the page offers "Allow this address":

    POST /api/confirm/request {"action": "hostname"}   from that address
    POST /api/hosts {"confirm": token}                 from that address

Both are let through the address check only when the request is same-origin
(Origin, or Referer without it, is the very Host it came to), and the address
saved is always the Host header the request came through - never one named in
the body - so a page can only ever allow the address it is open at. The key
press is what makes it safe: a page reached from outside can ask, but only a
hand on the cabinet says yes.

Saved in the state directory (allowed-hosts.json), so updates keep them;
box.yaml ``web.hostnames`` still works, and is listed but not removable here.
Removing one is an ordinary change (from an allowed address or the radio's
own), and needs no press.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

FILE = "allowed-hosts.json"
MAX_HOSTS = 10
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def valid_host(host: str) -> bool:
    """A DNS name or an IP address, nothing else (no port, no path)."""
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return all(_LABEL.match(label) for label in host.split("."))


class AllowedHosts:
    def __init__(self, state_dir: Path | None) -> None:
        self.path = state_dir / FILE if state_dir is not None else None
        self._lock = threading.Lock()
        self._hosts: dict[str, float] = self._load()

    def _load(self) -> dict[str, float]:
        if self.path is None:
            return {}
        try:
            doc = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("allowed hosts: %s unreadable, starting empty", self.path)
            return {}
        out = {}
        for item in doc.get("hosts") or []:
            host = str((item or {}).get("host") or "").lower()
            if valid_host(host):
                out[host] = float(item.get("added") or 0)
        return out

    def _save(self) -> None:
        if self.path is None:
            return
        doc = {"schema": 1, "hosts": [{"host": h, "added": t} for h, t in sorted(self._hosts.items())]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n")
        tmp.replace(self.path)

    def names(self) -> set[str]:
        with self._lock:
            return set(self._hosts)

    def listing(self) -> list[dict]:
        with self._lock:
            return [{"host": h, "added": t} for h, t in sorted(self._hosts.items())]

    def add(self, host: str) -> None:
        host = host.lower()
        if not valid_host(host):
            raise ValueError(f"not an address the radio can allow: {host or '(empty)'}")
        with self._lock:
            if host not in self._hosts and len(self._hosts) >= MAX_HOSTS:
                raise ValueError(f"the radio already allows {MAX_HOSTS} other addresses; remove one first")
            self._hosts.setdefault(host, time.time())
            self._save()
        log.info("web: changes now accepted through %s (confirmed on the radio)", host)

    def remove(self, host: str) -> bool:
        host = host.lower()
        with self._lock:
            if host not in self._hosts:
                return False
            del self._hosts[host]
            self._save()
        log.info("web: changes through %s no longer accepted", host)
        return True

    def clear(self) -> list[str]:
        """Every allowed address gone (the hand-over: a new owner, a new home's
        names). box.yaml's web.hostnames are the makers', not touched here."""
        with self._lock:
            gone = sorted(self._hosts)
            self._hosts = {}
            if self.path is not None:
                self.path.unlink(missing_ok=True)
        if gone:
            log.info("web: changes through %s no longer accepted", ", ".join(gone))
        return gone
