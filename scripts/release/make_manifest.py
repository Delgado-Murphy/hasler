#!/usr/bin/env python3
"""release.json for a release tarball, and a checker for both (docs/ota-format.md).

Stdlib only (runs on the signing runner and on a laptop). Two jobs:

* ``build_manifest(...)`` / ``release_json(...)`` - used by build_release.py.
  ``manifest.json`` inside the tarball is the release.json payload *without*
  ``artifacts`` (a file cannot carry the hash of the archive it sits in).
* ``check`` - re-derive everything from a tarball and compare with a
  release.json: size, sha256, safe members, files.sha256 against the
  contents, manifest.json against release.json. The signer runs this before
  signing anything.

    make_manifest.py check --release dist/release.json --tarball dist/hasler-0.2.0-arm64.tar.zst
    make_manifest.py make  --tarball dist/hasler-0.2.0-arm64.tar.zst --out release.json
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

PRODUCT = "hasler"
SCHEMA = 1
DEFAULT_BASE_URL = "https://github.com/delgado-murphy/hasler-releases/releases/download/v{version}/"
DEFAULT_REQUIRES = {"os": "trixie", "moode": ">=10.0,<11", "python_abi": "cp313", "arch": "aarch64"}
RELEASE_EXPIRES_DAYS = 120
KINDS = ("patch", "minor", "major")
CHANNELS = ("bench", "stable")   # bench: the makers' test radio; stable: everyone

# semver with an optional -rc.N (the only pre-release form we ship)
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?$")

# The top dirs of integration/ the last-shipped updater has a destination for
# (update/hasler_update/integrate.py ``_rules`` as shipped in 0.4.x; drop-ins
# live under systemd/). A box only installs a release's system files of another
# kind if the release is ``major``: the trial's (old) updater skips them and the
# release's own updater puts them in place after the commit (docs/ota-format.md,
# 2026-10-21). Add a kind here only once an updater that knows it has shipped -
# update/tests/test_ota_ops.py checks this against the newest v* tag.
KNOWN_KINDS_SINCE = frozenset({"systemd", "udev", "sbin", "bin", "sudoers", "shutdown", "configtxt", "avahi"})

# release.json key order, as in docs/ota-format.md
KEY_ORDER = ("schema", "product", "version", "seq", "channel", "created", "expires", "kind",
             "requires_migration", "min_from_seq", "requires", "config_schema", "variants",
             "artifacts", "notes", "keyring_seq")


class ReleaseError(Exception):
    pass


# --- versions -----------------------------------------------------------------

def parse_version(v: str) -> tuple[int, int, int, int | None]:
    m = VERSION_RE.match(v)
    if not m:
        raise ReleaseError(f"version {v!r} is not X.Y.Z or X.Y.Z-rc.N")
    major, minor, patch, rc = m.groups()
    return int(major), int(minor), int(patch), (int(rc) if rc else None)


def seq_for(version: str) -> int:
    """A monotonic seq derived from the version, so no counter has to be kept.

    ``(M*10000 + m*100 + p) * 100 + (N for rc.N | 99 for the final)``: follows
    semver precedence (1.2.0-rc.1 < 1.2.0-rc.2 < 1.2.0 < 1.2.1) as long as
    minor, patch and N stay below 100.
    """
    major, minor, patch, rc = parse_version(version)
    if minor > 99 or patch > 99 or (rc or 0) > 98:
        raise ReleaseError(f"{version}: minor/patch must be < 100 and rc < 99 for the derived seq; pass --seq")
    return (major * 10000 + minor * 100 + patch) * 100 + (rc if rc is not None else 99)


def kind_between(prev: str | None, version: str) -> str:
    """patch/minor/major from the previous release; unknown previous -> major (asks)."""
    if not prev:
        return "major"
    a, b = parse_version(prev)[:3], parse_version(version)[:3]
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "minor"
    return "patch"


def iso(ts: int | float | datetime) -> str:
    if not isinstance(ts, datetime):
        ts = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return ts.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def artifact_name(version: str) -> str:
    return f"{PRODUCT}-{version}-arm64.tar.zst"


# --- manifest -------------------------------------------------------------------

def build_manifest(*, version: str, created: int, seq: int | None = None, channel: str | None = None,
                   kind: str = "major", requires_migration: bool = False, min_from_seq: int = 0,
                   requires: dict | None = None, config_schema: int = 1, variants: list[str] = (),
                   notes: str = "", keyring_seq: int = 1,
                   expires_days: int = RELEASE_EXPIRES_DAYS) -> dict:
    """The release.json payload without ``artifacts`` (= manifest.json)."""
    parse_version(version)
    if kind not in KINDS:
        raise ReleaseError(f"kind {kind!r} not in {KINDS}")
    if channel is None:
        channel = "bench" if parse_version(version)[3] is not None else "stable"
    if channel not in CHANNELS:
        raise ReleaseError(f"channel {channel!r} not in {CHANNELS}")
    created_dt = datetime.fromtimestamp(int(created), tz=timezone.utc)
    return {
        "schema": SCHEMA,
        "product": PRODUCT,
        "version": version,
        "seq": seq if seq is not None else seq_for(version),
        "channel": channel,
        "created": iso(created_dt),
        "expires": iso(created_dt + timedelta(days=expires_days)),
        "kind": kind,
        "requires_migration": bool(requires_migration),
        "min_from_seq": int(min_from_seq),
        "requires": dict(requires if requires is not None else DEFAULT_REQUIRES),
        "config_schema": int(config_schema),
        "variants": sorted(variants),
        "notes": notes,
        "keyring_seq": int(keyring_seq),
    }


def ordered(doc: dict) -> dict:
    out = {k: doc[k] for k in KEY_ORDER if k in doc}
    out.update({k: v for k, v in doc.items() if k not in out})
    return out


def dumps(doc: dict) -> bytes:
    """Canonical bytes: fixed key order, 2-space indent, ASCII, trailing newline."""
    return (json.dumps(ordered(doc), indent=2, ensure_ascii=True) + "\n").encode()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def release_json(manifest: dict, tarball: Path, base_url: str = DEFAULT_BASE_URL) -> dict:
    doc = dict(manifest)
    doc["artifacts"] = [{
        "name": tarball.name,
        "sha256": sha256_file(tarball),
        "size": tarball.stat().st_size,
        "url": base_url.format(version=manifest["version"]) + tarball.name,
    }]
    return ordered(doc)


def validate_release(doc: dict) -> None:
    """Shape check per docs/ota-format.md; raises ReleaseError."""
    missing = [k for k in KEY_ORDER if k not in doc]
    if missing:
        raise ReleaseError(f"release.json missing {missing}")
    if doc["schema"] != SCHEMA or doc["product"] != PRODUCT:
        raise ReleaseError("release.json: wrong schema/product")
    parse_version(doc["version"])
    if not isinstance(doc["seq"], int) or doc["seq"] <= 0:
        raise ReleaseError("release.json: seq must be a positive int")
    if doc["kind"] not in KINDS or doc["channel"] not in CHANNELS:
        raise ReleaseError("release.json: bad kind/channel")
    if parse_iso(doc["expires"]) <= parse_iso(doc["created"]):
        raise ReleaseError("release.json: expires not after created")
    for key in ("os", "moode", "python_abi", "arch"):
        if key not in doc["requires"]:
            raise ReleaseError(f"release.json: requires.{key} missing")
    arts = doc["artifacts"]
    if not (isinstance(arts, list) and len(arts) == 1):
        raise ReleaseError("release.json: exactly one artifact expected")
    a = arts[0]
    if a["name"] != artifact_name(doc["version"]) or not re.fullmatch(r"[0-9a-f]{64}", a["sha256"]):
        raise ReleaseError("release.json: bad artifact name/sha256")
    if not a["url"].startswith("https://") or not a["url"].endswith("/" + a["name"]):
        raise ReleaseError("release.json: artifact url must be https and end in the name")


def check_integration_kinds(paths, kind: str) -> None:
    """A non-major release may only carry integration/ kinds the running
    updaters know; a new kind waits for a major release. Raises ReleaseError."""
    new = sorted({PurePosixPath(p).parts[1] for p in paths
                  if p.startswith("integration/") and len(PurePosixPath(p).parts) > 2}
                 - KNOWN_KINDS_SINCE)
    if new and kind != "major":
        raise ReleaseError(
            f"integration/{', integration/'.join(new)}: a kind the shipped updater has no rule for; "
            f"boxes refuse it in a {kind} release. Ship it in a major release (--kind major): the "
            "release's own updater installs it after the commit (docs/ota-format.md, 2026-10-21)")


# --- reading a tarball ---------------------------------------------------------------

def zstd_decompress(path: Path) -> bytes:
    exe = shutil.which("zstd")
    if exe:
        return subprocess.run([exe, "-dc", "-q", str(path)], check=True, capture_output=True).stdout
    try:  # Python 3.14+
        from compression import zstd  # type: ignore[import-not-found]
        return zstd.decompress(path.read_bytes())
    except ImportError:
        raise ReleaseError("need the zstd CLI (apt install zstd / brew install zstd)") from None


SHA_LINE = re.compile(r"^([0-9a-f]{64})  (0[0-7]{3})  (.+)$")


def parse_files_sha256(text: str) -> dict[str, tuple[str, int]]:
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        m = SHA_LINE.match(line)
        if not m:
            raise ReleaseError(f"files.sha256 line {n} malformed: {line!r}")
        out[m.group(3)] = (m.group(1), int(m.group(2), 8))
    return out


def read_tarball(path: Path) -> tuple[str, dict[str, tuple[bytes, int]]]:
    """(top dir, {relative path: (content, mode)}) with every safety rule enforced."""
    data = zstd_decompress(path)
    files: dict[str, tuple[bytes, int]] = {}
    top = None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tf:
        names = []
        for m in tf.getmembers():
            names.append(m.name)
            p = PurePosixPath(m.name)
            if p.is_absolute() or ".." in p.parts or not p.parts:
                raise ReleaseError(f"unsafe path in tarball: {m.name}")
            if top is None:
                top = p.parts[0]
            if p.parts[0] != top:
                raise ReleaseError(f"more than one top-level dir: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise ReleaseError(f"not a file or dir: {m.name}")
            if m.mode & 0o7000:
                raise ReleaseError(f"setuid/setgid/sticky bit: {m.name}")
            if m.uid or m.gid:
                raise ReleaseError(f"owner not 0:0: {m.name}")
            if m.isfile():
                rel = str(PurePosixPath(*p.parts[1:]))
                files[rel] = (tf.extractfile(m).read(), m.mode & 0o777)
        if names != sorted(names):
            raise ReleaseError("tarball entries are not sorted")
        mtimes = {m.mtime for m in tf.getmembers()}
        if len(mtimes) != 1:
            raise ReleaseError(f"tarball mtimes not uniform: {sorted(mtimes)[:3]}")
    if top is None:
        raise ReleaseError("empty tarball")
    return top, files


def check(release_path: Path, tarball: Path) -> dict:
    """Everything the signer must see hold before signing. Returns release.json."""
    raw = release_path.read_bytes()
    doc = json.loads(raw)
    validate_release(doc)
    art = doc["artifacts"][0]
    if tarball.name != art["name"]:
        raise ReleaseError(f"tarball name {tarball.name} != {art['name']}")
    if tarball.stat().st_size != art["size"]:
        raise ReleaseError("tarball size differs from release.json")
    if sha256_file(tarball) != art["sha256"]:
        raise ReleaseError("tarball sha256 differs from release.json")
    top, files = read_tarball(tarball)
    if top != f"{PRODUCT}-{doc['version']}":
        raise ReleaseError(f"top dir {top} does not match the version")
    if "files.sha256" not in files or "manifest.json" not in files:
        raise ReleaseError("files.sha256 or manifest.json missing")
    listed = parse_files_sha256(files["files.sha256"][0].decode())
    actual = {k: v for k, v in files.items() if k != "files.sha256"}
    if set(listed) != set(actual):
        extra, gone = set(actual) - set(listed), set(listed) - set(actual)
        raise ReleaseError(f"files.sha256 mismatch: unlisted {sorted(extra)[:5]}, missing {sorted(gone)[:5]}")
    for rel, (sha, mode) in listed.items():
        content, real_mode = actual[rel]
        if hashlib.sha256(content).hexdigest() != sha:
            raise ReleaseError(f"sha256 mismatch: {rel}")
        if real_mode != mode:
            raise ReleaseError(f"mode mismatch: {rel}")
    manifest = json.loads(files["manifest.json"][0])
    expect = {k: v for k, v in doc.items() if k != "artifacts"}
    if manifest != expect:
        diff = sorted(k for k in set(manifest) | set(expect) if manifest.get(k) != expect.get(k))
        raise ReleaseError(f"manifest.json differs from release.json in {diff}")
    if raw != dumps(doc):
        raise ReleaseError("release.json is not in canonical form")
    check_integration_kinds(files, doc["kind"])
    return doc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="verify a tarball against release.json")
    c.add_argument("--release", type=Path, required=True)
    c.add_argument("--tarball", type=Path, required=True)
    m = sub.add_parser("make", help="(re)make release.json from a tarball's manifest.json")
    m.add_argument("--tarball", type=Path, required=True)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--base-url", default=DEFAULT_BASE_URL)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "check":
            doc = check(a.release, a.tarball)
            print(f"ok: {doc['version']} seq {doc['seq']} sha256 {doc['artifacts'][0]['sha256']}")
        else:
            _, files = read_tarball(a.tarball)
            manifest = json.loads(files["manifest.json"][0])
            a.out.write_bytes(dumps(release_json(manifest, a.tarball, a.base_url)))
            print(a.out)
    except (ReleaseError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print(f"make_manifest: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
