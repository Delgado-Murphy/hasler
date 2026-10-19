#!/usr/bin/env python3
"""Channel indexes (docs/ota-format.md "Channel index"), signed by a timestamp key.

Stdlib only. Edits ``channels/<channel>.json``; signing is separate
(``sign.sh -n hasler-channel ...``) so this never touches a key.

    # point a channel at a release (publish, or promote the same artifact)
    channel.py promote --index channels/bench.json --release release.json \\
        --release-url https://github.com/delgado-murphy/hasler-releases/releases/download/v0.2.0/release.json
    channel.py halt --index channels/stable.json --seq 20199      # stop a bad release
    channel.py refresh channels/*.json                            # daily: new issued/expires
    channel.py check channels/stable.json

``promote`` refuses a seq that is not above the channel's current one (a
pull is a new, higher-seq release), a halted seq, and a release whose
``product``/``version`` do not parse. Times are UTC, whole seconds.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CHANNELS = ("bench", "beta", "stable")
EXPIRES_DAYS = 14
RINGS_DEFAULT = {"0": [], "1": [], "2": ["*"]}
URL_RE = re.compile(r"^https://[^\s]+/release\.json$")


class ChannelError(Exception):
    pass


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def dumps(doc: dict) -> bytes:
    order = ("schema", "channel", "issued", "expires", "current", "halted", "min_supported_seq",
             "rings", "ring_open")
    out = {k: doc[k] for k in order if k in doc}
    out.update({k: v for k, v in doc.items() if k not in out})
    return (json.dumps(out, indent=2, ensure_ascii=True) + "\n").encode()


def channel_of(path: Path) -> str:
    name = path.stem
    if name not in CHANNELS:
        raise ChannelError(f"{path}: file name must be <channel>.json, channel in {CHANNELS}")
    return name


def stamp(doc: dict, at: datetime, days: int = EXPIRES_DAYS) -> None:
    doc["issued"] = iso(at)
    doc["expires"] = iso(at + timedelta(days=days))


def validate(doc: dict, at: datetime | None = None, allow_expired: bool = False) -> None:
    for k in ("schema", "channel", "issued", "expires", "current", "halted", "min_supported_seq",
              "rings", "ring_open"):
        if k not in doc:
            raise ChannelError(f"index missing {k}")
    if doc["schema"] != 1 or doc["channel"] not in CHANNELS:
        raise ChannelError("index: bad schema/channel")
    if parse_iso(doc["expires"]) <= parse_iso(doc["issued"]):
        raise ChannelError("index: expires not after issued")
    if not allow_expired and parse_iso(doc["expires"]) <= (at or now()):
        raise ChannelError(f"index expired {doc['expires']}")
    cur = doc["current"]
    if cur is not None:
        if not (isinstance(cur.get("seq"), int) and URL_RE.match(cur.get("release_url", ""))
                and re.fullmatch(r"[0-9a-f]{64}", cur.get("release_sha256", ""))):
            raise ChannelError("index: bad current")
    if not all(isinstance(s, int) for s in doc["halted"]):
        raise ChannelError("index: halted must be ints")


def load(path: Path, create: bool = False) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    if not create:
        raise ChannelError(f"{path} does not exist")
    return {"schema": 1, "channel": channel_of(path), "issued": "", "expires": "", "current": None,
            "halted": [], "min_supported_seq": 0, "rings": dict(RINGS_DEFAULT), "ring_open": {}}


def cmd_promote(a: argparse.Namespace) -> None:
    path = Path(a.index)
    doc = load(path, create=True)
    if doc["channel"] != channel_of(path):
        raise ChannelError(f"{path}: says channel {doc['channel']}")
    raw = Path(a.release).read_bytes()
    rel = json.loads(raw)
    if rel.get("product") != "hasler" or not isinstance(rel.get("seq"), int):
        raise ChannelError("not a hasler release.json")
    if not URL_RE.match(a.release_url) or f"/v{rel['version']}/" not in a.release_url:
        raise ChannelError(f"release url {a.release_url} does not match version {rel['version']}")
    seq = rel["seq"]
    if seq in doc["halted"]:
        raise ChannelError(f"seq {seq} is halted on {doc['channel']}")
    cur = doc["current"]
    if cur and seq <= cur["seq"] and not a.force:
        raise ChannelError(f"seq {seq} is not above the current {cur['seq']} on {doc['channel']}")
    at = now()
    if parse_iso(rel["expires"]) <= at:
        raise ChannelError(f"release {rel['version']} expired {rel['expires']}")
    doc["current"] = {"version": rel["version"], "seq": seq, "release_url": a.release_url,
                      "release_sha256": hashlib.sha256(raw).hexdigest()}
    if a.min_supported_seq is not None:
        doc["min_supported_seq"] = a.min_supported_seq
    doc["ring_open"] = {r: iso(at) for r in sorted(doc["rings"])}
    stamp(doc, at)
    validate(doc, at)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(doc))
    print(f"{path}: {doc['channel']} -> {rel['version']} (seq {seq}), expires {doc['expires']}")


def cmd_halt(a: argparse.Namespace) -> None:
    path = Path(a.index)
    doc = load(path)
    if a.seq not in doc["halted"]:
        doc["halted"] = sorted(doc["halted"] + [a.seq])
    # current stays as it is: a box never installs a seq listed in halted, and
    # stays where it is until a new, higher release is promoted.
    stamp(doc, now())
    validate(doc)
    path.write_bytes(dumps(doc))
    print(f"{path}: halted {doc['halted']}")


def cmd_refresh(a: argparse.Namespace) -> None:
    at = now()
    for p in a.index:
        path = Path(p)
        doc = load(path)
        validate(doc, at, allow_expired=True)   # refresh is how an expired index comes back
        stamp(doc, at)
        path.write_bytes(dumps(doc))
        print(f"{path}: issued {doc['issued']}, expires {doc['expires']}")


def cmd_check(a: argparse.Namespace) -> None:
    for p in a.index:
        doc = load(Path(p))
        validate(doc)
        print(f"ok: {p} -> {doc['current'] and doc['current']['version']}, expires {doc['expires']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("promote")
    p.add_argument("--index", required=True)
    p.add_argument("--release", required=True, help="the release.json (bytes are hashed as served)")
    p.add_argument("--release-url", required=True)
    p.add_argument("--min-supported-seq", type=int)
    p.add_argument("--force", action="store_true", help="allow a seq that is not higher (emergency)")
    p.set_defaults(fn=cmd_promote)
    h = sub.add_parser("halt")
    h.add_argument("--index", required=True)
    h.add_argument("--seq", type=int, required=True)
    h.set_defaults(fn=cmd_halt)
    r = sub.add_parser("refresh")
    r.add_argument("index", nargs="+")
    r.set_defaults(fn=cmd_refresh)
    c = sub.add_parser("check")
    c.add_argument("index", nargs="+")
    c.set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (ChannelError, OSError, ValueError, KeyError) as e:
        print(f"channel: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
