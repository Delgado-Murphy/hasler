"""What the box remembers across a power cut: the last preset, and standby.

The whole point is that the radio behaves like a radio. Flip the mains on and
it plays whatever it was playing; leave it "off at the key" and it stays quiet.
That needs two facts on disk and nothing else.

Two rules, both about the SD card:

* **Writes are atomic** (temp file in the same directory, fsync, rename), so a
  power cut mid-write leaves the old file or the new one, never half of each.
  This box *is* switched off at the wall.
* **Failure is never fatal.** A read-only or full card means the box forgets,
  not that the box stops playing. Everything is logged and swallowed.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("/var/lib/haslerd/state.json")
FALLBACK_STATE_PATH = Path("~/.local/state/haslerd/state.json")


def _writable_dir(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(directory, os.W_OK | os.X_OK)


def resolve_state_path(path: str | Path | None = None) -> Path | None:
    """The configured path (or /var/lib default) if writable, else the fallback.

    /var/lib/haslerd needs systemd's ``StateDirectory=`` or an install step to
    exist; a dev laptop or a hand-run daemon has neither, hence the per-user
    fallback. None if neither is usable - the box then simply forgets.
    """
    for candidate in (Path(path or DEFAULT_STATE_PATH), FALLBACK_STATE_PATH):
        candidate = candidate.expanduser()
        if _writable_dir(candidate.parent):
            return candidate
    log.warning("no writable state directory; last preset will not survive a restart")
    return None


class StateStore:
    """A tiny JSON file. ``path=None`` keeps state in memory only (``--fake``)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._last: dict | None = None

    @classmethod
    def at(cls, path: str | Path | None = None) -> "StateStore":
        return cls(resolve_state_path(path))

    def load(self) -> dict:
        if self.path is None:
            return dict(self._last or {})
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("unreadable state file %s; starting fresh", self.path, exc_info=True)
            return {}
        if not isinstance(data, dict):
            return {}
        self._last = dict(data)
        return data

    def save(self, data: dict) -> None:
        """Write only when something changed - the card has finite writes."""
        if data == self._last:
            return
        self._last = dict(data)
        if self.path is None:
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(data, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            log.warning("could not write state file %s", self.path, exc_info=True)
