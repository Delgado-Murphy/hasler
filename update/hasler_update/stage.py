"""Safe extraction of a verified artifact, and the tree check used on both sides.

Every member is checked *before* anything is written: one top directory
``hasler-<version>/``, no absolute paths, no ``..``, only files, dirs and
symlinks that resolve inside the tree, no hard links, devices, FIFOs, setuid,
setgid or sticky bits. Then ``tarfile`` extracts with ``filter="data"`` as a
second fence. After extraction ``files.sha256`` must list every regular file
with its hash and mode, and nothing else may be there.
"""

from __future__ import annotations

import gzip
import lzma
import os
import posixpath
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

from .state import fsync_dir, write_atomic, write_json
from .verify import VerifyError, sha256_bytes, sha256_file

LIST_NAME = "files.sha256"
ALLOWED_MODE_BITS = 0o755      # no setuid/setgid/sticky, no group/other write


class StageError(VerifyError):
    pass


# --- decompression ---------------------------------------------------------------


def decompress(src: Path, out: Path) -> Path:
    """Write the plain tar of ``src`` (.tar.zst / .tar.xz / .tar.gz / .tar) to ``out``."""
    name = src.name
    out.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith(".tar"):
        shutil.copyfile(src, out)
    elif name.endswith((".tar.gz", ".tgz")):
        with gzip.open(src, "rb") as f, open(out, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)
    elif name.endswith(".tar.xz"):
        with lzma.open(src, "rb") as f, open(out, "wb") as o:
            shutil.copyfileobj(f, o, 1 << 20)
    elif name.endswith(".tar.zst"):
        try:
            from compression import zstd  # type: ignore[import-not-found]  # Python 3.14+

            with zstd.open(src, "rb") as f, open(out, "wb") as o:
                shutil.copyfileobj(f, o, 1 << 20)
        except ImportError:
            exe = shutil.which("zstd") or shutil.which("unzstd")
            if exe is None:
                raise StageError("no zstd decompressor (apt install zstd)")
            with open(out, "wb") as o:
                proc = subprocess.run([exe, "-d", "-c", "-q", str(src)], stdout=o, stderr=subprocess.PIPE, timeout=1800)
            if proc.returncode != 0:
                raise StageError(f"zstd failed: {proc.stderr.decode(errors='replace').strip()}")
    else:
        raise StageError(f"unknown artifact format: {name}")
    return out


# --- member checks -------------------------------------------------------------------


def _inside(path: str, top: str) -> bool:
    return path == top or path.startswith(top + "/")


def check_member(m: tarfile.TarInfo, top: str) -> None:
    name = m.name
    if name.startswith("/") or "\\" in name or "\0" in name:
        raise StageError(f"unsafe path in artifact: {name!r}")
    parts = name.split("/")
    if ".." in parts or "" in parts[:-1]:
        raise StageError(f"path traversal in artifact: {name!r}")
    norm = posixpath.normpath(name)
    if not _inside(norm, top):
        raise StageError(f"member outside {top}/: {name!r}")
    if m.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise StageError(f"setuid/setgid/sticky bit on {name!r}")
    if m.isreg() or m.isdir():
        return
    if m.issym():
        target = m.linkname
        if target.startswith("/"):
            raise StageError(f"absolute symlink {name!r} -> {target!r}")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(norm), target))
        if not _inside(resolved, top) or resolved == top:
            raise StageError(f"symlink escapes the tree: {name!r} -> {target!r}")
        return
    if m.islnk():
        raise StageError(f"hard link in artifact: {name!r}")
    raise StageError(f"device, FIFO or other special file in artifact: {name!r}")


def extract(tar_path: Path, dest: Path, top: str) -> Path:
    """Check every member of ``tar_path``, then extract it under ``dest``.

    Returns ``dest/top``. ``dest`` must be empty or absent.
    """
    if dest.exists() and any(dest.iterdir()):
        raise StageError(f"{dest} is not empty")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:") as tf:
        members = tf.getmembers()
        if not members:
            raise StageError("empty artifact")
        seen = set()
        for m in members:
            check_member(m, top)
            norm = posixpath.normpath(m.name)
            if norm in seen and not m.isdir():
                raise StageError(f"duplicate member {m.name!r}")
            seen.add(norm)
        tf.extractall(dest, members=members, filter="data")
    return dest / top


# --- tree check (files.sha256) ----------------------------------------------------------


def parse_list(text: str) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            digest, mode, rel = line.split("  ", 2)
            mode_i = int(mode, 8) & 0o7777
        except ValueError as e:
            raise StageError(f"{LIST_NAME} line {n} malformed") from e
        if len(digest) != 64 or rel in out:
            raise StageError(f"{LIST_NAME} line {n} malformed or duplicate")
        if mode_i & ~ALLOWED_MODE_BITS:
            raise StageError(f"{LIST_NAME}: mode {mode} not allowed for {rel}")
        if rel.startswith("/") or ".." in rel.split("/"):
            raise StageError(f"{LIST_NAME}: unsafe path {rel!r}")
        out[rel] = (digest, mode_i)
    return out


def verify_tree(tree: Path, list_sha256: str | None = None, fix_modes: bool = True) -> dict[str, tuple[str, int]]:
    """Every regular file listed with the right hash; nothing unlisted; links inside.

    ``list_sha256``: the signed hash of files.sha256 itself (release.json
    ``files_sha256``) - without it the list only proves the extraction.
    ``fix_modes``: chmod each file to its listed mode (the canonical one).
    """
    list_path = tree / LIST_NAME
    try:
        raw = list_path.read_bytes()
    except OSError as e:
        raise StageError(f"no {LIST_NAME} in release") from e
    if list_sha256 is not None and sha256_bytes(raw) != list_sha256:
        raise StageError(f"{LIST_NAME} does not match the signed manifest")
    listed = parse_list(raw.decode("utf-8"))
    tree_real = tree.resolve()
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(tree):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = p.relative_to(tree).as_posix()
            st = os.lstat(p)
            if st.st_mode & (stat.S_ISUID | stat.S_ISGID):
                raise StageError(f"setuid/setgid on {rel}")
            if stat.S_ISLNK(st.st_mode):
                target = (p.parent / os.readlink(p)).resolve()
                if not str(target).startswith(str(tree_real) + os.sep):
                    raise StageError(f"symlink escapes the tree: {rel}")
                if rel in listed:       # a builder may list links too; their hash isn't checked
                    found.add(rel)
                continue
            if stat.S_ISDIR(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                raise StageError(f"special file in tree: {rel}")
            if rel == LIST_NAME:
                continue
            if rel not in listed:
                raise StageError(f"file not in {LIST_NAME}: {rel}")
            digest, mode = listed[rel]
            if sha256_file(p) != digest:
                raise StageError(f"hash mismatch: {rel}")
            if fix_modes:
                os.chmod(p, mode)
            elif stat.S_IMODE(st.st_mode) != mode:
                raise StageError(f"mode mismatch: {rel}")
            found.add(rel)
    missing = set(listed) - found
    if missing:
        raise StageError(f"missing from release: {sorted(missing)[0]}")
    return listed


# --- staging ---------------------------------------------------------------------


def fsync_tree(tree: Path) -> None:
    for dirpath, _dirs, files in os.walk(tree):
        for f in files:
            p = Path(dirpath) / f
            if p.is_symlink():
                continue
            fd = os.open(p, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fsync_dir(Path(dirpath))


def stage(staged_root: Path, release: dict, signed: dict[str, bytes], artifact: Path, work: Path) -> Path:
    """Extract ``artifact`` for ``release`` into ``staged_root/<seq>/``.

    ``signed`` holds the raw bytes to keep beside it for apply to re-verify:
    ``release.json``, ``release.json.sig``, ``index.json``, ``index.json.sig``.
    Harmless at any time; a power cut leaves a ``.tmp`` that the next run wipes.
    """
    seq = int(release["seq"])
    top = f"hasler-{release['version']}"
    final = staged_root / str(seq)
    tmp = staged_root / f"{seq}.tmp"
    if final.exists() and (final / "STAGED").exists():
        return final
    for p in (tmp, final):
        if p.exists():
            shutil.rmtree(p)
    tar = work / (artifact.name + ".plain.tar")
    try:
        decompress(artifact, tar)
        extract(tar, tmp / "x", top)
        os.rename(tmp / "x" / top, tmp / "tree")
        (tmp / "x").rmdir()
        verify_tree(tmp / "tree", release.get("files_sha256"))
        for name, data in signed.items():
            write_atomic(tmp / name, data, 0o644)
        fsync_tree(tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        tar.unlink(missing_ok=True)
    write_json(tmp / "STAGED", {"seq": seq, "version": release["version"], "artifact": artifact.name,
                                "release_sha256": sha256_bytes(signed["release.json"])})
    os.rename(tmp, final)
    fsync_dir(staged_root)
    return final


def clean_staged(staged_root: Path, keep: set[int]) -> None:
    if not staged_root.is_dir():
        return
    for p in staged_root.iterdir():
        name = p.name.removesuffix(".tmp")
        if p.name.endswith(".tmp") or not name.isdigit() or int(name) not in keep:
            shutil.rmtree(p, ignore_errors=True)
