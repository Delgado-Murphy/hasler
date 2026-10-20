#!/usr/bin/env python3
"""Build a Hasler release tarball + unsigned release.json (docs/ota-format.md).

    # anywhere (macOS/Linux): code, data, integration - no wheels, no binaries
    python scripts/release/build_release.py --version 0.2.0 --out dist/ --no-wheels

    # arm64 Linux (CI: release.yml in debian:trixie): the real thing
    python scripts/release/build_release.py --version 0.2.0 --out dist/ \\
        --wheelhouse wheelhouse/ --bins --strict

Sources are read from a git ref (default HEAD), never the working tree, so
an untracked haslerd.yaml, a .venv or a cookie file cannot end up in a
release; ``--worktree`` reads the checkout instead (tests, quick looks) with
the same allowlist and deny rules. Only paths matched by ALLOWLIST below are
shipped; anything secret-looking inside an allowlisted directory fails the
build instead of being skipped quietly.

Reproducible: same ref + same wheelhouse + same binaries + same zstd = same
bytes (sorted entries, uid/gid 0, every mtime = SOURCE_DATE_EPOCH, which
defaults to the ref's commit time).

Outputs in --out: ``hasler-<v>-arm64.tar.zst``, ``release.json`` (unsigned),
``files.sha256`` (copy of the one inside), ``SHA256SUMS``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_manifest as mm  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO_DEFAULT = HERE.parents[1]

# --- what ships -------------------------------------------------------------------
# (repo glob, destination prefix in the release, how the repo path maps to it)
#   "flat"  -> prefix + basename
#   "under:<repo dir>" -> prefix + path relative to <repo dir>
# Globs: '*' within one path segment, '**' any number of segments.
@dataclass(frozen=True)
class Rule:
    glob: str
    dest: str
    strip: str | None = None     # repo dir to strip; None = basename only
    mode: int | None = None      # force a mode (helpers are always 0755)
    exts: tuple[str, ...] = ()   # if set, only these suffixes, others fail the build


PKG_EXTS = (".py", ".html", ".css", ".js", ".json", ".yaml", ".yml", ".svg", ".png",
            ".ico", ".txt", ".woff2", ".typed")

ALLOWLIST: tuple[Rule, ...] = (
    # app/: the haslerd package (incl. web/static) and its data
    Rule("daemon/haslerd/**", "app/haslerd/", strip="daemon/haslerd", exts=PKG_EXTS),
    Rule("daemon/pyproject.toml", "app/"),
    Rule("config/variants/*.yaml", "app/config/variants/"),
    Rule("config/catalog/*.json", "app/config/catalog/"),
    Rule("config/stations/*.json", "app/config/stations/"),
    Rule("config/haslerd.yaml.example", "app/config/"),
    Rule("config/box.yaml.example", "app/config/"),
    # the updater (stdlib only); a new one is used only after its release passes
    Rule("update/hasler_update/**", "app/hasler_update/", strip="update/hasler_update", exts=(".py",)),
    # data that install.sh puts in system places the contract's integration/
    # allowlist has no destination for yet (see docs/release.md "Gaps")
    Rule("config/camilladsp/*.yml", "app/config/camilladsp/"),
    Rule("config/snapcast/*.conf", "app/config/snapcast/"),
    Rule("config/captive/*.conf", "app/config/captive/"),
    Rule("config/go-librespot/config.yml", "app/config/go-librespot/"),
    Rule("config/moode/*", "app/config/moode/"),
    Rule("CHANGELOG.md", "app/"),
    # integration/: a repo-level integration/ tree wins over the legacy places below
    Rule("integration/**", "integration/", strip="integration"),
    # (config/legacy/*.service are the rsync layout's units: never shipped)
    Rule("config/*.service", "integration/systemd/"),
    Rule("config/*.timer", "integration/systemd/"),
    Rule("config/systemd/*", "integration/systemd/"),
    Rule("config/udev/*.rules", "integration/udev/"),
    Rule("update/systemd/*", "integration/systemd/"),
    Rule("update/bin/hasler-*", "integration/sbin/", mode=0o755),
    Rule("scripts/hasler-audio-mono", "integration/sbin/", mode=0o755),
    Rule("scripts/hasler-multiroom", "integration/sbin/", mode=0o755),
    Rule("scripts/hasler-wifi-join", "integration/sbin/", mode=0o755),
    Rule("scripts/hasler-lamp-boot", "integration/sbin/", mode=0o755),
    Rule("scripts/hasler-ytdlp-update", "integration/sbin/", mode=0o755),
    Rule("scripts/hasler-bootcfg-guard", "integration/sbin/", mode=0o755),
    Rule("scripts/hasler-tone", "integration/bin/", mode=0o755),
    Rule("scripts/hasler-lamp-halt", "integration/shutdown/", mode=0o755),
    Rule("config/sudoers/haslerd", "integration/sudoers/"),
    Rule("config/configtxt/hasler.block", "integration/configtxt/"),
    Rule("config/avahi/hasler.service", "integration/avahi/"),
    # migrations/: NNN_name.py
    Rule("migrations/*.py", "migrations/"),
    Rule("daemon/migrations/*.py", "migrations/"),
)

# integration/ destinations the box accepts (docs/ota-format.md)
INTEGRATION_OK = re.compile(
    r"^integration/(systemd/[^/]+\.(service|timer)|udev/[^/]+\.rules|sbin/hasler-[^/]+|"
    r"bin/hasler-[^/]+|sudoers/haslerd|shutdown/hasler-[^/]+|configtxt/hasler\.block|"
    r"avahi/hasler\.service)$")
MIGRATION_OK = re.compile(r"^migrations/\d{3}_[a-z0-9_]+\.py$")

# skipped quietly wherever they appear
NOISE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache)(/|$)|\.py[cod]$|(^|/)\.DS_Store$")
# fail the build if an allowlisted glob ever matches one of these
DENY_PARTS = {".venv", "venv", "secrets", ".git", "node_modules", ".ssh"}
DENY_NAMES = {"haslerd.yaml", "keys.yaml", "box.yaml", "settings.json", "state.json",
              "credentials.json", "cookies.txt", ".env", ".netrc", "id_rsa", "id_ed25519"}
DENY_SUFFIX = (".pem", ".key", ".p12", ".pfx", ".sqlite", ".db", ".age", ".gpg")
DENY_CONTENT = (b"PRIVATE KEY-----", b"BEGIN OPENSSH PRIVATE")

SELFTEST_SHIM = HERE / "selftest_shim.py"
WHEEL_RE = re.compile(r"^(?P<name>[^-]+)-(?P<ver>[^-]+)(-\d[^-]*)?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")


def log(msg: str) -> None:
    print(f"build_release: {msg}", file=sys.stderr)


def glob_re(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


# --- sources -----------------------------------------------------------------------

@dataclass
class Src:
    path: str      # repo-relative, posix
    mode: int      # 0o644 or 0o755
    data: bytes


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def sources_from_git(repo: Path, ref: str) -> tuple[list[Src], list[str]]:
    """(regular files, symlink paths) at ref."""
    out, links = [], []
    for entry in git(repo, "ls-tree", "-r", "-z", "--full-tree", ref).split(b"\0"):
        if not entry:
            continue
        meta, path = entry.split(b"\t", 1)
        mode, kind, sha = meta.split()
        p = path.decode()
        if kind != b"blob":
            continue          # submodules: never shipped
        if mode == b"120000":
            links.append(p)
            continue
        out.append(Src(p, 0o755 if mode == b"100755" else 0o644, b""))
    # one cat-file --batch for all blobs
    if out:
        req = "".join(f"{ref}:{s.path}\n" for s in out).encode()
        res = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"], input=req,
                             check=True, capture_output=True).stdout
        pos = 0
        for s in out:
            nl = res.index(b"\n", pos)
            header = res[pos:nl].split()
            size = int(header[2])
            s.data = res[nl + 1: nl + 1 + size]
            pos = nl + 1 + size + 1
    return out, links


def sources_from_worktree(repo: Path) -> tuple[list[Src], list[str]]:
    out, links = [], []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            full = Path(root) / name
            rel = full.relative_to(repo).as_posix()
            if full.is_symlink():
                links.append(rel)
                continue
            if not full.is_file():
                continue
            mode = 0o755 if os.access(full, os.X_OK) else 0o644
            out.append(Src(rel, mode, b""))   # read lazily: only allowlisted ones
    for s in out:
        s.data = None  # type: ignore[assignment]
    return out, links


def deny_reason(path: str, data: bytes | None) -> str | None:
    p = PurePosixPath(path)
    if DENY_PARTS & set(p.parts):
        return "directory never shipped"
    if p.name in DENY_NAMES or p.name.startswith("id_") or p.suffix in DENY_SUFFIX:
        return "secret/config file name"
    if data is not None and any(sig in data for sig in DENY_CONTENT):
        return "contains a private key"
    return None


def select(srcs: list[Src], links: list[str], repo: Path) -> dict[str, tuple[bytes, int]]:
    """Apply ALLOWLIST: {release path: (content, mode)}. Raises on any violation."""
    compiled = [(r, glob_re(r.glob)) for r in ALLOWLIST]
    for link in links:
        if any(rx.match(link) for _, rx in compiled) and not NOISE.search(link):
            raise mm.ReleaseError(f"{link}: symlinks are not shipped")
    chosen: dict[str, tuple[bytes, int, str]] = {}
    for s in srcs:
        if NOISE.search(s.path):
            continue
        for rule, rx in compiled:
            if not rx.match(s.path):
                continue
            if s.data is None:
                s.data = (repo / s.path).read_bytes()
            why = deny_reason(s.path, s.data)
            if why:
                raise mm.ReleaseError(f"{s.path}: refused ({why}) - remove it or narrow the allowlist")
            if rule.exts and PurePosixPath(s.path).suffix not in rule.exts:
                raise mm.ReleaseError(f"{s.path}: unexpected file type under {rule.glob}")
            if rule.strip is None:
                dest = rule.dest + PurePosixPath(s.path).name
            else:
                dest = rule.dest + PurePosixPath(s.path).relative_to(rule.strip).as_posix()
            if dest in chosen:
                prev = chosen[dest][2]
                if prev.startswith("integration/") and rule.dest.startswith("integration/"):
                    log(f"{s.path}: {dest} already comes from {prev}, skipping the legacy copy")
                    break
                raise mm.ReleaseError(f"{dest}: both {prev} and {s.path} map here")
            chosen[dest] = (s.data, rule.mode if rule.mode is not None else s.mode, s.path)
            break
    for dest in chosen:
        if dest.startswith("integration/") and not INTEGRATION_OK.match(dest):
            raise mm.ReleaseError(f"{dest}: not an integration destination the box accepts")
        if dest.startswith("migrations/") and not MIGRATION_OK.match(dest):
            raise mm.ReleaseError(f"{dest}: migrations are named NNN_name.py")
    return {k: (v[0], v[1]) for k, v in chosen.items()}


def lint_integration(files: dict[str, tuple[bytes, int]]) -> list[str]:
    """Problems that must be fixed before a real release (errors with --strict)."""
    problems = []
    for dest, (data, _) in sorted(files.items()):
        if not dest.startswith("integration/systemd/"):
            continue
        text = data.decode(errors="replace")
        if "/opt/hasler/releases/" in text:
            problems.append(f"{dest}: references a version path; use /opt/hasler/current")
        if "/home/pi" in text:
            problems.append(f"{dest}: still references /home/pi (units must use /opt/hasler/current)")
    return problems


# --- binaries (go-librespot, deno) ---------------------------------------------------

def pins_from_install_sh(repo_files: dict[str, bytes]) -> dict[str, str]:
    text = repo_files.get("scripts/install.sh", b"").decode()
    pins = dict(re.findall(r"^(GLR_VERSION|GLR_SHA256|DENO_VERSION|DENO_SHA256)=(\S+)", text, re.M))
    if len(pins) != 4:
        raise mm.ReleaseError("could not read GLR_/DENO_ pins from scripts/install.sh")
    return pins


def fetch(url: str, sha256: str, cache: Path) -> bytes:
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / sha256
    if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha256:
        return dest.read_bytes()
    log(f"downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - https, pinned hash
        data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise mm.ReleaseError(f"checksum mismatch for {url}: {got}")
    dest.write_bytes(data)
    return data


def binaries(pins: dict[str, str], cache: Path) -> dict[str, tuple[bytes, int]]:
    glr = fetch(f"https://github.com/devgianlu/go-librespot/releases/download/{pins['GLR_VERSION']}/"
                "go-librespot_linux_arm64.tar.gz", pins["GLR_SHA256"], cache)
    with tarfile.open(fileobj=io.BytesIO(glr), mode="r:gz") as tf:
        glr_bin = tf.extractfile("go-librespot").read()
    deno = fetch(f"https://github.com/denoland/deno/releases/download/{pins['DENO_VERSION']}/"
                 "deno-aarch64-unknown-linux-gnu.zip", pins["DENO_SHA256"], cache)
    with zipfile.ZipFile(io.BytesIO(deno)) as zf:
        deno_bin = zf.read("deno")
    return {"bin/go-librespot": (glr_bin, 0o755), "bin/deno": (deno_bin, 0o755)}


# --- wheelhouse -----------------------------------------------------------------------

def norm_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def wheelhouse(src: Path, apt_deps: list[str]) -> tuple[dict[str, tuple[bytes, int]], bytes, list[str]]:
    """Wheel files, a --require-hashes lock generated from exactly these wheels, warnings."""
    out, lines, warn = {}, [], []
    wheels = sorted(src.glob("*.whl"))
    if not wheels:
        raise mm.ReleaseError(f"no wheels in {src}")
    seen = set()
    for w in wheels:
        m = WHEEL_RE.match(w.name)
        if not m:
            raise mm.ReleaseError(f"not a wheel file name: {w.name}")
        name = norm_name(m["name"])
        if name in seen:
            raise mm.ReleaseError(f"two wheels for {name}")
        seen.add(name)
        py, abi, plat = m["py"], m["abi"], m["plat"]
        pure = plat == "any"
        if not pure:
            if not ("aarch64" in plat and ("cp313" in py or abi == "abi3")):
                raise mm.ReleaseError(f"{w.name}: not an aarch64 cp313/abi3 wheel")
            if plat.startswith("linux_") and not apt_deps:
                warn.append(f"{w.name}: not repaired (linux_aarch64) - its system libraries "
                            "must be declared with --apt-dep")
        data = w.read_bytes()
        out[f"wheelhouse/{w.name}"] = (data, 0o644)
        lines.append(f"{name}=={m['ver']} \\\n    --hash=sha256:{hashlib.sha256(data).hexdigest()}\n")
    lock = ("# hash-locked to the wheels in wheelhouse/; install with\n"
            "#   pip install --no-index --find-links wheelhouse --require-hashes -r requirements.lock\n"
            + "".join(lines)).encode()
    return out, lock, warn


def build_wheels_here(repo: Path, dest: Path) -> None:
    if not (platform.system() == "Linux" and platform.machine() in ("aarch64", "arm64")):
        raise mm.ReleaseError("wheels are built on arm64 Linux only (CI); use --no-wheels or --wheelhouse DIR")
    subprocess.run(["bash", str(HERE / "build_wheels.sh"), str(repo / "daemon"), str(dest)], check=True)


# --- archive -----------------------------------------------------------------------------

def files_sha256(files: dict[str, tuple[bytes, int]]) -> bytes:
    return "".join(f"{hashlib.sha256(d).hexdigest()}  {m:04o}  {p}\n"
                   for p, (d, m) in sorted(files.items())).encode()


def make_tar(top: str, files: dict[str, tuple[bytes, int]], mtime: int, dirs: set[str]) -> bytes:
    entries: dict[str, tuple[bytes | None, int]] = {top: (None, 0o755)}
    for d in dirs:
        entries[f"{top}/{d}"] = (None, 0o755)
    for path, (data, mode) in files.items():
        parts = PurePosixPath(path).parts
        for i in range(1, len(parts)):
            entries.setdefault(f"{top}/{'/'.join(parts[:i])}", (None, 0o755))
        entries[f"{top}/{path}"] = (data, mode)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name in sorted(entries):
            data, mode = entries[name]
            ti = tarfile.TarInfo(name)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = mtime
            ti.mode = mode
            if data is None:
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def zstd_compress(data: bytes, level: int) -> bytes:
    exe = shutil.which("zstd")
    if not exe:
        raise mm.ReleaseError("need the zstd CLI (apt install zstd / brew install zstd)")
    return subprocess.run([exe, f"-{level}", "-T1", "-q", "-c", "--no-progress"], input=data,
                          check=True, capture_output=True).stdout


# --- metadata -------------------------------------------------------------------------------

def changelog_notes(text: str, version: str) -> str:
    m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def previous_version(repo: Path, ref: str, version: str) -> str | None:
    try:
        tags = git(repo, "tag", "--merged", ref, "--list", "v*").decode().split()
    except subprocess.CalledProcessError:
        return None
    vs = []
    for t in tags:
        try:
            v = t[1:]
            mm.parse_version(v)
        except mm.ReleaseError:
            continue
        if v != version and mm.seq_for(v) < mm.seq_for(version):
            vs.append(v)
    return max(vs, key=mm.seq_for) if vs else None


def build(args: argparse.Namespace) -> Path:
    repo = args.repo.resolve()
    mm.parse_version(args.version)
    if args.worktree:
        srcs, links = sources_from_worktree(repo)
        commit_time = int(git(repo, "log", "-1", "--format=%ct").strip() or 0) if (repo / ".git").exists() else 0
    else:
        srcs, links = sources_from_git(repo, args.ref)
        commit_time = int(git(repo, "log", "-1", "--format=%ct", args.ref).strip())
    sde = args.source_date_epoch
    if sde is None:
        sde = int(os.environ["SOURCE_DATE_EPOCH"]) if os.environ.get("SOURCE_DATE_EPOCH") else commit_time
    files = select(srcs, links, repo)
    by_path = {s.path: (s.data if s.data is not None else (repo / s.path).read_bytes())
               for s in srcs if s.path in ("scripts/install.sh", "daemon/pyproject.toml",
                                            "daemon/haslerd/boxconfig.py", "CHANGELOG.md")}
    problems = lint_integration(files)

    pyproject = by_path.get("daemon/pyproject.toml", b"").decode()
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M)
    if not m or m.group(1) != args.version:
        problems.append(f"daemon/pyproject.toml version {m.group(1) if m else '?'} != {args.version}")

    # migrations/: the contract's top-level copy of the package's migrations
    # (haslerd/migrations/NNN_name.py, also importable from app/)
    for p, v in sorted(files.items()):
        m_ = re.match(r"^app/haslerd/migrations/(\d{3}_[a-z0-9_]+\.py)$", p)
        if m_:
            if f"migrations/{m_.group(1)}" in files:
                raise mm.ReleaseError(f"migrations/{m_.group(1)}: in the package and in migrations/")
            files[f"migrations/{m_.group(1)}"] = v

    # selftest.py: thin entry point into haslerd.selftest (contract: run as a file)
    files["selftest.py"] = (SELFTEST_SHIM.read_bytes(), 0o755)

    dirs = {"app", "integration", "migrations", "wheelhouse", "bin"}
    if args.no_wheels:
        files["requirements.lock"] = (b"# development build (--no-wheels): no wheelhouse, not installable\n", 0o644)
        log("--no-wheels: wheelhouse/ is empty; this tarball is for testing only")
    else:
        wh_dir = args.wheelhouse
        tmp = None
        if wh_dir is None:
            tmp = tempfile.TemporaryDirectory()
            wh_dir = Path(tmp.name)
            build_wheels_here(repo, wh_dir)
        apt_file = wh_dir / "apt-deps.txt"     # written by build_wheels.sh if a repair failed
        if apt_file.exists():
            args.apt_dep += [ln.strip() for ln in apt_file.read_text().splitlines() if ln.strip()]
        wfiles, lock, warns = wheelhouse(wh_dir, args.apt_dep)
        files.update(wfiles)
        files["requirements.lock"] = (lock, 0o644)
        problems += warns
        if tmp:
            tmp.cleanup()
    if args.bins:
        files.update(binaries(pins_from_install_sh(by_path), args.bin_cache))
    else:
        log("no --bins: bin/ is empty (go-librespot, deno not bundled)")

    for p in problems:
        log(("error: " if args.strict else "warning: ") + p)
    if problems and args.strict:
        raise mm.ReleaseError(f"{len(problems)} problem(s) with --strict")

    sm = re.search(r"^SCHEMA\s*=\s*(\d+)", by_path.get("daemon/haslerd/boxconfig.py", b"").decode(), re.M)
    variants = [PurePosixPath(p).stem for p in files if p.startswith("app/config/variants/")]
    requires = dict(mm.DEFAULT_REQUIRES, moode=args.moode)
    if args.apt_dep:
        requires["apt"] = sorted(set(args.apt_dep))
    notes = args.notes
    if notes is None:
        notes = changelog_notes(by_path.get("CHANGELOG.md", b"").decode(), args.version)
    kind = args.kind or mm.kind_between(args.prev_version or previous_version(repo, args.ref, args.version),
                                        args.version)
    manifest = mm.build_manifest(
        version=args.version, created=sde, seq=args.seq, channel=args.channel, kind=kind,
        requires_migration=args.requires_migration, min_from_seq=args.min_from_seq,
        requires=requires, config_schema=int(sm.group(1)) if sm else 1, variants=variants,
        notes=notes, keyring_seq=args.keyring_seq)
    files["manifest.json"] = (mm.dumps(manifest), 0o644)
    sums = files_sha256(files)
    files["files.sha256"] = (sums, 0o644)

    top = f"{mm.PRODUCT}-{args.version}"
    tar = make_tar(top, files, sde, dirs)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    tarball = out / mm.artifact_name(args.version)
    tarball.write_bytes(zstd_compress(tar, args.zstd_level))
    rel = mm.release_json(manifest, tarball, args.base_url)
    (out / "release.json").write_bytes(mm.dumps(rel))
    (out / "files.sha256").write_bytes(sums)
    (out / "SHA256SUMS").write_text(
        "".join(f"{mm.sha256_file(out / n)}  {n}\n" for n in sorted([tarball.name, "release.json"])))
    mm.check(out / "release.json", tarball)
    log(f"{tarball} ({tarball.stat().st_size} bytes) sha256 {rel['artifacts'][0]['sha256']}")
    return tarball


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="X.Y.Z or X.Y.Z-rc.N (no leading v)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--repo", type=Path, default=REPO_DEFAULT)
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--ref", default="HEAD", help="git ref to build from (default HEAD)")
    src.add_argument("--worktree", action="store_true", help="read the checkout instead of a git ref")
    wh = ap.add_mutually_exclusive_group()
    wh.add_argument("--no-wheels", action="store_true", help="skip the wheelhouse (dev/test build)")
    wh.add_argument("--wheelhouse", type=Path, help="directory of prebuilt (repaired) wheels")
    ap.add_argument("--apt-dep", action="append", default=[], help="apt runtime dep -> requires.apt")
    ap.add_argument("--bins", action="store_true", help="bundle pinned go-librespot + deno (downloads)")
    ap.add_argument("--bin-cache", type=Path, default=Path.home() / ".cache" / "hasler-release")
    ap.add_argument("--strict", action="store_true", help="warnings are errors (CI release)")
    ap.add_argument("--seq", type=int, help="override the version-derived seq")
    ap.add_argument("--channel", choices=mm.CHANNELS)
    ap.add_argument("--kind", choices=mm.KINDS, help="default: from the previous v* tag (none -> major)")
    ap.add_argument("--prev-version")
    ap.add_argument("--requires-migration", action="store_true")
    ap.add_argument("--min-from-seq", type=int, default=0)
    ap.add_argument("--keyring-seq", type=int, default=1)
    ap.add_argument("--moode", default=mm.DEFAULT_REQUIRES["moode"])
    ap.add_argument("--notes", help="default: the CHANGELOG.md section for --version")
    ap.add_argument("--base-url", default=mm.DEFAULT_BASE_URL)
    ap.add_argument("--source-date-epoch", type=int)
    ap.add_argument("--zstd-level", type=int, default=19)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        build(args)
    except (mm.ReleaseError, subprocess.CalledProcessError, OSError) as e:
        detail = getattr(e, "stderr", None)
        log(f"error: {e}" + (f"\n{detail.decode(errors='replace')}" if isinstance(detail, bytes) else ""))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
