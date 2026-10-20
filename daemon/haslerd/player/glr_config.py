"""Small, careful edits to go-librespot's config.yml - and its restart.

The file (``/var/lib/go-librespot/config.yml``, owned by pi like haslerd) is
written once by scripts/ota-bootstrap.sh from config/go-librespot/config.yml
and never again by a release, so anything a radio that already exists should
get is set here, at runtime: the Spotify Connect name (web/room.py) and
``zeroconf_enabled`` - Spotify for guests on the Wi-Fi (settings.json
``spotify_guests``, default on; ``SpotifyGuests`` below).

Edits touch one top-level ``key: value`` line and keep every other line,
comment and the file's mode; the write is atomic. credentials / state.json is
never touched. go-librespot reads its config only at start, so a change ends
in ``systemctl try-restart go-librespot.service`` (config/sudoers/haslerd
allows exactly that line; try-restart leaves a stopped service stopped).
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .. import boxconfig
from .renderers import GO_LIBRESPOT_CONFIG

log = logging.getLogger(__name__)

RESTART = ["sudo", "-n", "/usr/bin/systemctl", "try-restart", "go-librespot.service"]
GUESTS_DEFAULT = True          # owner, 2026-10: guests on the network can play


def restart_go_librespot() -> None:
    """``systemctl try-restart go-librespot.service``; RuntimeError says why not."""
    try:
        out = subprocess.run(RESTART, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(str(exc)) from exc
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "systemctl failed").strip()[-200:])


def _top_level(lines: list[str], key: str) -> tuple[int, str] | None:
    """(index, value without its trailing comment) of the top-level ``key:`` line."""
    for i, line in enumerate(lines):
        m = re.match(rf"{re.escape(key)}\s*:(.*)$", line)     # column 0 only
        if m:
            return i, re.sub(r"\s+#.*$", "", m.group(1)).strip()
    return None


def set_top_level(path: Path, key: str, value: str, *, append: bool = True,
                  same: Callable[[str], bool] | None = None) -> bool:
    """Make the top-level ``key: value``. True if the file changed.

    A line whose value already counts as ``value`` (``same``; default: equal
    text) is left alone, comment and all. A missing key is added at the end
    (``append``) or the top. Raises OSError (missing / unwritable file)."""
    path = Path(path)
    lines = path.read_text().splitlines()
    want = f"{key}: {value}"
    found = _top_level(lines, key)
    if found is not None:
        i, old = found
        if (same or (lambda v: v == value))(old):
            return False
        lines[i] = want
    elif append:
        lines.append(want)
    else:
        lines.insert(0, want)
    mode = path.stat().st_mode & 0o777
    boxconfig.atomic_write_text(path, "\n".join(lines) + "\n", mode=mode)
    return True


def set_device_name(path: Path, name: str) -> bool:
    """Write go-librespot's ``device_name`` (quoted, at the top if missing),
    keeping the rest of the file and its mode. True if the file changed."""
    return set_top_level(path, "device_name", f'"{name}"', append=False)


_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}


def _yaml_bool(raw: str) -> bool | None:
    v = raw.strip().strip("'\"").lower()
    return True if v in _TRUE else False if v in _FALSE else None


def zeroconf_enabled(path: Path = GO_LIBRESPOT_CONFIG) -> bool | None:
    """The file's ``zeroconf_enabled`` (go-librespot's default, False, if
    absent); None if the file can't be read."""
    try:
        found = _top_level(Path(path).read_text().splitlines(), "zeroconf_enabled")
    except OSError:
        return None
    return bool(_yaml_bool(found[1])) if found is not None else False


def set_zeroconf(path: Path, on: bool) -> bool:
    """``zeroconf_enabled: true|false``; True if the file changed. Raises OSError."""
    return set_top_level(path, "zeroconf_enabled", "true" if on else "false",
                         same=lambda old: _yaml_bool(old) is on)


class SpotifyGuests:
    """Keeps config.yml's ``zeroconf_enabled`` at the owner's ``spotify_guests``:
    applied at haslerd's start and on every change; go-librespot is restarted
    only when the file really changed."""

    def __init__(self, path: Path = GO_LIBRESPOT_CONFIG,
                 restart: Callable[[], None] = restart_go_librespot) -> None:
        self.path = Path(path)
        self.restart = restart
        self._lock = threading.Lock()
        self._said_missing = False

    def apply(self, on: bool) -> str:
        """"changed", "same", "missing" (no go-librespot here) or "failed"."""
        with self._lock:
            if not self.path.is_file():
                if not self._said_missing:
                    log.info("no %s: Spotify for guests not applied", self.path)
                    self._said_missing = True
                return "missing"
            try:
                if not set_zeroconf(self.path, on):
                    return "same"
            except OSError as exc:
                log.warning("can't write %s: %s", self.path, exc.strerror or exc)
                return "failed"
            try:
                self.restart()
            except Exception as exc:
                log.warning("Spotify for guests %s; go-librespot restart failed: %s",
                            "on" if on else "off", exc)
                return "failed"
            log.info("Spotify for guests %s; go-librespot restarted", "on" if on else "off")
            return "changed"

    def apply_async(self, on: bool) -> threading.Thread:
        t = threading.Thread(target=self.apply, args=(on,), name="spotify-guests", daemon=True)
        t.start()
        return t


def guests_from(settings: dict) -> bool:
    """settings.json's ``spotify_guests`` (default on; anything not a bool too)."""
    v = settings.get("spotify_guests", GUESTS_DEFAULT)
    return v if isinstance(v, bool) else GUESTS_DEFAULT
