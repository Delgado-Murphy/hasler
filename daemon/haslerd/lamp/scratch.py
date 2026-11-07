"""A scratch lamp engine: what the real one would draw, without touching a lamp.

The guided Lamp check (web/lamp_check.py) shows a *draft* calibration on the
real lamp, and the boot breath (scripts/hasler-lamp-boot) replays haslerd's
own frames after a power cut or between two haslerds (lamp/handover.py).
Neither models the engine: both build a ``LampEngine`` on a ``NullOutput``
with a clock of their own and read its frames, so whatever the engine draws
today - breath shapes, night levels, the amber band, the gate on a minor
channel, kicks, sags, beckons, hue changes through the trough - is what they
show. Only the engine's public API is used: ``calibrate``, ``set_night``,
``set_brightness``, ``show``, ``reply``, ``render``, ``duty_for``.

``Stage`` plays such frames onto the live lamp through ``preview_duty`` (raw
duty, drawn over the Look, ended by any other preview), on a thread of its own
at the engine's 50 Hz.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .contract import Look
from .outputs import CHANNELS, NullOutput

log = logging.getLogger(__name__)

RGB = tuple[float, float, float]
FPS = 50                    # the engine's own frame rate while anything moves
HOLD_S = 1.0                # each streamed frame holds this long: a stall blends back, never sticks


class Clock:
    """A clock the caller sets: ``clock.t`` is now."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def engine(spec: dict | None, night: bool = False, brightness: float | str = 1.0) -> tuple[Any, Clock]:
    """A LampEngine calibrated with ``spec`` (io.lamp's shape), drawing to nowhere."""
    from .engine import LampEngine

    clock = Clock(0.0)
    eng = LampEngine(NullOutput(), threaded=False, clock=clock)
    eng.calibrate(spec or {})
    if brightness not in (1.0, "normal"):
        eng.set_brightness(brightness)
    if night:
        eng.set_night(True)
    return eng, clock


def duty_for(spec: dict | None, colour: str, level: float) -> RGB:
    """The duty a steady ``colour`` at perceptual ``level`` gets with ``spec``."""
    eng, _ = engine(spec)
    return tuple(eng.duty_for(colour, level))  # type: ignore[return-value]


# -- scenes: a Look and what happens over it ------------------------------------


@dataclass(frozen=True)
class Scene:
    """A Look, and events over it: ``(at_s, "reply", word)`` or ``(at_s, "show", Look)``.

    With events it starts again every ``length_s``; ``length()`` is also one
    cycle of a Look alone (its breath or beckon) for sampling.
    """

    look: Look
    events: tuple = field(default=())
    length_s: float = 0.0

    def length(self) -> float:
        if self.length_s > 0:
            return self.length_s
        if self.look.motion == "breath" and self.look.period_s > 0:
            return self.look.period_s
        if self.look.motion == "beckon":
            return 0.8 + max(0.0, self.look.period_s)     # two kicks 0.25 s apart, then the rest
        return 1.0

    @property
    def steady(self) -> bool:
        return self.look.motion == "solid" and not self.events


class SceneFrames:
    """``at(t)``: the duty a scene shows ``t`` seconds in (repeating), from a scratch engine."""

    def __init__(self, spec: dict | None, scene: Scene, night: bool = False,
                 brightness: float | str = 1.0) -> None:
        self.spec, self.scene, self.night, self.brightness = spec, scene, night, brightness
        self.length = scene.length()
        self._cycle = -1
        self._eng: Any = None
        self._clock = Clock()
        self._next = 0

    def _restart(self) -> None:
        self._eng, self._clock = engine(self.spec, self.night, self.brightness)
        self._eng.show(self.scene.look, cut=True)
        self._next = 0

    def at(self, t: float) -> RGB:
        t = max(0.0, t)
        # A Look alone repeats by itself (the engine loops breaths and
        # beckons); a scene with events starts again every length_s.
        cycle, u = divmod(t, self.length) if self.scene.events else (0.0, t)
        if int(cycle) != self._cycle:
            self._cycle = int(cycle)
            self._restart()
        events = self.scene.events
        while self._next < len(events) and events[self._next][0] <= u:
            at, kind, arg = events[self._next]
            self._clock.t = at
            if kind == "reply":
                self._eng.reply(arg)
            elif kind == "show":
                self._eng.show(arg)
            self._next += 1
        self._clock.t = u
        duty, _ = self._eng.render(u)
        return (float(duty[0]), float(duty[1]), float(duty[2]))

    def sample(self, fps: int = FPS, cycles: int = 1) -> list[RGB]:
        """Every frame of ``cycles`` repeats, from the start."""
        n = max(1, round(self.length * cycles * fps))
        return [self.at(i / fps) for i in range(n)]


def look_frames(spec: dict | None, look: Look, night: bool = False, seconds: float | None = None,
                fps: int = 25, brightness: float | str = 1.0) -> list[RGB]:
    """One cycle of ``look`` (or ``seconds`` of it), frame by frame, from its start."""
    frames = SceneFrames(spec, Scene(look), night, brightness)
    n = max(1, round((seconds if seconds else frames.length) * fps))
    return [frames.at(i / fps) for i in range(n)]


def flicker_channel(frames: Sequence[Sequence[float]]) -> str | None:
    """The channel to blame for "the colour jumped" in these frames.

    A channel that goes dark and lit again while the lamp is lit (it crosses
    its floor), the faintest of those; else the faintest lit channel that
    isn't the main one; None for a dark scene.
    """
    lit = [f for f in frames if max(f) > 0.0]
    if not lit:
        return None
    lo = [min(f[i] for f in lit) for i in range(3)]
    hi = [max(f[i] for f in lit) for i in range(3)]
    main = max(range(3), key=lambda i: hi[i])
    flick = [i for i in range(3) if hi[i] > 0.0 and lo[i] <= 0.0]
    if flick:
        return CHANNELS[min(flick, key=lambda i: hi[i])]
    minor = [i for i in range(3) if hi[i] > 0.0 and i != main]
    pick = min(minor, key=lambda i: hi[i]) if minor else main
    return CHANNELS[pick]


# -- the stage: frames onto the live lamp ----------------------------------------


class Stage:
    """Plays a frame function onto the live lamp for a while, one show at a time.

    ``play(key, lamp, frame_at, seconds)`` starts a thread writing
    ``frame_at(t)`` through ``write(lamp, duty, hold_s)`` at FPS; the same
    ``key`` again while it runs only extends it (the page keeps a scene alive
    without restarting it). ``stop()`` ends it and leaves the lamp to the
    caller; a show that runs out ends its preview (the lamp blends back).
    """

    def __init__(self, write: Callable[[Any, RGB, float], None],
                 end: Callable[[Any], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._write = write
        self._end = end
        self._clock = clock
        self._lock = threading.Lock()
        self._key: Any = None
        self._until = 0.0
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def playing(self) -> Any:
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            return self._key if alive else None

    def play(self, key: Any, lamp: Any, frame_at: Callable[[float], RGB], seconds: float) -> bool:
        """Start (or extend) a show. True: a new one started."""
        with self._lock:
            now = self._clock()
            alive = self._thread is not None and self._thread.is_alive()
            if alive and key == self._key and self._stop is not None and not self._stop.is_set():
                self._until = now + seconds
                return False
        self.stop()
        stop = threading.Event()
        with self._lock:
            self._key, self._until, self._stop = key, now + seconds, stop
            self._thread = threading.Thread(target=self._run, args=(lamp, frame_at, stop, now),
                                            name="lamp-stage", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            stop, thread = self._stop, self._thread
            self._stop, self._thread, self._key = None, None, None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _run(self, lamp: Any, frame_at: Callable[[float], RGB], stop: threading.Event, t0: float) -> None:
        frame = 1.0 / FPS
        while not stop.is_set():
            now = self._clock()
            with self._lock:
                until = self._until
            if now >= until:
                break
            try:
                self._write(lamp, frame_at(now - t0), HOLD_S)
            except Exception:
                log.exception("lamp preview frame failed")
                break
            stop.wait(frame)
        if not stop.is_set() and self._end is not None:
            try:
                self._end(lamp)
            except Exception:
                log.debug("ending the lamp preview failed", exc_info=True)
