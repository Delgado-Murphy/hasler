#!/usr/bin/env python3
"""Bump the pinned go-librespot and deno builds (workflow deps-bump.yml).

Stdlib only. Run from the repository root.

    deps_bump.py check --out bump.json            # newest upstream releases, sha256 of the arm64 assets
    deps_bump.py apply --plan bump.json [--version X.Y.Z]   # edit the pins (+ the release's version)
    deps_bump.py next-version                      # the next patch after the newest final v* tag

``check`` downloads each newer asset and hashes it itself; GitHub's own asset
digest, when the API gives one, must agree. ``auto`` in the plan is true when
every bump stays inside the policy below (CI may then merge it by itself);
anything bigger opens a PR for a person.

Policy (go-librespot is 0.x: a minor can change its config file):
    go-librespot  same major.minor (patch releases)
    deno          same major
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

INSTALL_SH = Path("scripts/install.sh")
NOTICES = Path("THIRD_PARTY_NOTICES.md")
PYPROJECT = Path("daemon/pyproject.toml")
INIT = Path("daemon/haslerd/__init__.py")
CHANGELOG = Path("CHANGELOG.md")

DEPS = {
    "GLR": {"repo": "devgianlu/go-librespot", "asset": "go-librespot_linux_arm64.tar.gz",
            "name": "go-librespot", "what": "the Spotify engine", "notice": "[go-librespot](",
            "same": 2},
    "DENO": {"repo": "denoland/deno", "asset": "deno-aarch64-unknown-linux-gnu.zip",
             "name": "Deno", "what": "the JavaScript runtime yt-dlp uses for YouTube", "notice": "[Deno](",
             "same": 1},
}
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class BumpError(Exception):
    pass


def api(path: str) -> dict:
    req = urllib.request.Request(f"https://api.github.com/{path}",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "hasler-deps-bump"})
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def sha256_url(url: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    req = urllib.request.Request(url, headers={"User-Agent": "hasler-deps-bump"})
    with urllib.request.urlopen(req, timeout=300) as r:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def vtuple(tag: str) -> tuple[int, int, int]:
    m = TAG_RE.match(tag)
    if not m:
        raise BumpError(f"unexpected tag {tag!r} (want vX.Y.Z)")
    return tuple(int(x) for x in m.groups())


def read_pins(text: str) -> dict:
    pins = dict(re.findall(r"^(GLR_VERSION|GLR_SHA256|DENO_VERSION|DENO_SHA256)=(\S+)", text, re.M))
    if len(pins) != 4:
        raise BumpError(f"{INSTALL_SH}: could not read the four GLR_/DENO_ pins")
    return pins


def gh_output(**kv: str) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in kv.items())
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(lines)
    sys.stdout.write(lines)


def cmd_check(a: argparse.Namespace) -> None:
    pins = read_pins(INSTALL_SH.read_text())
    bumps = []
    for key, d in DEPS.items():
        old = pins[f"{key}_VERSION"]
        rel = api(f"repos/{d['repo']}/releases/latest")      # never drafts or pre-releases
        new = rel["tag_name"]
        if vtuple(new) <= vtuple(old):
            print(f"{d['name']}: {old} is current (upstream {new})")
            continue
        asset = next((x for x in rel.get("assets", []) if x["name"] == d["asset"]), None)
        if asset is None:
            raise BumpError(f"{d['name']} {new}: no asset {d['asset']}")
        sha, size = sha256_url(asset["browser_download_url"])
        digest = (asset.get("digest") or "").removeprefix("sha256:")
        if digest and digest != sha:
            raise BumpError(f"{d['name']} {new}: downloaded sha256 {sha} != GitHub's digest {digest}")
        if asset.get("size") and asset["size"] != size:
            raise BumpError(f"{d['name']} {new}: size {size} != {asset['size']}")
        auto = vtuple(new)[:d["same"]] == vtuple(old)[:d["same"]]
        bumps.append({"key": key, "name": d["name"], "what": d["what"], "old": old, "new": new,
                      "old_sha256": pins[f"{key}_SHA256"], "sha256": sha, "size": size, "auto": auto,
                      "release_url": rel.get("html_url", ""), "asset_url": asset["browser_download_url"]})
        print(f"{d['name']}: {old} -> {new} sha256 {sha} ({size} bytes, {'auto' if auto else 'needs review'})")
    plan = {"bumps": bumps, "auto": bool(bumps) and all(b["auto"] for b in bumps)}
    Path(a.out).write_text(json.dumps(plan, indent=2) + "\n")
    gh_output(changed=str(bool(bumps)).lower(), auto=str(plan["auto"]).lower(),
              title=title(bumps) if bumps else "")


def title(bumps: list[dict]) -> str:
    return "deps: " + ", ".join(f"{b['name']} {b['new']}" for b in bumps)


def sub_line(text: str, pattern: str, repl: str, path: Path) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M)
    if n != 1:
        raise BumpError(f"{path}: no line matching {pattern!r}")
    return new


def next_version() -> str:
    tags = subprocess.run(["git", "tag", "-l", "v*"], capture_output=True, text=True, check=True).stdout.split()
    finals = sorted((vtuple(t) for t in tags if TAG_RE.match(t)), reverse=True)
    if not finals:
        raise BumpError("no final v* tag")
    x, y, z = finals[0]
    return f"{x}.{y}.{z + 1}"


def cmd_next(a: argparse.Namespace) -> None:
    print(next_version())


def cmd_apply(a: argparse.Namespace) -> None:
    plan = json.loads(Path(a.plan).read_text())
    if not plan["bumps"]:
        print("nothing to bump")
        return
    sh, notices = INSTALL_SH.read_text(), NOTICES.read_text()
    for b in plan["bumps"]:
        k = b["key"]
        if not re.fullmatch(r"[0-9a-f]{64}", b["sha256"]):
            raise BumpError(f"{b['name']}: bad sha256")
        sh = sub_line(sh, rf"^{k}_VERSION=\S+$", f"{k}_VERSION={b['new']}", INSTALL_SH)
        sh = sub_line(sh, rf"^{k}_SHA256=\S+$", f"{k}_SHA256={b['sha256']}", INSTALL_SH)
        # the notices name the version in the table row (text, tag and source
        # links) and in the prose under it; the exact tag string is unique to it
        if sum(ln.startswith("| " + DEPS[k]["notice"]) for ln in notices.splitlines()) != 1:
            raise BumpError(f"{NOTICES}: expected one table row for {b['name']}")
        notices = re.sub(rf"(?<![0-9.]){re.escape(b['old'])}(?![0-9])", b["new"], notices)
    INSTALL_SH.write_text(sh)
    NOTICES.write_text(notices)
    print("pins: " + ", ".join(f"{b['name']} {b['new']}" for b in plan["bumps"]))
    if a.version:
        if not re.fullmatch(r"\d+\.\d+\.\d+", a.version):
            raise BumpError(f"bad --version {a.version}")
        PYPROJECT.write_text(sub_line(PYPROJECT.read_text(), r'^version = "[^"]+"$', f'version = "{a.version}"',
                                      PYPROJECT))
        INIT.write_text(sub_line(INIT.read_text(), r'^__version__ = "[^"]+"$', f'__version__ = "{a.version}"', INIT))
        log = CHANGELOG.read_text()
        if f"## {a.version} " in log:
            raise BumpError(f"{CHANGELOG} already has {a.version}")
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        items = "".join(f"- {b['name']} ({b['what']}) {b['new'].lstrip('v')}, was {b['old'].lstrip('v')}.\n"
                        for b in plan["bumps"])
        entry = (f"## {a.version} - {day}\n\nA routine update of the programs the radio is built on, "
                 f"nothing else changes:\n\n{items}\n")
        head = "# Changelog\n\n"
        if not log.startswith(head):
            raise BumpError(f"{CHANGELOG} does not start with '# Changelog'")
        CHANGELOG.write_text(head + entry + log[len(head):])
        print(f"version: {a.version} (pyproject, __init__, CHANGELOG)")


def cmd_body(a: argparse.Namespace) -> None:
    plan = json.loads(Path(a.plan).read_text())
    out = ["Automatic dependency update (`.github/workflows/deps-bump.yml`).", "",
           "| component | from | to | arm64 asset sha256 | policy |", "|---|---|---|---|---|"]
    for b in plan["bumps"]:
        out.append(f"| [{b['name']}]({b['release_url']}) | {b['old']} | {b['new']} | `{b['sha256']}` | "
                   f"{'auto-merge' if b['auto'] else '**needs review** (bigger than the auto policy)'} |")
    out += ["", "Each asset was downloaded and hashed by the workflow; GitHub's asset digest agreed.",
            f"Version: {a.version or 'unchanged - master has unreleased work, so this rides on the next release'}."]
    if a.version:
        out += [f"After merge, `auto-release.yml` tags `v{a.version}`; `release.yml` builds it and "
                "`sign-publish` puts it on **bench**. Auto-promotion moves it to stable after 7 days."]
    out += ["", "Python packages (ytmusicapi and the rest) are not pinned in the repo: the release build "
            "resolves them fresh (`build_wheels.sh`, `uv pip compile --generate-hashes`), so every release "
            "already carries the newest ones."]
    if not plan["auto"]:
        out += ["", "Not merged automatically: a bump is outside the policy in `scripts/ci/deps_bump.py`. "
                "Check the upstream changelog (config changes for go-librespot, breaking changes for deno)."]
    Path(a.out).write_text("\n".join(out) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("apply")
    p.add_argument("--plan", required=True)
    p.add_argument("--version")
    p.set_defaults(fn=cmd_apply)
    p = sub.add_parser("next-version")
    p.set_defaults(fn=cmd_next)
    p = sub.add_parser("body")
    p.add_argument("--plan", required=True)
    p.add_argument("--version")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_body)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (BumpError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print(f"deps-bump: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
