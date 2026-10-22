"""Early boot (``hasler-update-boot.service``, Before=haslerd.service).

In a trial: finish an interrupted rollback, undo a switch that never
completed, count the boot, and at the 2nd boot in trial roll back before
haslerd starts. Outside a trial: end an expired probation and drop
half-built releases. Never fails the boot - every error is logged and
swallowed (exit 0), because a box that can't boot is worse than one that
stayed on the old release.
"""

from __future__ import annotations

import sys
import traceback

from .apply import Applier, Hooks
from .layout import Layout


def main(layout: Layout | None = None, hooks: Hooks | None = None) -> int:
    layout = layout or Layout.from_env()
    try:
        print(Applier(layout, hooks).boot())
    except Exception:
        traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
