"""Release pre-flight: can this release run on *this* box? (docs/ota-format.md)

Run by the updater with the new release's venv, before anything switches:
``python selftest.py --etc /etc/hasler --state /var/lib/haslerd``. It imports
every module, loads the box's config and calibration overlay with the new
code, builds every control from the merged descriptor, and checks that the
release's migrations know this box. Nothing is written. Exit 0 = go.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path

import haslerd
from haslerd import boxconfig
from haslerd.panel.controls import build_controls

# Hardware or optional extras: importing them is checked at runtime, not here.
SKIP_IMPORTS = ("haslerd.migrations.0",)


def check_imports() -> list[str]:
    problems = []
    for mod in pkgutil.walk_packages(haslerd.__path__, prefix="haslerd."):
        name = mod.name
        if name.startswith(SKIP_IMPORTS) or name == "haslerd.selftest":
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:   # noqa: BLE001 - report every failure, not just the first
            problems.append(f"import {name}: {exc}")
    return problems


def check_config(paths: boxconfig.Paths) -> list[str]:
    problems = []
    try:
        box = boxconfig.read_box_file(paths) if paths.box_file.exists() else {}
    except Exception as exc:   # noqa: BLE001
        return [f"box.yaml: {exc}"]
    variant = box.get("variant") or "hasler-hm"
    try:
        effective = boxconfig.load_effective_variant(paths, variant)
        build_controls(effective.controls)
    except Exception as exc:   # noqa: BLE001
        problems.append(f"variant {variant} + overlay: {exc}")
    return problems


def check_migrations(paths: boxconfig.Paths) -> list[str]:
    try:
        from haslerd import migrations

        unknown = [m for m in migrations.applied(paths) if m not in migrations.available()]
    except Exception as exc:   # noqa: BLE001
        return [f"migrations: {exc}"]
    # A box that ran a migration this release doesn't ship is from the future.
    return [f"migrations: box has {', '.join(unknown)}, unknown to this release"] if unknown else []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="selftest")
    ap.add_argument("--etc", default="/etc/hasler")
    ap.add_argument("--state", default="/var/lib/haslerd")
    args = ap.parse_args(argv)
    paths = boxconfig.Paths(release=boxconfig.find_release(), etc=Path(args.etc), state=Path(args.state))
    problems = check_imports() + check_config(paths) + check_migrations(paths)
    for p in problems:
        print(f"selftest: {p}", file=sys.stderr)
    print("selftest: ok" if not problems else f"selftest: {len(problems)} problem(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
