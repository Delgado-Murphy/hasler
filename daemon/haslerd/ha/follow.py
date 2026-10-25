"""Follow the radio's own Home Assistant when its address changes.

The saved address in ha.json is whatever discovery (or the owner) picked on
the day. A new router, a DHCP lease running out or HA being reinstalled can
move HA to another IP, and then every scene, light and the published sensor
quietly stop. This notices that and finds HA again - but only *the same* HA.

When does it look?

* Every request the installed client makes reports whether *HA* answered or
  not (unreachable, DNS, refused, timeout, or an answer that isn't HA's - an
  HTML page or bare 404 from whatever now holds the old IP). A rejected token
  (401/403) or an HA error is an answer, never a move.
* After the first miss, ``HaApi.watch`` probes the saved address every
  ``PROBE_S`` without the token (``GET /api/``: 401, or 200 with HA's "API
  running." = HA is there; ha/discover.probe_ha), so a quiet radio still notices. At boot, the background check counts the same
  way, so a radio that starts somewhere HA has moved looks within minutes.
* ``MISSES`` misses spanning at least ``BROKEN_S`` = broken. Then discovery
  runs at most every ``RETRY_S``, doubling to ``RETRY_MAX_S`` while nothing
  is adopted. Any answer from the saved address resets all of it.

Where can the token go? It is a long-lived secret, so **only** to an address
that announced, over mDNS, the uuid saved for this instance - never to a uuid
mismatch, a record without a uuid, or the homeassistant.local guess. Even
there it is sent once, to check (``check``: ``/api/`` and ``/api/config``),
and the answer must still fingerprint as the same instance. A saved
connection with no uuid is never moved automatically: the page says where an
HA was seen and the owner decides.

The address adopted is the one HA announces (its ``internal_url``, else the
resolved IP), the same rule as the setup page. Not rewritten to a .local name:
that would trade a rare IP change (now handled here) for an mDNS lookup on
every key press, which some routers and Wi-Fi extenders quietly break.
"""

from __future__ import annotations

import threading
from typing import Callable

MISSES = 3              # connection-level misses in a row...
BROKEN_S = 120.0        # ...spanning at least this long = the address is gone
PROBE_S = 60.0          # while suspicious, ask the saved address this often (no token)
RETRY_S = 600.0         # discovery at most this often while broken...
RETRY_MAX_S = 3600.0    # ...backing off to this


class Tracker:
    """Misses, the probe schedule and the discovery backoff. Thread-safe."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.misses = 0
            self.first_miss: float | None = None
            self.next_probe = float("-inf")
            self.next_look = float("-inf")
            self.retry = RETRY_S

    def reached(self, ok: bool) -> bool:
        """One request's outcome. True if this answer ended a run of misses."""
        now = self.clock()
        with self._lock:
            if ok:
                was = self.misses > 0
                self.misses, self.first_miss = 0, None
                self.next_look, self.retry = float("-inf"), RETRY_S
                return was
            if self.first_miss is None:
                self.first_miss = now
                self.next_probe = now + PROBE_S
            self.misses += 1
            return False

    def suspicious(self) -> bool:
        return self.misses > 0

    def probe_due(self) -> bool:
        now = self.clock()
        with self._lock:
            if self.misses == 0 or now < self.next_probe:
                return False
            self.next_probe = now + PROBE_S
            return True

    def broken(self) -> bool:
        now = self.clock()
        with self._lock:
            return self.misses >= MISSES and self.first_miss is not None and now - self.first_miss >= BROKEN_S

    def look_due(self) -> bool:
        """Broken and not looked lately; claims the slot (backoff doubles)."""
        if not self.broken():
            return False
        now = self.clock()
        with self._lock:
            if now < self.next_look:
                return False
            self.next_look = now + self.retry
            self.retry = min(self.retry * 2, RETRY_MAX_S)
            return True


def candidate(instances: list[dict], saved_url: str, saved_uuid: str) -> tuple[dict | None, str]:
    """Discovery's answer -> (the same instance at a new address, or None; why).

    Reasons: "moved" (candidate returned), "same-address" (it announces the
    saved address: down or busy, not moved), "no-uuid" (nothing to match on),
    "other" (only different instances), "none" (nothing found).
    """
    if not instances:
        return None, "none"
    if not saved_uuid:
        return None, "no-uuid"
    for item in instances:
        if item.get("uuid") == saved_uuid:
            if item.get("url", "").rstrip("/") == saved_url.rstrip("/"):
                return None, "same-address"
            return item, "moved"
    return None, "other"
