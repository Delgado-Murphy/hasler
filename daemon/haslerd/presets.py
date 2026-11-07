"""Starter sets: the stations and playlists a radio's keys ship with.

A cabinet descriptor says only how many keys hold music (``presets: {slots:
6}``; the Excelsior has seven, the seventh being Tape). *What* they hold comes
from a starter set in ``config/presets/<pack>.yaml``::

    pack: eclectic
    title: "Eclectic"
    blurb: "A little of everything ..."
    presets:
      - {n: 1, name: "FIP", short: "FIP", station: fr-fip, why: "..."}
      - {n: 3, name: "Coffee Shop Blend", short: "Coffee Shop", ytm: RDCLAK5uy_..., why: "..."}

Each entry names exactly one of ``station`` (an id in
config/stations/collection.json - the stream URL has one spelling, the
collection's, and the weekly check covers the keys too), ``ytm`` (a YouTube
Music list id, the ``list=`` of the link config/catalog/catalog.json stores),
or, for a hand-written set, a plain ``radio``/``spotify``/``ytm`` link as a
descriptor preset would. Resolving a set gives ordinary preset specs
(``{n, name, short, station: fr-fip, radio: https://...}``): the resolved link
stays beside the id, as a ytm reference keeps the link it last resolved to,
so a key still plays when an id is gone from a later collection.

Which set a radio uses: ``box.yaml starter:`` (hasler-install writes
``eclectic``), overridden by ``settings.json starter`` when the owner chose
one on the key page ("Start from a set"). ``DEFAULT_PACK`` when neither says.
"Reset keys to defaults" resets to that set.

``layout_rev`` names a shipped layout (the set's presets and the descriptor's
key table) by content; keys.yaml carries it as ``defaults_rev`` beside a
snapshot of what was shipped at save time, so a later release can tell an
untouched key from an owner's choice (web/keymap.py ``apply_saved``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_PACK = "eclectic"
# The order the page lists the sets in; anything else comes after, by name.
PACK_ORDER = ("eclectic", "classical", "jazz", "pop", "raices", "family")
PACK_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SHORT_MAX = 12                  # a key-cap label (web/keymap.py SHORT_MAX)
ENTRY_SOURCES = ("station", "ytm", "radio", "spotify", "url")
LIST_ID = re.compile(r"^[A-Za-z0-9_-]{2,64}$")

PRESETS_DIR = "config/presets"
COLLECTION_FILE = "config/stations/collection.json"
CATALOG_FILE = "config/catalog/catalog.json"


def find_release(start: Path) -> Path | None:
    """The nearest directory at or above ``start`` holding config/presets."""
    start = Path(start).resolve()
    for candidate in (start, *start.parents):
        if (candidate / PRESETS_DIR).is_dir():
            return candidate
    return None


def package_release() -> Path | None:
    """The release this package runs from (the checkout root, or a release's app/)."""
    from .boxconfig import Paths          # noqa: PLC0415 - boxconfig imports nothing from here

    try:
        release = Paths.from_env().release
    except Exception:
        return None
    return release if (release / PRESETS_DIR).is_dir() else find_release(release)


# -- the collection and the catalogue ------------------------------------------

_cache: dict[tuple[str, Path], tuple[float, dict]] = {}


def _indexed(kind: str, path: Path, build) -> dict:
    """``build(doc)`` for the JSON at ``path``, cached by its mtime."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    hit = _cache.get((kind, path))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("could not read %s", path, exc_info=True)
        return {}
    index = build(doc)
    _cache[(kind, path)] = (mtime, index)
    return index


def _stations_by_id(doc: Any) -> dict[str, dict]:
    stations = doc.get("stations") if isinstance(doc, dict) else None
    return {str(s["id"]): s for s in stations or [] if isinstance(s, dict) and s.get("id") and s.get("url")}


def _lists_by_id(doc: Any) -> dict[str, dict]:
    """Catalogue YouTube Music items by the ``list=`` of their link (first wins)."""
    out: dict[str, dict] = {}
    sections = doc.get("sections") if isinstance(doc, dict) else None
    for section in sections or []:
        for item in (section.get("items") or []) if isinstance(section, dict) else []:
            if not isinstance(item, dict) or item.get("source") != "ytm":
                continue
            m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", str(item.get("uri") or ""))
            if m and m.group(1) not in out:
                out[m.group(1)] = item
    return out


def collection(release: Path | None = None) -> dict[str, dict]:
    """The shipped station collection by id ({} when the release has none)."""
    release = release or package_release()
    return _indexed("stations", release / COLLECTION_FILE, _stations_by_id) if release else {}


def catalogue(release: Path | None = None) -> dict[str, dict]:
    """The shipped Browse catalogue's YouTube Music items by list id."""
    release = release or package_release()
    return _indexed("catalog", release / CATALOG_FILE, _lists_by_id) if release else {}


def station_url(station_id: Any, release: Path | None = None) -> str | None:
    """The collection's stream URL for ``station_id``, or None."""
    if not isinstance(station_id, str) or not station_id:
        return None
    entry = collection(release).get(station_id)
    return str(entry["url"]) if entry else None


def resolve_station(spec: dict, release: Path | None = None) -> dict:
    """A preset spec with ``station`` -> the same spec with ``radio`` = the
    collection's URL (its one spelling). An id the collection no longer has
    keeps the link stored beside it; with no link either, ValueError.
    Specs without ``station`` come back untouched."""
    sid = spec.get("station") if isinstance(spec, dict) else None
    if not sid:
        return spec
    url = station_url(sid, release)
    if url:
        out = {k: v for k, v in spec.items() if k != "url"}
        out["radio"] = url
        return out
    if spec.get("radio") or spec.get("url"):
        log.debug("preset %s: station %r is not in the collection; playing its stored link", spec.get("n"), sid)
        return spec
    raise ValueError(f"preset {spec.get('n')} names station {sid!r}, which the collection doesn't have")


# -- the sets --------------------------------------------------------------------


@dataclass(frozen=True)
class Pack:
    name: str
    title: str
    blurb: str
    entries: tuple[dict, ...]       # as written, by n ascending (``why`` included)
    path: Path | None = None


def _check_entry(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: an entry is a mapping")
    try:
        n = int(raw["n"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"{where}: entry without a key number n") from None
    given = [k for k in ENTRY_SOURCES if raw.get(k)]
    if len(given) != 1:
        raise ValueError(f"{where}: key {n} must name exactly one of {', '.join(ENTRY_SOURCES)}")
    name = " ".join(str(raw.get("name") or "").split())
    if not name:
        raise ValueError(f"{where}: key {n} has no name")
    short = " ".join(str(raw.get("short") or "").split())
    if len(short) > SHORT_MAX:
        raise ValueError(f"{where}: key {n}: short label over {SHORT_MAX} characters")
    if given[0] == "ytm" and not (LIST_ID.match(str(raw["ytm"])) or str(raw["ytm"]).startswith("http")):
        raise ValueError(f"{where}: key {n}: ytm is a list id or a music.youtube.com link")
    out = {"n": n, "name": name[:60], "short": short, given[0]: str(raw[given[0]]).strip(),
           "why": " ".join(str(raw.get("why") or "").split())}
    if raw.get("shuffle"):
        out["shuffle"] = True
    return out


def load_pack(path: Path) -> Pack:
    """Read and check one set. Raises ValueError (a broken file is a release bug)."""
    path = Path(path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: not a starter set")
    name = str(doc.get("pack") or path.stem)
    if not PACK_NAME.match(name) or name != path.stem:
        raise ValueError(f"{path}: pack must be {path.stem!r} (the file's name)")
    entries = []
    seen: set[int] = set()
    for raw in doc.get("presets") or []:
        entry = _check_entry(raw, str(path))
        if entry["n"] in seen:
            raise ValueError(f"{path}: key {entry['n']} twice")
        seen.add(entry["n"])
        entries.append(entry)
    if not entries:
        raise ValueError(f"{path}: no presets")
    entries.sort(key=lambda e: e["n"])
    return Pack(name=name, title=" ".join(str(doc.get("title") or name).split()),
                blurb=" ".join(str(doc.get("blurb") or "").split()), entries=tuple(entries), path=path)


def load_packs(directory: Path) -> dict[str, Pack]:
    """Every set in ``directory``, in page order. A broken file costs that set only."""
    found: dict[str, Pack] = {}
    directory = Path(directory)
    if not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.yaml")):
        try:
            pack = load_pack(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.error("ignoring starter set %s: %s", path.name, exc)
            continue
        found[pack.name] = pack
    rank = {name: i for i, name in enumerate(PACK_ORDER)}
    return dict(sorted(found.items(), key=lambda kv: (rank.get(kv[0], len(rank)), kv[0])))


def resolve_entry(entry: dict, stations: dict[str, dict], lists: dict[str, dict]) -> dict:
    """A set's entry -> a preset spec (``why`` dropped). ValueError when the
    id is in neither the collection nor the catalogue."""
    n = entry["n"]
    spec: dict[str, Any] = {"n": n, "name": entry["name"]}
    if entry.get("short"):
        spec["short"] = entry["short"]
    if entry.get("station"):
        station = stations.get(entry["station"])
        if station is None:
            raise ValueError(f"key {n}: station {entry['station']!r} is not in the collection")
        spec["station"] = entry["station"]
        spec["radio"] = str(station["url"])
    elif entry.get("ytm") and not str(entry["ytm"]).startswith("http"):
        item = lists.get(entry["ytm"])
        if item is None:
            raise ValueError(f"key {n}: YouTube Music list {entry['ytm']!r} is not in the catalogue")
        spec["ytm"] = str(item["uri"])
    else:
        for key in ("radio", "spotify", "ytm", "url"):
            if entry.get(key):
                spec["radio" if key == "url" else key] = str(entry[key])
    if entry.get("shuffle"):
        spec["shuffle"] = True
    return spec


def resolve_pack(pack: Pack, slots: int, stations: dict[str, dict], lists: dict[str, dict]) -> list[dict]:
    """The set's preset specs for a cabinet with ``slots`` keys (keys past the
    last slot are left out). An entry that doesn't resolve costs that key."""
    out = []
    for entry in pack.entries:
        if slots and entry["n"] > slots:
            continue
        try:
            out.append(resolve_entry(entry, stations, lists))
        except ValueError as exc:
            log.error("starter set %s: %s", pack.name, exc)
    return out


class Starters:
    """The sets a release ships, resolved for one cabinet.

    ``release`` is where config/presets, the collection and the catalogue
    live; ``slots`` how many keys the cabinet gives to music.
    """

    def __init__(self, release: Path | None, slots: int) -> None:
        self.release = Path(release) if release else None
        self.slots = int(slots or 0)
        self.packs: dict[str, Pack] = load_packs(self.release / PRESETS_DIR) if self.release else {}

    @classmethod
    def near(cls, path: Path, slots: int) -> "Starters":
        """For a descriptor at ``path``: the release holding it (none: no sets)."""
        return cls(find_release(Path(path).parent), slots)

    def __bool__(self) -> bool:
        return bool(self.packs)

    @property
    def default(self) -> str | None:
        if DEFAULT_PACK in self.packs:
            return DEFAULT_PACK
        return next(iter(self.packs), None)

    def names(self) -> list[str]:
        return list(self.packs)

    def pack(self, name: str) -> Pack | None:
        return self.packs.get(name)

    def chosen(self, box_starter: Any, settings: Any) -> str | None:
        """The set in force: the page's (settings.json ``starter``) over
        box.yaml's, over the default. An unknown name is a log line, not a
        silent fallback that nobody can see."""
        page = settings.get("starter") if isinstance(settings, dict) else None
        for name, where in ((page, "settings.json"), (box_starter, "box.yaml")):
            if name in (None, ""):
                continue
            if isinstance(name, str) and name in self.packs:
                return name
            log.warning("%s names starter set %r, which this release doesn't have", where, name)
        return self.default

    def source(self, box_starter: Any, settings: Any) -> str:
        """Where the set in force came from: "page", "box" or "default"."""
        page = settings.get("starter") if isinstance(settings, dict) else None
        if isinstance(page, str) and page in self.packs:
            return "page"
        if isinstance(box_starter, str) and box_starter in self.packs:
            return "box"
        return "default"

    def specs(self, name: str | None) -> list[dict]:
        """The set's preset specs for this cabinet ([] for a set we don't have)."""
        pack = self.packs.get(name or "")
        if pack is None:
            return []
        return resolve_pack(pack, self.slots, collection(self.release), catalogue(self.release))

    def doc(self) -> list[dict]:
        """For the page: every set, its keys as it would put them on this cabinet."""
        out = []
        for pack in self.packs.values():
            keys = []
            for entry in pack.entries:
                if self.slots and entry["n"] > self.slots:
                    continue
                source = "radio" if entry.get("station") or entry.get("radio") or entry.get("url") else \
                    "spotify" if entry.get("spotify") else "ytm"
                keys.append({"n": entry["n"], "name": entry["name"], "short": entry.get("short", ""),
                             "source": source, "why": entry.get("why", "")})
            out.append({"id": pack.name, "title": pack.title, "blurb": pack.blurb,
                        "default": pack.name == self.default, "keys": keys})
        return out


def default_specs(variant_path: Path, slots: int, pack: str | None = None) -> list[dict]:
    """The default set's presets for a descriptor at ``variant_path`` (for
    ``variant.load_variant``): [] when no release with sets holds the file."""
    starters = Starters.near(variant_path, slots)
    if not starters:
        return []
    name = pack if pack in starters.packs else starters.default
    return starters.specs(name)


def box_starter(paths: Any) -> str:
    """box.yaml's ``starter`` ("" when absent or unreadable)."""
    try:
        from .boxconfig import read_box_file   # noqa: PLC0415

        value = read_box_file(paths).get("starter")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


# -- comparing layouts -------------------------------------------------------------


def _preset_key(spec: dict) -> tuple:
    """What makes two preset specs the same key's worth of music."""
    from .player.base import Preset        # noqa: PLC0415

    try:
        p = Preset.from_spec(spec)
    except ValueError:
        return ("?", json.dumps(spec, sort_keys=True, default=str))
    return (p.n, p.name, p.short, p.source, p.station, p.uri, p.shuffle, p.ref)


def same_preset(a: Any, b: Any) -> bool:
    """Two preset specs mean the same thing. A station is compared by its id
    when both carry one (a changed stream URL is not the owner's doing)."""
    if not (isinstance(a, dict) and isinstance(b, dict)):
        return a == b
    ka, kb = _preset_key(a), _preset_key(b)
    if ka[0] == "?" or kb[0] == "?":
        return ka == kb
    if ka[4] and kb[4]:
        return ka[:5] == kb[:5] and ka[6:] == kb[6:]
    return ka[:4] == kb[:4] and ka[5:] == kb[5:]


def same_layout(specs: list[dict], table: dict, shipped_specs: list[dict], shipped_table: dict,
                known: set[str] | None = None) -> bool:
    """The layout on the keys equals the shipped one: every preset by content
    (``same_preset``) and every key event in ``known`` (all of ``table`` when
    None). Content, not whether keys.yaml exists."""
    mine = {s.get("n"): s for s in specs if isinstance(s, dict)}
    theirs = {s.get("n"): s for s in shipped_specs if isinstance(s, dict)}
    if set(mine) != set(theirs) or not all(same_preset(mine[n], theirs[n]) for n in mine):
        return False
    names = known if known is not None else set(table) | set(shipped_table)
    return all((table.get(name) or None) == (shipped_table.get(name) or None) for name in names)


def layout_rev(specs: list[dict], table: dict) -> str:
    """A short hash naming a shipped layout by content (``defaults_rev``)."""
    raw = json.dumps({"presets": [list(_preset_key(s)) for s in specs if isinstance(s, dict)],
                      "actions": {k: v for k, v in sorted(table.items()) if v}},
                     sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def follow_release(
    specs: list[dict], table: dict, old_specs: list[dict], old_table: dict,
    new_specs: list[dict], new_table: dict, known: set[str],
) -> tuple[list[dict], dict, int]:
    """"Untouched keys follow the release": every saved preset or key action
    that still equals what was shipped when it was saved (``old_*``) becomes
    what this release ships (``new_*``); the owner's own choices are left
    alone by construction. A key the old set didn't have and the owner never
    filled takes the new set's. Returns (specs, table, how many changed)."""
    by_old = {s.get("n"): s for s in old_specs if isinstance(s, dict)}
    by_new = {s.get("n"): s for s in new_specs if isinstance(s, dict)}
    out_specs: list[dict] = []
    changed = 0
    seen: set = set()
    for spec in specs:
        n = spec.get("n") if isinstance(spec, dict) else None
        seen.add(n)
        if n in by_old and n in by_new and same_preset(spec, by_old[n]):
            if not same_preset(spec, by_new[n]):
                changed += 1
            out_specs.append(copy.deepcopy(by_new[n]))
        else:
            out_specs.append(spec)
    for n, spec in by_new.items():
        if n not in seen and n not in by_old:
            out_specs.append(copy.deepcopy(spec))
            changed += 1
    out_specs.sort(key=lambda s: (s.get("n") is None, s.get("n")))
    out_table = dict(table)
    for name in known:
        if (table.get(name) or None) == (old_table.get(name) or None):
            new = new_table.get(name) or None
            if (table.get(name) or None) != new:
                changed += 1
            out_table[name] = copy.deepcopy(new)
    return out_specs, out_table, changed
