"""``hasler-integrate``: a release's declarative system files, put in place.

``integration/`` mirrors destinations under an allowlist only (docs/ota-format.md).
Every write is atomic (tmp in the destination dir + fsync + rename). Before a
destination is first touched for a release, its old copy (or the fact that
there was none) is recorded in ``backup/<tag>/`` - so a rollback restores
exactly what was there before the switch, whoever had put it there
(install.sh included), and removes what the release added.

The updater's own units (``hasler-update-*``) and the watchdog are never taken
from a release under trial: a broken release must not be able to break its
own rollback. They come with the updater, installed at commit.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .layout import Layout
from .state import fsync_dir, read_json, write_atomic, write_json

BLOCK_BEGIN = "# >>> hasler (managed by hasler-update; edits here are replaced) >>>"
BLOCK_END = "# <<< hasler <<<"
LEGACY_BLOCKS = [("# >>> hasler pilot lamp (scripts/install.sh; edits here are replaced) >>>",
                  "# <<< hasler pilot lamp <<<")]
# The updater's own launcher and units: never put in place during a trial
# (a broken release must not break its own rollback) - installed at commit.
RESERVED = re.compile(r"^hasler-update")


class IntegrateError(Exception):
    pass


@dataclass
class Item:
    src: Path
    dest: Path
    mode: int
    kind: str


def _rules(layout: Layout) -> list[tuple[str, re.Pattern, Callable[[str], Path], int]]:
    return [
        ("systemd", re.compile(r"^[A-Za-z0-9@._-]+\.(service|timer)$"), lambda n: layout.systemd_dir / n, 0o644),
        ("systemd-dropin", re.compile(r"^[A-Za-z0-9@._-]+\.service\.d/hasler-[A-Za-z0-9._-]+\.conf$"),
         lambda n: layout.systemd_dir / n, 0o644),
        ("udev", re.compile(r"^[A-Za-z0-9._-]+\.rules$"), lambda n: layout.udev_dir / n, 0o644),
        ("sbin", re.compile(r"^hasler-[A-Za-z0-9._-]+$"), lambda n: layout.sbin / n, 0o755),
        ("bin", re.compile(r"^hasler-[A-Za-z0-9._-]+$"), lambda n: layout.bin / n, 0o755),
        ("sudoers", re.compile(r"^haslerd$"), lambda n: layout.sudoers, 0o440),
        ("shutdown", re.compile(r"^hasler-[A-Za-z0-9._-]+$"), lambda n: layout.shutdown_dir / n, 0o755),
        ("configtxt", re.compile(r"^hasler\.block$"), lambda n: layout.config_txt, 0o644),
    ]


def plan(layout: Layout, release_dir: Path, reserved: bool = False) -> list[Item]:
    """What ``release_dir/integration`` installs. Anything off the allowlist is an error.

    ``reserved=False``: everything but the updater's own files (``hasler-update*``
    units and sbin launcher); ``True``: only those (installed at commit)."""
    base = release_dir / "integration"
    if not base.is_dir():
        return []
    rules = {r[0]: r for r in _rules(layout)}
    items: list[Item] = []
    for p in sorted(base.rglob("*")):
        if p.is_dir() and not p.is_symlink():
            continue
        rel = p.relative_to(base).as_posix()
        top, _, rest = rel.partition("/")
        if p.is_symlink() or not p.is_file():
            raise IntegrateError(f"integration/{rel}: not a regular file")
        kind = top
        if top == "systemd" and "/" in rest:
            kind = "systemd-dropin"
        rule = rules.get(kind)
        if rule is None or not rule[1].match(rest):
            raise IntegrateError(f"integration/{rel}: not on the allowlist")
        if bool(RESERVED.match(rest.split("/")[0])) != reserved:
            continue
        if kind in ("systemd", "systemd-dropin"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if "/opt/hasler/releases/" in text:
                raise IntegrateError(f"integration/{rel}: references a version path (use /opt/hasler/current)")
        items.append(Item(p, rule[2](rest), rule[3], kind))
    return items


# --- config.txt block --------------------------------------------------------------


def _strip_blocks(text: str) -> tuple[str, str | None]:
    """config.txt without any hasler block, and the current block's body."""
    lines, out, body, skip = text.splitlines(), [], None, None
    pairs = [(BLOCK_BEGIN, BLOCK_END)] + LEGACY_BLOCKS
    cur: list[str] = []
    for line in lines:
        if skip is None:
            match = next((e for b, e in pairs if line == b), None)
            if match is not None:
                skip, cur = match, []
                continue
            out.append(line)
        elif line == skip:
            if skip == BLOCK_END:
                body = "\n".join(cur)
            skip = None
        else:
            cur.append(line)
    return "\n".join(out).rstrip("\n") + "\n", body


def render_config_txt(current: str, block: str) -> str:
    rest, _ = _strip_blocks(current)
    return rest + BLOCK_BEGIN + "\n" + block.strip("\n") + "\n" + BLOCK_END + "\n"


def config_block(current: str) -> str | None:
    return _strip_blocks(current)[1]


# --- applying ---------------------------------------------------------------------


class Integrator:
    """Puts items in place for ``tag`` (the release being switched to)."""

    def __init__(self, layout: Layout, tag: str, checker: Callable[[Item], None] | None = None,
                 chown: Callable[[Path], None] | None = None) -> None:
        self.layout = layout
        self.tag = tag
        self.dir = layout.backups / tag
        self.checker = checker
        self.chown = chown or (lambda p: None)
        self.changed: set[str] = set()

    @property
    def manifest_path(self) -> Path:
        return self.dir / "manifest.json"

    def manifest(self) -> dict:
        return read_json(self.manifest_path, {}) or {}

    def reset(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def _backup(self, dest: Path) -> None:
        """Record dest's pre-switch state once (first backup wins)."""
        man = self.manifest()
        key = str(dest)
        if key in man:
            return
        if dest.exists() or dest.is_symlink():
            copy = self.dir / "files" / dest.relative_to(self.layout.root).as_posix()
            self.layout.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, copy, follow_symlinks=False)
            st = os.lstat(dest)
            man[key] = {"was": "file", "mode": stat.S_IMODE(st.st_mode), "uid": st.st_uid, "gid": st.st_gid}
        else:
            man[key] = {"was": "absent"}
        write_json(self.manifest_path, man, 0o600)

    def _put(self, dest: Path, data: bytes, mode: int, kind: str) -> None:
        try:
            if dest.is_file() and not dest.is_symlink() and dest.read_bytes() == data \
                    and stat.S_IMODE(dest.stat().st_mode) == mode:
                return
        except OSError:
            pass
        self._backup(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(dest, data, mode)
        self.chown(dest)
        self.changed.add(kind)

    def remove(self, dest: Path, kind: str) -> None:
        if not (dest.exists() or dest.is_symlink()):
            return
        self._backup(dest)
        dest.unlink()
        fsync_dir(dest.parent)
        self.changed.add(kind)

    def apply(self, items: list[Item], obsolete: list[Path] = (), allow_configtxt: bool = False) -> None:
        for it in items:
            if self.checker is not None:
                self.checker(it)
        for it in items:
            data = it.src.read_bytes()
            if it.kind == "configtxt":
                try:
                    current = it.dest.read_text(encoding="utf-8")
                except OSError:
                    raise IntegrateError(f"{it.dest} missing - cannot place the config.txt block")
                if (config_block(current) or "").strip("\n") == data.decode().strip("\n"):
                    continue
                if not allow_configtxt:
                    raise IntegrateError("config.txt block changed: needs a reboot (apply --allow-reboot)")
                self._put(it.dest, render_config_txt(current, data.decode()).encode(), 0o644, "configtxt")
            else:
                self._put(it.dest, data, it.mode, it.kind)
        planned = {str(it.dest) for it in items}
        for dest in obsolete:
            if str(dest) not in planned and dest != self.layout.config_txt:
                self.remove(dest, "systemd" if dest.suffix in (".service", ".timer") else "other")

    def restore(self) -> set[str]:
        """Put back everything recorded for this tag. Idempotent. Returns kinds touched."""
        touched: set[str] = set()
        for key, info in self.manifest().items():
            dest = Path(key)
            kind = "configtxt" if dest == self.layout.config_txt else (
                "udev" if dest.suffix == ".rules" else "systemd")
            if info.get("was") == "file":
                copy = self.dir / "files" / dest.relative_to(self.layout.root).as_posix()
                if not copy.exists() and not copy.is_symlink():
                    continue
                if copy.is_symlink():
                    tmp = dest.with_name(f".{dest.name}.restore")
                    tmp.unlink(missing_ok=True)
                    os.symlink(os.readlink(copy), tmp)
                    os.replace(tmp, dest)
                else:
                    data = copy.read_bytes()
                    if dest.is_file() and dest.read_bytes() == data:
                        continue
                    write_atomic(dest, data, int(info.get("mode", 0o644)))
                    try:
                        if os.geteuid() == 0:
                            os.chown(dest, int(info.get("uid", 0)), int(info.get("gid", 0)))
                    except OSError:
                        pass
                touched.add(kind)
            elif dest.exists() or dest.is_symlink():
                dest.unlink()
                fsync_dir(dest.parent)
                touched.add(kind)
        return touched


def dests(items: list[Item]) -> list[str]:
    return [str(it.dest) for it in items]


def dump(items: list[Item]) -> str:
    return json.dumps([{"src": str(i.src), "dest": str(i.dest), "mode": oct(i.mode), "kind": i.kind} for i in items], indent=2)
