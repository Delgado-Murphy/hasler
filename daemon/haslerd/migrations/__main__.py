"""python -m haslerd.migrations --check|--apply [--etc DIR] [--state DIR] ..."""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from pathlib import Path

from ..boxconfig import Paths
from . import applied, pending, run


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m haslerd.migrations", description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="print the plan; write nothing")
    mode.add_argument("--apply", action="store_true", help="run the pending migrations")
    mode.add_argument("--list", action="store_true", help="applied and pending ids")
    ap.add_argument("--etc", help="box config directory (default /etc/hasler, or $HASLER_ETC)")
    ap.add_argument("--state", help="state directory (default /var/lib/haslerd, or $HASLER_STATE)")
    ap.add_argument("--release", help="release root holding config/variants (default: this one)")
    ap.add_argument("--legacy", help="001: the old rsync tree (default /home/pi/hasler, or $HASLER_LEGACY)")
    ap.add_argument("--variant", help="001: the variant name (default: from haslerd.service)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    paths = Paths.from_env(Path(args.release) if args.release else None)
    if args.release:
        paths = dataclasses.replace(paths, release=Path(args.release))
    if args.etc:
        paths = dataclasses.replace(paths, etc=Path(args.etc))
    if args.state:
        paths = dataclasses.replace(paths, state=Path(args.state))

    if args.list:
        for mid in applied(paths):
            print(f"applied  {mid}")
        for mid in pending(paths):
            print(f"pending  {mid}")
        return 0
    try:
        results = run(paths, apply=args.apply, legacy=args.legacy, variant=args.variant)
    except Exception as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 1
    if not results:
        print("nothing to do")
    for mid, lines in results:
        print(f"{mid}{'' if args.apply else ' (dry run)'}:")
        for line in lines or ["nothing to do"]:
            print(f"  - {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
