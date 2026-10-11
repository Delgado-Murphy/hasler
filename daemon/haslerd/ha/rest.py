"""Fire-and-forget Home Assistant calls.

The box is standalone-first: it must play music with HA switched off, mid-
upgrade, or unreachable. So every call goes out on a background thread with a
short timeout and its result is ignored. A key press must never wait on the
network, and an HA outage must never surface as a dead key.

Used only for the *hold* actions (scenes). The `media_player.hasler` entity
comes free from HA's built-in MPD integration, with no code here at all.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


class HomeAssistant:
    def __init__(self, base_url: str, token: str, timeout: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def __call__(self, kind: str, payload: dict) -> None:
        if kind == "scene":
            self.call_service("scene", "turn_on", {"entity_id": payload["entity"]})
        else:
            log.warning("unknown HA call kind %r", kind)

    def call_service(self, domain: str, service: str, data: dict) -> None:
        threading.Thread(
            target=self._post,
            args=(f"/api/services/{domain}/{service}", data),
            daemon=True,
        ).start()

    def _post(self, path: str, data: dict) -> None:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(data).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except (urllib.error.URLError, OSError) as exc:
            # Expected whenever HA is down. Not worth a traceback.
            log.info("HA call %s failed (%s) - continuing", path, exc)
