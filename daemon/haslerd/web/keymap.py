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
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from ..actions import Actions, build_presets
from ..boxconfig import SCHEMA as KEYS_SCHEMA, atomic_write_text
from ..ha import behaviour
from ..panel.combos import COMBO_HOLD_RANGE, DEFAULT_COMBO_HOLD_S, MAX_COMBOS, combo_id
from ..panel.controls import Momentary
from ..player.base import Preset
from ..player.sources import ytm_refs

log = logging.getLogger(__name__)

EVENTS = ("tap", "hold")
VOLUME_DELTA = 2          # knob units per step; ~1.8 dB on the Amp4 Pro
VOLUME_REPEAT_S = 0.15    # a held volume key ramps at this rate

# The menu, in the order the page shows it. ``params`` are the fields the page
# must collect for that choice.
VERBS: list[dict[str, Any]] = [
    {"do": "none", "label": "Nothing"},
    {"do": "preset", "label": "Play a preset", "params": ["n"]},
    {"do": "cycle_source", "label": "Next source (order set in Settings → Sources & casting)"},
    {"do": "bt_pair", "label": "Bluetooth pairing", "params": ["seconds"]},
    # Selects Bluetooth and calls the last phone; while a phone plays, again =
    # next track and this key's hold = pause/play (Settings -> Bluetooth).
    {"do": "bluetooth", "label": "Bluetooth (again: next track, hold: pause)"},
    {"do": "volume_up", "label": "Volume up (ramps while held)"},
    {"do": "volume_down", "label": "Volume down (ramps while held)"},
    {"do": "standby_toggle", "label": "Standby / wake"},
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


def key_ids(actions: Actions) -> list[str]:
    """The cabinet's keys, left to right: its momentary controls."""
    return [c["id"] for c in actions.variant.controls if c.get("kind") == "momentary"]


def label_for(cid: str) -> str:
    return cid[1:] if re.fullmatch(r"k\d+", cid) else cid.upper()


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
    if verb in HA_VERBS:
        return behaviour.to_page(spec)
    # Something the page has no control for (set in the descriptor by hand):
    # shown, kept as-is on save.
    return {"do": "custom", "label": verb or "?", "spec": dict(spec)}


def preset_to_page(p: Preset) -> dict:
    """A preset for the page; a ytm reference adds its fields (kind, id, mode,
    seed, country, list, title, params - player/sources/ytm_refs.py)."""
    return {"n": p.n, "name": p.name, "source": p.source, "uri": p.uri, "shuffle": p.shuffle, **p.ytm_ref}


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
    """Keys that may be half of a combination: every key of the cabinet (its
    momentary controls). Interlocked banks, their release key and end switches
    are never keys here."""
    return key_ids(actions)


def combo_to_page(combo: dict) -> dict:
    """A stored combination -> the page's: ``{"keys", "hold_s", "do", ...params}``."""
    spec = {k: v for k, v in combo.items() if k not in ("keys", "hold_s")}
    action = to_page(spec) or {"do": "none"}
    return {
        "keys": list(combo.get("keys") or []),
        "hold_s": float(combo.get("hold_s", DEFAULT_COMBO_HOLD_S)),
        **action,
    }


def config_doc(actions: Actions, sources: dict[str, bool], ha: Any) -> dict:
    table = actions.variant.actions
    keys = []
    for cid in key_ids(actions):
        keys.append({
            "id": cid,
            "label": label_for(cid),
            "tap": to_page(table.get(f"{cid}.tap")),
            "hold": to_page(table.get(f"{cid}.hold")),
        })
    return {
        "variant": actions.variant.name,
        "keys": keys,
        "presets": [preset_to_page(p) for p in sorted(actions.presets.values(), key=lambda p: p.n)],
        "verbs": VERBS,
        "sources": sources,
        "ha": ha_connected(ha),
        "combos": [combo_to_page(c) for c in actions.variant.combos or [] if isinstance(c, dict)],
        "combo_keys": combo_keys(actions),
        "combo_limits": {"hold_s": list(COMBO_HOLD_RANGE), "default_hold_s": DEFAULT_COMBO_HOLD_S,
                         "max": MAX_COMBOS},
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


def preset_from_page(raw: Any, where: str, errors: list) -> dict | None:
    """Page preset -> descriptor-style spec (``{n, name, radio: …}``).

    A ytm preset with a ``kind`` keeps its reference beside the link:
    ``{n, name, ytm: <link or "">, kind, id, …, shuffle}`` (ytm_ref_from_page).
    """
    if not isinstance(raw, dict):
        errors.append({"where": where, "message": "not a preset"})
        return None
    try:
        n = int(raw.get("n"))
    except (TypeError, ValueError):
        errors.append({"where": f"{where}.n", "message": "missing preset number"})
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
        return spec
    uri = normalise_uri(source, str(raw.get("uri") or ""))
    problem = check_uri(source, uri)
    if problem:
        errors.append({"where": f"{where}.uri", "message": problem})
        return None
    name = str(raw.get("name") or "").strip() or f"Preset {n}"
    spec: dict[str, Any] = {"n": n, "name": name[:60], source: uri}
    if source != "radio" and raw.get("shuffle"):
        spec["shuffle"] = True
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
                           f"{label_for(keys[0])} + {label_for(keys[1])} is already a combination"})
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
    table: dict[str, dict | None] = {}
    for i, raw in enumerate(doc.get("keys") or []):
        cid = raw.get("id") if isinstance(raw, dict) else None
        if cid not in known:
            errors.append({"where": f"keys[{i}]", "message": f"no key called {cid!r}"})
            continue
        for event in EVENTS:
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


def save(path: Path, specs: list[dict], table: dict[str, dict | None], combos: Any = _KEEP) -> None:
    """Write the overlay atomically (tmp + fsync + rename): the box is switched
    off at the wall, and a power cut mid-save must leave the old file.

    ``combos``: the key combinations to write; left out, the file keeps the
    ones it had (none written = the descriptor's apply)."""
    if combos is _KEEP:
        saved = load_layout(path)
        combos = saved[2] if saved is not None else None
    # schema as boxconfig writes it, so a migration knows what it is reading.
    doc: dict[str, Any] = {"schema": KEYS_SCHEMA, "presets": specs, "actions": table}
    if combos is not None:
        doc["combos"] = combos
    text = "# Written by the haslerd web page. Delete to go back to the descriptor.\n" + \
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    atomic_write_text(path, text, mode=0o600)


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
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        log.exception("could not read %s; using the descriptor's keys", path)
        return None
    presets, actions = (doc.get("presets"), doc.get("actions")) if isinstance(doc, dict) else (None, None)
    if not isinstance(doc, dict) or not isinstance(presets or [], list) or not isinstance(actions or {}, dict):
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


def apply_saved(path: Path | None, actions: Actions, panel: Any) -> bool:
    """At startup: lay the saved overlay over the descriptor. True if there was one."""
    saved = load_layout(path)
    if saved is None:
        tune_repeat(actions, panel)
        tune_combos(actions, panel)
        return False
    specs, table, raw_combos = saved
    known = {f"{cid}.{e}" for cid in key_ids(actions) for e in EVENTS}
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
