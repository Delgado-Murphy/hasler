"""``hasler-update`` - the one command.

  check [--stage] [--force]       (hasler-update user) look for an update; --stage also downloads + extracts;
                                  then the daily check-in, if the owner switched it on (checkin.py)
  stage [--force]                 (hasler-update user) = check --stage
  checkin [--preview [--stdin]]   (anyone) --preview prints today's check-in and sends nothing (--stdin:
                                  haslerd's /api/diagnostics + /api/health as {"diagnostics", "health"} on
                                  stdin, for the key page's "See what it sends"); without it: send it now
  apply [--seq N] [--auto] [--now] [--force] [--allow-reboot]   (root)
  rollback [--to VERSION]         (root) go back to previous / a kept release
  commit                          (root) accept a trial now / end probation
  status                          (anyone) JSON: installed, kept, state, last check
  boot                            (root, early boot) boot counter + interrupted-switch recovery
  resume                          (root, after haslerd at boot) finish judging a trial
  on-failure                      (root, haslerd OnFailure=) roll back during trial/probation
  instance NAME                   (root, hasler-update-apply@NAME) NAME = <seq> | rollback | rollback-<version>
  integrate-pending [--locked]    (root) put in place the system files of the running (major) release that
                                  the updater which ran its trial had no rule for; run by the commit through
                                  the newly installed updater, retried by apply and resume. Idempotent.

Exit codes: 0 ok / nothing to do, 1 error, 75 deferred (not now: radio in use,
outside the window, mode).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from . import boot as boot_mod
from . import checkin as checkin_mod
from .apply import Applier, ApplyError, Deferred
from .check import Checker
from .fetch import FetchError
from .layout import Layout
from .verify import VerifyError

EX_DEFERRED = 75


def _need_root(cmd: str) -> None:
    import os

    if os.geteuid() != 0 and not os.environ.get("HASLER_ROOT"):
        raise SystemExit(f"hasler-update {cmd}: needs root")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hasler-update", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--stage", action="store_true")
    c.add_argument("--force", action="store_true", help="stage even when metered / mode off")
    s = sub.add_parser("stage")
    s.add_argument("--force", action="store_true")
    a = sub.add_parser("apply")
    a.add_argument("--seq", type=int)
    a.add_argument("--auto", action="store_true", help="nightly timer: obey update_mode and the window")
    a.add_argument("--now", action="store_true", help="'Update now': skip the night window, still needs idle")
    a.add_argument("--force", action="store_true", help="skip window and idle checks (bench drills)")
    a.add_argument("--allow-reboot", action="store_true", help="allow a config.txt change + reboot")
    r = sub.add_parser("rollback")
    r.add_argument("--to")
    sub.add_parser("commit")
    sub.add_parser("status")
    sub.add_parser("boot")
    sub.add_parser("resume")
    sub.add_parser("on-failure")
    i = sub.add_parser("instance")
    i.add_argument("name")
    ip = sub.add_parser("integrate-pending")
    ip.add_argument("--locked", action="store_true",
                    help="internal: the calling commit holds the apply lock")
    ci = sub.add_parser("checkin")
    ci.add_argument("--preview", action="store_true", help="print today's check-in; send nothing")
    ci.add_argument("--stdin", action="store_true", help="with --preview: haslerd's documents on stdin")
    args = ap.parse_args(argv)
    layout = Layout.from_env()

    try:
        if args.cmd in ("check", "stage"):
            ch = Checker(layout)
            out = ch.stage(force=args.force) if (args.cmd == "stage" or args.stage) else ch.check()
            print(json.dumps(out, indent=2))
            # The opt-in check-in rides on the daily check; it never fails the check.
            try:
                checkin_mod.after_check(layout, ch.box)
            except Exception as e:      # noqa: BLE001 - a check-in must never cost a check
                print(f"check-in: {e}", file=sys.stderr)
            return 0 if out.get("result") == "ok" else 1
        if args.cmd == "checkin":
            if args.preview:
                docs = json.load(sys.stdin) if args.stdin else None
                print(json.dumps(checkin_mod.collect(layout, docs if isinstance(docs, dict) else None),
                                 ensure_ascii=False))
                return 0
            box = {**Checker(layout).box, "checkin": True}      # asked for by hand: the switch is implied
            rec = checkin_mod.after_check(layout, box, uptime=lambda: None) or {}   # by hand: no boot hold
            print(json.dumps(rec, indent=2))
            return 0 if rec.get("ok") else 1
        if args.cmd == "status":
            print(json.dumps(Applier(layout).status(), indent=2, default=str))
            return 0
        if args.cmd == "boot":
            _need_root(args.cmd)
            return boot_mod.main(layout)
        _need_root(args.cmd)
        ap_ = Applier(layout)
        if args.cmd == "apply":
            print(ap_.apply(seq=args.seq, auto=args.auto, now_=args.now, force=args.force,
                            allow_reboot=args.allow_reboot))
        elif args.cmd == "rollback":
            print(ap_.rollback(args.to))
        elif args.cmd == "commit":
            print(ap_.commit())
        elif args.cmd == "resume":
            print(ap_.resume())
        elif args.cmd == "on-failure":
            print(ap_.on_failure())
        elif args.cmd == "integrate-pending":
            print(ap_.integrate_pending(locked=args.locked))
        elif args.cmd == "instance":
            name = args.name
            if re.fullmatch(r"[0-9]{1,9}", name):
                print(ap_.apply(seq=int(name), now_=True))
            # A go-back from the key page was confirmed with a key press on the
            # radio: run it now, even with music playing. Deferring it to "idle"
            # would quietly do nothing while the owner stands there (bench drill).
            elif name == "rollback":
                print(ap_.rollback(None, force=True))
            elif m := re.fullmatch(r"rollback-([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.]+)?)", name):
                print(ap_.rollback(m.group(1), force=True))
            else:
                print(f"unknown instance {name!r}", file=sys.stderr)
                return 1
        return 0
    except Deferred as e:
        print(f"deferred: {e}", file=sys.stderr)
        return EX_DEFERRED
    except (ApplyError, VerifyError, FetchError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
