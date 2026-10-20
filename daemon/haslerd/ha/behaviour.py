"""What a key (or the page's "Try it") asks Home Assistant to do: one behaviour.

A behaviour is a small dict, the same on the page, in keys.yaml and on the
wire to ``POST /api/ha/try``::

    {do: ha_light, entities: [light.sofa, light.desk], mode: on|off|toggle,
     brightness: 0-100|null, color: "#rrggbb"|null, kelvin: 2700|null,
     transition: seconds|null}
    {do: ha_scene, entity: scene.evening, transition: seconds|null}
    {do: ha_script, entity: script.good_night}
    {do: ha_automation, entity: automation.lights_out}
    {do: ha_toggle, entity: switch.fan}                    # any on/off thing
    {do: ha_service, service: "media_player.volume_set",
     entities: [media_player.kitchen], data: {volume_level: 0.3}}

``normalise`` validates what the page sends and returns the form that is
stored (nulls dropped, one ``entities`` list for lights and services);
``calls`` turns a stored behaviour into Home Assistant service calls. The four
original verbs keep their exact keys.yaml form and their exact service calls,
so keys saved by older releases work unchanged.

**Lights differ.** A bulb may do brightness only, white temperature only, or
full colour. ``calls`` is given what each light can do (``light_caps``, from
the light's ``supported_color_modes``, learnt by web/ha_api from HA's states)
and sends each light only what it supports, one call per group of lights
that share a capability set - so a "warm, 40 %" key on a mixed group dims the
plain bulbs and tints the colour ones, instead of HA rejecting the call.
Unknown lights (not seen yet) get everything and HA decides.

**The generic service call is fenced.** Only the (domain, service) pairs in
``SAFE_SERVICES`` and only their listed data keys are accepted, and every
target must be in that domain: a key can turn things on and off, set lights
and volume, never unlock a door or run a shell command.

Pure functions, no I/O: imported by actions, web/keymap, ha/rest and web/ha_api.
"""

from __future__ import annotations

import json
import re
from typing import Any

ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
COLOR = re.compile(r"^#?([0-9a-fA-F]{6})$")
MAX_ENTITIES = 24
KELVIN_RANGE = (1000, 12000)
TRANSITION_MAX = 300.0
LIGHT_MODES = ("on", "off", "toggle")
COLOR_MODES = ("hs", "xy", "rgb", "rgbw", "rgbww")

# The original verbs: one entity, of these domains.
SIMPLE: dict[str, list[str]] = {
    "ha_scene": ["scene"],
    "ha_script": ["script"],
    "ha_toggle": ["light", "switch", "input_boolean", "fan", "cover", "media_player"],
    "ha_automation": ["automation"],
}
# Verb -> the kind ha/rest's transport is called with (``ha(kind, payload)``).
KIND = {"ha_scene": "scene", "ha_script": "script", "ha_toggle": "toggle", "ha_automation": "automation",
        "ha_light": "light", "ha_service": "service"}
VERB_OF_KIND = {v: k for k, v in KIND.items()}
# The original verbs' services (ha/rest.SERVICES is built from this).
SIMPLE_SERVICE = {
    "ha_scene": ("scene", "turn_on"),
    "ha_script": ("script", "turn_on"),
    "ha_toggle": ("homeassistant", "toggle"),
    "ha_automation": ("automation", "trigger"),
}


def _num(lo: float, hi: float):
    def check(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi
    return check


def _bool(v: Any) -> bool:
    return isinstance(v, bool)


def _rgb(v: Any) -> bool:
    return isinstance(v, list) and len(v) == 3 and all(_num(0, 255)(x) for x in v)


def _short_text(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v) <= 64


_ONOFF = {"turn_on": {}, "turn_off": {}, "toggle": {}}
_LIGHT_DATA = {
    "brightness_pct": _num(0, 100), "rgb_color": _rgb, "color_temp_kelvin": _num(*KELVIN_RANGE),
    "transition": _num(0, TRANSITION_MAX), "effect": _short_text, "flash": lambda v: v in ("short", "long"),
}
# (domain -> service -> {data key: validator}). Nothing outside this runs.
SAFE_SERVICES: dict[str, dict[str, dict[str, Any]]] = {
    "light": {"turn_on": _LIGHT_DATA, "turn_off": {"transition": _num(0, TRANSITION_MAX)},
              "toggle": _LIGHT_DATA},
    "switch": dict(_ONOFF),
    "input_boolean": dict(_ONOFF),
    "fan": {**_ONOFF, "set_percentage": {"percentage": _num(0, 100)}},
    "cover": {"open_cover": {}, "close_cover": {}, "stop_cover": {}, "toggle": {},
              "set_cover_position": {"position": _num(0, 100)}},
    "scene": {"turn_on": {"transition": _num(0, TRANSITION_MAX)}},
    "script": {"turn_on": {}, "turn_off": {}, "toggle": {}},
    "automation": {"trigger": {"skip_condition": _bool}, "turn_on": {}, "turn_off": {}, "toggle": {}},
    "media_player": {
        **_ONOFF, "volume_set": {"volume_level": _num(0, 1)}, "volume_up": {}, "volume_down": {},
        "volume_mute": {"is_volume_muted": _bool}, "media_play_pause": {}, "media_play": {},
        "media_pause": {}, "media_stop": {}, "media_next_track": {}, "media_previous_track": {},
    },
    "button": {"press": {}},
    "input_button": {"press": {}},
}
# Required data keys (a volume_set without a level is meaningless).
REQUIRED_DATA = {("media_player", "volume_set"): "volume_level", ("media_player", "volume_mute"): "is_volume_muted",
                 ("fan", "set_percentage"): "percentage", ("cover", "set_cover_position"): "position"}


class Problem(ValueError):
    """A behaviour that can't be stored or run. ``str(exc)`` is for the page."""


def verbs() -> tuple[str, ...]:
    return tuple(KIND)


def services_doc() -> dict:
    """``GET /api/ha/services``: what ha_service accepts, for the page's editor."""
    return {
        "services": [
            {"service": f"{domain}.{service}", "data": sorted(keys),
             "required": [REQUIRED_DATA[(domain, service)]] if (domain, service) in REQUIRED_DATA else []}
            for domain, table in SAFE_SERVICES.items() for service, keys in table.items()
        ]
    }


# -- validating ------------------------------------------------------------


def _example(domain: str) -> str:
    return {"scene": "scene.living_room", "script": "script.good_night",
            "automation": "automation.lights_out"}.get(domain, f"{domain}.kitchen" if domain != "light" else "light.kitchen")


def check_entity(verb: str, entity: str) -> str | None:
    """Why ``entity`` can't be the target of one of the original verbs, or None."""
    domains = SIMPLE[verb]
    example = _example(domains[0]) if domains[0] != "light" else "light.kitchen"
    if not entity:
        return f"pick a Home Assistant entity, e.g. {example}"
    if not ENTITY_ID.match(entity):
        if verb == "ha_scene":
            return "a scene id looks like scene.living_room"
        return f"an entity id looks like {example}"
    if entity.split(".", 1)[0] not in domains:
        kinds = ", ".join(domains[:-1]) + (" or " if len(domains) > 1 else "") + domains[-1]
        return f"{entity} isn't a {kinds} - pick one like {example}"
    return None


def _entities(raw: dict, domains: list[str], what: str) -> list[str]:
    """``entities: [...]`` (or a single ``entity``) -> clean, deduplicated list."""
    value = raw.get("entities")
    if value is None or value == []:
        value = raw.get("entity")
        value = [value] if value else []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise Problem(f"pick at least one {what}, e.g. {_example(domains[0])}")
    out: list[str] = []
    for item in value:
        entity = str(item or "").strip()
        if not ENTITY_ID.match(entity):
            raise Problem(f"{entity or 'an empty name'} isn't an entity id - they look like {_example(domains[0])}")
        if entity.split(".", 1)[0] not in domains:
            raise Problem(f"{entity} isn't a {' or '.join(domains)}")
        if entity not in out:
            out.append(entity)
    if len(out) > MAX_ENTITIES:
        raise Problem(f"at most {MAX_ENTITIES} at once - make a group in Home Assistant for more")
    return out


def _optional_number(raw: dict, key: str, lo: float, hi: float, words: str, integer: bool) -> float | int | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise Problem(words) from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise Problem(words)
    return int(round(value)) if integer else round(float(value), 1)


def _light(raw: dict) -> dict:
    entities = _entities(raw, ["light"], "light")
    mode = raw.get("mode") or "on"
    if mode not in LIGHT_MODES:
        raise Problem("mode is on, off or toggle")
    spec: dict[str, Any] = {"do": "ha_light", "entities": entities, "mode": mode}
    if mode != "off":
        brightness = _optional_number(raw, "brightness", 0, 100, "brightness is 0 to 100 (%)", True)
        kelvin = _optional_number(raw, "kelvin", *KELVIN_RANGE,
                                  f"white temperature is {KELVIN_RANGE[0]} to {KELVIN_RANGE[1]} K", True)
        color = raw.get("color")
        if color in (None, ""):
            color = None
        else:
            m = COLOR.match(str(color).strip())
            if not m:
                raise Problem("a colour looks like #ff8800")
            color = "#" + m.group(1).lower()
        if color is not None and kelvin is not None:
            raise Problem("pick a colour or a white temperature, not both")
        for key, value in (("brightness", brightness), ("color", color), ("kelvin", kelvin)):
            if value is not None:
                spec[key] = value
    transition = _optional_number(raw, "transition", 0, TRANSITION_MAX,
                                  f"fade time is 0 to {int(TRANSITION_MAX)} seconds", False)
    if transition is not None:
        spec["transition"] = transition
    return spec


def _service(raw: dict) -> dict:
    name = str(raw.get("service") or "").strip()
    domain, _, service = name.partition(".")
    table = SAFE_SERVICES.get(domain, {})
    if service not in table:
        raise Problem(f"{name or 'that'} isn't a service the radio may call (see GET /api/ha/services)")
    domains = [domain]
    entities = _entities(raw, domains, domain.replace("_", " "))
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        raise Problem("data must be an object")
    allowed = table[service]
    clean: dict[str, Any] = {}
    for key, value in data.items():
        check = allowed.get(key)
        if check is None:
            raise Problem(f"{name} doesn't take {key!r} here")
        if not check(value):
            raise Problem(f"{key} has a value {name} won't take")
        clean[key] = value
    need = REQUIRED_DATA.get((domain, service))
    if need and need not in clean:
        raise Problem(f"{name} needs {need}")
    spec: dict[str, Any] = {"do": "ha_service", "service": name, "entities": entities}
    if clean:
        spec["data"] = clean
    return spec


def normalise(raw: Any) -> dict:
    """What the page sent -> the stored behaviour. Raises Problem with a sentence."""
    if not isinstance(raw, dict) or raw.get("do") not in KIND:
        raise Problem("pick a Home Assistant action")
    verb = raw["do"]
    if verb == "ha_light":
        return _light(raw)
    if verb == "ha_service":
        return _service(raw)
    entity = str(raw.get("entity") or "").strip()
    if not entity and isinstance(raw.get("entities"), list) and len(raw["entities"]) == 1:
        entity = str(raw["entities"][0] or "").strip()
    problem = check_entity(verb, entity)
    if problem:
        raise Problem(problem)
    spec: dict[str, Any] = {"do": verb, "entity": entity}
    if verb == "ha_scene":
        transition = _optional_number(raw, "transition", 0, TRANSITION_MAX,
                                      f"fade time is 0 to {int(TRANSITION_MAX)} seconds", False)
        if transition is not None:
            spec["transition"] = transition
    return spec


def to_page(spec: dict) -> dict:
    """Stored behaviour -> what the page edits: every field present, null = unset."""
    verb = spec.get("do")
    if verb == "ha_light":
        entities = entity_ids(spec)
        return {"do": verb, "entities": entities, "entity": entities[0] if entities else "",
                "mode": spec.get("mode") or "on", "brightness": spec.get("brightness"),
                "color": spec.get("color"), "kelvin": spec.get("kelvin"), "transition": spec.get("transition")}
    if verb == "ha_service":
        entities = entity_ids(spec)
        return {"do": verb, "service": str(spec.get("service") or ""), "entities": entities,
                "entity": entities[0] if entities else "", "data": dict(spec.get("data") or {})}
    page: dict[str, Any] = {"do": verb, "entity": str(spec.get("entity", ""))}
    if verb == "ha_scene" and spec.get("transition") is not None:
        page["transition"] = spec["transition"]
    return page


def entity_ids(spec: dict) -> list[str]:
    """Every entity a stored behaviour (or a transport payload) acts on."""
    value = spec.get("entities")
    if isinstance(value, list):
        return [e for e in value if isinstance(e, str) and e]
    entity = spec.get("entity")
    return [entity] if isinstance(entity, str) and entity else []


# -- what lights can do --------------------------------------------------------


def light_caps(state: dict) -> dict | None:
    """HA state of a light -> {brightness, color, color_temp: [min_k, max_k]|None}.

    None when HA doesn't say (an unavailable light carries no attributes).
    """
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    modes = attrs.get("supported_color_modes")
    if not isinstance(modes, list) or not modes:
        return None
    modes = [str(m) for m in modes]
    color = any(m in COLOR_MODES for m in modes)
    temp = None
    if "color_temp" in modes:
        lo, hi = attrs.get("min_color_temp_kelvin"), attrs.get("max_color_temp_kelvin")
        lo = int(lo) if isinstance(lo, (int, float)) and not isinstance(lo, bool) else 2000
        hi = int(hi) if isinstance(hi, (int, float)) and not isinstance(hi, bool) else 6500
        temp = [min(lo, hi), max(lo, hi)]
    brightness = color or temp is not None or any(m in ("brightness", "white") for m in modes)
    return {"brightness": brightness, "color": color, "color_temp": temp}


def _rgb_of(color: str) -> list[int]:
    h = color.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def _light_data(spec: dict) -> dict:
    data: dict[str, Any] = {}
    if spec.get("brightness") is not None:
        data["brightness_pct"] = int(spec["brightness"])
    if spec.get("color"):
        data["rgb_color"] = _rgb_of(spec["color"])
    if spec.get("kelvin") is not None:
        data["color_temp_kelvin"] = int(spec["kelvin"])
    if spec.get("transition") is not None:
        data["transition"] = spec["transition"]
    return data


def fit(data: dict, caps: dict | None) -> tuple[dict, list[str]]:
    """Light data -> what this light can take, and what was left out (words)."""
    if caps is None:
        return dict(data), []
    out, dropped = dict(data), []
    if "rgb_color" in out and not caps.get("color"):
        out.pop("rgb_color")
        dropped.append("colour")
    if "color_temp_kelvin" in out:
        temp = caps.get("color_temp")
        if temp:
            out["color_temp_kelvin"] = max(temp[0], min(temp[1], out["color_temp_kelvin"]))
        elif not caps.get("color"):         # a colour bulb shows a white temperature as a colour
            out.pop("color_temp_kelvin")
            dropped.append("white temperature")
    if "brightness_pct" in out and not caps.get("brightness"):
        out.pop("brightness_pct")
        dropped.append("brightness")
    return out, dropped


def _target(entities: list[str]) -> str | list[str]:
    return entities[0] if len(entities) == 1 else list(entities)


def calls(spec: dict, caps: dict[str, dict | None] | None = None,
          skip: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, str, dict]]:
    """A stored behaviour -> [(domain, service, data)]; entities in ``skip`` left out."""
    verb = spec.get("do")
    entities = [e for e in entity_ids(spec) if e not in skip]
    if not entities:
        return []
    if verb in SIMPLE_SERVICE:
        domain, service = SIMPLE_SERVICE[verb]
        data: dict[str, Any] = {"entity_id": entities[0]}
        if verb == "ha_scene" and spec.get("transition") is not None:
            data["transition"] = spec["transition"]
        return [(domain, service, data)]
    if verb == "ha_service":
        domain, _, service = str(spec.get("service") or "").partition(".")
        if service not in SAFE_SERVICES.get(domain, {}):
            return []
        return [(domain, service, {"entity_id": _target(entities), **dict(spec.get("data") or {})})]
    if verb == "ha_light":
        mode = spec.get("mode") or "on"
        if mode == "off":
            data = {"entity_id": _target(entities)}
            if spec.get("transition") is not None:
                data["transition"] = spec["transition"]
            return [("light", "turn_off", data)]
        service = "toggle" if mode == "toggle" else "turn_on"
        wanted = _light_data(spec)
        groups: dict[str, tuple[dict, list[str]]] = {}
        for entity in entities:
            fitted, _ = fit(wanted, (caps or {}).get(entity))
            key = json.dumps(fitted, sort_keys=True)
            groups.setdefault(key, (fitted, []))[1].append(entity)
        return [("light", service, {"entity_id": _target(ents), **fitted}) for fitted, ents in groups.values()]
    return []


def warnings(spec: dict, caps: dict[str, dict | None], names: dict[str, str] | None = None) -> list[str]:
    """What some lights will leave out, for "Try it" ("Hall light can't show colour")."""
    if spec.get("do") != "ha_light" or (spec.get("mode") or "on") == "off":
        return []
    wanted = _light_data(spec)
    out = []
    for entity in entity_ids(spec):
        _, dropped = fit(wanted, caps.get(entity))
        if dropped:
            name = (names or {}).get(entity) or entity
            out.append(f"{name} can't show {' or '.join(dropped)}; it does the rest")
    return out
