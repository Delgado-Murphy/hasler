"""Every path the updater touches, under one root prefix (``HASLER_ROOT``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://delgado-murphy.github.io/hasler-releases"
HEALTH_URL = "http://127.0.0.1:8080/api/health"
KEEP_RELEASES = 3


@dataclass(frozen=True)
class Layout:
    root: Path = Path("/")

    @classmethod
    def from_env(cls) -> "Layout":
        return cls(Path(os.environ.get("HASLER_ROOT") or "/"))

    def _p(self, rel: str) -> Path:
        return self.root / rel.lstrip("/")

    # --- /opt/hasler -----------------------------------------------------
    @property
    def opt(self) -> Path:
        return self._p("/opt/hasler")

    @property
    def releases(self) -> Path:
        return self.opt / "releases"

    @property
    def current(self) -> Path:
        return self.opt / "current"

    @property
    def previous(self) -> Path:
        return self.opt / "previous"

    @property
    def updater(self) -> Path:
        """The updater that is actually run - only replaced at a commit."""
        return self.opt / "updater"

    def release(self, version: str) -> Path:
        return self.releases / version

    # --- /etc/hasler -----------------------------------------------------
    @property
    def etc(self) -> Path:
        return self._p("/etc/hasler")

    @property
    def box_yaml(self) -> Path:
        return self.etc / "box.yaml"

    @property
    def trust(self) -> Path:
        return self.etc / "trust"

    # --- /var/lib --------------------------------------------------------
    @property
    def haslerd_state(self) -> Path:
        return self._p("/var/lib/haslerd")

    @property
    def upd(self) -> Path:
        """Owned by the unprivileged hasler-update user."""
        return self._p("/var/lib/hasler-update")

    @property
    def incoming(self) -> Path:
        return self.upd / "incoming"

    @property
    def staged(self) -> Path:
        return self.upd / "staged"

    @property
    def check_status(self) -> Path:
        return self.upd / "status.json"

    @property
    def apply_dir(self) -> Path:
        """Root only."""
        return self._p("/var/lib/hasler-apply")

    @property
    def state_file(self) -> Path:
        return self.apply_dir / "state.json"

    @property
    def backups(self) -> Path:
        return self.apply_dir / "backup"

    @property
    def snapshots(self) -> Path:
        return self.apply_dir / "snapshots"

    @property
    def lock_file(self) -> Path:
        return self.apply_dir / "lock"

    # --- system destinations (integration allowlist) -----------------------
    @property
    def systemd_dir(self) -> Path:
        return self._p("/etc/systemd/system")

    @property
    def udev_dir(self) -> Path:
        return self._p("/etc/udev/rules.d")

    @property
    def sbin(self) -> Path:
        return self._p("/usr/local/sbin")

    @property
    def bin(self) -> Path:
        return self._p("/usr/local/bin")

    @property
    def sudoers(self) -> Path:
        return self._p("/etc/sudoers.d/haslerd")

    @property
    def shutdown_dir(self) -> Path:
        return self._p("/usr/lib/systemd/system-shutdown")

    @property
    def config_txt(self) -> Path:
        p = self._p("/boot/firmware/config.txt")
        return p if p.exists() or not self._p("/boot/config.txt").exists() else self._p("/boot/config.txt")

    @property
    def asound_cards(self) -> Path:
        return self._p("/proc/asound/cards")

    @property
    def run_dir(self) -> Path:
        return self._p("/run/hasler-update")


def read_box_yaml(path: Path) -> dict:
    """The few flat ``key: value`` lines of box.yaml the updater needs.

    No PyYAML here (stdlib only). Only top-level scalars are read; nested
    blocks, lists and comments are skipped. ``true/false/yes/no`` become bools.
    """
    out: dict = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        if not line or line[0] in " \t#-" or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.split(" #", 1)[0].strip()
        if not val:
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        low = val.lower()
        if low in ("true", "yes", "on"):
            out[key.strip()] = True
        elif low in ("false", "no", "off") and key.strip() != "update_mode":
            out[key.strip()] = False
        else:
            out[key.strip()] = val
    return out


def box_settings(layout: Layout) -> dict:
    box = read_box_yaml(layout.box_yaml)
    mode = str(box.get("update_mode", "ask")).lower()
    return {
        "channel": str(box.get("channel", "stable")),
        "update_mode": mode if mode in ("auto", "ask", "off") else "ask",
        "box_id": str(box.get("box_id", "")),
        "metered": bool(box.get("metered", False)),
        "variant": str(box.get("variant", "")),
        "update_url": str(os.environ.get("HASLER_UPDATE_URL") or box.get("update_url") or DEFAULT_BASE_URL).rstrip("/"),
    }
