"""The phone's Bluetooth volume is the radio's volume.

Left alone, a phone's volume slider over Bluetooth (AVRCP absolute volume)
only moves BlueALSA's own attenuation of the stream: software volume inside
the A2DP-sink PCM, on top of the radio's mixer ("Digital", driven by the
knob). Two volumes, multiplied. With ``bluetooth.phone_volume`` on (the
default) haslerd makes them one, while a phone plays over Bluetooth:

* BlueALSA's ``SoftVolume`` is set false on the phone's PCM, so it no longer
  scales the samples (BlueALSA 4.3.1, io.c ``io_pcm_scale``: with soft volume
  off only a mute bit silences; the level is left to the "device"). BlueALSA
  keeps that per phone in /var/lib/bluealsa; switching the setting off sets
  it back to true.
* The phone moved its slider -> the PCM's ``Volume`` changed (bluez.c copies
  BlueZ's MediaTransport1.Volume onto it) -> the radio's volume is set to it,
  0..127 read as the knob's 0..100, so floor and ceiling apply as for the
  knob, keys and page.
* The radio's volume moved (knob, keys 5/6, the page, moOde) -> the PCM's
  ``Volume`` is written; BlueALSA passes it to BlueZ's transport, which
  notifies the phone (its slider follows).

When a phone starts playing, the radio's volume wins: the phone's slider is
set to it, so a connect never changes how loud the radio is.

Feedback: after haslerd writes the phone's volume it ignores what the phone
reports for ECHO_S (phones answer with their own rounding - an iPhone has 16
steps) and takes that as the new baseline instead. The knob is absolute:
after the phone has moved the volume, the knob's position no longer matches,
and the next turn sets the volume from where the knob is (the knob wins on
movement, as after a change from the page).

One ``busctl`` call per tick (ObjectManager.GetManagedObjects on
org.bluealsa, ~15 ms, allowed for the ``audio`` group), every TICK_S while
Bluetooth is the source, on its own thread; writes only on a change.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .bluetooth import Runner, _busctl, parse_objects

log = logging.getLogger(__name__)

TICK_S = 0.5            # how often the phone's and the radio's volume are compared
ECHO_S = 1.5            # after our write, the phone's answer is not a move of its own
A2DP_MAX = 127          # AVRCP absolute volume, BlueALSA's A2DP Volume per channel
PCM1 = "org.bluealsa.PCM1"


def phone_to_knob(level: int) -> float:
    """0..127 -> the knob's 0..100."""
    return round(max(0, min(A2DP_MAX, int(level))) * 100.0 / A2DP_MAX, 1)


def knob_to_phone(knob: float) -> int:
    """The knob's 0..100 -> 0..127."""
    return int(round(max(0.0, min(100.0, float(knob))) * A2DP_MAX / 100.0))


def unpack(volume: object) -> int | None:
    """BlueALSA's Volume (L << 8 | R, bit 7 of each a mute) -> one 0..127 level."""
    if not isinstance(volume, int) or isinstance(volume, bool):
        return None
    left, right = (volume >> 8) & 0x7F, volume & 0x7F
    return (left + right + 1) // 2


def pack(level: int) -> int:
    """One 0..127 level -> BlueALSA's Volume, both channels, not muted."""
    level = max(0, min(A2DP_MAX, int(level)))
    return level << 8 | level


def phone_pcm(objects: dict) -> tuple[str, dict] | None:
    """The phone -> radio PCM (A2DP sink, BlueALSA's "source" end): the running
    one if there are two phones, else the first."""
    best = None
    for path in sorted(objects):
        pcm = objects[path].get(PCM1)
        if not pcm or str(pcm.get("Transport") or "") != "A2DP-sink" or str(pcm.get("Mode") or "") != "source":
            continue
        rank = bool(pcm.get("Running"))
        if best is None or rank > best[0]:
            best = (rank, path, pcm)
    return (best[1], best[2]) if best else None


class PhoneVolume:
    """Keeps a Bluetooth phone's volume and the radio's the same.

    ``get_volume`` / ``set_volume`` are the player's (knob scale 0..100);
    ``enabled`` reads the owner's setting. ``tick(now, active)`` from the main
    loop; the work runs on ``spawn`` (a thread), one at a time.
    """

    def __init__(
        self,
        get_volume: Callable[[], float | None],
        set_volume: Callable[[float], None],
        enabled: Callable[[], bool],
        run: Runner = _busctl,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None], str], None] | None = None,
    ) -> None:
        self._get, self._set, self._enabled = get_volume, set_volume, enabled
        self._run, self._clock = run, clock
        self._spawn = spawn or (lambda fn, name: threading.Thread(target=fn, name=name, daemon=True).start())
        self._lock = threading.Lock()
        self._busy = False
        self._next = float("-inf")
        self._path: str | None = None       # the PCM we are in step with
        self._phone: int | None = None      # its level as last read or written
        self._radio: float | None = None    # the radio's volume as last read, in step
        self._wrote_at = float("-inf")
        self._soft_checked: set[str] = set()   # setting off: PCMs given SoftVolume back

    # -- the main loop ----------------------------------------------------

    def tick(self, now: float, active: bool) -> None:
        """While ``active`` (a phone plays over Bluetooth), every TICK_S."""
        if not active:
            if self._path is not None:
                self._path = None           # the next phone starts in step again
            return
        if now < self._next:
            return
        with self._lock:
            if self._busy:
                return
            self._busy = True
        self._next = now + TICK_S

        def run() -> None:
            try:
                self.step()
            except Exception:
                log.exception("phone volume: step failed")
            finally:
                with self._lock:
                    self._busy = False
        self._spawn(run, "bt-volume")

    # -- one comparison -----------------------------------------------------

    def _objects(self) -> dict | None:
        rc, out, err = self._run(["--json=short", "call", "org.bluealsa", "/org/bluealsa",
                                  "org.freedesktop.DBus.ObjectManager", "GetManagedObjects"], 2)
        if rc != 0:
            log.debug("phone volume: BlueALSA unreadable: %s", err.strip()[-200:])
            return None
        return parse_objects(out)

    def _set_property(self, path: str, name: str, sig: str, value: str) -> bool:
        rc, _, err = self._run(["set-property", "org.bluealsa", path, PCM1, name, sig, value], 2)
        if rc != 0:
            log.warning("phone volume: could not set %s on %s: %s", name, path,
                        (err.strip().splitlines() or ["?"])[-1])
        return rc == 0

    def _write_phone(self, path: str, level: int) -> bool:
        ok = self._set_property(path, "Volume", "q", str(pack(level)))
        if ok:
            self._phone = level
            self._wrote_at = self._clock()
        return ok

    def step(self) -> None:
        objects = self._objects()
        if objects is None:
            return
        found = phone_pcm(objects)
        if found is None:
            self._path = None
            return
        path, pcm = found
        if not self._enabled():
            self._give_back(path, pcm)
            return
        self._soft_checked.discard(path)
        phone = unpack(pcm.get("Volume"))
        radio = self._get()
        if phone is None or radio is None:
            return
        now = self._clock()

        if path != self._path:
            # A phone starts (or haslerd did): the radio's volume wins, then
            # BlueALSA stops attenuating - in that order, so it never gets louder
            # than the knob says.
            target = knob_to_phone(radio)
            if target != phone and not self._write_phone(path, target):
                return
            if pcm.get("SoftVolume") is not False:
                if not self._set_property(path, "SoftVolume", "b", "false"):
                    return
                log.info("phone volume: BlueALSA's own volume off for %s; phone slider at %d/127 "
                         "(radio %.0f)", path.rsplit("/", 2)[0].rsplit("/", 1)[-1], target, radio)
            self._path, self._phone, self._radio = path, target, radio
            self._wrote_at = now
            return

        if phone != self._phone:
            if now - self._wrote_at < ECHO_S:
                self._phone = phone         # the phone's answer to our write
                return
            knob = phone_to_knob(phone)
            log.info("phone volume: phone at %d/127 -> radio %.1f", phone, knob)
            self._set(knob)
            self._phone = phone
            reread = self._get()
            self._radio = knob if reread is None else reread
            return

        if self._radio is None or abs(radio - self._radio) >= 0.05:
            # The knob, a key or the page moved the radio: the phone follows.
            target = knob_to_phone(radio)
            self._radio = radio
            if target != phone:
                self._write_phone(path, target)

    def _give_back(self, path: str, pcm: dict) -> None:
        """The setting is off: BlueALSA's own attenuation back, once per PCM."""
        self._path = None
        if path in self._soft_checked:
            return
        self._soft_checked.add(path)
        if pcm.get("SoftVolume") is False and self._set_property(path, "SoftVolume", "b", "true"):
            log.info("phone volume: off; BlueALSA's own volume back for %s", path)

