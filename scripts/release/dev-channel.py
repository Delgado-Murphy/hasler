#!/usr/bin/env python3
"""A local, DEV-signed release channel for bench OTA drills - no GitHub.

Signs with the throwaway keys from ``dev-keys.sh`` and lays out a site the box
fetches exactly like the public one (docs/ota-format.md):

    SITE/keyring.json(.sig)
    SITE/channels/<channel>.json(.sig)             signed by timestamp-dev
    SITE/releases/v<version>/release.json(.sig)    signed by release-dev
    SITE/releases/v<version>/hasler-<version>-arm64.tar.zst

    # after build_release.py (or dev-build.sh) wrote dist/<version>/
    dev-channel.py publish --keys ~/.hasler-dev/keys --dist dist/0.2.0-rc.1 \\
        --site ~/.hasler-dev/site --base-url http://laptop.local:8000
    dev-channel.py serve --site ~/.hasler-dev/site --port 8000      # = python -m http.server
    dev-channel.py halt --keys ... --site ... --seq 20099           # halt drill
    dev-channel.py refresh --keys ... --site ...                    # re-sign before the 14-day expiry

On the box: ``update_url: <base-url>`` in /etc/hasler/box.yaml
(``ota-bootstrap.sh --update-url URL``) and ``channel: bench``. ``file://``
base URLs work too (a copy of SITE on the box, outside /home and /tmp - the
check unit cannot see those).

Unlike the production tools this accepts http:// and file:// URLs; everything
is still signed and hash-checked, the transport just is not TLS. The release's
artifact URL is rewritten to the local site and release.json re-signed with the
dev release key - the tarball itself is untouched.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_manifest as mm  # noqa: E402


def _load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ch = _load("hasler_dev_channel_tool", "channel.py")
# Dev only: a LAN http server or a directory on the box. The box accepts these
# too (fetch.py); signatures and hashes are what make them safe.
ch.URL_RE = re.compile(r"^(https?|file)://[^\s]+/release\.json$")


class DevError(Exception):
    pass


def sign(keys: Path, kind: str, *files: Path) -> None:
    key = keys / f"{kind}-dev"
    if not key.exists():
        raise DevError(f"no {key} - run scripts/release/dev-keys.sh first")
    ns = {"release": "hasler-release", "timestamp": "hasler-channel"}[kind]
    r = subprocess.run(["bash", str(HERE / "sign.sh"), "-n", ns, "-k", str(key), "-p", str(key) + ".pub",
                        *map(str, files)], capture_output=True, text=True)
    if r.returncode != 0:
        raise DevError(f"signing failed: {r.stderr.strip()}")


def index_path(site: Path, channel: str) -> Path:
    return site / "channels" / f"{channel}.json"


def copy_keyring(keys: Path, site: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    for n in ("keyring.json", "keyring.json.sig"):
        shutil.copyfile(keys / n, site / n)


def cmd_publish(a: argparse.Namespace) -> None:
    keys, site, dist = Path(a.keys).expanduser(), Path(a.site).expanduser(), Path(a.dist).expanduser()
    base = a.base_url.rstrip("/")
    if not re.match(r"^(https?|file)://", base):
        raise DevError("--base-url must be http(s):// or file://")
    rel = json.loads((dist / "release.json").read_text())
    art = rel["artifacts"][0]
    tarball = dist / art["name"]
    if not tarball.exists():
        raise DevError(f"{tarball} missing")
    if mm.sha256_file(tarball) != art["sha256"]:
        raise DevError(f"{tarball} does not match its release.json")
    version = rel["version"]
    if rel.get("channel") != a.channel:
        print(f"dev-channel: note: release.json says channel {rel.get('channel')!r}, publishing on {a.channel!r}",
              file=sys.stderr)
    rdir = site / "releases" / f"v{version}"
    rdir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tarball, rdir / tarball.name)
    art["url"] = f"{base}/releases/v{version}/{tarball.name}"
    (rdir / "release.json").write_bytes(mm.dumps(rel))
    sign(keys, "release", rdir / "release.json")
    idx = index_path(site, a.channel)
    argv = ["promote", "--index", str(idx), "--release", str(rdir / "release.json"),
            "--release-url", f"{base}/releases/v{version}/release.json"]
    if a.force:
        argv.append("--force")
    if ch.main(argv) != 0:
        raise DevError("channel promote refused (see above)")
    sign(keys, "timestamp", idx)
    copy_keyring(keys, site)
    print(f"dev-channel: {a.channel} -> {version} (seq {rel['seq']}) at {base}\n"
          f"  box: update_url: {base}   channel: {a.channel}\n"
          f"  then: sudo systemctl start hasler-update-check.service; cat /var/lib/hasler-update/status.json")


def cmd_halt(a: argparse.Namespace) -> None:
    keys, site = Path(a.keys).expanduser(), Path(a.site).expanduser()
    idx = index_path(site, a.channel)
    if ch.main(["halt", "--index", str(idx), "--seq", str(a.seq)]) != 0:
        raise DevError("halt refused")
    sign(keys, "timestamp", idx)


def cmd_refresh(a: argparse.Namespace) -> None:
    keys, site = Path(a.keys).expanduser(), Path(a.site).expanduser()
    idx = index_path(site, a.channel)
    if ch.main(["refresh", str(idx)]) != 0:
        raise DevError("refresh refused")
    sign(keys, "timestamp", idx)
    copy_keyring(keys, site)


def cmd_serve(a: argparse.Namespace) -> None:
    site = Path(a.site).expanduser()
    print(f"dev-channel: serving {site} on http://0.0.0.0:{a.port}/ (Ctrl-C to stop)", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "http.server", str(a.port), "--bind", a.bind, "--directory", str(site)])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("publish", help="put dist/ (release.json + tarball) on the channel")
    p.add_argument("--keys", default="~/.hasler-dev/keys")
    p.add_argument("--dist", required=True, help="build_release.py --out dir")
    p.add_argument("--site", default="~/.hasler-dev/site")
    p.add_argument("--base-url", required=True, help="where the box finds SITE, e.g. http://laptop.local:8000")
    p.add_argument("--channel", default="bench", choices=ch.CHANNELS)
    p.add_argument("--force", action="store_true", help="allow a seq that is not higher")
    h = sub.add_parser("halt")
    h.add_argument("--keys", default="~/.hasler-dev/keys")
    h.add_argument("--site", default="~/.hasler-dev/site")
    h.add_argument("--channel", default="bench", choices=ch.CHANNELS)
    h.add_argument("--seq", type=int, required=True)
    r = sub.add_parser("refresh")
    r.add_argument("--keys", default="~/.hasler-dev/keys")
    r.add_argument("--site", default="~/.hasler-dev/site")
    r.add_argument("--channel", default="bench", choices=ch.CHANNELS)
    s = sub.add_parser("serve")
    s.add_argument("--site", default="~/.hasler-dev/site")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--bind", default="0.0.0.0")
    a = ap.parse_args(argv)
    try:
        {"publish": cmd_publish, "halt": cmd_halt, "refresh": cmd_refresh, "serve": cmd_serve}[a.cmd](a)
    except (DevError, ch.ChannelError, mm.ReleaseError, OSError, KeyError, ValueError) as e:
        print(f"dev-channel: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
