#!/usr/bin/env python3
"""Should bench's release go to stable now? (workflow auto-promote.yml)

Stdlib only; signatures are checked by the workflow before this runs.

    autopromote.py decide --bench channels/bench.json [--stable channels/stable.json] \\
        --release release.json --state auto-promote.json [--min-days 7]
    autopromote.py pause  --state auto-promote.json --by NAME [--reason TEXT]
    autopromote.py resume --state auto-promote.json --by NAME
    autopromote.py record --state auto-promote.json --version V --bench-since T

``decide`` prints ``promote=true|false``, ``version=`` and ``why=`` (also to
$GITHUB_OUTPUT). It promotes bench's *current* release only when all hold:

- auto-promotion is not paused (``auto-promote.json`` on gh-pages);
- the release has been bench's current for at least --min-days (bench's
  ``ring_open``, set when the channel was pointed at it; a newer bench release
  restarts the clock - the soak is of what the bench radio runs now);
- it is not halted on bench or stable, is a final (not -rc) release, not
  ``major`` (majors are promoted by hand), and its release.json is valid for
  at least 2 more days;
- stable exists (the first stable promotion is a person's decision) and its
  current seq is lower.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def load_state(path: Path) -> dict:
    try:
        d = json.loads(path.read_text())
    except FileNotFoundError:
        d = {}
    d.setdefault("schema", 1)
    d.setdefault("paused", False)
    d.setdefault("history", [])
    return d


def save_state(path: Path, d: dict) -> None:
    d["updated"] = iso(now())
    path.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n")


def out(**kv: str) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in kv.items())
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(lines)
    sys.stdout.write(lines)


def decide(bench: dict, stable: dict | None, release: dict, state: dict, min_days: float,
           at: datetime) -> tuple[bool, str, str]:
    cur = bench.get("current") or {}
    v, seq = cur.get("version"), cur.get("seq")
    if not v:
        return False, "", "bench has no current release"
    since = min((parse_iso(t) for t in (bench.get("ring_open") or {}).values()), default=None)
    if state.get("paused"):
        return False, v, f"paused by {state.get('paused_by', '?')}: {state.get('reason') or 'no reason given'}"
    if release.get("version") != v or release.get("seq") != seq:
        return False, v, f"release.json is {release.get('version')}, bench says {v}"
    if seq in (bench.get("halted") or []):
        return False, v, f"{v} is halted on bench"
    if "-" in v:
        return False, v, f"{v} is a release candidate - never promoted automatically"
    if release.get("kind") == "major":
        return False, v, f"{v} is a major release - promote it by hand (docs/release.md)"
    if stable is None:
        return False, v, "stable does not exist yet - the first stable promotion is done by hand"
    if seq in (stable.get("halted") or []):
        return False, v, f"{v} is halted on stable"
    scur = stable.get("current") or {}
    if scur.get("seq") is not None and scur["seq"] >= seq:
        return False, v, f"stable already has {scur.get('version')}"
    if since is None:
        return False, v, "bench index has no ring_open time"
    age = at - since
    if age < timedelta(days=min_days):
        left = timedelta(days=min_days) - age
        return False, v, (f"{v} on bench since {iso(since)} ({age.days} d {age.seconds // 3600} h); "
                          f"promotes in {left.days} d {left.seconds // 3600} h")
    if parse_iso(release["expires"]) <= at + timedelta(days=2):
        return False, v, f"release.json of {v} expires {release['expires']} - re-release it"
    return True, v, f"{v} on bench since {iso(since)} ({age.days} days), stable has {scur.get('version')}"


def cmd_decide(a: argparse.Namespace) -> None:
    bench = json.loads(Path(a.bench).read_text())
    stable = json.loads(Path(a.stable).read_text()) if a.stable and Path(a.stable).is_file() else None
    release = json.loads(Path(a.release).read_text())
    state = load_state(Path(a.state))
    ok, v, why = decide(bench, stable, release, state, a.min_days, now())
    since = min((t for t in (bench.get("ring_open") or {}).values()), default="")
    out(promote=str(ok).lower(), version=v, bench_since=since, why=why.replace("\n", " "))


def cmd_pause(a: argparse.Namespace) -> None:
    p = Path(a.state)
    d = load_state(p)
    d.update(paused=True, paused_by=a.by, paused_at=iso(now()), reason=a.reason or "")
    save_state(p, d)
    print(f"auto-promotion paused by {a.by}")


def cmd_resume(a: argparse.Namespace) -> None:
    p = Path(a.state)
    d = load_state(p)
    d.update(paused=False, resumed_by=a.by, resumed_at=iso(now()), reason="")
    d.pop("paused_by", None)
    d.pop("paused_at", None)
    save_state(p, d)
    print(f"auto-promotion resumed by {a.by}")


def cmd_record(a: argparse.Namespace) -> None:
    p = Path(a.state)
    d = load_state(p)
    d["history"].insert(0, {"version": a.version, "channel": "stable", "bench_since": a.bench_since,
                            "dispatched": iso(now()), "run": a.run or ""})
    del d["history"][50:]
    save_state(p, d)
    print(f"recorded {a.version} -> stable")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decide")
    p.add_argument("--bench", required=True)
    p.add_argument("--stable")
    p.add_argument("--release", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--min-days", type=float, default=7)
    p.set_defaults(fn=cmd_decide)
    p = sub.add_parser("pause")
    p.add_argument("--state", required=True)
    p.add_argument("--by", required=True)
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_pause)
    p = sub.add_parser("resume")
    p.add_argument("--state", required=True)
    p.add_argument("--by", required=True)
    p.set_defaults(fn=cmd_resume)
    p = sub.add_parser("record")
    p.add_argument("--state", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--bench-since", default="")
    p.add_argument("--run")
    p.set_defaults(fn=cmd_record)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (OSError, ValueError, KeyError) as e:
        print(f"autopromote: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
