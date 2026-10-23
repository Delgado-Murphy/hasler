#!/usr/bin/env python3
"""components.json: the yt-dlp builds the radios may install (workflow components.yml).

Stdlib only. Signing is separate (``sign.sh -n hasler-components``, the
release key), so this never touches a key.

    components.py pypi --out pypi.json                       # newest stable + nightly on PyPI
    components.py plan --pypi pypi.json [--current components.json] \\
        [--block V] [--unblock V] --out plan.json            # what to build, what to keep
    components.py lock --version V --out requirements.txt    # hash-locked, aarch64 cp313 wheels
    components.py test --version V --requirements requirements.txt \\
        --canary canary.json [--deno /path/deno] --out tested.json
    components.py assemble --plan plan.json --results DIR --canary canary.json \\
        [--current components.json] --out components.json    # exit 0 always; prints changed=true|false
    components.py check components.json                      # schema, expiry, requirement lines

Schema (the radio reads it; docs/release.md "The yt-dlp lane"):

    {"schema": 1, "issued": "...Z", "expires": "...Z (14 d)",
     "components": {"yt-dlp": {
       "candidates": [{"version": "...", "channel": "stable|nightly",
                       "requirements": "<pip --require-hashes text>",
                       "tested": {"install": true, "offline": true, "online": true|false|null}}],
       "blocked": ["..."],
       "canary": {"ytm_ids": ["..."], "min_bytes": 65536}}}}

Kept: the newest two of each channel that passed install + offline (so a radio
can go back to the previous one by hash). Blocked versions are dropped and
never built again until unblocked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PKG = "yt-dlp"
SPEC_EXTRA = "default"
EXPIRES_DAYS = 14
RESIGN_AFTER_H = 24          # re-sign an unchanged doc once a day (expiry stays ~14 d ahead)
KEEP_PER_CHANNEL = 2
CHANNELS = ("stable", "nightly")
PYPI_URL = f"https://pypi.org/pypi/{PKG}/json"
# Wheels for the radio: Raspberry Pi OS trixie, aarch64, CPython 3.13. The
# glibc ceiling is the runner's (ubuntu-24.04, 2.39) so the lock installs on
# the runner too; trixie has 2.41, so every wheel picked here runs there.
TARGET_PY = "3.13"
TARGET_ABI = "cp313"
GLIBC_MAX = 39
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*(\.dev[0-9]+|(a|b|rc)[0-9]+)?$")
# The lock format the radio accepts (scripts/hasler-ytdlp-update validate_requirements):
# a "name[extras]==version \\" line, then "    --hash=sha256:<hex>" lines (all but
# the last ending in " \\"). No options, URLs, paths, markers or -e.
REQ_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?==[A-Za-z0-9.!+_-]+ \\$")
REQ_HASH_RE = re.compile(r"^--hash=sha256:[0-9a-f]{64}( \\)?$")
YTM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
# yt-dlp's message when YouTube wants a login: the runner's IP is bot-flagged.
BOT_RE = re.compile(r"Sign in to confirm|not a bot|HTTP Error 429|po_token|PO Token", re.I)


class ComponentsError(Exception):
    pass


# --- small helpers -------------------------------------------------------------

def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def vkey(v: str) -> tuple:
    """PEP 440 order for the shapes yt-dlp uses: 2026.10.21 and 2026.10.21.233146.dev0."""
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)*)(?:\.dev([0-9]+)|(a|b|rc)([0-9]+))?", v)
    if not m:
        raise ComponentsError(f"unsupported version {v!r}")
    rel = tuple(int(x) for x in m.group(1).split("."))
    rel = rel + (0,) * (6 - len(rel))
    if m.group(2) is not None:
        pre = (0, 0, int(m.group(2)))                       # .devN: before everything
    elif m.group(3):
        pre = (1, "abrc".index(m.group(3)) if m.group(3) != "rc" else 3, int(m.group(4)))
    else:
        pre = (9, 0, 0)                                     # final
    return rel + pre


def channel_of(v: str) -> str:
    return "stable" if vkey(v)[6] == 9 else "nightly"


def dumps(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2, ensure_ascii=True) + "\n").encode()


def write_out(path: str, doc: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(dumps(doc))


def gh_output(**kv: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in kv.items())
    if out:
        with open(out, "a") as f:
            f.write(lines)
    sys.stdout.write(lines)


# --- validation ------------------------------------------------------------------

def validate_requirements(text: str, version: str) -> None:
    """The first package is yt-dlp[default]==<version>; every package has a hash;
    yt-dlp-ejs is in it; nothing but package and hash lines."""
    if not isinstance(text, str) or not text.strip() or len(text) > 256 * 1024:
        raise ComponentsError(f"{version}: empty or oversized requirements")
    pkgs: list[str] = []
    hashes = 0
    cont = False                     # the previous line ended in a backslash
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            raise ComponentsError(f"{version}: blank line in the lock")
        if REQ_HASH_RE.match(line):
            if not pkgs or not cont:
                raise ComponentsError(f"{version}: a hash that belongs to no package")
            hashes += 1
            cont = line.endswith("\\")
            continue
        if REQ_PKG_RE.match(line):
            if cont or (pkgs and not hashes):
                raise ComponentsError(f"{version}: {pkgs[-1] if pkgs else line} has no hash")
            pkgs.append(line[:-1].strip())
            hashes, cont = 0, True
            continue
        raise ComponentsError(f"{version}: not allowed in the lock: {line[:80]!r}")
    if not pkgs or not hashes or cont:
        raise ComponentsError(f"{version}: the lock ends without a hash")
    if pkgs[0] != f"{PKG}[{SPEC_EXTRA}]=={version}":
        raise ComponentsError(f"{version}: the first line must be {PKG}[{SPEC_EXTRA}]=={version}, not {pkgs[0]!r}")
    names = [re.split(r"[\[=]", p_, maxsplit=1)[0].lower().replace("_", "-") for p_ in pkgs]
    if len(set(names)) != len(names):
        raise ComponentsError(f"{version}: a package listed twice")
    if "yt-dlp-ejs" not in names:
        raise ComponentsError(f"{version}: requirements lack yt-dlp-ejs (YouTube's JS challenges)")


def validate_canary(c: dict) -> dict:
    ids = c.get("ytm_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and YTM_ID_RE.match(i) for i in ids):
        raise ComponentsError("canary: ytm_ids must be a non-empty list of 11-character video ids")
    mb = c.get("min_bytes")
    if not isinstance(mb, int) or not 1024 <= mb <= 16 << 20:
        raise ComponentsError("canary: min_bytes must be an int between 1024 and 16 MiB")
    return {"ytm_ids": list(ids), "min_bytes": mb}


def validate(doc: dict, at: datetime | None = None, allow_expired: bool = False) -> None:
    if doc.get("schema") != 1:
        raise ComponentsError("components: schema must be 1")
    issued, expires = parse_iso(doc["issued"]), parse_iso(doc["expires"])
    if expires <= issued:
        raise ComponentsError("components: expires not after issued")
    if not allow_expired and expires <= (at or now()):
        raise ComponentsError(f"components expired {doc['expires']}")
    comp = doc.get("components", {}).get(PKG)
    if not isinstance(comp, dict):
        raise ComponentsError(f"components: no {PKG}")
    blocked = comp.get("blocked")
    if not isinstance(blocked, list) or not all(isinstance(b, str) and VERSION_RE.match(b) for b in blocked):
        raise ComponentsError("components: blocked must be a list of versions")
    validate_canary(comp.get("canary") or {})
    seen = set()
    for c in comp.get("candidates", []):
        v = c.get("version", "")
        if not VERSION_RE.match(v) or v in seen:
            raise ComponentsError(f"components: bad or duplicate candidate version {v!r}")
        seen.add(v)
        if c.get("channel") != channel_of(v):
            raise ComponentsError(f"components: {v} is not on channel {c.get('channel')!r}")
        if v in blocked:
            raise ComponentsError(f"components: {v} is both a candidate and blocked")
        validate_requirements(c.get("requirements", ""), v)
        t = c.get("tested") or {}
        if t.get("install") is not True or t.get("offline") is not True or t.get("online") not in (True, False, None):
            raise ComponentsError(f"components: {v}: tested must be install/offline true, online true|false|null")
        if set(c) != {"version", "channel", "requirements", "tested"} or set(t) != {"install", "offline", "online"}:
            raise ComponentsError(f"components: {v}: unexpected keys")


def load_current(path: str | None) -> dict | None:
    if not path or not Path(path).is_file():
        return None
    doc = json.loads(Path(path).read_text())
    validate(doc, allow_expired=True)       # an expired doc still carries the blocked list
    return doc


# --- pypi --------------------------------------------------------------------------

def fetch_pypi(url: str = PYPI_URL) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "hasler-components"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def summarize_pypi(data: dict) -> dict:
    """Non-yanked versions that ship a py3 wheel, newest stable and newest nightly."""
    avail = []
    for v, files in data.get("releases", {}).items():
        if not VERSION_RE.match(v) or not files:
            continue
        if any(f.get("yanked") for f in files):
            continue
        if not any(f.get("packagetype") == "bdist_wheel" for f in files):
            continue
        avail.append(v)
    avail.sort(key=vkey, reverse=True)
    return {"package": PKG, "fetched": iso(now()), "available": avail,
            "stable": next((v for v in avail if channel_of(v) == "stable"), None),
            "nightly": next((v for v in avail if channel_of(v) == "nightly"), None)}


def cmd_pypi(a: argparse.Namespace) -> None:
    data = json.loads(Path(a.from_file).read_text()) if a.from_file else fetch_pypi()
    s = summarize_pypi(data)
    write_out(a.out, s)
    print(f"pypi: newest stable {s['stable']}, newest nightly {s['nightly']} ({len(s['available'])} usable versions)")


# --- plan -----------------------------------------------------------------------------

def cmd_plan(a: argparse.Namespace) -> None:
    pypi = json.loads(Path(a.pypi).read_text())
    cur = load_current(a.current)
    comp = (cur or {}).get("components", {}).get(PKG, {})
    blocked = set(comp.get("blocked", []))
    for v in a.block:
        if not VERSION_RE.match(v):
            raise ComponentsError(f"--block {v!r} is not a version")
        blocked.add(v)
    for v in a.unblock:
        blocked.discard(v)
    avail = set(pypi["available"])
    keep = [c for c in comp.get("candidates", []) if c["version"] not in blocked and c["version"] in avail]
    have = {c["version"] for c in keep}
    build = []
    for ch in CHANNELS:
        # the newest two unblocked of each channel (the previous one is the
        # radios' way back); only those not already tested are built
        top = [v for v in pypi["available"] if channel_of(v) == ch and v not in blocked][:KEEP_PER_CHANNEL]
        build += [{"version": v, "channel": ch} for v in top if v not in have]
    plan = {"blocked": sorted(blocked, key=vkey), "keep": keep, "build": build,
            "dropped": sorted({c["version"] for c in comp.get("candidates", [])} - have, key=vkey)}
    write_out(a.out, plan)
    matrix = json.dumps({"include": build}, separators=(",", ":"))
    print(f"plan: build {[b['version'] for b in build]}, keep {sorted(have, key=vkey)}, "
          f"blocked {plan['blocked']}, dropped {plan['dropped']}")
    gh_output(matrix=matrix, any=str(bool(build)).lower())


# --- lock -------------------------------------------------------------------------------

WHEEL_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.]+)-(?P<ver>[A-Za-z0-9_.!+]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")


def platform_args() -> list[str]:
    args = ["--only-binary", ":all:", "--python-version", TARGET_PY, "--implementation", "cp",
            "--abi", TARGET_ABI, "--abi", "abi3", "--abi", "none"]
    for minor in range(GLIBC_MAX, 16, -1):
        args += ["--platform", f"manylinux_2_{minor}_aarch64"]
    args += ["--platform", "manylinux2014_aarch64", "--platform", "linux_aarch64", "--platform", "any"]
    return args


def lock_from_dir(dl: Path, version: str) -> str:
    """pip's --require-hashes format, yt-dlp[default] first, the rest by name."""
    entries = {}
    for f in sorted(dl.iterdir()):
        m = WHEEL_RE.match(f.name)
        if not m:
            raise ComponentsError(f"lock: {f.name} is not a wheel (sdists are never installed on a radio)")
        name = m.group("name").lower().replace("_", "-").replace(".", "-")
        if name in entries:
            raise ComponentsError(f"lock: two wheels for {name}")
        entries[name] = (m.group("ver"), hashlib.sha256(f.read_bytes()).hexdigest())
    if entries.get(PKG, ("",))[0] != version:
        raise ComponentsError(f"lock: pip fetched {PKG} {entries.get(PKG, ('none',))[0]}, not {version}")

    def block(spec: str, h: str) -> str:
        return f"{spec} \\\n    --hash=sha256:{h}\n"
    out = block(f"{PKG}[{SPEC_EXTRA}]=={version}", entries.pop(PKG)[1])
    out += "".join(block(f"{n}=={v}", h) for n, (v, h) in sorted(entries.items()))
    return out


def cmd_lock(a: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="ytdlp-lock-") as tmp:
        dl = Path(tmp) / "dl"
        cmd = [a.python, "-m", "pip", "download", "--quiet", "--disable-pip-version-check", "--no-cache-dir",
               "--dest", str(dl), *([] if a.native else platform_args()), f"{PKG}[{SPEC_EXTRA}]=={a.version}"]
        subprocess.run(cmd, check=True)
        text = lock_from_dir(dl, a.version)
    validate_requirements(text, a.version)
    Path(a.out).write_text(text)
    print(f"lock: {a.version}: {text.count('--hash=')} wheels -> {a.out}")
    sys.stdout.write(text)


# --- test --------------------------------------------------------------------------------

OFFLINE_PY = r"""
import importlib.metadata, re, sys, yt_dlp, yt_dlp_ejs
from yt_dlp.extractor.youtube import YoutubeIE
base = lambda v: re.sub(r"\.?dev[0-9]*$", "", v)   # nightlies say 2026.10.21.233146dev
have = importlib.metadata.version("yt-dlp")
assert base(have) == base(sys.argv[1]), have
assert YoutubeIE.suitable("https://music.youtube.com/watch?v=KJEzFvXx3Xw")
print("offline ok", yt_dlp.version.__version__)
"""


def run(cmd: list[str], timeout: int = 300, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


def first_bytes(cmd: list[str], n: int, env: dict, timeout: int = 180) -> tuple[int, str]:
    """Run yt-dlp writing the stream to stdout; return (bytes read, up to n; stderr tail).
    A watchdog kills it after ``timeout`` s, so a stalled stream can't hang the job."""
    with tempfile.TemporaryFile() as err:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, env=env)
        dog = threading.Timer(timeout, p.kill)
        dog.start()
        got = 0
        try:
            while got < n:
                chunk = p.stdout.read(min(65536, n - got))
                if not chunk:
                    break
                got += len(chunk)
        finally:
            dog.cancel()
            p.stdout.close()
            if p.poll() is None:
                p.kill()
            p.wait(timeout=30)
        err.seek(0)
        return got, err.read().decode(errors="replace")[-2000:]


def online_check(ytdlp: str, canary: dict, env: dict) -> tuple[bool | None, list[str]]:
    """True: a canary id played (yt-dlp's own downloader, bestaudio, as the radio
    plays it) and min_bytes of audio came back. False: YouTube answered and
    nothing played. None: inconclusive (bot-flagged runner IP, no network) -
    never blocks."""
    notes, verdicts = [], []
    for vid in canary["ytm_ids"]:
        url = f"https://music.youtube.com/watch?v={vid}"
        try:
            got, err = first_bytes([ytdlp, "--no-config", "--no-playlist", "--no-cache-dir", "--no-part",
                                    "--no-progress", "-f", "bestaudio", "-o", "-", url],
                                   canary["min_bytes"], env)
        except (OSError, subprocess.SubprocessError) as e:
            notes.append(f"{vid}: {e}"); verdicts.append(None); continue
        if got >= canary["min_bytes"]:
            notes.append(f"{vid}: {got} bytes of bestaudio")
            return True, notes
        bot = bool(BOT_RE.search(err))
        last = [ln for ln in err.splitlines() if "ERROR" in ln][-1:] or err.strip().splitlines()[-1:] or ["?"]
        notes.append(f"{vid}: {'bot-flagged' if bot else 'failed'} after {got} bytes: {last[0][:200]}")
        verdicts.append(None if bot or "Unable to download" in err or "timed out" in err else False)
    # one clear failure (YouTube answered, nothing played) outweighs bot-flagged ids
    return (False if any(v is False for v in verdicts) else None), notes


def cmd_test(a: argparse.Namespace) -> None:
    text = Path(a.requirements).read_text()
    validate_requirements(text, a.version)
    canary = validate_canary(json.loads(Path(a.canary).read_text()))
    tested = {"install": False, "offline": False, "online": None}
    notes: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ytdlp-test-") as tmp:
        venv = Path(tmp) / "venv"
        try:
            subprocess.run([a.python, "-m", "venv", str(venv)], check=True)
            py, ytdlp = venv / "bin" / "python", venv / "bin" / "yt-dlp"
            p = run([str(py), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-cache-dir",
                     "--require-hashes", "--no-deps", "--only-binary", ":all:", "-r", a.requirements], timeout=600)
            tested["install"] = p.returncode == 0
            notes.append("install: " + ("ok" if p.returncode == 0 else (p.stderr or p.stdout)[-400:]))
            if tested["install"]:
                p1 = run([str(py), "-I", "-c", OFFLINE_PY, a.version])
                p2 = run([str(ytdlp), "--version"])
                # the same offline extraction the radio runs (scripts/hasler-ytdlp-update)
                page = Path(tmp) / "index.html"
                page.write_text("<html><head><title>hasler selftest</title></head><body>"
                                f"<video src=\"{(Path(tmp) / 'tone.mp3').as_uri()}\"></video></body></html>\n")
                p3 = run([str(ytdlp), "--ignore-config", "--no-cache-dir", "--enable-file-urls", "--simulate",
                          "--no-warnings", "--print", "title", page.as_uri()])
                want = a.version.split(".dev")[0]
                ok = (p1.returncode == 0 and p2.returncode == 0 and p2.stdout.strip().startswith(want)
                      and "hasler selftest" in p3.stdout)
                tested["offline"] = ok
                notes.append(f"offline: {'ok' if ok else 'FAILED'} (import {p1.returncode}, --version "
                             f"{p2.stdout.strip()!r}, generic {p3.returncode}) {(p1.stderr + p3.stderr)[-300:]}")
            if tested["offline"] and not a.no_online:
                env = dict(os.environ)
                if a.deno:
                    env["PATH"] = f"{Path(a.deno).parent}{os.pathsep}{env.get('PATH', '')}"
                if not shutil.which("deno", path=env.get("PATH")):
                    notes.append("online: skipped - no deno (yt-dlp needs a JS runtime for YouTube)")
                else:
                    tested["online"], on = online_check(str(ytdlp), canary, env)
                    notes += [f"online: {n}" for n in on]
        except (OSError, subprocess.SubprocessError) as e:
            notes.append(f"error: {e}")
    write_out(a.out, {"version": a.version, "channel": channel_of(a.version), "requirements": text,
                      "tested": tested, "notes": notes})
    print(f"test {a.version}: {tested}")
    for n in notes:
        print("  " + n)


# --- assemble ---------------------------------------------------------------------------

def strip_stamp(doc: dict | None) -> dict | None:
    if doc is None:
        return None
    return {k: v for k, v in doc.items() if k not in ("issued", "expires")}


def cmd_assemble(a: argparse.Namespace) -> None:
    plan = json.loads(Path(a.plan).read_text())
    canary = validate_canary(json.loads(Path(a.canary).read_text()))
    cur = load_current(a.current)
    blocked = plan["blocked"]
    cands = {c["version"]: c for c in plan["keep"]}
    failed = []
    for b in plan["build"]:
        f = Path(a.results) / b["version"] / "tested.json"
        if not f.is_file():
            failed.append(f"{b['version']}: no test result (the build job failed)")
            continue
        r = json.loads(f.read_text())
        if r.get("version") != b["version"]:
            failed.append(f"{b['version']}: result is for {r.get('version')}")
            continue
        try:
            validate_requirements(r["requirements"], b["version"])
        except ComponentsError as e:
            failed.append(str(e)); continue
        t = r["tested"]
        if t.get("install") is not True or t.get("offline") is not True:
            failed.append(f"{b['version']}: install {t.get('install')}, offline {t.get('offline')} - not offered")
            continue
        online = t.get("online") if t.get("online") in (True, False) else None
        cands[b["version"]] = {"version": b["version"], "channel": channel_of(b["version"]),
                               "requirements": r["requirements"],
                               "tested": {"install": True, "offline": True, "online": online}}
    out = []
    for ch in CHANNELS:
        same = sorted((c for c in cands.values() if c["channel"] == ch), key=lambda c: vkey(c["version"]),
                      reverse=True)
        out += same[:KEEP_PER_CHANNEL]
    at = now()
    doc = {"schema": 1, "issued": iso(at), "expires": iso(at + timedelta(days=EXPIRES_DAYS)),
           "components": {PKG: {"candidates": out, "blocked": blocked, "canary": canary}}}
    validate(doc, at)
    changed = strip_stamp(doc) != strip_stamp(cur)
    stale = cur is None or parse_iso(cur["issued"]) <= at - timedelta(hours=RESIGN_AFTER_H)
    if changed or stale or a.force:
        write_out(a.out, doc)
        write = True
    else:
        write = False
    for f in failed:
        print(f"::warning::yt-dlp {f}")
    print(f"assemble: candidates {[c['version'] for c in out]}, blocked {blocked}, "
          f"content changed {changed}, re-sign due {stale}")
    gh_output(write=str(write).lower(), changed=str(changed).lower(),
              summary=", ".join(f"{c['version']} ({c['channel']}, online {c['tested']['online']})" for c in out))


def cmd_check(a: argparse.Namespace) -> None:
    doc = json.loads(Path(a.file).read_text())
    validate(doc, allow_expired=a.allow_expired)
    comp = doc["components"][PKG]
    print(f"ok: {a.file}: {[c['version'] for c in comp['candidates']]}, blocked {comp['blocked']}, "
          f"expires {doc['expires']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pypi")
    p.add_argument("--out", required=True)
    p.add_argument("--from-file", help="a saved PyPI JSON response (tests)")
    p.set_defaults(fn=cmd_pypi)
    p = sub.add_parser("plan")
    p.add_argument("--pypi", required=True)
    p.add_argument("--current")
    p.add_argument("--block", action="append", default=[])
    p.add_argument("--unblock", action="append", default=[])
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("lock")
    p.add_argument("--version", required=True)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--native", action="store_true", help="wheels for this machine (local dry runs only)")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_lock)
    p = sub.add_parser("test")
    p.add_argument("--version", required=True)
    p.add_argument("--requirements", required=True)
    p.add_argument("--canary", required=True)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--deno")
    p.add_argument("--no-online", action="store_true")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_test)
    p = sub.add_parser("assemble")
    p.add_argument("--plan", required=True)
    p.add_argument("--results", required=True)
    p.add_argument("--canary", required=True)
    p.add_argument("--current")
    p.add_argument("--force", action="store_true", help="write even if nothing changed")
    p.add_argument("--out", required=True)
    p.set_defaults(fn=cmd_assemble)
    p = sub.add_parser("check")
    p.add_argument("file")
    p.add_argument("--allow-expired", action="store_true")
    p.set_defaults(fn=cmd_check)
    a = ap.parse_args(argv)
    try:
        a.fn(a)
    except (ComponentsError, OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        print(f"components: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
