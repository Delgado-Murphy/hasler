"""The sleep timer: play on for a while, fade out, then standby.

Set from the key page (``POST /api/sleep``, web/sleep_api.py) for a number of
minutes. In the last FADE_S the volume falls smoothly to nothing and the
pilot lamp dims; at the end the radio goes to standby exactly as the Standby
button and the tone knob's click do (``Actions.enter_standby``), and the
mixer is put back to the owner's level - still muted - so the next wake is
as loud as the knob says, not as quiet as the fade left it.

The fade never touches the owner's volume: the player's ``fade_level`` moves
the mixer under the knob's level and reports the knob's level meanwhile
(player/moode.py), so the page, Home Assistant and a phone's Bluetooth slider
see no change. A player without it (an old one, a test double) just goes to
standby at the end.

Anyone touching the radio meant to: a key, a knob (volume or tone), standby
or a wake - from the cabinet or the page's remote, which dispatches the same
events - cancels the timer (``hand``), and a fade under way goes back to the
level at once. The page's volume slider cancels it only during the fade (a
fade and a level someone just chose can't both hold); before that it is the
person in bed turning it down a little, and the timer stays.

Phones. Standby pauses Spotify Connect (go-librespot is ours to stop) and the
timer asks a Bluetooth phone to pause (AVRCP) as it fires. AirPlay and UPnP
senders can't be stopped from here - haslerd may not touch shairport-sync -
so they go on sending to a muted radio. Either way a phone that was already
casting when the radio fell asleep doesn't wake it again (``keeps_asleep``,
read by the main loop and the lamp); a phone that starts a new cast later
does, as always.

"End of track" isn't offered: a station has no tracks, and the radio can't
see where a song ends in YouTube Music (served through the proxy, MPD rarely
knows the length), Spotify (go-librespot) or a phone's stream alike.

State is in memory only: a restart or a power cut forgets the timer, which
is the safe way round (a radio that sleeps when nobody asked is worse than one
that plays on).

Driven from the main loop (``tick``) and the web threads (``set``,
``cancel``, ``status``), so everything goes under one lock; calls into
Actions and the player are made outside it. Mixer writes go to one
background writer that only ever does the newest job: amixer can take a
while, and the 50 Hz panel loop must not wait on it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger(__name__)

FADE_S = 60.0           # the last minute fades out
FADE_STEP_S = 0.5       # a mixer write at most this often while fading
MAX_MINUTES = 240       # four hours: past that it's not a sleep timer
WRITER_IDLE_S = 10.0    # the writer thread ends after this long with nothing to do


class SleepError(ValueError):
    """A timer that can't be set: ``status`` is the HTTP answer (400, 409)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def minutes_from(raw: Any) -> float:
    """The page's ``minutes``: a number, more than 0, at most MAX_MINUTES.

    Fractions are fine - the chip's Undo sets back exactly what was left.
    Raises SleepError (400) saying why.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SleepError("minutes must be a number")
    minutes = float(raw)
    if not minutes > 0 or minutes > MAX_MINUTES:
        raise SleepError(f"minutes must be more than 0 and at most {MAX_MINUTES}")
    return minutes


class LatestWriter:
    """One background thread that runs the newest job submitted, and only it.

    A slow amixer never queues writes up: while one runs, newer jobs replace
    each other, and the last one wins. The thread goes away when idle.
    """

    def __init__(self, idle_s: float = WRITER_IDLE_S) -> None:
        self.idle_s = idle_s
        self._cv = threading.Condition()
        self._job: Callable[[], None] | None = None
        self._thread: threading.Thread | None = None

    def __call__(self, job: Callable[[], None]) -> None:
        with self._cv:
            self._job = job
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="sleep-fade", daemon=True)
                self._thread.start()
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                if not self._cv.wait_for(lambda: self._job is not None, timeout=self.idle_s):
                    self._thread = None
                    return
                job, self._job = self._job, None
            try:
                job()
            except Exception:
                log.exception("sleep timer: mixer write failed")


class SleepTimer:
    """See the module docstring. ``clock`` and ``write`` are for the tests."""

    def __init__(
        self,
        actions: Any,
        clock: Callable[[], float] = time.monotonic,
        write: Callable[[Callable[[], None]], None] | None = None,
        fade_s: float = FADE_S,
    ) -> None:
        self.actions = actions
        self.clock = clock
        self.fade_s = fade_s
        self._write = write if write is not None else LatestWriter()
        self._lock = threading.RLock()
        self._ends_at: float | None = None
        self._minutes = 0.0
        self._fading = False
        self._next_step = 0.0
        # The renderer that had the speaker when the timer put the radio to
        # sleep: it doesn't wake it again (see keeps_asleep).
        self._kept: str | None = None

    # -- asking -------------------------------------------------------------

    def remaining(self, now: float | None = None) -> float | None:
        """Seconds until standby, or None when no timer is set."""
        with self._lock:
            if self._ends_at is None:
                return None
            now = self.clock() if now is None else now
            return max(0.0, self._ends_at - now)

    @property
    def fading(self) -> bool:
        """In the last FADE_S: the volume is going down (the lamp dims too)."""
        with self._lock:
            return self._ends_at is not None and self._fading

    def status(self) -> dict | None:
        """``/api/status``'s ``sleep``: {"ends_in_s", "fading", "minutes"} or None.

        ``ends_in_s`` is rounded up, so the page never shows 0 while it plays;
        ``minutes`` is what it was set for.
        """
        with self._lock:
            left = self.remaining()
            if left is None:
                return None
            return {"ends_in_s": int(-(-left // 1)), "fading": self._fading,
                    "minutes": round(self._minutes, 2)}

    def keeps_asleep(self, source: str | None) -> bool:
        """Is ``source`` the phone that was casting when the timer slept the radio?

        The main loop's "a phone wakes the radio" and the lamp's blue then
        leave it alone: the phone didn't start anything, it just went on.
        """
        with self._lock:
            return self._kept is not None and source == self._kept

    # -- setting ------------------------------------------------------------

    def set(self, minutes: Any) -> dict:
        """Start (or restart) the timer; returns ``status()``.

        Raises SleepError: 400 for a bad ``minutes``, 409 while the radio is
        in standby or shutting down (there is nothing to put to sleep).
        """
        minutes = minutes_from(minutes)
        if getattr(self.actions, "shutting_down", False):
            raise SleepError("the radio is shutting down", 409)
        if self.actions.standby:
            raise SleepError("the radio is in standby", 409)
        with self._lock:
            was_fading = self._fading
            self._ends_at = self.clock() + minutes * 60.0
            self._minutes = minutes
            self._fading = False
            self._next_step = 0.0
        if was_fading:
            self._restore()     # set again during the fade: back up, the new time runs
        log.info("sleep timer: set for %s min", f"{minutes:g}")
        return self.status() or {}

    def cancel(self, why: str = "the page") -> bool:
        """Stop the timer; a fade under way goes back to the level. True if one was set."""
        with self._lock:
            if self._ends_at is None:
                return False
            was_fading = self._fading
            self._ends_at, self._fading = None, False
        if was_fading:
            self._restore()
        log.info("sleep timer: cancelled (%s)", why)
        return True

    def hand(self, event: Any = None) -> None:
        """Actions.on_hand: someone used a key or a knob - the timer is off."""
        if self._ends_at is None:
            return
        what = f"{event.control}.{event.kind}" if hasattr(event, "control") else str(event or "a hand")
        self.cancel(f"a hand on the radio: {what}")

    def volume_set(self) -> None:
        """The page's volume slider: cancels only a fade (see the module docstring)."""
        if self.fading:
            self.cancel("volume set from the page during the fade")

    # -- the main loop ------------------------------------------------------

    def tick(self, now: float) -> None:
        """Called from the main loop every pass. Never raises."""
        try:
            self._tick(now)
        except Exception:
            log.exception("sleep timer tick failed")

    def _tick(self, now: float) -> None:
        actions = self.actions
        if self._kept is not None and not (actions.standby and self._still_casting(self._kept)):
            with self._lock:
                self._kept = None     # the phone stopped (or the radio woke): a new cast wakes it
        if self._ends_at is None:
            return
        if actions.standby or getattr(actions, "shutting_down", False):
            # Standby by any other way (the knob, the page, a key): done.
            self.cancel("standby" if actions.standby else "shutting down")
            return
        with self._lock:
            ends_at = self._ends_at
            if ends_at is None:
                return
            left = ends_at - now
            fire = left <= 0
            step = None
            if not fire and left <= self.fade_s and now >= self._next_step:
                if not self._fading:
                    log.info("sleep timer: fading out over %.0f s", left)
                self._fading = True
                self._next_step = now + FADE_STEP_S
                step = max(0.0, min(1.0, left / self.fade_s))
            if fire:
                self._ends_at, self._fading = None, False
        if step is not None:
            self._fade(step)
        if fire:
            self._fire()

    def _still_casting(self, source: str) -> bool:
        """``source`` still has the speaker (the player caches its answer, so cheap)."""
        try:
            active = self.actions.player.active_source()
            return active == source and bool(self.actions.phone_has_it(active))
        except Exception:
            return True     # unsure: keep it asleep; the next tick asks again

    # -- doing --------------------------------------------------------------

    def _fade(self, fraction: float) -> None:
        fade_level = getattr(self.actions.player, "fade_level", None)
        if callable(fade_level):
            self._write(lambda: fade_level(fraction))

    def _restore(self) -> None:
        end_fade = getattr(self.actions.player, "end_fade", None)
        if callable(end_fade):
            self._write(end_fade)

    def _fire(self) -> None:
        """Time's up: standby as the Standby button does it, the level put back."""
        actions = self.actions
        log.info("sleep timer: time's up - standby")
        source = None
        try:
            source = actions.player.active_source()
            if source == "bluetooth" and actions.bluetooth is not None:
                actions.bluetooth.control_async("pause")    # the phone stops sending
        except Exception:
            log.debug("sleep timer: could not ask the phone to pause", exc_info=True)
        try:
            phone = bool(source) and actions.phone_has_it(source)
        except Exception:
            phone = False
        with self._lock:
            self._kept = source if phone else None
        try:
            actions.enter_standby()
        except Exception:
            log.exception("sleep timer: standby failed")
        # Muted now; the mixer goes back to the owner's level for the next wake.
        self._restore()
