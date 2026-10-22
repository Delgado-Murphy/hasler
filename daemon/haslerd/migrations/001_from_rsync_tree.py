"""001: from the rsync'd checkout in /home/pi/hasler to /etc/hasler.

Before OTA the bench box ran straight from a copy of the repo: its settings
were ``config/haslerd.yaml`` in that tree and its calibration was edited into
the shipped descriptor ``config/variants/<v>.yaml`` itself. This step:

1. compares that edited descriptor with the release's own and writes only the
   differences, by control id (curve, range, hysteresis, taper, end switch...)
   and ``io`` (MCP address, lamp palette/balance/min_duty), to
   ``/etc/hasler/variant.d/<v>.yaml``;
2. writes ``/etc/hasler/box.yaml``: schema, the variant, channel ``bench``,
   update mode ``auto``, a random box id, and only the haslerd.yaml settings
   that differ from the code defaults (the Home Assistant token goes to
   secrets/ha.json instead, if the page never saved one);
3. moves ``ha.json`` and the YouTube Music sign-in into
   ``/var/lib/haslerd/secrets`` (0700), leaving symlinks behind so a rollback
   to the old code still finds them;
4. stamps ``schema: 1`` into settings.json and keys.yaml.

Idempotent: what is already there wins (box.yaml keys, overlay values,
secrets), so running it twice changes nothing. Presets and key actions are
not carried over - the key page's keys.yaml already holds the owner's layout.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from ..boxconfig import (
    SCHEMA,
    Paths,
    atomic_write_text,
    deep_merge,
    env_flag,
    read_box_file,
    read_overlay,
    read_yaml,
    write_box_file,
    write_overlay,
)

AUTO = False         # moves files and writes /etc: the updater or a person runs it

LEGACY_DIR = Path("/home/pi/hasler")
UNIT = Path("/etc/systemd/system/haslerd.service")
DEFAULT_VARIANT = "hasler-hm"
SECRETS = ("ha.json", "ytm-cookies.txt", "ytm-browser.json")


# -- what differs -------------------------------------------------------------


def _same(a: Any, b: Any) -> bool:
    return a == b or (a in (None, "") and b in (None, ""))


def diff(base: Any, edited: Any, where: str = "") -> tuple[Any, list[str]]:
    """(what ``edited`` sets differently from ``base``, keys it dropped).

    Dicts recurse; lists and scalars are a whole value. A key ``edited`` lacks
    cannot be expressed in an overlay, so it is only reported.
    """
    if not (isinstance(base, dict) and isinstance(edited, dict)):
        return (None if _same(base, edited) else edited), []
    out: dict = {}
    dropped = [f"{where}{k}" for k in base if k not in edited]
    for key, value in edited.items():
        if key not in base:
            out[key] = value
            continue
        part, gone = diff(base[key], value, f"{where}{key}.")
        dropped += gone
        if part is not None and part != {}:
            out[key] = part
    return out, dropped


def variant_overlay(shipped: dict, edited: dict) -> tuple[dict, list[str]]:
    """The overlay that turns ``shipped`` into ``edited`` (+ notes)."""
    notes: list[str] = []
    by_id = {spec.get("id"): spec for spec in shipped.get("controls") or []}
    controls: dict[str, dict] = {}
    for spec in edited.get("controls") or []:
        cid = spec.get("id")
        if cid not in by_id:
            notes.append(f"control {cid!r} is not in the release's descriptor; not carried over")
            continue
        part, dropped = diff({k: v for k, v in by_id[cid].items() if k != "id"},
                             {k: v for k, v in spec.items() if k != "id"})
        if part:
            controls[cid] = part
        for key in dropped:
            notes.append(f"{cid}: {key} was removed on the box; the release's value stays")
    edited_ids = {spec.get("id") for spec in edited.get("controls") or []}
    for cid in by_id:
        if cid not in edited_ids:
            notes.append(f"control {cid!r} was removed on the box; it stays (overlays cannot remove)")
    io, dropped = diff(shipped.get("io") or {}, edited.get("io") or {})
    for key in dropped:
        notes.append(f"io.{key} was removed on the box; the release's value stays")
    for key in ("actions", "presets"):
        if not _same(shipped.get(key), edited.get(key)):
            notes.append(f"{key} differ from the release; not carried over (the key page's keys.yaml holds them)")
    return {"controls": controls, "io": io or {}}, notes


def defaults(paths: Paths) -> dict:
    """haslerd's code defaults, in haslerd.yaml's shape."""
    from ..player.moode import MoodeConfig

    return {
        "moode": dataclasses.asdict(MoodeConfig()),
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080},
        "connectivity": {"host": "1.1.1.1", "port": 53, "timeout_s": 2.0, "interval_s": 10.0},
        "state_file": str(paths.state / "state.json"),
        "keys_file": str(paths.state / "keys.yaml"),
    }


def settings_overrides(legacy: dict, paths: Paths) -> tuple[dict, dict | None, list[str]]:
    """(box.yaml override blocks, the home_assistant block or None, notes)."""
    base = defaults(paths)
    out: dict = {}
    notes: list[str] = []
    for key, value in legacy.items():
        if key == "home_assistant":
            continue
        if key not in base:
            out[key] = value
            notes.append(f"haslerd.yaml {key!r} is not a known setting; kept as it is")
            continue
        part, _ = diff(base[key], value)
        if part is not None and part != {}:
            out[key] = part
    ha = legacy.get("home_assistant")
    return out, (ha if isinstance(ha, dict) else None), notes


def _flat(doc: dict, where: str = "") -> list[str]:
    keys = []
    for key, value in doc.items():
        if isinstance(value, dict) and value:
            keys += _flat(value, f"{where}{key}.")
        else:
            keys.append(f"{where}{key}")
    return keys


# -- where things are ---------------------------------------------------------


def find_variant(legacy: Path, name: str | None, box: dict, unit: Path) -> tuple[str, Path]:
    """(variant name, the box's edited descriptor)."""
    name = name or box.get("variant")
    if name:
        return name, legacy / "config" / "variants" / f"{name}.yaml"
    try:
        match = re.search(r"--variant[=\s]+(\S+)", unit.read_text())
    except OSError:
        match = None
    if match:
        path = Path(match.group(1))
        try:
            name = (yaml.safe_load(path.read_text()) or {}).get("variant")
        except (OSError, yaml.YAMLError, AttributeError):
            name = None
        return name or path.stem, path
    return DEFAULT_VARIANT, legacy / "config" / "variants" / f"{DEFAULT_VARIANT}.yaml"


def _owner(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_uid, st.st_gid


def _chown(path: Path, owner: tuple[int, int] | None, link: bool = False) -> None:
    if owner is None:
        return
    try:
        (os.lchown if link else os.chown)(path, *owner)
    except OSError:
        pass


# -- the step -----------------------------------------------------------------


def up(
    paths: Paths, dry_run: bool = False, legacy: str | Path | None = None,
    variant: str | None = None, unit: str | Path | None = None,
) -> list[str]:
    legacy = Path(legacy or env_flag("HASLER_LEGACY") or LEGACY_DIR)
    unit = Path(unit or UNIT)
    will = "would " if dry_run else ""
    lines: list[str] = []
    box = read_box_file(paths)

    # 1. calibration overlay
    name, edited_path = find_variant(legacy, variant, box, unit)
    shipped_path = paths.shipped_variant(name)
    if not shipped_path.exists():
        raise FileNotFoundError(f"the release has no descriptor for {name!r} ({shipped_path})")
    if edited_path.exists() and edited_path.resolve() != shipped_path.resolve():
        overlay, notes = variant_overlay(read_yaml(shipped_path), read_yaml(edited_path))
        lines += notes
        existing = read_overlay(paths, name)
        merged = deep_merge(overlay, existing)            # what is there already wins
        if merged != existing:
            changed = _flat({"controls": merged["controls"], "io": merged["io"]})
            lines.append(f"{will}write {paths.overlay_file(name)}: {', '.join(changed)}")
            if not dry_run:
                write_overlay(paths, name, merged)
        elif overlay["controls"] or overlay["io"]:
            lines.append(f"{paths.overlay_file(name)} already holds the box's calibration")
        else:
            lines.append(f"{edited_path} matches the release's {name}: no calibration overlay needed")
    else:
        lines.append(f"no edited descriptor at {edited_path}: no calibration to carry over")

    # 2. box.yaml
    legacy_conf = read_yaml(legacy / "config" / "haslerd.yaml")
    overrides, ha_block, notes = settings_overrides(legacy_conf, paths)
    lines += notes
    fresh = {"schema": SCHEMA, "variant": name, "channel": "bench", "update_mode": "auto",
             "box_id": str(uuid.uuid4()), **overrides}
    new_box = deep_merge(fresh, box)                      # an existing box.yaml wins
    if new_box != box:
        added = [k for k in new_box if k not in box]
        detail = ", ".join(_flat({k: new_box[k] for k in added})) or "nothing new"
        lines.append(f"{will}write {paths.box_file}: {detail}")
        if not dry_run:
            write_box_file(paths, new_box)

    # 3. secrets
    owner = _owner(paths.state)
    for secret in SECRETS:
        src, dst = paths.state / secret, paths.secrets / secret
        if src.is_symlink() or not src.exists():
            continue
        if dst.exists():
            lines.append(f"both {src} and {dst} exist; keeping {dst}, {src} left alone")
            continue
        lines.append(f"{will}move {src} to {dst} (symlink left behind)")
        if not dry_run:
            _secrets_dir(paths, owner)
            os.replace(src, dst)
            os.symlink(Path("secrets") / secret, src)
            _chown(src, owner, link=True)
    has_ha = any((d / "ha.json").exists() or (d / "ha.json").is_symlink() for d in (paths.state, paths.secrets))
    if ha_block and ha_block.get("url") and ha_block.get("token") and not has_ha:
        lines.append(f"{will}move haslerd.yaml's Home Assistant connection to {paths.secrets / 'ha.json'}")
        if not dry_run:
            _secrets_dir(paths, owner)
            doc = {"url": str(ha_block["url"]), "token": str(ha_block["token"]),
                   "publish": bool(ha_block.get("publish", False))}
            atomic_write_text(paths.secrets / "ha.json", json.dumps(doc), mode=0o600)
            _chown(paths.secrets / "ha.json", owner)
    elif ha_block and ha_block.get("token"):
        lines.append("haslerd.yaml's Home Assistant token dropped: the page's ha.json already wins")

    # 4. schema in the web-written files
    lines += _stamp_json(paths.state / "settings.json", will, dry_run)
    lines += _stamp_yaml(paths.state / "keys.yaml", will, dry_run)
    return lines


def _secrets_dir(paths: Paths, owner: tuple[int, int] | None) -> None:
    if not paths.secrets.is_dir():
        paths.secrets.mkdir(mode=0o700, parents=True)
        _chown(paths.secrets, owner)
    os.chmod(paths.secrets, 0o700)


def _stamp_json(path: Path, will: str, dry_run: bool) -> list[str]:
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return [f"{path} unreadable; left alone"]
    if not isinstance(doc, dict) or "schema" in doc:
        return []
    if not dry_run:
        owner = _owner(path)
        atomic_write_text(path, json.dumps({"schema": SCHEMA, **doc}, indent=2, sort_keys=True) + "\n")
        _chown(path, owner)
    return [f"{will}add schema {SCHEMA} to {path}"]


def _stamp_yaml(path: Path, will: str, dry_run: bool) -> list[str]:
    try:
        text = path.read_text()
        doc = yaml.safe_load(text) or {}
    except FileNotFoundError:
        return []
    except (OSError, yaml.YAMLError):
        return [f"{path} unreadable; left alone"]
    if not isinstance(doc, dict) or "schema" in doc:
        return []
    if not dry_run:
        owner = _owner(path)
        header = "".join(line + "\n" for line in text.splitlines() if line.startswith("#"))
        body = yaml.safe_dump({"schema": SCHEMA, **doc}, sort_keys=False, allow_unicode=True)
        atomic_write_text(path, header + body)
        _chown(path, owner)
    return [f"{will}add schema {SCHEMA} to {path}"]
