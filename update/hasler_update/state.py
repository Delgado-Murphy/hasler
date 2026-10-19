"""Atomic JSON files, and the root-owned apply state (docs/ota-format.md).

Every write: tmp in the same dir + fsync + rename + fsync of the directory,
so a power cut leaves either the old or the new file, never half of one.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

PHASES = ("idle", "trial", "probation", "committed", "rolled_back")
PROBATION_S = 24 * 3600
HISTORY_KEEP = 30


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    fsync_dir(path.parent)


def write_json(path: Path, doc: Any, mode: int = 0o644) -> None:
    write_atomic(path, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode(), mode)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if ts is None else ts))


def parse_iso(s: str) -> float:
    """``2026-10-20T03:00:00Z`` -> epoch seconds. Raises ValueError."""
    import calendar

    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1]
    s = s.split(".")[0].split("+")[0]
    return float(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")))


DEFAULT_STATE: dict = {
    "max_seq": 0,
    "keyring_seq": 0,
    "phase": "idle",
    "from": None,
    "to": None,
    "to_seq": None,
    "boots": 0,
    "boot_id": None,   # the kernel boot id a trial began under (a restart is not a boot)
    "deadline": None,
    "bad": [],
    "history": [],
}


class ApplyState:
    """``/var/lib/hasler-apply/state.json`` - the switch's memory.

    Contract fields plus our own: ``to_seq``, ``step`` (where inside the
    trial we are: ``integrate`` -> ``switched`` -> ``restarted``, or
    ``rollback`` while rolling back), ``baseline`` (the health probe on the
    old release), ``integrated`` (destination paths of the release's system
    files), ``reason`` (why the last one rolled back). Readable by all
    (0644) so haslerd's key page can show the last result.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.doc = self.load()

    def load(self) -> dict:
        doc = dict(DEFAULT_STATE)
        doc["bad"], doc["history"] = [], []
        stored = read_json(self.path, {})
        if isinstance(stored, dict):
            doc.update(stored)
        if doc.get("phase") not in PHASES:
            doc["phase"] = "idle"
        return doc

    def save(self) -> None:
        self.doc["history"] = self.doc.get("history", [])[-HISTORY_KEEP:]
        write_json(self.path, self.doc, 0o644)

    def __getitem__(self, key: str) -> Any:
        return self.doc.get(key)

    def update(self, **fields: Any) -> None:
        self.doc.update(fields)
        self.save()

    def log(self, event: str, **fields: Any) -> None:
        self.doc.setdefault("history", []).append({"at": iso(), "event": event, **fields})

    def mark_bad(self, seq: int | None) -> None:
        if seq is not None and seq not in self.doc["bad"]:
            self.doc["bad"].append(int(seq))
