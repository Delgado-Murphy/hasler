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

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1   # of box.yaml and the overlay files; bumped with a migration


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
        raise NotImplementedError

    @classmethod
    def from_env(cls, release: Path | None = None) -> "Paths":
        """Defaults, overridden by HASLER_ETC / HASLER_STATE / HASLER_RELEASE."""
        raise NotImplementedError


def deep_merge(base: Any, over: Any) -> Any:
    """Return ``base`` with ``over`` applied: dicts recursively, anything else
    (lists included) replaced. Never mutates its arguments."""
    raise NotImplementedError


def merge_controls(shipped: list[dict], overlay: dict[str, dict]) -> list[dict]:
    """Apply ``overlay`` ({control id: partial spec}) to the shipped controls
    list, keeping its order. Unknown ids raise ValueError."""
    raise NotImplementedError


def load_box_config(paths: Paths) -> dict:
    """``box.yaml`` as a dict ({} if absent), after running any migrations."""
    raise NotImplementedError


def read_overlay(paths: Paths, variant: str) -> dict:
    """The calibration overlay for ``variant``: ``{"controls": {id: {...}},
    "io": {...}}`` - empty dicts when there is none yet."""
    raise NotImplementedError


def write_overlay(paths: Paths, variant: str, patch: dict) -> dict:
    """Deep-merge ``patch`` into the overlay and save it atomically (tmp +
    fsync + rename, ``schema`` kept). Returns the new overlay. A value of None
    in ``patch`` removes that key (back to the shipped default)."""
    raise NotImplementedError


def load_effective_variant(paths: Paths, variant: str):
    """The shipped descriptor with this box's overlay applied, as a
    ``haslerd.variant.Variant`` (its ``overlay_path`` set when one exists)."""
    raise NotImplementedError


def env_flag(name: str) -> str | None:
    return os.environ.get(name) or None
