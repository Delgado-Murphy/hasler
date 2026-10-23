#!/usr/bin/env python3
"""Create, sign and check the release keyring (docs/ota-format.md "Trust on the box").

Stdlib + ssh-keygen only. The root key never leaves the offline machine; this
tool runs there for `new` and `sign`, and anywhere for `verify` and
`allowed-signers`.

    # first keyring (key ceremony, docs/release.md)
    keyring.py new --seq 1 --expires 2027-10-20 \\
        --key release-2026a:release:release-2026a.pub \\
        --key timestamp-2026a:timestamp:timestamp-2026a.pub -o keyring.json
    keyring.py sign --root-key /media/usb/hasler-root-2026.key keyring.json   # -> keyring.json.sig
    keyring.py verify --root root.pub keyring.json

    # rotate / revoke: next seq from the current one, drop or add keys
    keyring.py new --from keyring.json --drop release-2026a \\
        --key release-2026b:release:release-2026b.pub -o keyring.json

    # what the box (and verify.sh) feeds to ssh-keygen -Y verify
    keyring.py allowed-signers --role release keyring.json
    keyring.py allowed-signers --role release --namespace hasler-components keyring.json
    keyring.py find --pub release.pub --role release keyring.json      # prints the key id
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROLES = {"release": "hasler-release", "timestamp": "hasler-channel"}
# What else a role's key may sign. The release key also signs components.json
# (the yt-dlp builds radios may install, hasler-releases components.yml).
ROLE_NAMESPACES = {"release": ("hasler-release", "hasler-components"), "timestamp": ("hasler-channel",)}
KEYRING_NS = "hasler-keyring"
ROOT_ID = "hasler-root"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
PUB_RE = re.compile(r"^(ssh-ed25519|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]+$")
DEFAULT_DAYS = 365


class KeyringError(Exception):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_when(s: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise KeyringError(f"bad date {s!r} (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)")


def read_pub(spec: str) -> str:
    """'ssh-ed25519 AAAA...' from a .pub file or the string itself (comment dropped)."""
    text = Path(spec).read_text() if Path(spec).is_file() else spec
    pub = " ".join(text.strip().split()[:2])
    if not PUB_RE.match(pub):
        raise KeyringError(f"not an ed25519 OpenSSH public key: {spec}")
    return pub


def dumps(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=True) + "\n").encode()


def validate(doc: dict, at: datetime | None = None) -> None:
    at = at or now()
    if doc.get("schema") != 1:
        raise KeyringError("keyring: schema must be 1")
    if not isinstance(doc.get("keyring_seq"), int) or doc["keyring_seq"] < 1:
        raise KeyringError("keyring: keyring_seq must be an int >= 1")
    if parse_when(doc["expires"]) <= at:
        raise KeyringError(f"keyring expired {doc['expires']}")
    ids = set()
    for k in doc.get("keys", []):
        if not ID_RE.match(k.get("id", "")) or k["id"] in ids:
            raise KeyringError(f"keyring: bad or duplicate id {k.get('id')!r}")
        ids.add(k["id"])
        if k.get("role") not in ROLES:
            raise KeyringError(f"keyring: {k['id']}: role must be one of {sorted(ROLES)}")
        read_pub(k.get("pub", ""))
        parse_when(k["not_after"])
    for role in ROLES:
        if not any(k["role"] == role and parse_when(k["not_after"]) > at for k in doc["keys"]):
            raise KeyringError(f"keyring: no valid {role} key")


def cmd_new(a: argparse.Namespace) -> None:
    if a.from_:
        base = json.loads(Path(a.from_).read_text())
        keys = [k for k in base["keys"] if k["id"] not in set(a.drop)]
        seq = a.seq if a.seq is not None else base["keyring_seq"] + 1
        if seq <= base["keyring_seq"]:
            raise KeyringError(f"--seq must be above the current {base['keyring_seq']}")
    else:
        keys, seq = [], a.seq or 1
    expires = parse_when(a.expires) if a.expires else now() + timedelta(days=DEFAULT_DAYS)
    for spec in a.key:
        parts = spec.split(":", 3)
        if len(parts) not in (3, 4):
            raise KeyringError(f"--key wants id:role:pubfile[:not_after], got {spec!r}")
        kid, role, pub = parts[:3]
        not_after = parse_when(parts[3]) if len(parts) == 4 else expires
        if any(k["id"] == kid for k in keys):
            raise KeyringError(f"key id {kid} already in the keyring")
        keys.append({"id": kid, "role": role, "pub": read_pub(pub), "not_after": iso(not_after)})
    doc = {"schema": 1, "keyring_seq": seq, "expires": iso(expires), "keys": keys}
    validate(doc)
    Path(a.out).write_bytes(dumps(doc))
    print(f"{a.out}: keyring_seq {seq}, {len(keys)} keys, expires {iso(expires)}")


def ssh_verify(allowed: str, ident: str, ns: str, path: Path, sig: Path) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".allowed", delete=False) as f:
        f.write(allowed)
    try:
        with open(path, "rb") as data:
            r = subprocess.run(["ssh-keygen", "-q", "-Y", "verify", "-f", f.name, "-I", ident,
                                "-n", ns, "-s", str(sig)], stdin=data, capture_output=True)
        return r.returncode == 0
    finally:
        Path(f.name).unlink()


def cmd_sign(a: argparse.Namespace) -> None:
    validate(json.loads(Path(a.keyring).read_text()))
    sig = Path(a.keyring + ".sig")
    sig.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", a.root_key, "-n", KEYRING_NS, a.keyring], check=True)
    print(f"signed {a.keyring} -> {sig} ({KEYRING_NS})")


def verify_keyring(path: Path, roots: list[str], min_seq: int = 0) -> dict:
    doc = json.loads(path.read_text())
    validate(doc)
    allowed = "".join(f'{ROOT_ID} namespaces="{KEYRING_NS}" {read_pub(r)}\n' for r in roots)
    if not ssh_verify(allowed, ROOT_ID, KEYRING_NS, path, Path(str(path) + ".sig")):
        raise KeyringError(f"{path}: no root key verifies {path}.sig")
    if doc["keyring_seq"] <= min_seq:
        raise KeyringError(f"{path}: keyring_seq {doc['keyring_seq']} not above {min_seq}")
    return doc


def cmd_verify(a: argparse.Namespace) -> None:
    doc = verify_keyring(Path(a.keyring), a.root, a.min_seq)
    print(f"ok: keyring_seq {doc['keyring_seq']}, keys {[k['id'] for k in doc['keys']]}")


def allowed_signers(doc: dict, role: str, at: datetime | None = None, namespace: str | None = None) -> str:
    at = at or now()
    ns = namespace or ROLES[role]
    if ns not in ROLE_NAMESPACES[role]:
        raise KeyringError(f"a {role} key never signs namespace {ns}")
    lines = []
    for k in doc["keys"]:
        na = parse_when(k["not_after"])
        if k["role"] != role or na <= at:
            continue
        lines.append(f'{k["id"]} namespaces="{ns}",valid-before="{na.strftime("%Y%m%d%H%M%SZ")}" '
                     f'{k["pub"]}\n')
    return "".join(lines)


def cmd_allowed(a: argparse.Namespace) -> None:
    doc = json.loads(Path(a.keyring).read_text())
    validate(doc)
    sys.stdout.write(allowed_signers(doc, a.role, namespace=a.namespace))


def cmd_find(a: argparse.Namespace) -> None:
    doc = json.loads(Path(a.keyring).read_text())
    validate(doc)
    pub = read_pub(a.pub)
    for k in doc["keys"]:
        if k["pub"] == pub and k["role"] == a.role and parse_when(k["not_after"]) > now():
            print(k["id"])
            return
    raise KeyringError(f"that key is not a valid {a.role} key in {a.keyring}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("--from", dest="from_", help="start from this keyring (seq + 1)")
    n.add_argument("--seq", type=int)
    n.add_argument("--expires", help="keyring expiry (default: one year)")
    n.add_argument("--key", action="append", default=[], help="id:role:pubfile[:not_after]")
    n.add_argument("--drop", action="append", default=[], help="key id to remove (revoke)")
    n.add_argument("-o", "--out", required=True)
    n.set_defaults(fn=cmd_new)
    s = sub.add_parser("sign")
    s.add_argument("--root-key", required=True)
    s.add_argument("keyring")
    s.set_defaults(fn=cmd_sign)
    v = sub.add_parser("verify")
    v.add_argument("--root", action="append", required=True, help="root.pub (repeat for root2.pub)")
    v.add_argument("--min-seq", type=int, default=0)
    v.add_argument("keyring")
    v.set_defaults(fn=cmd_verify)
    al = sub.add_parser("allowed-signers")
    al.add_argument("--role", choices=sorted(ROLES), required=True)
    al.add_argument("--namespace", help="default: the role's own (release: hasler-release)")
    al.add_argument("keyring")
    al.set_defaults(fn=cmd_allowed)
    f = sub.add_parser("find")
    f.add_argument("--pub", required=True)
    f.add_argument("--role", choices=sorted(ROLES), required=True)
    f.add_argument("keyring")
    f.set_defaults(fn=cmd_find)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (KeyringError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print(f"keyring: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
