"""Home Assistant on the key page: connect, pick entities, try, publish, tidy up.

The web server knows nothing about Home Assistant; it hands every request
under ``/api/ha`` to ``HaApi.route(method, path, body)`` and sends back the
``(status, doc)`` it returns (``None`` = not mine). Routes:

    GET    /api/ha               -> status (below)
    POST   /api/ha               {"url", "token", "publish"?} -> status | 400 {"ok": false, "error"}
    PATCH  /api/ha               {"publish": bool} -> status
    DELETE /api/ha               -> status (disconnected)
    GET    /api/ha/discover      -> {"instances": [{"name", "url"}], "note": str|null}
    GET    /api/ha/entities[?refresh=1][&domains=scene,script] -> {"areas": [...], "entities": [...]} | 409 | 502
    GET    /api/ha/scenes[?refresh=1] -> the same, scenes only
    GET    /api/ha/lights[?refresh=1] -> {"areas": [...], "lights": [...]} | 409 | 502   (states <= 3 s old)
    GET    /api/ha/services      -> {"services": [{"service", "data", "required"}]}  (what ha_service may call)
    POST   /api/ha/try           {"action": <behaviour>} (or the behaviour itself, as older pages send)
                                 -> {"ok": true, "calls": [...], "changed": [...], "warnings": [...]}
    POST   /api/ha/clear-missing -> status, plus "cleared": n

A behaviour is ha/behaviour.py's dict (``ha_light``, ``ha_scene``, ``ha_script``,
``ha_automation``, ``ha_toggle``, ``ha_service``) - the same thing a key's tap
or hold stores in keys.yaml.

The status doc::

    {"configured", "connected", "url", "location", "version", "publish",
     "error": str|null,
     "missing": [{"key", "event", "do", "entity", "label", "reason"}],
     "previous_instance": str|null}

**The token never leaves.** No response is built from anything that holds it.

**Hot swap.** Connecting builds a fresh client, checks it, saves it, then sets
``actions.ha`` to it - one reference assignment, so the panel loop sees the old
transport or the new one, never neither. Disconnecting sets ``actions.ha =
None`` (keys bound to HA then log once and do nothing). No restart, ever.

**The radio moves house.** Nothing here assumes a particular HA. Key actions
name entity ids; when the connected HA has no such entity the action is listed
under ``missing`` (reason "not found"), and when no HA is connected at all
every HA-bound action is listed (reason "not connected"). Such a key logs once
and does nothing (the client's ``missing`` set). ``previous_instance`` names
the HA the key layout was made for when it isn't the one connected now (see
ha/config.fingerprint). ``clear-missing`` removes the listed actions through
keymap's normal save/apply path, so keys.yaml is updated too.

**Startup** (the server agent's build_web)::

    ha_api = HaApi(actions, state_dir, config.get("home_assistant"),
                   keys_file=keys_file, panel=panel, lock=app.lock)
    ha_api.load()           # reads ha.json / yaml, sets actions.ha, verifies in the background

``load()`` never blocks on the network and never raises.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..ha import behaviour
from ..ha import config as ha_config
from ..ha import discover as ha_discover
from ..ha.rest import HaError, HomeAssistant
from . import keymap

log = logging.getLogger(__name__)

# Domains worth offering in a key's entity picker, in the order the page lists them.
DOMAINS = ("scene", "script", "automation", "light", "switch", "input_boolean", "fan",
           "cover", "media_player", "button", "input_button", "climate")
CACHE_S = 60.0
LIGHTS_CACHE_S = 3.0      # the lights list shows live on/off and brightness
PUBLISH_MIN_S = 5.0       # at most one sensor push per 5 s
REPUSH_S = 600.0          # re-push an unchanged state this often: HA forgets it on restart
SENSOR = "sensor.hasler_radio"
RENDERERS = ("bluetooth", "airplay", "dlna", "spotify")


class HaApi:
    """The page's Home Assistant endpoints, plus the client hot swap and the sensor."""

    def __init__(
        self,
        actions: Any,
        state_dir: Path | None,
        yaml_conf: dict | None,
        client_factory: Callable[[str, str], Any] = HomeAssistant,
        discover: Callable[[], tuple[list[dict], str | None]] = ha_discover.discover,
        *,
        keys_file: Path | None = None,
        panel: Any = None,
        lock: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.actions = actions
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.yaml_conf = yaml_conf or {}
        self.client_factory = client_factory
        self.discover = discover
        # keys.yaml, for clear-missing. build_web's default is the same path.
        if keys_file is None and self.state_dir is not None:
            keys_file = self.state_dir / "keys.yaml"
        self.keys_file = keys_file
        self.panel = panel
        self.layout_lock = lock if lock is not None else threading.Lock()
        self.clock = clock
        self._lock = threading.RLock()
        self.settings: ha_config.HaSettings | None = None
        self.client: Any = None
        self.info: dict = {}
        self.error: str | None = None
        self._states: tuple[float, list[dict], list[dict]] | None = None   # (when, states, areas)
        self._areas_t = float("-inf")
        self._uuids: dict[str, str] = {}          # url -> zeroconf uuid, from discovery
        self._pub_last: dict | None = None
        self._pub_t = float("-inf")
        self._verifier: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def connected(self) -> bool:
        """A client is installed as ``actions.ha`` (HA itself may still be down)."""
        return self.client is not None

    def load(self, verify: bool = True) -> None:
        """Startup: install the saved connection, check it off-thread. Never raises."""
        try:
            settings = ha_config.load(self.state_dir, self.yaml_conf)
        except Exception:
            log.exception("could not load the Home Assistant connection")
            settings = None
        if settings is None:
            self.actions.ha = None
            log.info("Home Assistant not connected; HA key actions are inert")
            return
        self._install(settings, self.client_factory(settings.url, settings.token))
        log.info("Home Assistant: %s (from %s)", settings.url, "ha.json" if settings.source == "file" else "haslerd.yaml")
        if verify:
            self._verifier = threading.Thread(target=self._verify, name="ha-verify", daemon=True)
            self._verifier.start()

    def _install(self, settings: ha_config.HaSettings, client: Any) -> None:
        with self._lock:
            self.settings = settings
            self.client = client
            self._states = None
            self._pub_last, self._pub_t = None, float("-inf")
        self.actions.ha = client                  # the hot swap: one reference assignment

    def _uninstall(self) -> None:
        with self._lock:
            self.settings = None
            self.client = None
            self.info, self.error, self._states = {}, None, None
            self._pub_last, self._pub_t = None, float("-inf")
        self.actions.ha = None

    def _verify(self) -> None:
        """Background check after load: fill location/version, learn missing entities."""
        client, settings = self.client, self.settings
        if client is None or settings is None:
            return
        try:
            info = client.check()
        except HaError as exc:
            self.error = str(exc)
            log.info("Home Assistant check failed: %s (key actions stay fire-and-forget)", exc)
            return
        except Exception:
            log.exception("Home Assistant check failed")
            return
        self.info, self.error = info, None
        if not settings.instance:
            settings.instance = ha_config.fingerprint(info, self._uuids.get(settings.url))
            if settings.source == "file":
                try:
                    ha_config.save(self.state_dir, settings)
                except OSError:
                    log.exception("could not update %s", ha_config.FILENAME)
        try:
            self._fetch_states(refresh=True)
        except HaError as exc:
            log.info("Home Assistant entity list unavailable: %s", exc)

    # -- entities and missing actions --------------------------------------

    def _fetch_states(self, refresh: bool = False, max_age: float = CACHE_S) -> tuple[list[dict], list[dict]]:
        """(states, areas), states cached ``max_age``, areas CACHE_S. Raises HaError.

        Updates client.missing and client.lights."""
        client = self.client
        if client is None:
            raise HaError("not connected")
        cached = self._states
        now = self.clock()
        if not refresh and cached is not None and now - cached[0] < max_age:
            return cached[1], cached[2]
        states = client.states()
        if not refresh and cached is not None and now - self._areas_t < CACHE_S:
            areas = cached[2]               # areas change rarely: no template call for a fresh light list
            areas_t = self._areas_t
        else:
            try:
                areas = client.areas()
            except Exception:
                areas = []
            areas_t = now
        with self._lock:
            if self.client is client:
                self._states = (now, states, areas)
                self._areas_t = areas_t
        self._learn(client, states)
        return states, areas

    def _learn(self, client: Any, states: list[dict]) -> None:
        """Tell the client which bound entities don't exist and what each light can do;
        record a consistent layout."""
        known = {s["entity_id"] for s in states}
        bound = keymap.ha_entries(self.actions)
        missing = frozenset(e for _, _, spec in bound for e in behaviour.entity_ids(spec) if e not in known)
        lights = {s["entity_id"]: behaviour.light_caps(s) for s in states if s["entity_id"].startswith("light.")}
        try:
            client.missing = missing
            client.lights = lights
        except AttributeError:
            pass
        settings = self.settings
        if not missing and settings is not None and settings.instance:
            # Every HA action resolves here: the layout belongs to this HA now.
            if ha_config.load_layout_instance(self.state_dir) != settings.instance:
                ha_config.save_layout_instance(self.state_dir, settings.instance)

    def missing(self) -> list[dict]:
        """Key actions that can't work: entity unknown here, or no HA at all."""
        bound = keymap.ha_entries(self.actions)
        if not bound:
            return []
        if not self.connected:
            reason, known = "not connected", None
        else:
            try:
                states, _ = self._fetch_states()
            except HaError:
                return []           # can't tell; the status' "error" says why
            reason, known = "not found", {s["entity_id"] for s in states}
        out = []
        for cid, event, spec in bound:
            for entity in behaviour.entity_ids(spec):
                if known is not None and entity in known:
                    continue
                out.append({
                    "key": cid,
                    "event": event,
                    "do": spec["do"],
                    "entity": entity,
                    "label": f"Key {keymap.label_for(cid)} {event}: {entity}",
                    "reason": reason,
                })
        return out

    def previous_instance(self) -> str | None:
        """Name of the HA the key layout was made for, if it isn't the connected one."""
        settings = self.settings
        if settings is None or not settings.instance or not keymap.ha_entries(self.actions):
            return None
        layout = ha_config.load_layout_instance(self.state_dir)
        if ha_config.same_instance(layout, settings.instance):
            return None
        return (layout or {}).get("name") or "another Home Assistant"

    @staticmethod
    def _area_of(areas: list[dict]) -> dict[str, str]:
        area_of: dict[str, str] = {}
        for area in areas:
            for entity_id in area["entities"]:
                area_of.setdefault(entity_id, area["area_id"])
        return area_of

    @staticmethod
    def _areas_doc(areas: list[dict]) -> list[dict]:
        return sorted(({"id": a["area_id"], "name": a["name"]} for a in areas), key=lambda a: a["name"].lower())

    def entities(self, refresh: bool = False, domains: tuple[str, ...] | None = None) -> dict:
        states, areas = self._fetch_states(refresh)
        area_of = self._area_of(areas)
        rank = {d: i for i, d in enumerate(DOMAINS)}
        entities = []
        for s in states:
            entity_id = s["entity_id"]
            domain = entity_id.split(".", 1)[0]
            if domain not in rank or (domains and domain not in domains):
                continue
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            entities.append({
                "entity_id": entity_id,
                "name": str(attrs.get("friendly_name") or entity_id),
                "domain": domain,
                "area": area_of.get(entity_id),
                "state": str(s.get("state", "")),
            })
        entities.sort(key=lambda e: (rank[e["domain"]], e["name"].lower()))
        return {"areas": self._areas_doc(areas), "entities": entities}

    def lights(self, refresh: bool = False) -> dict:
        """Every light with what it is doing and what it can do (states <= LIGHTS_CACHE_S old)."""
        states, areas = self._fetch_states(refresh, max_age=LIGHTS_CACHE_S)
        area_of = self._area_of(areas)
        names = {a["area_id"]: a["name"] for a in areas}
        lights = []
        for s in states:
            entity_id = s["entity_id"]
            if not entity_id.startswith("light."):
                continue
            attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
            state = str(s.get("state", ""))
            raw_b = attrs.get("brightness")
            brightness = (round(raw_b * 100 / 255) if isinstance(raw_b, (int, float)) and not isinstance(raw_b, bool)
                          and state == "on" else None)
            rgb = attrs.get("rgb_color")
            rgb = [int(x) for x in rgb] if isinstance(rgb, (list, tuple)) and len(rgb) == 3 and state == "on" else None
            kelvin = attrs.get("color_temp_kelvin")
            kelvin = int(kelvin) if isinstance(kelvin, (int, float)) and not isinstance(kelvin, bool) and state == "on" \
                else None
            members = attrs.get("entity_id")
            area = area_of.get(entity_id)
            lights.append({
                "entity_id": entity_id,
                "name": str(attrs.get("friendly_name") or entity_id),
                "area": area,
                "area_name": names.get(area) if area else None,
                "state": state,
                "brightness": brightness,
                "rgb": rgb,
                "color": "#%02x%02x%02x" % tuple(rgb) if rgb else None,
                "kelvin": kelvin,
                "supported": behaviour.light_caps(s),
                "members": [m for m in members if isinstance(m, str)] if isinstance(members, list) else None,
            })
        lights.sort(key=lambda x: ((x["area_name"] or "~").lower(), x["name"].lower()))
        return {"areas": self._areas_doc(areas), "lights": lights}

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        settings = self.settings
        missing = self.missing()           # may refresh states, so before reading info
        return {
            "configured": settings is not None,
            "connected": self.connected,
            "url": settings.url if settings else "",
            "location": str(self.info.get("location_name") or (settings.instance.get("name", "") if settings else "")),
            "version": str(self.info.get("version") or ""),
            "publish": bool(settings.publish) if settings else False,
            "error": self.error,
            "missing": missing,
            "previous_instance": self.previous_instance(),
        }

    # -- operations -------------------------------------------------------

    def connect(self, body: Any) -> tuple[int, dict]:
        if not isinstance(body, dict):
            return 400, {"ok": False, "error": "expected {\"url\", \"token\"}"}
        try:
            url = ha_config.normalise_url(body.get("url"))
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        token = str(body.get("token") or "").strip()
        current = self.settings
        if not token:
            if current is not None and current.url == url:
                token = current.token          # same HA, keep the saved token
            else:
                return 400, {"ok": False, "error": "Paste a long-lived access token from Home Assistant "
                                                   "(your profile → Security → Long-lived access tokens)"}
        publish = body.get("publish")
        if not isinstance(publish, bool):
            publish = bool(current.publish) if current else False
        client = self.client_factory(url, token)
        try:
            info = client.check()             # network: up to ~6 s, outside any lock
        except HaError as exc:
            return 400, {"ok": False, "error": str(exc)}
        settings = ha_config.HaSettings(url=url, token=token, publish=publish, source="file",
                                        instance=ha_config.fingerprint(info, self._uuids.get(url)))
        save_error = None
        try:
            ha_config.save(self.state_dir, settings)
        except OSError as exc:
            log.error("could not save the Home Assistant connection: %s", exc)
            save_error = "Connected, but the radio couldn't save it - it will be forgotten at restart"
        if current is not None and current.publish and self.client is not None and current.url != url:
            self._drop_sensor(self.client)
        self._install(settings, client)
        self.info, self.error = info, save_error
        log.info("Home Assistant connected from the key page: %s (%s)", url, info.get("location_name", ""))
        try:
            self._fetch_states(refresh=True)
        except HaError as exc:
            log.info("Home Assistant entity list unavailable: %s", exc)
        return 200, self.status()

    def set_publish(self, body: Any) -> tuple[int, dict]:
        publish = body.get("publish") if isinstance(body, dict) else None
        if not isinstance(publish, bool):
            return 400, {"ok": False, "error": "expected {\"publish\": true|false}"}
        settings = self.settings
        if settings is None:
            return 409, {"ok": False, "error": "not connected"}
        was = settings.publish
        settings.publish = publish
        try:
            if ha_config.save(self.state_dir, settings):
                settings.source = "file"
        except OSError as exc:
            log.error("could not save the Home Assistant setting: %s", exc)
            self.error = "The radio couldn't save that setting"
        with self._lock:
            self._pub_last, self._pub_t = None, float("-inf")
        if was and not publish and self.client is not None:
            self._drop_sensor(self.client)
        return 200, self.status()

    def disconnect(self) -> tuple[int, dict]:
        settings, client = self.settings, self.client
        if settings is not None and settings.publish and client is not None:
            self._drop_sensor(client)
        try:
            ha_config.forget(self.state_dir, self.yaml_conf)
        except OSError:
            log.exception("could not remove the saved Home Assistant connection")
        self._uninstall()
        log.info("Home Assistant disconnected from the key page")
        return 200, self.status()

    def try_action(self, body: Any) -> tuple[int, dict]:
        """Run one behaviour now and report what HA did (the page's "Try it")."""
        raw = body.get("action") if isinstance(body, dict) and isinstance(body.get("action"), dict) else body
        try:
            spec = behaviour.normalise(raw)
        except behaviour.Problem as exc:
            return 400, {"ok": False, "error": str(exc)}
        client = self.client
        if client is None:
            return 409, {"ok": False, "error": "not connected"}
        caps: dict[str, dict | None] = {}
        names: dict[str, str] = {}
        if spec["do"] == "ha_light":
            try:
                states, _ = self._fetch_states(max_age=LIGHTS_CACHE_S)
            except HaError:
                states = []             # can't tell what the lights do: send everything, HA decides
            for s in states:
                if s["entity_id"] in behaviour.entity_ids(spec):
                    caps[s["entity_id"]] = behaviour.light_caps(s)
                    attrs = s.get("attributes") if isinstance(s.get("attributes"), dict) else {}
                    names[s["entity_id"]] = str(attrs.get("friendly_name") or s["entity_id"])
        calls = behaviour.calls(spec, caps)
        try:
            changed = client.run_calls(calls)
        except HaError as exc:
            return 502, {"ok": False, "error": str(exc)}
        with self._lock:
            cached = self._states
            if self.client is client and cached is not None:
                self._states = (float("-inf"), cached[1], cached[2])   # next list shows the new states
        return 200, {
            "ok": True,
            "action": spec,
            "calls": [{"service": f"{d}.{sv}", "data": data} for d, sv, data in calls],
            "changed": [{"entity_id": c.get("entity_id"), "state": c.get("state")}
                        for c in changed if isinstance(c.get("entity_id"), str)],
            "warnings": behaviour.warnings(spec, caps, names),
        }

    def clear_missing(self) -> tuple[int, dict]:
        gone = self.missing()
        names = sorted({f"{m['key']}.{m['event']}" for m in gone})
        if names:
            with self.layout_lock:
                keymap.remove_entries(self.actions, self.panel, self.keys_file, names)
            log.info("cleared %d Home Assistant key action(s) with nothing to act on: %s", len(names), ", ".join(names))
        client, settings = self.client, self.settings
        if client is not None:
            try:
                client.missing = frozenset()
            except AttributeError:
                pass
            if settings is not None and settings.instance:
                ha_config.save_layout_instance(self.state_dir, settings.instance)
        return 200, {**self.status(), "cleared": len(names)}

    def find(self) -> tuple[int, dict]:
        try:
            instances, note = self.discover()
        except Exception:
            log.exception("Home Assistant discovery failed")
            instances, note = [], "Network discovery failed on the radio"
        for item in instances:
            if item.get("uuid"):
                self._uuids[item["url"]] = item["uuid"]
        return 200, {"instances": [{"name": i["name"], "url": i["url"]} for i in instances], "note": note}

    # -- publishing ---------------------------------------------------------

    @staticmethod
    def sensor(status: dict) -> dict:
        """WebApp.status() -> {"state", "attributes"} for sensor.hasler_radio."""
        playing = status.get("playing") or {}
        track = status.get("track") or {}
        source = str(status.get("source") or "")
        if status.get("standby"):
            state = "standby"
        elif playing or (source in RENDERERS and not status.get("waiting")):
            state = "playing"
        else:
            state = "idle"
        on = state == "playing"
        return {
            "state": state,
            "attributes": {
                "friendly_name": "Hasler radio",
                "icon": "mdi:radio",
                "station": str(playing.get("name") or "") if on else "",
                "title": str(track.get("title") or "") if on else "",
                "artist": str(track.get("artist") or "") if on else "",
                "source": source if on else "",
            },
        }

    def publish(self, status: dict) -> bool:
        """Push what the radio plays to HA, if the owner asked. True if sent. Never raises.

        Sends only on change and at most every PUBLISH_MIN_S; an unchanged
        state is re-sent every REPUSH_S, because a state created over REST is
        not stored by HA and vanishes when HA restarts.
        """
        try:
            settings, client = self.settings, self.client
            if settings is None or not settings.publish or client is None:
                return False
            doc = self.sensor(status or {})
            now = self.clock()
            with self._lock:
                since = now - self._pub_t
                if since < PUBLISH_MIN_S:
                    return False
                if doc == self._pub_last and since < REPUSH_S:
                    return False
                self._pub_last, self._pub_t = doc, now
            client.set_state_async(SENSOR, doc["state"], doc["attributes"])
            return True
        except Exception:
            log.debug("Home Assistant publish failed", exc_info=True)
            return False

    def _drop_sensor(self, client: Any) -> None:
        try:
            client.delete_state_async(SENSOR)
        except Exception:
            log.debug("could not remove %s", SENSOR, exc_info=True)

    # -- routing ------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        """One request -> (status, doc), or None if the path isn't under /api/ha."""
        url = urlparse(path)
        p = url.path.rstrip("/") or "/"
        if p != "/api/ha" and not p.startswith("/api/ha/"):
            return None
        method = method.upper()
        try:
            if p == "/api/ha":
                if method == "GET":
                    return 200, self.status()
                if method == "POST":
                    return self.connect(body)
                if method == "PATCH":
                    return self.set_publish(body)
                if method == "DELETE":
                    return self.disconnect()
            elif p == "/api/ha/discover" and method == "GET":
                return self.find()
            elif p in ("/api/ha/entities", "/api/ha/scenes", "/api/ha/lights") and method == "GET":
                if not self.connected:
                    return 409, {"error": "not connected"}
                query = parse_qs(url.query)
                refresh = query.get("refresh", [""])[0] in ("1", "true", "yes")
                try:
                    if p == "/api/ha/lights":
                        return 200, self.lights(refresh)
                    if p == "/api/ha/scenes":
                        return 200, self.entities(refresh, ("scene",))
                    wanted = tuple(d for d in query.get("domains", [""])[0].split(",") if d)
                    return 200, self.entities(refresh, wanted or None)
                except HaError as exc:
                    return 502, {"error": str(exc)}
            elif p == "/api/ha/services" and method == "GET":
                return 200, behaviour.services_doc()
            elif p == "/api/ha/try" and method == "POST":
                return self.try_action(body)
            elif p == "/api/ha/clear-missing" and method == "POST":
                return self.clear_missing()
            if p in ("/api/ha", "/api/ha/discover", "/api/ha/entities", "/api/ha/scenes", "/api/ha/lights",
                     "/api/ha/services", "/api/ha/try", "/api/ha/clear-missing"):
                return 405, {"ok": False, "error": f"{method} not allowed here"}
            return 404, {"ok": False, "error": "not found"}
        except Exception as exc:
            log.exception("Home Assistant request failed: %s %s", method, p)
            return 500, {"ok": False, "error": type(exc).__name__}
