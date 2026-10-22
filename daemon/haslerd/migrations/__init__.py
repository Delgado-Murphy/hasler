"""Config migrations: small, idempotent steps from one config shape to the next.

docs/ota.md section 1. Each module here is ``NNN_name.py`` and exposes

    up(paths, dry_run=False, **options) -> list[str]

which does its step (or, with ``dry_run``, only says what it would do) and
returns one line per thing done. Applied ids are recorded in box.yaml's
``migrations:`` list, so each runs once per box. ``AUTO = True`` in a module
lets haslerd run it by itself at startup (``boxconfig.load_box_config``);
anything that moves files or needs root, like 001, leaves it False and is run
by the updater or by hand::

    python -m haslerd.migrations --check        # the plan, nothing written
    sudo python -m haslerd.migrations --apply
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import re
from types import ModuleType

from ..boxconfig import Paths, read_box_file, write_box_file

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^\d{3}_[a-z0-9_]+$")


def available() -> list[str]:
    """Every migration id this release ships, in order."""
    return sorted(m.name for m in pkgutil.iter_modules(__path__) if ID_RE.match(m.name))


def load(mid: str) -> ModuleType:
    return importlib.import_module(f"{__name__}.{mid}")


def applied(paths: Paths) -> list[str]:
    return [str(m) for m in (read_box_file(paths).get("migrations") or [])]


def pending(paths: Paths) -> list[str]:
    done = set(applied(paths))
    return [mid for mid in available() if mid not in done]


def record(paths: Paths, mid: str) -> None:
    """Add ``mid`` to box.yaml's ``migrations:`` (box.yaml created if need be)."""
    box = read_box_file(paths)
    done = [str(m) for m in (box.get("migrations") or [])]
    if mid not in done:
        box["migrations"] = done + [mid]
        write_box_file(paths, box)


def _call(module: ModuleType, paths: Paths, dry_run: bool, options: dict) -> list[str]:
    params = inspect.signature(module.up).parameters
    kwargs = {k: v for k, v in options.items() if k in params and v is not None}
    return list(module.up(paths, dry_run=dry_run, **kwargs) or [])


def run(paths: Paths, apply: bool = False, auto_only: bool = False, **options) -> list[tuple[str, list[str]]]:
    """Run the pending migrations in order: ``[(id, lines), ...]``.

    Without ``apply`` it is a dry run (nothing written, nothing recorded). A
    failing migration raises and stops the run; those before it stay recorded.
    """
    results: list[tuple[str, list[str]]] = []
    for mid in pending(paths):
        module = load(mid)
        if auto_only and not getattr(module, "AUTO", False):
            continue
        lines = _call(module, paths, not apply, options)
        if apply:
            record(paths, mid)
            log.info("migration %s applied: %s", mid, "; ".join(lines) or "nothing to do")
        results.append((mid, lines))
    return results


def run_auto(paths: Paths) -> list[tuple[str, list[str]]]:
    """The pending migrations haslerd may run by itself (``AUTO = True``)."""
    return run(paths, apply=True, auto_only=True)
