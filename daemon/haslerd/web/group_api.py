"""``/api/group`` (the key page) and ``/api/peer`` (radio to radio): multiroom.

The group itself is multiroom/group.py; this is its HTTP face, routed by
``WebApp.extra_routes`` like web/sources_api.py. The owner's settings live in
``settings.json`` -

    "multiroom": {"enabled": false, "follower_keys": "group"}

- read at startup (``apply_saved``), applied live on a PUT. Absent = off.

    GET  /api/group            the group, the other Haslers found (?refresh=1: browse again now)
    PUT  /api/group            {"members": [ids]} (leader), {"enabled": bool},
                               {"follower_keys": "group"|"leave"} - any of them
    POST /api/group/join       {"leader": id} - follow that Hasler (no id: pick one)
    POST /api/group/leave      out of the group (a leader ends it)

    POST /api/peer/follow      {"leader_id", "name", "host", "port", "api_port"}   leader -> follower
    POST /api/peer/release     {"leader_id"}                                       leader -> follower
    POST /api/peer/join        {"id", "name", "host", "port"}                      follower -> leader
    POST /api/peer/left        {"id"}                                              follower -> leader
    POST /api/peer/key         {"from": id, "preset": {n, name, source, uri, shuffle}}  follower -> leader

The peer endpoints are called by other radios with no Origin header and a
Host that is this radio's own address; web/server.py's ``request_refusal``
lets exactly those through, and still refuses a browser on another site
(it can't send ``Content-Type: application/json`` without a preflight). A
radio with multiroom off answers them 409.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..multiroom.group import Group, GroupError, settings_from
from .server import load_settings, save_settings

log = logging.getLogger(__name__)

PREFIXES = ("/api/group", "/api/peer")
PEER_ROUTES = {
    "/api/peer/follow": "peer_follow",
    "/api/peer/release": "peer_release",
    "/api/peer/join": "peer_join",
    "/api/peer/left": "peer_left",
    "/api/peer/key": "peer_key",
}


def apply_saved(group: Group, settings: dict) -> None:
    """settings.json's ``multiroom`` onto the group at startup."""
    group.apply_saved(settings)


class GroupApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any, group: Group) -> None:
        self.app = app
        self.group = group

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        url = urlparse(path)
        p = url.path.rstrip("/") or "/"
        if p not in ("/api/group", "/api/group/join", "/api/group/leave") and p not in PEER_ROUTES:
            return None
        method = method.upper()
        try:
            if p == "/api/group":
                if method == "GET":
                    refresh = parse_qs(url.query).get("refresh", ["0"])[0] not in ("", "0", "false")
                    return 200, self.group.doc(refresh=refresh)
                if method in ("PUT", "POST"):
                    return 200, {"ok": True, **self.put(body)}
            elif method == "POST":
                if p == "/api/group/join":
                    body = body if isinstance(body, dict) else {}
                    return 200, {"ok": True, **self.group.join(body.get("leader"))}
                if p == "/api/group/leave":
                    return 200, {"ok": True, **self.group.leave_doc()}
                return 200, getattr(self.group, PEER_ROUTES[p])(body)
        except GroupError as exc:
            return 409, {"ok": False, "errors": [], "error": str(exc)}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    def put(self, body: Any) -> dict:
        keys = {"members", "enabled", "follower_keys"}
        if not isinstance(body, dict) or not (keys & body.keys()):
            raise ValueError("expected {\"members\": [ids]}, {\"enabled\": true|false} and/or "
                             "{\"follower_keys\": \"group\"|\"leave\"}")
        settings = settings_from({k: body[k] for k in ("enabled", "follower_keys") if k in body})
        if settings:
            if settings.get("enabled") and not self.group.available:
                raise GroupError("Multiroom isn't installed on this radio (Snapcast is missing)")
            with self.app.lock:
                if self.app.settings_file is not None:
                    doc = load_settings(self.app.settings_file)
                    saved = doc.get("multiroom") if isinstance(doc.get("multiroom"), dict) else {}
                    saved.update(settings)
                    doc["multiroom"] = saved
                    save_settings(self.app.settings_file, doc)
            self.group.configure(**settings)
            log.info("multiroom settings %s from the web page", settings)
        if "members" in body:
            self.group.set_members(body["members"])
        return self.group.doc()
