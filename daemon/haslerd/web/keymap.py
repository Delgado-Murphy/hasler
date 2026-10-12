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
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..actions import Actions, build_presets
from ..panel.controls import Momentary
from ..player.base import Preset

log = logging.getLogger(__name__)

EVENTS = ("tap", "hold")
VOLUME_DELTA = 2          # knob units per step; ~1.8 dB on the Amp4 Pro
VOLUME_REPEAT_S = 0.15    # a held volume key ramps at this rate

# The menu, in the order the page shows it. ``params`` are the fields the page
# must collect for that choice.
VERBS: list[dict[str, Any]] = [
    {"do": "none", "label": "Nothing"},
    {"do": "preset", "label": "Play a preset", "params": ["n"]},
    {"do": "cycle_source", "label": "Next source (radio → Bluetooth → AirPlay → Spotify)"},
    {"do": "bt_pair", "label": "Bluetooth pairing", "params": ["seconds"]},
    {"do": "volume_up", "label": "Volume up (ramps while held)"},
    {"do": "volume_down", "label": "Volume down (ramps while held)"},
    {"do": "standby_toggle", "label": "Standby / wake"},
    {"do": "ha_scene", "label": "Home Assistant scene", "params": ["entity"]},
]
VERB_NAMES = {v["do"] for v in VERBS}

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
    if verb in ("standby_toggle", "cycle_source"):
        return {"do": verb}
    if verb == "preset":
        return {"do": "preset", "n": int(spec["n"])}
    if verb == "bt_pair":
        return {"do": "bt_pair", "seconds": int(spec.get("seconds", 120))}
    if verb == "ha_scene":
        return {"do": "ha_scene", "entity": str(spec.get("entity", ""))}
    # Something the page has no control for (set in the descriptor by hand):
    # shown, kept as-is on save.
    return {"do": "custom", "label": verb or "?", "spec": dict(spec)}


def preset_to_page(p: Preset) -> dict:
    return {"n": p.n, "name": p.name, "source": p.source, "uri": p.uri, "shuffle": p.shuffle}


def config_doc(actions: Actions, sources: dict[str, bool], ha: bool) -> dict:
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
        "ha": ha,
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


def preset_from_page(raw: Any, where: str, errors: list) -> dict | None:
    """Page preset -> descriptor-style spec (``{n, name, radio: …}``)."""
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
    if verb == "ha_scene":
        entity = str(raw.get("entity") or "").strip()
        if not re.fullmatch(r"scene\.[a-z0-9_]+", entity):
            errors.append({"where": where, "message": "a scene id looks like scene.living_room"})
            return None
        return {"do": "ha_scene", "entity": entity}
    return {"do": verb}         # cycle_source, standby_toggle


def parse(doc: Any, actions: Actions) -> tuple[list[dict], dict[str, dict | None]]:
    """A PUT body -> (preset specs, {"k1.tap": entry-or-None, …}). Raises Invalid."""
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
    if errors:
        raise Invalid(errors)
    return specs, table


# -- applying and persisting ------------------------------------------------


def apply(actions: Actions, panel: Any, specs: list[dict], table: dict[str, dict | None]) -> None:
    """Swap in the new layout. Safe against a panel loop running alongside."""
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
    tune_repeat(actions, panel)


def tune_repeat(actions: Actions, panel: Any) -> None:
    """A key whose hold steps the volume ramps while held; any other fires once."""
    if panel is None:
        return
    table = actions.variant.actions
    for control in getattr(panel, "controls", []):
        if isinstance(control, Momentary):
            hold = table.get(f"{control.id}.hold") or {}
            control.repeat_s = VOLUME_REPEAT_S if hold.get("do") == "volume_step" else None


def save(path: Path, specs: list[dict], table: dict[str, dict | None]) -> None:
    """Write the overlay atomically - a power cut mid-save leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"presets": specs, "actions": table}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keys-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("# Written by the haslerd web page. Delete to go back to the descriptor.\n")
            yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load(path: Path | None) -> tuple[list[dict], dict[str, dict | None]] | None:
    if path is None or not path.exists():
        return None
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        log.exception("could not read %s; using the descriptor's keys", path)
        return None
    return list(doc.get("presets") or []), dict(doc.get("actions") or {})


def apply_saved(path: Path | None, actions: Actions, panel: Any) -> bool:
    """At startup: lay the saved overlay over the descriptor. True if there was one."""
    saved = load(path)
    if saved is None:
        tune_repeat(actions, panel)
        return False
    specs, table = saved
    known = {f"{cid}.{e}" for cid in key_ids(actions) for e in EVENTS}
    apply(actions, panel, specs, {k: v for k, v in table.items() if k in known})
    log.info("keys from %s", path)
    return True
