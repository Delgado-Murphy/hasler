"""Home Assistant over its REST API: fire-and-forget for keys, synchronous for setup.

The box is standalone-first: it must play music with HA switched off, mid-
upgrade, or unreachable. So two very different calling styles live here:

* **Key actions** (``__call__``, ``call_service``, ``set_state_async``) go out
  on a background thread with a short timeout and their result is ignored. A
  key press must never wait on the network, and an HA outage must never
  surface as a dead key.
* **The setup screen** (``check``, ``states``, ``areas``, ``run``) is called
  from a web request thread, where the owner is waiting for an answer: these
  block for at most ``SYNC_TIMEOUT`` (3 s) and raise ``HaError`` carrying a
  sentence fit to show on the page ("Home Assistant rejected the token").

All HTTP goes through one injectable ``fetch(method, url, headers, body,
timeout) -> (status, body_bytes)`` so tests never touch the network. The
default returns 4xx/5xx as statuses and raises only for transport failures
(DNS, refused, timeout, TLS), which ``_human`` turns into words.

The token is sent only in the Authorization header and is never logged.

What the radio is playing reaches HA through the Hasler integration (a separate
repo) talking to the radio's own API - ``GET /api/status``, ``/api/info``,
``/api/art/current``, found over mDNS as ``_hasler._tcp``. Pointing HA's MPD
integration at the radio is *not* the way: MPD only knows the radio stream,
not Spotify, YouTube Music, Bluetooth or AirPlay, nor the presets, the keys or
standby, and an HA-side "play"/"stop" behind haslerd's back confuses it.
"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import urllib.error
import urllib.request
from typing import Any, Callable

from . import behaviour

log = logging.getLogger(__name__)

SYNC_TIMEOUT = 3.0

Fetch = Callable[[str, str, dict, "bytes | None", float], "tuple[int, bytes]"]

# The original key-action kinds (what Actions passes) -> HA service. The
# richer kinds ("light", "service") are translated by ha/behaviour.calls.
SERVICES: dict[str, tuple[str, str]] = {
    behaviour.KIND[verb]: svc for verb, svc in behaviour.SIMPLE_SERVICE.items()
}

# One template, one round trip: every area with its name and its entities.
# REST has no area-registry endpoint, but templates can read it. areas() lists
# area ids, area_name() names one, area_entities() lists the entity ids in an
# area (entities assigned directly *and* those of devices in the area). The
# namespace() dance is how Jinja accumulates across a loop; HA's to_json makes it
# machine-readable. /api/template returns the rendered text as-is.
AREAS_TEMPLATE = (
    "{% set ns = namespace(out=[]) %}"
    "{% for a in areas() %}"
    "{% set ns.out = ns.out + [{'area_id': a, 'name': area_name(a), 'entities': area_entities(a)}] %}"
    "{% endfor %}"
    "{{ ns.out | to_json }}"
)


class HaError(Exception):
    """A setup-screen failure. ``str(exc)`` is a sentence for the owner."""


def urllib_fetch(method: str, url: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    """The real transport. HTTP error statuses are returned, not raised."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read()
        except Exception:
            data = b""
        return exc.code, data


def _human(exc: BaseException) -> str:
    """A transport exception -> what to tell the owner."""
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (ssl.SSLCertVerificationError,)) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
        return ("Home Assistant's HTTPS certificate isn't trusted by the radio (self-signed?). "
                "Use its http:// LAN address instead, e.g. http://192.168.1.10:8123")
    if isinstance(reason, ssl.SSLError) or "SSL" in type(reason).__name__ or "WRONG_VERSION_NUMBER" in str(reason):
        return "Secure connection failed - if Home Assistant doesn't use HTTPS, start the address with http://"
    if isinstance(reason, socket.gaierror):
        return "Can't find that host name on the network - try its IP address, e.g. http://192.168.1.10:8123"
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(reason):
        return "Can't reach that address (no answer within 3 seconds)"
    if isinstance(reason, ConnectionRefusedError):
        return "Can't reach that address - nothing is listening on that port (Home Assistant uses 8123)"
    return "Can't reach that address"


def _ha_shaped(path: str, status: int, raw: bytes) -> bool:
    """Was that Home Assistant answering (for ha/follow), or something else at its address?

    401/403 are HA refusing the token: an answer, never a reason to go looking.
    Otherwise HA answers JSON (errors too: ``{"message": "Entity not found."}``),
    except a rendered template, which is text. An HTML page or a bare 404 from
    whatever now holds the IP is not HA.
    """
    if status in (401, 403):
        return True
    if path == "/api/template" and 200 <= status < 300:
        return True
    try:
        json.loads(raw or b"")
    except (ValueError, TypeError):
        return False
    return True


class HomeAssistant:
    """One HA instance. Callable as Actions' ``ha`` transport."""

    def __init__(self, base_url: str, token: str, timeout: float = 2.0, fetch: Fetch | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.fetch: Fetch = fetch or urllib_fetch
        # Entities this HA was last seen *not* to have (set by web/ha_api from
        # GET /api/states). A key bound to one does nothing and says so once:
        # typically the radio moved house and the key still names the old
        # home's scene. Replaced wholesale, never mutated, so no lock.
        self.missing: frozenset[str] = frozenset()
        # What each light can do ({entity_id: behaviour.light_caps(...)}), also
        # learnt by web/ha_api, so a key sends each bulb only what it supports.
        self.lights: dict[str, dict | None] = {}
        self._warned: set[str] = set()
        # Told after every request whether HA answered (see _ha_shaped) or
        # not (unreachable, DNS, refused, timeout, someone else) - ha/follow notices a move.
        self.on_reach: Callable[[bool], None] | None = None

    def __repr__(self) -> str:
        return f"HomeAssistant({self.base_url!r})"

    # -- fire-and-forget (keys) --------------------------------------------

    def __call__(self, kind: str, payload: dict) -> None:
        verb = behaviour.VERB_OF_KIND.get(kind)
        if verb is None:
            log.warning("unknown HA call kind %r", kind)
            return
        spec = {**payload, "do": verb}
        entities = behaviour.entity_ids(spec)
        gone = [e for e in entities if e in self.missing]
        for entity in gone:
            if entity not in self._warned:
                self._warned.add(entity)
                log.warning("%s doesn't exist in this Home Assistant; the key does nothing "
                            "(fix or clear it on the key page)", entity)
        for domain, service, data in behaviour.calls(spec, self.lights, frozenset(gone)):
            self.call_service(domain, service, data)

    def call_service(self, domain: str, service: str, data: dict) -> None:
        self._background("POST", f"/api/services/{domain}/{service}", data)

    def set_state_async(self, entity_id: str, state: str, attributes: dict) -> None:
        self._background("POST", f"/api/states/{entity_id}", {"state": state, "attributes": attributes})

    def delete_state_async(self, entity_id: str) -> None:
        self._background("DELETE", f"/api/states/{entity_id}", None)

    def _background(self, method: str, path: str, data: dict | None) -> None:
        threading.Thread(target=self._post, args=(path, data, method), daemon=True).start()

    def _post(self, path: str, data: dict | None, method: str = "POST") -> None:
        try:
            status, _ = self._request(method, path, data, self.timeout)
        except Exception as exc:
            # Expected whenever HA is down. Not worth a traceback.
            log.info("HA call %s failed (%s) - continuing", path, _human(exc))
            return
        if status >= 400:
            log.info("HA call %s answered %s - continuing", path, status)

    # -- synchronous (setup screen) ----------------------------------------

    def _request(self, method: str, path: str, data: dict | None, timeout: float) -> tuple[int, bytes]:
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        body = None if data is None else json.dumps(data).encode()
        try:
            answer = self.fetch(method, self.base_url + path, headers, body, timeout)
        except Exception:
            self._reached(False)
            raise
        self._reached(_ha_shaped(path, *answer))
        return answer

    def _reached(self, ok: bool) -> None:
        hook = self.on_reach
        if hook is not None:
            try:
                hook(ok)
            except Exception:
                log.debug("on_reach hook failed", exc_info=True)

    def _sync(self, method: str, path: str, data: dict | None = None) -> Any:
        """One call, JSON answer or HaError with a sentence."""
        try:
            status, raw = self._request(method, path, data, SYNC_TIMEOUT)
        except Exception as exc:
            raise HaError(_human(exc)) from exc
        if status in (401, 403):
            raise HaError("Home Assistant rejected the token - create a new long-lived access token and paste it again")
        if status == 404:
            raise HaError("That address answers, but it isn't Home Assistant (no /api there)")
        if status >= 400:
            raise HaError(f"Home Assistant answered with an error ({status})")
        try:
            return json.loads(raw or b"null")
        except ValueError:
            if path == "/api/template":
                return raw.decode("utf-8", "replace")
            raise HaError("That address answers, but it isn't Home Assistant") from None

    def check(self) -> dict:
        """Is this HA, and does it take our token? -> {location_name, version, time_zone[, latitude, longitude]}."""
        doc = self._sync("GET", "/api/")
        if not (isinstance(doc, dict) and doc.get("message") == "API running."):
            raise HaError("That address answers, but it isn't Home Assistant")
        conf = self._sync("GET", "/api/config")
        if not isinstance(conf, dict):
            raise HaError("That address answers, but it isn't Home Assistant")
        info: dict[str, Any] = {k: str(conf.get(k) or "") for k in ("location_name", "version", "time_zone")}
        # Kept only to fingerprint the instance (ha/config.fingerprint hashes them).
        for k in ("latitude", "longitude"):
            if isinstance(conf.get(k), (int, float)):
                info[k] = conf[k]
        return info

    def states(self) -> list[dict]:
        doc = self._sync("GET", "/api/states")
        if not isinstance(doc, list):
            raise HaError("Home Assistant sent an unexpected answer for its entity list")
        return [s for s in doc if isinstance(s, dict) and isinstance(s.get("entity_id"), str)]

    def areas(self) -> list[dict]:
        """[{area_id, name, entities: [...]}], or [] if the template can't run.

        Areas are a nicety for the picker, never a reason to fail it: an old HA,
        a token without template rights, or any odd answer degrades to no areas.
        """
        try:
            rendered = self._sync("POST", "/api/template", {"template": AREAS_TEMPLATE})
        except HaError as exc:
            log.info("HA areas unavailable (%s); entities will be ungrouped", exc)
            return []
        return parse_areas(rendered)

    def run(self, kind: str, entity: str) -> None:
        """A key action, now, and say if it failed (the page's "Try it")."""
        domain, service = SERVICES[kind]
        self._sync("POST", f"/api/services/{domain}/{service}", {"entity_id": entity})

    def run_calls(self, calls: list[tuple[str, str, dict]]) -> list[dict]:
        """Service calls, now, in order -> the states HA says changed. Raises HaError."""
        changed: list[dict] = []
        for domain, service, data in calls:
            doc = self._sync("POST", f"/api/services/{domain}/{service}", data)
            if isinstance(doc, list):
                changed.extend(d for d in doc if isinstance(d, dict))
        return changed


def parse_areas(rendered: Any) -> list[dict]:
    """The AREAS_TEMPLATE output (text, or already-parsed JSON) -> clean list."""
    doc = rendered
    if isinstance(rendered, str):
        try:
            doc = json.loads(rendered)
        except ValueError:
            log.info("HA areas template returned something that isn't JSON; ignoring")
            return []
    if not isinstance(doc, list):
        return []
    out = []
    for item in doc:
        if not isinstance(item, dict) or not isinstance(item.get("area_id"), str):
            continue
        entities = item.get("entities")
        out.append({
            "area_id": item["area_id"],
            "name": str(item.get("name") or item["area_id"]),
            "entities": [e for e in entities if isinstance(e, str)] if isinstance(entities, list) else [],
        })
    return out
