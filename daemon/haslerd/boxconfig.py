"""Where a Hasler keeps what is *its own* - separate from what a release ships.

docs/ota.md section 1. A release (code, shipped variants, defaults) is
read-only and replaced wholesale by an update; everything a person chose for
this particular box lives outside it and is never overwritten:

* ``/etc/hasler/box.yaml`` - schema, variant name, channel, update mode, and
  only the haslerd settings that differ from defaults;
* ``/etc/hasler/variant.d/<variant>.yaml`` - the **calibration overlay**:
  per-control overrides keyed by control id (pot curves, hysteresis, range,
  direction, end-switch tweaks), plus ``io`` overrides (MCP address, lamp
  palette/balance). The knob calibration page writes here;
* ``/var/lib/haslerd`` - web-written settings and runtime state;
* ``/var/lib/haslerd/secrets`` (0700) - tokens, cookies, account files.

Merge rule (later wins): dicts deep-merged, lists replaced whole, and the
variant's ``controls`` list merged **by id** (an overlay entry for an id that
the shipped descriptor doesn't have is an error, not a new control).

This module is the shared contract for the config, knob and OTA work. Keep the
public names and signatures; implement the bodies.
"""

from __future__ import annotations

import copy
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

SCHEMA = 1   # of box.yaml and the overlay files; bumped with a migration

# The box.yaml keys that are not haslerd settings: the updater's and the
# migrations' own. Everything else in box.yaml is a haslerd override block in
# the shape the old config/haslerd.yaml had (moode:, web:, connectivity: ...).
BOX_KEYS = ("schema", "variant", "channel", "update_mode", "box_id", "checkin", "metered", "migrations")
# The haslerd settings box.yaml may override (code defaults otherwise).
HASLERD_KEYS = ("moode", "web", "connectivity", "home_assistant", "state_file", "keys_file")

OVERLAY_GROUP = "pi"     # haslerd's user: the calibration page writes variant.d
PACKAGE_DIR = Path(__file__).resolve().parent


def find_release(package_dir: Path = PACKAGE_DIR) -> Path:
    """The release root: the nearest directory above the haslerd package that
    holds ``config/variants``. In the repo (or the old rsync tree) that is the
    checkout root (``daemon/haslerd`` -> ``.``); in a release tarball it is
    ``app/`` (``app/haslerd`` beside ``app/config``)."""
    for candidate in (package_dir.parent, *package_dir.parent.parents):
        if (candidate / "config" / "variants").is_dir():
            return candidate
    return package_dir.parents[1]


@dataclass(frozen=True)
class Paths:
    """Every location haslerd reads or writes. Overridable for tests and for
    the bench box before it is migrated (``HASLER_ETC``, ``HASLER_STATE``,
    ``HASLER_RELEASE``)."""

    release: Path            # the running release's root (shipped data lives here)
    etc: Path = Path("/etc/hasler")
    state: Path = Path("/var/lib/haslerd")

    @property
    def secrets(self) -> Path:
        return self.state / "secrets"

    @property
    def box_file(self) -> Path:
        return self.etc / "box.yaml"

    def overlay_file(self, variant: str) -> Path:
        return self.etc / "variant.d" / f"{variant}.yaml"

    def shipped_variant(self, variant: str) -> Path:
        """The release's own descriptor for ``variant`` (never written)."""
        if not variant or "/" in variant or variant.startswith("."):
            raise ValueError(f"not a variant name: {variant!r}")
        return self.data("variants") / f"{variant}.yaml"

    def data(self, relative: str) -> Path:
        """Something the release ships under ``config/`` (variants, catalog,
        stations)."""
        return self.release / "config" / relative

    @classmethod
    def from_env(cls, release: Path | None = None) -> "Paths":
        """Defaults, overridden by HASLER_ETC / HASLER_STATE / HASLER_RELEASE."""
        env_release = env_flag("HASLER_RELEASE")
        root = Path(env_release) if env_release else (Path(release) if release else find_release())
        return cls(
            release=root,
            etc=Path(env_flag("HASLER_ETC") or cls.etc),
            state=Path(env_flag("HASLER_STATE") or cls.state),
        )


def shipped_data(relative: str) -> Path:
    """``Paths.from_env().data(relative)``, for module-level constants."""
    return Paths.from_env().data(relative)


def secret_file(state_dir: str | Path, name: str) -> Path:
    """Where a secret (``ha.json``, YTM cookies...) lives under ``state_dir``.

    ``<state>/secrets/<name>`` once that exists, or once the secrets directory
    does (a migrated or fresh box); otherwise the pre-OTA ``<state>/<name>``,
    so the bench box keeps working until migration 001 moves it.
    """
    state_dir = Path(state_dir)
    secret, legacy = state_dir / "secrets" / name, state_dir / name
    if secret.exists():
        return secret
    if legacy.exists():
        return legacy
    return secret if secret.parent.is_dir() else legacy


# -- merging ------------------------------------------------------------------


def deep_merge(base: Any, over: Any) -> Any:
    """Return ``base`` with ``over`` applied: dicts recursively, anything else
    (lists included) replaced. Never mutates its arguments."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = {k: copy.deepcopy(v) for k, v in base.items()}
        for key, value in over.items():
            out[key] = deep_merge(base[key], value) if key in base else copy.deepcopy(value)
        return out
    return copy.deepcopy(over)


def apply_patch(base: dict, patch: dict) -> dict:
    """``deep_merge`` where a None in ``patch`` deletes the key, and dicts the
    deletions leave empty are dropped. Never mutates its arguments."""
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict):
            merged = apply_patch(out.get(key) if isinstance(out.get(key), dict) else {}, value)
            if merged:
                out[key] = merged
            else:
                out.pop(key, None)
        else:
            out[key] = copy.deepcopy(value)
    return out


def merge_controls(shipped: list[dict], overlay: dict[str, dict]) -> list[dict]:
    """Apply ``overlay`` ({control id: partial spec}) to the shipped controls
    list, keeping its order. Unknown ids raise ValueError."""
    overlay = overlay or {}
    known = {spec.get("id") for spec in shipped}
    unknown = sorted(str(cid) for cid in overlay if cid not in known)
    if unknown:
        raise ValueError(
            f"calibration overlay names controls the descriptor doesn't have: {', '.join(unknown)}"
        )
    out = []
    for spec in shipped:
        part = overlay.get(spec.get("id"))
        if part is None:
            out.append(copy.deepcopy(spec))
            continue
        if not isinstance(part, dict):
            raise ValueError(f"calibration overlay for {spec['id']!r} must be a mapping")
        if "id" in part and part["id"] != spec["id"]:
            raise ValueError(f"calibration overlay cannot rename control {spec['id']!r}")
        out.append(deep_merge(spec, part))
    return out


# -- files --------------------------------------------------------------------


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """tmp in the same directory + fsync + rename + fsync of the directory:
    the box is switched off at the wall, and a half-written file must never
    be what it boots with."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def read_yaml(path: Path) -> dict:
    """A YAML mapping from ``path``; {} if the file is absent."""
    path = Path(path)
    if not path.exists():
        return {}
    doc = yaml.safe_load(path.read_text()) or {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: expected a mapping")
    return doc


def write_yaml(path: Path, doc: dict, header: str = "", mode: int = 0o644) -> None:
    """Atomically write ``doc`` as YAML, ``schema`` first, after ``header``."""
    doc = dict(doc)
    ordered = {"schema": doc.pop("schema")} if "schema" in doc else {}
    ordered.update(doc)
    text = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, default_flow_style=None)
    atomic_write_text(path, header + text, mode)


def read_box_file(paths: Paths) -> dict:
    """box.yaml exactly as it is on disk ({} if absent); no migrations run."""
    return read_yaml(paths.box_file)


BOX_HEADER = (
    "# This Hasler's own settings (docs/ota.md section 1). Updates never touch\n"
    "# this file; config/box.yaml.example in the release documents every key.\n"
)


def write_box_file(paths: Paths, box: dict) -> None:
    """Atomically replace box.yaml with ``box`` (``schema`` first)."""
    write_yaml(paths.box_file, {"schema": SCHEMA, **box}, BOX_HEADER)


def load_box_config(paths: Paths) -> dict:
    """``box.yaml`` as a dict ({} if absent), after running any migrations."""
    if not paths.box_file.exists():
        return {}
    try:
        from . import migrations

        migrations.run_auto(paths)
    except Exception:
        log.exception("automatic config migrations failed; reading box.yaml as it is")
    box = read_box_file(paths)
    schema = box.get("schema", SCHEMA)
    if not isinstance(schema, int) or schema > SCHEMA:
        log.warning("%s has schema %r, newer than this release's %d; reading what it can",
                    paths.box_file, schema, SCHEMA)
    return box


def haslerd_settings(box: dict) -> dict:
    """The haslerd override blocks of a box.yaml (code defaults fill the rest)."""
    return {k: copy.deepcopy(v) for k, v in box.items() if k not in BOX_KEYS}


def read_overlay(paths: Paths, variant: str) -> dict:
    """The calibration overlay for ``variant``: ``{"controls": {id: {...}},
    "io": {...}}`` - empty dicts when there is none yet."""
    doc = read_yaml(paths.overlay_file(variant))
    controls, io = doc.get("controls") or {}, doc.get("io") or {}
    if not isinstance(controls, dict) or not isinstance(io, dict):
        raise ValueError(f"{paths.overlay_file(variant)}: controls and io must be mappings")
    return {"controls": controls, "io": io}


def _overlay_dir(paths: Paths) -> Path:
    """variant.d, group-writable for haslerd when we are the one creating it."""
    directory = paths.etc / "variant.d"
    if directory.is_dir():
        return directory
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o775)
        shutil.chown(directory, group=OVERLAY_GROUP)
    except (LookupError, OSError):
        log.debug("could not hand %s to group %s", directory, OVERLAY_GROUP, exc_info=True)
    return directory


OVERLAY_HEADER = (
    "# This box's calibration, laid over the shipped descriptor by control id.\n"
    "# Written by the knob page and by migrations; updates never touch it.\n"
    "# Delete a key (or the file) to go back to the release's value.\n"
)


def write_overlay(paths: Paths, variant: str, patch: dict) -> dict:
    """Deep-merge ``patch`` into the overlay and save it atomically (tmp +
    fsync + rename, ``schema`` kept). Returns the new overlay. A value of None
    in ``patch`` removes that key (back to the shipped default)."""
    if not isinstance(patch, dict):
        raise ValueError("overlay patch must be a mapping")
    merged = apply_patch(read_overlay(paths, variant), patch)
    merged.pop("schema", None)
    new = {"controls": merged.pop("controls", None) or {}, "io": merged.pop("io", None) or {}}
    if merged:
        raise ValueError(f"overlay has only controls and io, not {', '.join(sorted(merged))}")
    shipped = paths.shipped_variant(variant)
    if shipped.exists():
        merge_controls(read_yaml(shipped).get("controls") or [], new["controls"])   # raises
    _overlay_dir(paths)
    path = paths.overlay_file(variant)
    write_yaml(path, {"schema": SCHEMA, **new}, OVERLAY_HEADER, mode=0o664)
    try:
        shutil.chown(path, group=OVERLAY_GROUP)
    except (LookupError, OSError):
        pass
    return new


def effective(variant, overlay: dict, overlay_path: Path | None = None):
    """``variant`` (a Variant) with ``overlay`` applied, as a new Variant."""
    return replace(
        variant,
        controls=merge_controls(variant.controls, overlay.get("controls") or {}),
        io=deep_merge(variant.io or {}, overlay.get("io") or {}),
        actions=copy.deepcopy(variant.actions),
        presets=copy.deepcopy(variant.presets),
        overlay_path=overlay_path,
    )


def overlay_variant(paths: Paths, variant):
    """An already-loaded Variant with this box's overlay for its name applied."""
    path = paths.overlay_file(variant.name)
    if not path.exists():
        return variant
    return effective(variant, read_overlay(paths, variant.name), path)


def load_effective_variant(paths: Paths, variant: str):
    """The shipped descriptor with this box's overlay applied, as a
    ``haslerd.variant.Variant`` (its ``overlay_path`` set when one exists)."""
    from .variant import load_variant

    return overlay_variant(paths, load_variant(paths.shipped_variant(variant)))


def env_flag(name: str) -> str | None:
    return os.environ.get(name) or None
