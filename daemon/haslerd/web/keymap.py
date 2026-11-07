"""What the keys do, as the web page sees it - read, validate, apply, persist.

The descriptor (config/variants/*.yaml) holds the cabinet's defaults and lives
in git. What the owner picks on the web page goes to an overlay file instead
(``keys.yaml``, next to state.json): syncing the code never clobbers it, and
"reset to defaults" is just deleting it.

The page speaks a small vocabulary (``volume_up`` rather than
``volume_step: {delta: 2}``); this module translates both ways and is the only
place that knows the mapping. Applying is a reference swap of the presets and
action table, so the panel loop - which never takes a lock - sees either the
old layout or the new one, never half of each.

Untouched keys follow the release. keys.yaml holds the whole layout, so it
also carries ``defaults_rev`` (haslerd/presets.py ``layout_rev``) and
``shipped``, the presets and key table the release shipped when the file was
saved. At startup (``apply_saved``) a key whose saved preset or action still
equals that snapshot takes what this release ships instead; the owner's own
choices are untouched by construction. Combinations work the same way when
absent from the file (the descriptor's apply), as they always have.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from .. import presets as starter_sets
from ..actions import Actions, build_presets
from ..boxconfig import atomic_write_text
from ..ha import behaviour
from ..panel.combos import COMBO_HOLD_RANGE, DEFAULT_COMBO_HOLD_S, MAX_COMBOS, combo_id
from ..panel.controls import Momentary, bank_keys, bank_latching, default_label, release_key
from ..player.base import Preset
from ..player.sources import ytm_refs
from ..sleep import DEFAULT_MINUTES as SLEEP_DEFAULT_MINUTES, MAX_MINUTES as SLEEP_MAX_MINUTES

log = logging.getLogger(__name__)

# keys.yaml's own schema (box.yaml's is boxconfig.SCHEMA). 2 since 1.5.0:
# presets by collection id (``station``), ``defaults_rev`` and ``shipped``
# (migration 002). A newer file on an older release logs and reads what it can.
KEYS_SCHEMA = 2
EVENTS = ("tap", "hold")
VOLUME_DELTA = 2          # knob units per step; ~1.8 dB on the Amp4 Pro
VOLUME_REPEAT_S = 0.15    # a held volume key ramps at this rate
SHORT_MAX = 12            # the owner's own key-cap label (preset ``short``)

# The menu, in the order the page shows it. ``params`` are the fields the page
# must collect for that choice.
VERBS: list[dict[str, Any]] = [
    {"do": "none", "label": "Nothing"},
    {"do": "preset", "label": "Play a preset", "params": ["n"]},
    {"do": "cycle_source", "label": "Next source (order set in Settings → Keys & knobs)"},
    {"do": "bt_pair", "label": "Bluetooth pairing", "params": ["seconds"]},
    # Selects Bluetooth and calls the last phone; while a phone plays, again =
    # next track and this key's hold = pause/play (Settings -> Bluetooth).
    {"do": "bluetooth", "label": "Bluetooth (again: next track, hold: pause)"},
    {"do": "volume_up", "label": "Volume up (ramps while held)"},
    {"do": "volume_down", "label": "Volume down (ramps while held)"},
    {"do": "standby_toggle", "label": "Standby / wake"},
    # The sleep timer (haslerd/sleep.py) from a hold or a combination; no
    # shipped key does it - nothing on the cabinet would hint at it.
    {"do": "sleep", "label": "Sleep timer", "params": ["minutes"]},
    # Multiroom (multiroom/group.py): follow the Hasler leading a group, else
    # the one playing; on a follower, leave the group.
    {"do": "join_group", "label": "Join the music in another room (again: leave)"},
    {"do": "ha_scene", "label": "Home Assistant scene", "params": ["entity", "transition"], "domains": ["scene"]},
    {"do": "ha_script", "label": "Run a Home Assistant script", "params": ["entity"], "domains": ["script"]},
    {"do": "ha_toggle", "label": "Toggle a Home Assistant device", "params": ["entity"],
     "domains": ["light", "switch", "input_boolean", "fan", "cover", "media_player"]},
    {"do": "ha_automation", "label": "Trigger a Home Assistant automation", "params": ["entity"],
     "domains": ["automation"]},
    # After ha_toggle: a page that picks "the first verb whose domains hold this
    # entity's domain" keeps toggling a light rather than switching it on.
    {"do": "ha_light", "label": "Set Home Assistant lights",
     "params": ["entities", "mode", "brightness", "color", "kelvin", "transition"], "domains": ["light"]},
    {"do": "ha_service", "label": "Call a Home Assistant service (advanced)",
     "params": ["service", "entities", "data"], "domains": list(behaviour.SAFE_SERVICES)},
]
VERB_NAMES = {v["do"] for v in VERBS}

# Home Assistant verbs -> the entity domains each accepts. The page filters its
# entity picker with the same lists (the "domains" hint above); the daemon
# re-checks here, because a hand-edited request must not toggle a scene.
HA_VERBS: dict[str, list[str]] = {v["do"]: v["domains"] for v in VERBS if "domains" in v}
ENTITY_ID = behaviour.ENTITY_ID

SPOTIFY_URI = re.compile(r"^spotify:(playlist|album|track|artist|show|episode):[A-Za-z0-9]{22}$")
SPOTIFY_LINK = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z-]+/)?(playlist|album|track|artist|show|episode)/([A-Za-z0-9]{22})"
)
YTM_LINK = re.compile(r"^https?://(?:music\.|www\.|m\.)?youtube\.com/(?:playlist|watch)\?\S*\b(?:list|v)=[\w-]+")


class Invalid(ValueError):
    """A submitted layout that can't be applied. ``errors`` name the fields."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['where']}: {e['message']}" for e in errors))
        self.errors = errors


# -- reading ---------------------------------------------------------------


def key_specs(actions: Actions) -> list[dict]:
    """The cabinet's assignable keys, left to right: ``[{"id", "label", "latching"}]``.

    Its momentary controls, and the keys of an interlocked bank (the
    Excelsior's 1-6, PU, Tape - ids and labels from the descriptor's
    ``keys:``). A latching bank key's ``tap`` is its selection and it has no
    hold (a latched key can't be told from a held one). The bank's release
    key (key 0) is fixed while the bank latches - every press of it pops the
    latched key, which is the bank's ``clear`` - and an ordinary key on a unit
    whose keys spring back (``latching: false``), where it comes first.
    """
    out: list[dict] = []
    for c in actions.variant.controls:
        kind = c.get("kind")
        if kind == "momentary":
            out.append({"id": c["id"], "label": str(c.get("label") or default_label(c["id"])), "latching": False})
        elif kind == "interlocked_bank":
            latching = bank_latching(c)
            release = release_key(c)
            if release and not latching:
                out.append({"id": release["id"], "label": release["label"], "latching": False})
            out.extend({**key, "latching": latching} for key in bank_keys(c))
    return out


def key_ids(actions: Actions) -> list[str]:
    """The cabinet's assignable keys, left to right (key_specs)."""
    return [k["id"] for k in key_specs(actions)]


def latching_ids(actions: Actions) -> set[str]:
    """The keys that stay down when pressed: a tap (their selection), no hold."""
    return {k["id"] for k in key_specs(actions) if k["latching"]}


def fixed_keys(actions: Actions) -> list[dict]:
    """Keys the page shows but doesn't offer to change: a latching bank's
    release key (the Excelsior's key 0), ``[{"id", "label", "tap", "hold"}]``."""
    table = actions.variant.actions
    out = []
    for c in actions.variant.controls:
        if c.get("kind") == "interlocked_bank" and bank_latching(c):
            release = release_key(c)
            if release:
                rid = release["id"]
                out.append({"id": rid, "label": release["label"], "release": True,
                            "tap": to_page(table.get(f"{rid}.tap")), "hold": to_page(table.get(f"{rid}.hold"))})
    return out


def label_for(cid: str, actions: Actions | None = None) -> str:
    """A key's caption: the descriptor's label (with ``actions``), else
    ``k3`` -> ``3``, ``pu`` -> ``PU``."""
    if actions is not None:
        for k in key_specs(actions):
            if k["id"] == cid:
                return k["label"]
    return default_label(cid)


def to_page(spec: dict | None) -> dict | None:
    """An action-table entry -> the page's vocabulary (None = does nothing)."""
    if not spec:
        return None
    verb = spec.get("do")
    if verb == "volume_step":
        delta = float(spec.get("delta", 0))
        if not delta:
            return None
        return {"do": "volume_up" if delta > 0 else "volume_down"}
    if verb in ("standby_toggle", "cycle_source", "join_group", "bluetooth"):
        return {"do": verb}
    if verb == "preset":
        return {"do": "preset", "n": int(spec["n"])}
    if verb == "bt_pair":
        return {"do": "bt_pair", "seconds": int(spec.get("seconds", 120))}
    if verb == "sleep":
        try:
            minutes = int(spec.get("minutes", SLEEP_DEFAULT_MINUTES))
        except (TypeError, ValueError):
            minutes = SLEEP_DEFAULT_MINUTES
        return {"do": "sleep", "minutes": minutes}
    if verb in HA_VERBS:
        return behaviour.to_page(spec)
    # Something the page has no control for (set in the descriptor by hand):
    # shown, kept as-is on save.
    return {"do": "custom", "label": verb or "?", "spec": dict(spec)}


def preset_to_page(p: Preset) -> dict:
    """A preset for the page; a ytm reference adds its fields (kind, id, mode,
    seed, country, list, title, params - player/sources/ytm_refs.py), a
    collection station its ``station`` id."""
    doc = {"n": p.n, "name": p.name, "source": p.source, "uri": p.uri, "shuffle": p.shuffle, **p.ytm_ref}
    if p.short:
        doc["short"] = p.short      # the owner's own key-cap label
    if p.station:
        doc["station"] = p.station
    return doc


def ha_connected(ha: Any) -> bool:
    """``ha`` as WebApp holds it: a bool, a callable returning one, or a HaApi."""
    try:
        if callable(ha):
            return bool(ha())
        if hasattr(ha, "connected"):
            return bool(ha.connected)
        return bool(ha)
    except Exception:
        return False


def combo_keys(actions: Actions) -> list[str]:
    """Keys that may be half of a combination: every key of the cabinet that
    springs back. Latching keys (an interlocked bank: pressing one releases
    the other, so two are never down together), a latching bank's release key
    and end switches are never keys here."""
    return [k["id"] for k in key_specs(actions) if not k["latching"]]


def combo_to_page(combo: dict) -> dict:
    """A stored combination -> the page's: ``{"keys", "hold_s", "do", ...params}``."""
    spec = {k: v for k, v in combo.items() if k not in ("keys", "hold_s")}
    action = to_page(spec) or {"do": "none"}
    return {
        "keys": list(combo.get("keys") or []),
        "hold_s": float(combo.get("hold_s", DEFAULT_COMBO_HOLD_S)),
        **action,
    }


def power_doc(actions: Actions) -> list[dict]:
    """How this cabinet is switched off, for the printed key card: one entry per
    switch that puts the radio in standby.

    ``{"kind": "end_switch", "control": "tone", "off": "standby", "off_held":
    "shutdown"|None, "held_s": 5.0|None}`` - a knob's switch at the end of its
    turn; ``{"kind": "release_key", "control": "presets", "tap": "standby",
    "hold": "shutdown"|None}`` - the release key of an interlocked bank.
    Keys bound to standby_toggle are on the card already, as keys."""
    table = actions.variant.actions

    def verb(name: str) -> str | None:
        spec = table.get(name)
        return spec.get("do") if isinstance(spec, dict) else None

    out: list[dict] = []
    for c in actions.variant.controls:
        if not isinstance(c, dict):
            continue
        sw = c.get("end_switch")
        if isinstance(sw, dict) and sw.get("id") and verb(f"{sw['id']}.off") == "standby":
            held = verb(f"{sw['id']}.off_held")
            ms = sw.get("off_hold_ms")
            out.append({"kind": "end_switch", "control": c.get("id"), "off": "standby", "off_held": held,
                        "held_s": round(ms / 1000, 1) if held and isinstance(ms, (int, float)) else None})
        # Latching only: on a unit whose keys spring back key 0 is a key, on the card already.
        release = release_key(c) if c.get("kind") == "interlocked_bank" and bank_latching(c) else None
        if release:
            rid = release["id"]
            if verb(f"{rid}.tap") == "standby":
                out.append({"kind": "release_key", "control": c.get("id"), "tap": "standby",
                            "hold": verb(f"{rid}.hold")})
    return out


def layout_doc(actions: Actions) -> dict:
    """What the owner sets on the page: keys, presets, combinations."""
    table = actions.variant.actions
    keys = []
    for spec in key_specs(actions):
        cid = spec["id"]
        keys.append({
            "id": cid,
            "label": spec["label"],
            "tap": to_page(table.get(f"{cid}.tap")),
            # A latching key has no hold: always None, and ignored on save.
            "hold": None if spec["latching"] else to_page(table.get(f"{cid}.hold")),
            # True: the key stays down when pressed (an interlocked bank) -
            # its tap is "selected", and it has no hold and no combinations.
            "latching": spec["latching"],
        })
    return {
        "variant": actions.variant.name,
        # What the page calls the cabinet ("Hasler HM · Bern 1964", "Excelsior").
        "title": actions.variant.title or actions.variant.name,
        "keys": keys,
        # Shown, not assignable: the Excelsior's key 0 while its bank latches.
        "fixed_keys": fixed_keys(actions),
        "presets": [preset_to_page(p) for p in sorted(actions.presets.values(), key=lambda p: p.n)],
        "combos": [combo_to_page(c) for c in actions.variant.combos or [] if isinstance(c, dict)],
    }


def layout_rev(layout: dict) -> str:
    """The layout's revision: a short hash of its content, so it changes with
    any change (the page, Browse, a reset, Home Assistant) and survives restarts.
    Two phones editing at once: a PUT carrying an old one is refused (409)."""
    raw = json.dumps(layout, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def config_doc(actions: Actions, sources: dict[str, bool], ha: Any, starter: dict | None = None) -> dict:
    """``GET /api/config``. ``starter`` (web/server.py WebApp.starter_doc) adds
    the starter sets: ``starter`` (the one in force), ``starter_from``,
    ``starters`` (every set with its keys) and ``factory`` (the keys equal
    the set in force, by content)."""
    layout = layout_doc(actions)
    return {
        **(starter or {}),
        "variant": actions.variant.name,
        "title": layout["title"],
        "keys": layout["keys"],
        "fixed_keys": layout["fixed_keys"],
        "presets": layout["presets"],
        "verbs": VERBS,
        "sources": sources,
        "ha": ha_connected(ha),
        "combos": layout["combos"],
        "combo_keys": combo_keys(actions),
        "combo_limits": {"hold_s": list(COMBO_HOLD_RANGE), "default_hold_s": DEFAULT_COMBO_HOLD_S,
                         "max": MAX_COMBOS},
        "power": power_doc(actions),
        "short_max": SHORT_MAX,
        # Send it back with the PUT ("rev", or If-Match) to be told about a change made meanwhile.
        "rev": layout_rev(layout),
    }


# -- validating ------------------------------------------------------------


def normalise_uri(source: str, uri: str) -> str:
    """Accept what people paste: open.spotify.com links become spotify: URIs."""
    uri = uri.strip()
    if source == "spotify":
        m = SPOTIFY_LINK.match(uri)
        if m:
            return f"spotify:{m.group(1)}:{m.group(2)}"
    return uri


def check_uri(source: str, uri: str) -> str | None:
    """Why ``uri`` can't be played by ``source``, or None if it looks right."""
    if not uri:
        return "add a link"
    if source == "radio":
        return None if re.match(r"^https?://\S+$", uri) else "a radio stream link starts with http:// or https://"
    if source == "spotify":
        return None if SPOTIFY_URI.match(uri) else "paste a Spotify playlist, album or track link"
    if source == "ytm":
        return None if YTM_LINK.match(uri) else "paste a YouTube Music playlist link (…playlist?list=…)"
    return f"unknown source {source!r}"


def check_entity(verb: str, entity: str) -> str | None:
    """Why ``entity`` can't be the target of HA ``verb``, or None if it can."""
    if verb in behaviour.SIMPLE:
        return behaviour.check_entity(verb, entity)
    try:
        behaviour.normalise({"do": verb, "entity": entity})
    except behaviour.Problem as exc:
        return str(exc)
    return None


def ytm_ref_from_page(raw: dict, where: str, errors: list) -> tuple[dict, str] | None:
    """A ytm preset with a ``kind`` -> (its reference, the link it plays now
    or "" when that is looked up at press time). Appends to ``errors``."""
    before = len(errors)

    def bad(field: str, message: str) -> None:
        errors.append({"where": f"{where}.{field}", "message": message})

    kind = raw.get("kind")
    if kind not in ytm_refs.KINDS:
        bad("kind", f"kind must be one of: {', '.join(ytm_refs.KINDS)}")
        return None
    ref: dict[str, str] = {"kind": kind}
    uri = str(raw.get("uri") or "").strip()
    if uri and not YTM_LINK.match(uri):
        bad("uri", "the fallback link must be a music.youtube.com playlist or watch link")
    rid = str(raw.get("id") or "").strip()
    if kind == "playlist":
        pid = ytm_refs.strip_vl(rid)
        if rid and not ytm_refs.LIST_ID.match(pid):
            bad("id", "not a YouTube Music playlist id")
        elif rid:
            ref["id"] = pid
        elif not uri:
            bad("uri", "paste a YouTube Music playlist link (…playlist?list=…)")
    elif kind == "album":
        if not (ytm_refs.OLAK_ID.match(rid) or ytm_refs.MPRE_ID.match(rid)):
            bad("id", "an album id starts OLAK5uy_ or MPREb_")
        ref["id"] = rid
    elif kind == "artist":
        mode = str(raw.get("mode") or "top")
        if not ytm_refs.CHANNEL_ID.match(rid):
            bad("id", "an artist id starts UC (22 more characters)")
        if mode not in ytm_refs.ARTIST_MODES:
            bad("mode", f"mode must be one of: {', '.join(ytm_refs.ARTIST_MODES)}")
        ref.update(id=rid, mode=mode)
    elif kind == "radio":
        seed = str(raw.get("seed") or "").strip()
        m = ytm_refs.SEED.match(seed)
        if not m or not (ytm_refs.VIDEO_ID.match(m.group(2)) if m.group(1) == "song"
                         else ytm_refs.OLAK_ID.match(m.group(2))):
            bad("seed", "a radio seed is song:<video id> or album:<OLAK5uy_… id>")
        ref["seed"] = seed
    elif kind == "chart":
        country = str(raw.get("country") or "ZZ").strip().upper()
        chart = str(raw.get("list") or "daily")
        if not ytm_refs.COUNTRY.match(country):
            bad("country", "a chart country is two letters, like ES (ZZ = global)")
        if chart not in ytm_refs.CHART_LISTS:
            bad("list", f"list must be one of: {', '.join(ytm_refs.CHART_LISTS)}")
        ref.update(country=country, list=chart)
    elif kind == "mood":
        params = str(raw.get("params") or "").strip()
        title = " ".join(str(raw.get("title") or "").split())
        if not ytm_refs.PARAMS.match(params):
            bad("params", "not a Moods & genres page")
        if not title or len(title) > 120:
            bad("title", "a mood preset needs the YouTube Music list's title")
        if rid and ytm_refs.LIST_ID.match(rid):
            ref["id"] = rid
        ref.update(params=params, title=title)
    if len(errors) > before:
        return None
    return ref, ytm_refs.link_for(ref) or uri


def short_from_page(raw: dict, where: str, errors: list) -> str | None:
    """The preset's optional ``short`` (its own key-cap label): "" when absent
    or blank, None (and an error at ``<where>.short``) when it can't be one."""
    value = raw.get("short")
    if value is None:
        return ""
    if not isinstance(value, str):
        errors.append({"where": f"{where}.short", "message": "the key label must be text"})
        return None
    value = " ".join(value.split())
    if len(value) > SHORT_MAX:
        errors.append({"where": f"{where}.short", "message": f"a key label fits {SHORT_MAX} characters"})
        return None
    return value


def preset_from_page(raw: Any, where: str, errors: list) -> dict | None:
    """Page preset -> descriptor-style spec (``{n, name, radio: …}``).

    A ytm preset with a ``kind`` keeps its reference beside the link:
    ``{n, name, ytm: <link or "">, kind, id, …, shuffle}`` (ytm_ref_from_page).
    An owner's key-cap label rides along as ``short`` (optional, up to SHORT_MAX).
    """
    if not isinstance(raw, dict):
        errors.append({"where": where, "message": "not a preset"})
        return None
    try:
        n = int(raw.get("n"))
    except (TypeError, ValueError):
        errors.append({"where": f"{where}.n", "message": "missing preset number"})
        return None
    short = short_from_page(raw, where, errors)
    if short is None:
        return None
    source = raw.get("source", "radio")
    if source not in ("radio", "spotify", "ytm"):
        errors.append({"where": f"{where}.source", "message": "pick radio, Spotify or YouTube Music"})
        return None
    if source == "ytm" and raw.get("kind"):
        got = ytm_ref_from_page(raw, where, errors)
        if got is None:
            return None
        ref, link = got
        name = str(raw.get("name") or "").strip() or f"Preset {n}"
        spec = {"n": n, "name": name[:60], "ytm": link, **ref}
        endless = ref["kind"] == "radio" or (ref["kind"] == "artist" and ref.get("mode") != "top")
        if raw.get("shuffle") and not endless:
            spec["shuffle"] = True
        if short:
            spec["short"] = short
        return spec
    uri = normalise_uri(source, str(raw.get("uri") or ""))
    problem = check_uri(source, uri)
    if problem:
        errors.append({"where": f"{where}.uri", "message": problem})
        return None
    name = str(raw.get("name") or "").strip() or f"Preset {n}"
    spec: dict[str, Any] = {"n": n, "name": name[:60], source: uri}
    # A station of the collection keeps its id while the link is still the
    # collection's; a link the owner edited is their own, id dropped.
    station = raw.get("station")
    if source == "radio" and isinstance(station, str) and station and starter_sets.station_url(station) == uri:
        spec["station"] = station
    if source != "radio" and raw.get("shuffle"):
        spec["shuffle"] = True
    if short:
        spec["short"] = short
    return spec


def action_from_page(raw: Any, where: str, preset_ns: set[int], current: dict | None, errors: list) -> dict | None:
    """Page action -> action-table entry. None = does nothing."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or raw.get("do") not in VERB_NAMES | {"custom"}:
        errors.append({"where": where, "message": "pick something from the list"})
        return None
    verb = raw["do"]
    if verb == "none":
        return None
    if verb == "custom":
        return current          # untouched: the page can't edit it
    if verb == "preset":
        try:
            n = int(raw.get("n"))
        except (TypeError, ValueError):
            n = None
        if n not in preset_ns:
            errors.append({"where": where, "message": "that preset doesn't exist"})
            return None
        return {"do": "preset", "n": n}
    if verb == "volume_up":
        return {"do": "volume_step", "delta": VOLUME_DELTA}
    if verb == "volume_down":
        return {"do": "volume_step", "delta": -VOLUME_DELTA}
    if verb == "bt_pair":
        try:
            seconds = int(raw.get("seconds", 120))
        except (TypeError, ValueError):
            seconds = 0
        if not 10 <= seconds <= 600:
            errors.append({"where": where, "message": "pairing window: 10–600 seconds"})
            return None
        return {"do": "bt_pair", "seconds": seconds}
    if verb == "sleep":
        minutes = raw.get("minutes", SLEEP_DEFAULT_MINUTES)
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes <= SLEEP_MAX_MINUTES:
            errors.append({"where": where, "message": f"sleep timer: 1–{SLEEP_MAX_MINUTES} minutes"})
            return None
        return {"do": "sleep", "minutes": int(minutes)}
    if verb in HA_VERBS:
        try:
            return behaviour.normalise(raw)
        except behaviour.Problem as exc:
            errors.append({"where": where, "message": str(exc)})
            return None
    return {"do": verb}         # cycle_source, standby_toggle, join_group, bluetooth


def combos_from_page(
    raw: Any, actions: Actions, preset_ns: set[int], errors: list, current: list[dict] | None = None
) -> list[dict]:
    """The page's ``combos`` -> stored combinations, in descriptor key order:
    ``[{"keys": [a, b], "hold_s": s, "do": verb, ...params}]``. Each problem is
    appended to ``errors`` as ``combos[i]``, ``combos[i].keys``,
    ``combos[i].hold_s`` or ``combos[i].do`` (the action)."""
    if not isinstance(raw, list):
        errors.append({"where": "combos", "message": "expected a list of key combinations"})
        return []
    if len(raw) > MAX_COMBOS:
        errors.append({"where": "combos", "message": f"at most {MAX_COMBOS} key combinations"})
        return []
    order = {cid: i for i, cid in enumerate(combo_keys(actions))}
    existing = {frozenset(c.get("keys") or ()): c for c in (current or []) if isinstance(c, dict)}
    out: list[dict] = []
    seen: set[frozenset] = set()
    for i, item in enumerate(raw):
        where = f"combos[{i}]"
        if not isinstance(item, dict):
            errors.append({"where": where, "message": "not a key combination"})
            continue
        keys = item.get("keys")
        if not isinstance(keys, list) or len(keys) != 2 or not all(isinstance(k, str) for k in keys):
            errors.append({"where": f"{where}.keys", "message": "pick two keys"})
            continue
        unknown = [k for k in keys if k not in order]
        if unknown:
            errors.append({"where": f"{where}.keys", "message": f"no key called {unknown[0]!r}"})
            continue
        if keys[0] == keys[1]:
            errors.append({"where": f"{where}.keys", "message": "pick two different keys"})
            continue
        keys = sorted(keys, key=order.__getitem__)
        pair = frozenset(keys)
        if pair in seen:
            errors.append({"where": f"{where}.keys", "message":
                           f"{label_for(keys[0], actions)} + {label_for(keys[1], actions)} is already a combination"})
            continue
        seen.add(pair)
        hold = item.get("hold_s", DEFAULT_COMBO_HOLD_S)
        lo, hi = COMBO_HOLD_RANGE
        if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not lo <= hold <= hi:
            errors.append({"where": f"{where}.hold_s", "message": f"hold together for {lo:g}–{hi:g} seconds"})
            continue
        action = {k: v for k, v in item.items() if k not in ("keys", "hold_s")}
        cur = existing.get(pair)
        cur_spec = {k: v for k, v in cur.items() if k not in ("keys", "hold_s")} if cur else None
        before = len(errors)
        entry = action_from_page(action, f"{where}.do", preset_ns, cur_spec, errors)
        if len(errors) > before:
            continue
        if entry is None:
            errors.append({"where": f"{where}.do", "message": "pick what the combination does"})
            continue
        out.append({"keys": keys, "hold_s": round(float(hold), 2), **entry})
    return out


def parse(doc: Any, actions: Actions) -> tuple[list[dict], dict[str, dict | None]]:
    """A PUT body -> (preset specs, {"k1.tap": entry-or-None, …}). Raises Invalid."""
    specs, table, _ = parse_layout(doc, actions)
    return specs, table


def parse_layout(doc: Any, actions: Actions) -> tuple[list[dict], dict[str, dict | None], list[dict] | None]:
    """A PUT body -> (preset specs, key table, combinations - None when the body
    has no ``combos``, which keeps the current ones). Raises Invalid."""
    errors: list[dict[str, str]] = []
    if not isinstance(doc, dict):
        raise Invalid([{"where": "", "message": "expected a JSON object"}])

    specs: list[dict] = []
    seen: set[int] = set()
    for i, raw in enumerate(doc.get("presets") or []):
        spec = preset_from_page(raw, f"presets[{i}]", errors)
        if spec is None:
            # Still a preset as far as the keys are concerned: one error on its
            # link, not a second "doesn't exist" on every key that plays it.
            try:
                seen.add(int(raw.get("n")))
            except (AttributeError, TypeError, ValueError):
                pass
            continue
        if spec["n"] in seen:
            errors.append({"where": f"presets[{i}].n", "message": f"preset {spec['n']} appears twice"})
            continue
        seen.add(spec["n"])
        specs.append(spec)

    known = set(key_ids(actions))
    latching = latching_ids(actions)
    table: dict[str, dict | None] = {}
    for i, raw in enumerate(doc.get("keys") or []):
        cid = raw.get("id") if isinstance(raw, dict) else None
        if cid not in known:
            errors.append({"where": f"keys[{i}]", "message": f"no key called {cid!r}"})
            continue
        for event in EVENTS:
            if event == "hold" and cid in latching:
                continue        # no hold on a latching key: whatever was there stays
            name = f"{cid}.{event}"
            table[name] = action_from_page(
                raw.get(event), f"keys[{i}].{event}", seen, actions.variant.actions.get(name), errors
            )
    combos = None
    if "combos" in doc:
        combos = combos_from_page(doc.get("combos") or [], actions, seen, errors, actions.variant.combos)
    if errors:
        raise Invalid(errors)
    return specs, table, combos


# -- applying and persisting ------------------------------------------------


def apply(
    actions: Actions, panel: Any, specs: list[dict], table: dict[str, dict | None],
    combos: list[dict] | None = None,
) -> None:
    """Swap in the new layout (``combos`` None = keep the current ones). Safe
    against a panel loop running alongside."""
    merged = dict(actions.variant.actions)
    for name, entry in table.items():
        if entry is None:
            merged.pop(name, None)
        else:
            merged[name] = entry
    presets = build_presets(specs, actions.variant.name)
    actions.variant.presets = specs
    actions.presets = presets           # reference swaps: atomic for the loop
    actions.variant.actions = merged
    if combos is not None:
        actions.variant.combos = combos
    tune_repeat(actions, panel)
    tune_combos(actions, panel)


def tune_combos(actions: Actions, panel: Any) -> None:
    """Tell the panel which key combinations to watch for."""
    set_combos = getattr(panel, "set_combos", None)
    if callable(set_combos):
        set_combos(list(actions.variant.combos or []))


def find_combo(actions: Actions, keys: Any) -> str | None:
    """``["k2", "k1"]`` -> ``"k1+k2"`` if that is a configured combination."""
    if not isinstance(keys, list) or len(keys) != 2:
        return None
    for combo in actions.variant.combos or []:
        if isinstance(combo, dict) and set(combo.get("keys") or ()) == set(keys) and keys[0] != keys[1]:
            order = {cid: i for i, cid in enumerate(key_ids(actions))}
            return combo_id(sorted(combo["keys"], key=lambda k: order.get(k, len(order))))
    return None


def ha_entries(actions: Actions) -> list[tuple[str, str, dict]]:
    """Every key action that targets Home Assistant entities: (key, event, spec).

    ``behaviour.entity_ids(spec)`` lists them (one ``entity``, or ``entities``)."""
    table = actions.variant.actions
    out = []
    for cid in key_ids(actions):
        for event in EVENTS:
            spec = table.get(f"{cid}.{event}")
            if spec and spec.get("do") in HA_VERBS and behaviour.entity_ids(spec):
                out.append((cid, event, dict(spec)))
    for combo in actions.variant.combos or []:
        spec = {k: v for k, v in combo.items() if k not in ("keys", "hold_s")}
        if spec.get("do") in HA_VERBS and behaviour.entity_ids(spec):
            out.append((combo_id(combo["keys"]), "combo", spec))
    return out


def remove_entries(actions: Actions, panel: Any, keys_file: Path | None, names: list[str]) -> None:
    """Clear some key actions ("k2.hold", …, "k1+k2.combo") through the normal
    save/apply path.

    The overlay holds every key's tap and hold, so the whole current key table
    is written with the named entries set to None - other keys keep whatever
    the owner chose. A named combination is removed; presets are untouched.
    """
    gone = set(names)
    combos = [c for c in actions.variant.combos or [] if f"{combo_id(c['keys'])}.combo" not in gone]
    table = {
        f"{cid}.{e}": (None if f"{cid}.{e}" in gone else actions.variant.actions.get(f"{cid}.{e}"))
        for cid in key_ids(actions)
        for e in EVENTS
    }
    specs = list(actions.variant.presets)
    if keys_file is not None:
        save(keys_file, specs, table, combos)
    apply(actions, panel, specs, table, combos)


def tune_repeat(actions: Actions, panel: Any) -> None:
    """A key whose hold steps the volume ramps while held; any other fires once."""
    if panel is None:
        return
    table = actions.variant.actions
    for control in getattr(panel, "controls", []):
        if isinstance(control, Momentary):
            hold = table.get(f"{control.id}.hold") or {}
            control.repeat_s = VOLUME_REPEAT_S if hold.get("do") == "volume_step" else None


_KEEP: Any = object()

KEYS_HEADER = ("# Written by the haslerd web page. Delete to go back to the built-in\n"
               "# stations (the starter set in force). `shipped` is what the release had on\n"
               "# the keys when this was saved: a key still equal to it follows the release.\n")


def save(
    path: Path, specs: list[dict], table: dict[str, dict | None], combos: Any = _KEEP, shipped: Any = _KEEP,
) -> None:
    """Write the overlay atomically (tmp + fsync + rename): the box is switched
    off at the wall, and a power cut mid-save must leave the old file.

    ``combos``: the key combinations to write; left out, the file keeps the
    ones it had (none written = the descriptor's apply). ``shipped``: the
    (presets, key table) the release ships, written as the ``shipped``
    snapshot with its ``defaults_rev``; left out, the file keeps its own;
    None writes none (a test, a file with no release to follow)."""
    if combos is _KEEP or shipped is _KEEP:
        file = read_file(path) or {}
        if combos is _KEEP:
            combos = file.get("combos") if isinstance(file.get("combos"), list) else None
        if shipped is _KEEP:
            old = file.get("shipped")
            shipped = ((old.get("presets") or [], old.get("actions") or {})
                       if isinstance(old, dict) and file.get("defaults_rev") else None)
    # schema, so a migration knows what it is reading.
    doc: dict[str, Any] = {"schema": KEYS_SCHEMA, "presets": specs, "actions": table}
    if combos is not None:
        doc["combos"] = combos
    if shipped is not None:
        shipped_specs, shipped_table = shipped
        doc["defaults_rev"] = starter_sets.layout_rev(shipped_specs, shipped_table)
        doc["shipped"] = {"presets": list(shipped_specs), "actions": {k: v for k, v in shipped_table.items() if v}}
    text = KEYS_HEADER + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    atomic_write_text(path, text, mode=0o600)


def read_file(path: Path | None) -> dict | None:
    """keys.yaml as a mapping, or None when absent or unreadable (logged)."""
    if path is None or not path.exists():
        return None
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        log.exception("could not read %s; using the descriptor's keys", path)
        return None
    return doc if isinstance(doc, dict) else {}


def load_shipped(path: Path | None) -> tuple[str, list[dict], dict] | None:
    """The file's ``shipped`` snapshot: (defaults_rev, presets, key table), or
    None when the file has none (saved before 1.5.0 and not yet migrated)."""
    doc = read_file(path)
    if not doc:
        return None
    rev, shipped = doc.get("defaults_rev"), doc.get("shipped")
    if not isinstance(rev, str) or not isinstance(shipped, dict):
        return None
    specs, table = shipped.get("presets"), shipped.get("actions")
    if not isinstance(specs, list) or not isinstance(table, dict):
        return None
    return rev, [s for s in specs if isinstance(s, dict)], dict(table)


def load(path: Path | None) -> tuple[list[dict], dict[str, dict | None]] | None:
    """The saved (presets, key table), or None - see load_layout."""
    saved = load_layout(path)
    return None if saved is None else (saved[0], saved[1])


def load_layout(path: Path | None) -> tuple[list[dict], dict[str, dict | None], list | None] | None:
    """The saved layout, or None (descriptor's keys) if absent or not a layout.
    Its third part is the key combinations, None when the file has none (the
    descriptor's then apply).

    A hand-edited or half-understood file must cost the saved keys, never the
    daemon: anything that isn't ``{presets: [...], actions: {...}}`` is logged
    and ignored.
    """
    if path is None or not path.exists():
        return None
    doc = read_file(path)
    if doc is None:
        return None
    presets, actions = doc.get("presets"), doc.get("actions")
    if not isinstance(presets or [], list) or not isinstance(actions or {}, dict):
        log.error("%s is not a key layout ({presets: [...], actions: {...}}); using the descriptor's keys", path)
        return None
    schema = doc.get("schema", KEYS_SCHEMA)
    if not isinstance(schema, int) or schema > KEYS_SCHEMA:
        log.warning("%s has schema %r, newer than this release's %d; reading what it can",
                    path, schema, KEYS_SCHEMA)
    combos = doc.get("combos")
    if combos is not None and not isinstance(combos, list):
        log.error("%s: combos is not a list; using the descriptor's key combinations", path)
        combos = None
    return list(presets or []), dict(actions or {}), combos


def apply_saved(
    path: Path | None, actions: Actions, panel: Any, shipped: tuple[list[dict], dict] | None = None,
) -> bool:
    """At startup: lay the saved overlay over the descriptor. True if there was one.

    ``shipped``: the (presets, key table) this release ships for the cabinet.
    When the file was saved against another release's (its ``defaults_rev``
    differs), every key still equal to that snapshot takes this release's
    (presets.follow_release), and the file is written once with the new
    snapshot. The owner's own keys are left as they are."""
    saved = load_layout(path)
    if saved is None:
        tune_repeat(actions, panel)
        tune_combos(actions, panel)
        return False
    specs, table, raw_combos = saved
    known = {f"{cid}.{e}" for cid in key_ids(actions) for e in EVENTS}
    if shipped is not None:
        old = load_shipped(path)
        new_specs, new_table = shipped
        new_table = {k: new_table.get(k) for k in known}
        if old is not None and old[0] != starter_sets.layout_rev(new_specs, new_table):
            specs, table, changed = starter_sets.follow_release(
                specs, table, old[1], old[2], new_specs, new_table, known)
            if changed:
                log.info("%s: %d untouched key(s) follow the release", path, changed)
            try:
                save(path, specs, table, raw_combos, (new_specs, new_table))
            except OSError:
                log.warning("could not write %s after following the release", path, exc_info=True)
    combos = None
    if raw_combos is not None:
        # A hand-edited combination that doesn't check out costs that one only.
        preset_ns = {s.get("n") for s in specs if isinstance(s, dict)}
        combos = []
        for i, raw in enumerate(raw_combos):
            errors: list = []
            ok = isinstance(raw, dict) and isinstance(raw.get("keys"), list)
            got = combos_from_page([combo_to_page(raw) if ok else raw], actions, preset_ns, errors,
                                   [raw] if ok else None)
            if errors:
                log.error("%s: ignoring key combination %d: %s", path, i, "; ".join(e["message"] for e in errors))
                continue
            if got and frozenset(got[0]["keys"]) in {frozenset(c["keys"]) for c in combos}:
                log.error("%s: ignoring key combination %d: that pair is there twice", path, i)
                continue
            combos.extend(got)
    apply(actions, panel, specs, {k: v for k, v in table.items() if k in known}, combos)
    log.info("keys from %s", path)
    return True
