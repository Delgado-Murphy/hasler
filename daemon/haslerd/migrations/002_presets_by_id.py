"""002: keys.yaml presets by collection id, and a snapshot of what was shipped.

Before 1.5.0 the keys were copied into every cabinet descriptor by URL, and a
keys.yaml held only what the owner saved, so a release that fixed a stream or
improved the built-in stations never reached a radio whose owner had changed
one key. From 1.5.0 (haslerd/presets.py, web/keymap.py):

1. a radio preset whose URL matches a station of the shipped collection is
   rewritten to ``station: <id>`` with the collection's URL (one spelling)
   beside it, so the weekly check and a later fix cover the key;
2. the file gets ``defaults_rev`` and ``shipped``: what the release had on the
   keys when the owner saved - for an old file, the 1.4 built-in set the
   descriptors shipped, by id now, and the descriptor's key table. At the next
   start every key still equal to that follows the release (keymap.apply_saved);
3. ``schema`` becomes keymap.KEYS_SCHEMA (2).

Only keys.yaml changes (settings.json keeps its schema). Idempotent: a file
that already has ``defaults_rev`` is left alone. AUTO: nothing moves and no
root is needed, so haslerd runs it at startup.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from ..boxconfig import Paths, atomic_write_text, read_box_file

AUTO = True

DEFAULT_VARIANT = "hasler-hm"
# What hasler-hm.yaml, hasler-hm-hat.yaml and excelsior.yaml shipped on the
# keys up to 1.4.x (key 7 = the Excelsior's Tape). Historical, on purpose.
SHIPPED_1_4 = [
    {"n": 1, "name": "Radio Swiss Classic", "radio": "http://stream.srg-ssr.ch/m/rsc_de/mp3_128"},
    {"n": 2, "name": "Radio Swiss Jazz", "radio": "http://stream.srg-ssr.ch/m/rsj/mp3_128"},
    {"n": 3, "name": "RNE Radio 3", "radio": "https://rtvelivestream.rtve.es/rtvesec/rne/rne_r3_main.m3u8"},
    {"n": 4, "name": "Today's Top Hits", "spotify": "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", "shuffle": True},
    {"n": 5, "name": "Famous classical", "ytm": "https://music.youtube.com/playlist?list=PL68AC80CBF3649BBB"},
    {"n": 6, "name": "Classical shuffle", "ytm": "https://music.youtube.com/playlist?list=PL68AC80CBF3649BBB",
     "shuffle": True},
    {"n": 7, "name": "Radio Swiss Pop", "radio": "http://stream.srg-ssr.ch/m/rsp/mp3_128"},
]


def _url_key(url: str) -> str:
    """The same stream over http and https, or with a trailing slash, is one
    (web/stations.py ``_key``)."""
    return str(url).strip().split("://", 1)[-1].rstrip("/").lower()


def by_id(spec: dict, stations: dict[str, dict]) -> tuple[dict, bool]:
    """``spec`` with ``station`` + the collection's URL when its link is a
    station of the collection; (spec, False) otherwise."""
    if not isinstance(spec, dict) or spec.get("station"):
        return spec, False
    url = spec.get("radio") or spec.get("url")
    if not url or spec.get("spotify") or spec.get("ytm"):
        return spec, False
    key = _url_key(url)
    for sid, station in stations.items():
        if _url_key(station["url"]) == key:
            out = {k: v for k, v in spec.items() if k not in ("radio", "url")}
            out["station"] = sid
            out["radio"] = str(station["url"])
            return out, out != spec
    return spec, False


def _keys_file(paths: Paths, box: dict) -> Path:
    name = box.get("keys_file") if isinstance(box.get("keys_file"), str) else None
    return Path(name) if name else paths.state / "keys.yaml"


def _slots(paths: Paths, variant: str) -> int:
    """How many keys hold music on this cabinet (7 for the Excelsior)."""
    try:
        from ..variant import load_variant   # noqa: PLC0415

        v = load_variant(paths.shipped_variant(variant))
        return v.slots or len(v.presets) or 6
    except Exception:
        return 7 if variant == "excelsior" else 6


def _known_table(paths: Paths, variant: str) -> dict:
    """The descriptor's key table for its assignable keys (the snapshot's actions)."""
    try:
        from ..actions import Actions           # noqa: PLC0415
        from ..player.base import FakePlayer    # noqa: PLC0415
        from ..variant import load_variant      # noqa: PLC0415
        from ..web import keymap                # noqa: PLC0415

        actions = Actions(load_variant(paths.shipped_variant(variant)), FakePlayer())
        known = {f"{cid}.{e}" for cid in keymap.key_ids(actions) for e in keymap.EVENTS}
        return {k: copy.deepcopy(v) for k, v in actions.variant.actions.items() if k in known and v}
    except Exception:
        return {}


def up(paths: Paths, dry_run: bool = False) -> list[str]:
    from .. import presets as starter_sets      # noqa: PLC0415
    from ..web import keymap                    # noqa: PLC0415

    will = "would " if dry_run else ""
    box = read_box_file(paths)
    path = _keys_file(paths, box)
    try:
        text = path.read_text()
        doc = yaml.safe_load(text) or {}
    except FileNotFoundError:
        return [f"no {path}: nothing to migrate"]
    except (OSError, yaml.YAMLError):
        return [f"{path} unreadable; left alone"]
    if not isinstance(doc, dict) or not isinstance(doc.get("presets") or [], list):
        return [f"{path} is not a key layout; left alone"]
    if doc.get("defaults_rev"):
        return [f"{path} already has defaults_rev"]

    stations = starter_sets.collection(paths.release)
    lines: list[str] = []
    presets: list[Any] = []
    for spec in doc.get("presets") or []:
        new, changed = by_id(spec, stations)
        if changed:
            lines.append(f"{will}write preset {new['n']} as station {new['station']}")
        presets.append(new)
    variant = str(box.get("variant") or DEFAULT_VARIANT)
    slots = _slots(paths, variant)
    shipped = [by_id(s, stations)[0] for s in SHIPPED_1_4 if s["n"] <= slots]
    table = _known_table(paths, variant)
    rev = starter_sets.layout_rev(shipped, table)
    lines.append(f"{will}stamp {path} with defaults_rev {rev} (the 1.4 built-in stations) and schema {keymap.KEYS_SCHEMA}")
    if not dry_run:
        out = {"schema": keymap.KEYS_SCHEMA}
        out.update({k: v for k, v in doc.items() if k not in ("schema", "presets", "defaults_rev", "shipped")})
        out["presets"] = presets
        out["defaults_rev"] = rev
        out["shipped"] = {"presets": shipped, "actions": table}
        try:
            st = path.stat()
            owner: tuple[int, int] | None = (st.st_uid, st.st_gid)
        except OSError:
            owner = None
        atomic_write_text(path, keymap.KEYS_HEADER + yaml.safe_dump(out, sort_keys=False, allow_unicode=True),
                          mode=0o600)
        if owner is not None:
            try:
                os.chown(path, *owner)
            except OSError:
                pass
    return lines
