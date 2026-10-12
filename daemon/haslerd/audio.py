"""Mono output for a one-speaker cabinet.

The Amp4 Pro is stereo and the Hasler has one speaker, on the left channel (L+ / L−).
Without a downmix anything panned right is simply lost. The mix happens in
ALSA, below every player: ``scripts/hasler-audio-mono`` (root, via sudoers)
adds or removes an override of moOde's ``_audioout`` device - which MPD,
Bluetooth, AirPlay and (as installed) go-librespot all play into.

ALSA reads its configuration when a device is opened, so a player that is
already playing keeps what it had. ``MonoSwitch.set`` restarts MPD's current
stream so radio and YouTube Music change at once; Spotify, Bluetooth and
AirPlay follow from their next track or connection.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

HELPER = "/usr/local/sbin/hasler-audio-mono"
OVERRIDE = Path("/etc/alsa/conf.d/.hasler-mono")   # the helper's mono marker
DEFAULT = True          # one speaker: mono unless the owner says otherwise


class MonoError(RuntimeError):
    """The helper failed; the message is fit to show on the page."""


class MonoSwitch:
    def __init__(
        self,
        helper: str = HELPER,
        override: Path = OVERRIDE,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.helper = helper
        self.override = override
        self._run = run

    @property
    def available(self) -> bool:
        """Installed on this box (not on a laptop running --fake)."""
        return os.access(self.helper, os.X_OK)

    def is_on(self) -> bool:
        return self.override.exists()

    def set(self, on: bool, reopen: bool = True) -> bool:
        """Switch mono on/off. Returns the new state; raises MonoError."""
        if not self.available:
            raise MonoError("mono output isn't installed on this radio (run install.sh)")
        try:
            out = self._run(["sudo", "-n", self.helper, "on" if on else "off"],
                            capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MonoError(f"could not switch mono: {exc}") from exc
        try:
            doc = json.loads((out.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            doc = {"ok": False, "error": (out.stderr or "no answer").strip()[:200]}
        if not doc.get("ok"):
            raise MonoError(f"could not switch mono: {doc.get('error') or 'unknown error'}")
        state = bool(doc.get("mono"))
        log.info("audio output: %s", "mono" if state else "stereo")
        if reopen:
            self._reopen_mpd()
        return state

    def _reopen_mpd(self) -> None:
        """Restart MPD's stream if it is playing, so it reopens the device."""
        try:
            status = self._run(["mpc", "status"], capture_output=True, text=True, timeout=5)
            if "[playing]" not in (status.stdout or ""):
                return
            self._run(["mpc", "-q", "stop"], capture_output=True, timeout=5)
            self._run(["mpc", "-q", "play"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            log.debug("could not restart MPD after the mono switch", exc_info=True)
