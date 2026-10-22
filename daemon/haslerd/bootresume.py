"""Coming back after power-up: the remembered preset, as soon as it can play.

docs/lamp.md's boot is a valve radio warming up: light first, sound when the
valves are ready. On the box "ready" is MPD (or go-librespot) answering, and
moOde's worker starts MPD late and at its own pace, so the resume waits for the
player itself rather than for a timer or the internet probe:

* **Ask often, never block.** While a resume is pending the player is asked
  every RESUME_POLL_S whether it answers (MPD's greeting on 6600, go-librespot's
  /status), on a short-lived thread; the main loop only collects the answer.
  The window is RESUME_WINDOW_S of wall time. A key press, standby or a phone
  ends it quietly; only running out of window earns the red pulses.
* **Swell, don't blast.** The mixer is held silent while the preset starts and,
  the moment it sounds, rises to the knob's level over SWELL_S - in step with
  the lamp's fade to playing. The knob turning wins (MoodePlayer.swell).
* **Sticky for a minute.** moOde's worker finishes its own startup after ours
  (``mpc enable only``, ``vol.sh``, sometimes a stop). For GUARD_S after the
  resume, or until moOde sets ``wrkready``, a preset that stops without a hand
  on the radio is started again once, and moOde's flip to ready re-asserts the
  knob's volume.
* **Not after following a group.** A radio that was playing another Hasler's
  music (multiroom) comes back alone and silent: ``Actions.resume_preset`` is
  None when state.json says ``"follower": true``, so the resume is "nothing".
* **Not at night after a power cut.** A box that lost power while playing and
  comes back in the small hours stays asleep (ember). Clean vs power cut is a
  marker hasler-clean-shutdown.service leaves (``shutdown_kind``).

Separately, ``FirstSound`` logs when the amp's PCM first runs - the number the
boot timeline is judged by.
"""

from __future__ import annotations

import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .player.base import Preset

log = logging.getLogger(__name__)

RESUME_POLL_S = 0.5      # "does the player answer yet?" / "is it sounding yet?"
RESUME_WINDOW_S = 150.0  # then give up, with red pulses
RESUME_RETRY_S = 5.0     # after a start that raised (yt-dlp offline, say)
RESUME_SOUND_S = 20.0    # started, but silent this long: start it again
GUARD_S = 60.0           # sticky resume, unless moOde says ready first
GUARD_POLL_S = 1.0
GUARD_MISSES = 2         # consecutive "not playing" reads before a re-start
CLOCK_WAIT_S = 60.0      # how long night waits for NTP after a power cut

CLEAN_SHUTDOWN_FILE = "clean-shutdown"   # hasler-clean-shutdown.service writes it
BOOT_ID_FILE = "boot-id"                 # ours: which boot we last started in
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
TIMESYNC_DIR = Path("/run/systemd/timesync")

ASOUND = Path("/proc/asound")
AMP_CARD = "sndrpihifiberry"
FIRST_SOUND_POLL_S = 0.5
FIRST_SOUND_SLOW_S = 5.0     # after FIRST_SOUND_PATIENCE_S nobody is in a hurry
FIRST_SOUND_PATIENCE_S = 300.0


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="boot-resume", daemon=True).start()


class Probe:
    """One check at a time, off the main loop; the loop collects the answer.

    Answers are tagged so one from an earlier phase (or attempt) is never
    mistaken for the current question. A check that raises answers nothing.
    ``spawn`` runs the check: a daemon thread on the box, inline in tests.
    """

    def __init__(self, spawn: Callable[[Callable[[], None]], None] | None = None) -> None:
        self._spawn = spawn or _thread
        self._lock = threading.Lock()
        self._busy = False
        self._answer: tuple[Any, Any] | None = None

    def ask(self, tag: Any, check: Callable[[], Any]) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True

        def run() -> None:
            try:
                value = check()
            except Exception:
                log.debug("boot probe %s failed", tag, exc_info=True)
                value = None
            with self._lock:
                self._answer = None if value is None else (tag, value)
                self._busy = False

        self._spawn(run)

    def take(self, tag: Any) -> Any:
        """The answer to ``tag``, once (None: none yet)."""
        with self._lock:
            answer, self._answer = self._answer, None
        if answer is None or answer[0] != tag:
            return None
        return answer[1]


class BootResume:
    """The boot resume, driven by ``tick`` from the main loop. Never raises.

    Phases: ``wait`` (for the player to answer, and for the night verdict),
    ``starting`` (started; waiting for sound), ``guard`` (playing; the sticky
    minute), ``done``. ``on_done(outcome)`` fires once when the resume itself
    is over: "playing", "nothing" (standby, or no preset remembered),
    "night", "cancelled" or "failed".
    """

    def __init__(
        self,
        actions,
        sounding: Callable[[object, Preset], bool] | None = None,
        ready: Callable[[Preset], bool] | None = None,
        still_playing: Callable[[Preset], bool] | None = None,
        wrkready: Callable[[], int | None] | None = None,
        night_hold: Callable[[], bool | None] | None = None,
        on_done: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        window_s: float = RESUME_WINDOW_S,
    ) -> None:
        self.actions = actions
        player = actions.player
        answering = getattr(player, "answering", None)
        self.ready = ready or (
            (lambda p: answering(p.source)) if callable(answering) else (lambda p: True)
        )
        self.sounding = sounding or (lambda pl, p: pl.current() == p)
        playing = getattr(player, "still_playing", None)
        self.still_playing = still_playing or (
            playing if callable(playing)
            else (lambda p: player.current() == p and player.active_source() == p.source)
        )
        moode = getattr(player, "wrkready", None)
        self.wrkready = wrkready or (moode if callable(moode) else (lambda: None))
        self.night_hold = night_hold or (lambda: False)
        self.on_done = on_done
        self.clock = clock
        self.window_s = window_s
        self.probe = Probe(spawn)
        self.phase = "wait"
        self.outcome: str | None = None
        self.preset: Preset | None = None
        self.attempts = 0
        self.started = clock()
        self._next = 0.0
        self._not_before = 0.0
        self._start_at = 0.0
        self._night: bool | None = None
        self._guard_until = 0.0
        self._misses = 0
        self._restarted = False
        # moOde's wrkready: seen 0 this boot (so a 1 is fresh, not last
        # boot's), then seen 1.
        self._moode_starting = False
        self._moode_ready = False

    @property
    def pending(self) -> bool:
        """The resume is still to happen (warm-up breath, quiet failures)."""
        return self.phase in ("wait", "starting")

    # -- from the main loop -------------------------------------------------

    def cancel(self, reason: str) -> None:
        """A hand on the radio, standby or a phone: the resume is moot."""
        if self.pending:
            log.info("boot resume: %s took over", reason)
            self._release()
            self._finish("cancelled")
        elif self.phase == "guard":
            self.phase = "done"

    def tick(self, now: float) -> None:
        try:
            if self.phase == "done":
                return
            if self.pending and self.actions.standby:
                self.cancel("standby")
                return
            if self.pending and now - self.started >= self.window_s:
                self._give_up()
                return
            {"wait": self._wait, "starting": self._starting, "guard": self._guard}[self.phase](now)
        except Exception:
            log.exception("boot resume tick failed")

    # -- phases ---------------------------------------------------------------

    def _wait(self, now: float) -> None:
        preset = self.actions.resume_preset()
        if preset is None:
            self._finish("nothing")
            return
        self.preset = preset
        if self._night is None:
            self._night = self.night_hold()     # None: the clock is not set yet
            if self._night:
                log.info("power cut at night: staying asleep instead of resuming preset %s", preset.n)
                self.actions.enter_standby(persist=False)
                self._finish("night")
                return
        answer = self.probe.take(("ready", self.attempts))
        if answer is not None:
            self._moode(answer[1])
            if answer[0] and self._night is False and now >= self._not_before:
                self._start(preset)
                return
        if now >= self._next:
            self._next = now + RESUME_POLL_S
            self.probe.ask(("ready", self.attempts), lambda: (bool(self.ready(preset)), self._read_wrkready()))

    def _start(self, preset: Preset) -> None:
        self.attempts += 1
        player = self.actions.player
        hold = getattr(player, "hold_silent", None)
        if callable(hold):
            try:
                hold()
            except Exception:
                log.debug("could not hold the mixer silent for the swell", exc_info=True)
        log.info("boot resume: starting preset %s (attempt %d, %.1f s after boot)",
                 preset.n, self.attempts, self.clock())
        ok = self.actions.resume_last(quiet=True)
        now = self.clock()
        if not ok:
            self._release()
            self._not_before = now + RESUME_RETRY_S
            return
        self.phase = "starting"
        self._start_at = now
        self._next = now + RESUME_POLL_S

    def _starting(self, now: float) -> None:
        preset, player = self.preset, self.actions.player
        try:
            current = player.current()
        except Exception:
            current = preset
        if current != preset:
            self.cancel("another start")
            return
        answer = self.probe.take(("sound", self.attempts))
        if answer is not None:
            self._moode(answer[1])
            if answer[0]:
                self._sounding(now)
                return
        if now - self._start_at >= RESUME_SOUND_S:
            log.info("boot resume: preset %s silent for %.0f s; starting it again",
                     preset.n, RESUME_SOUND_S)
            self._release()
            self.phase = "wait"
            self._next = now
            return
        if now >= self._next:
            self._next = now + RESUME_POLL_S
            self.probe.ask(
                ("sound", self.attempts),
                lambda: (bool(self.sounding(player, preset)), self._read_wrkready()),
            )

    def _sounding(self, now: float) -> None:
        log.info("boot resume: preset %s sounding at %.1f s after boot", self.preset.n, self.clock())
        swell = getattr(self.actions.player, "swell", None)
        if callable(swell):
            try:
                swell()
            except Exception:
                log.exception("volume swell failed")
                self._release()
        self._finish("playing")
        if self._moode_ready:
            return      # moOde finished its startup before ours: nothing to guard
        self.phase = "guard"
        self._guard_until = now + GUARD_S
        self._next = now + GUARD_POLL_S

    def _guard(self, now: float) -> None:
        actions, preset = self.actions, self.preset
        try:
            ours = actions.player.current() == preset
        except Exception:
            ours = False
        if actions.standby or not ours or now >= self._guard_until:
            self.phase = "done"
            return
        answer = self.probe.take("guard")
        if answer is not None:
            playing, wrk = answer
            if self._moode(wrk):
                log.info("moOde finished starting; re-asserting the knob's volume")
                self._reassert_volume()
                self.phase = "done"
                return
            self._misses = 0 if playing else self._misses + 1
            if self._misses >= GUARD_MISSES and not self._restarted:
                self._restarted = True
                log.info("preset %s stopped without a key press during moOde's startup; "
                         "starting it again", preset.n)
                self._reassert_volume()
                actions.resume_last(quiet=True)
        if now >= self._next:
            self._next = now + GUARD_POLL_S
            self.probe.ask("guard", lambda: (bool(self.still_playing(preset)), self._read_wrkready()))

    # -- helpers --------------------------------------------------------------

    def _read_wrkready(self) -> int | None:
        try:
            return self.wrkready()
        except Exception:
            return None

    def _moode(self, wrk: int | None) -> bool:
        """Track moOde's wrkready; True the first time it flips 0 -> 1."""
        if wrk is None:
            return False
        if wrk != 1:
            self._moode_starting = True
            return False
        if self._moode_starting and not self._moode_ready:
            self._moode_ready = True
            return True
        return False

    def _reassert_volume(self) -> None:
        again = getattr(self.actions.player, "reassert_volume", None)
        if callable(again):
            try:
                again()
            except Exception:
                log.debug("could not re-assert the volume", exc_info=True)

    def _release(self) -> None:
        release = getattr(self.actions.player, "release_hold", None)
        if callable(release):
            try:
                release()
            except Exception:
                log.debug("could not release the mixer hold", exc_info=True)

    def _give_up(self) -> None:
        n = self.preset.n if self.preset is not None else "?"
        log.warning("boot resume: preset %s did not play within %.0f s; giving up", n, self.window_s)
        self._release()
        self.actions.reply("fail")
        self._finish("failed")

    def _finish(self, outcome: str) -> None:
        self.phase = "done"
        self.outcome = outcome
        if self.on_done is not None:
            try:
                self.on_done(outcome)
            except Exception:
                log.debug("boot resume on_done failed", exc_info=True)


# -- readiness, asked off the main loop ---------------------------------------


def mpd_answers(host: str, port: int, timeout: float = 0.5) -> bool:
    """MPD greets every new client with ``OK MPD <version>``.

    The greeting, not just the connect: with socket activation systemd
    accepts on 6600 before MPD itself is running.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            return sock.recv(64).startswith(b"OK MPD")
    except OSError:
        return False


def http_answers(url: str, timeout: float = 0.5) -> bool:
    """Any HTTP reply at all (204 = go-librespot up with no session yet)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


# -- clean shutdown vs power cut ----------------------------------------------


def shutdown_kind(state_dir: Path | None, boot_id_file: Path = BOOT_ID) -> str:
    """How the last run ended: "clean", "power-cut", "restart" or "unknown".

    The contract with the OS: at a clean shutdown (system ``stopping``, while
    /var/lib is still writable) config/hasler-clean-shutdown.service's
    ExecStop writes ``<state_dir>/clean-shutdown`` (contents ignored). At the first haslerd
    start of a boot this reads and deletes it - missing means the power went.
    A haslerd restart within one boot (an update, a crash) is told apart by
    the kernel's boot id, remembered in ``<state_dir>/boot-id``: "restart",
    and the marker is left alone. Never raises.
    """
    if state_dir is None:
        return "unknown"
    try:
        boot_id = boot_id_file.read_text().strip()
    except OSError:
        return "unknown"
    state_dir = Path(state_dir)
    seen = state_dir / BOOT_ID_FILE
    try:
        if seen.read_text().strip() == boot_id:
            return "restart"
    except OSError:
        pass
    marker = state_dir / CLEAN_SHUTDOWN_FILE
    clean = marker.exists()
    try:
        if clean:
            marker.unlink()
        seen.write_text(boot_id + "\n")
    except OSError:
        log.warning("could not update %s / %s", marker, seen, exc_info=True)
    return "clean" if clean else "power-cut"


def clock_synced(timesync_dir: Path = TIMESYNC_DIR) -> bool:
    """Has systemd-timesyncd set the clock this boot? (No timesyncd: trust it.)

    A Pi has no RTC: after a power cut the clock restarts from fake-hwclock's
    last save until NTP answers, so "is it night?" can be hours wrong.
    """
    if not timesync_dir.exists():
        return True
    return (timesync_dir / "synchronized").exists()


def night_hold(
    power_cut: bool,
    night: Callable[[], bool] | None,
    synced: Callable[[], bool] = clock_synced,
    clock: Callable[[], float] = time.monotonic,
    wait_s: float = CLOCK_WAIT_S,
) -> Callable[[], bool | None]:
    """BootResume's night question: True = stay asleep, None = ask again.

    Only a power cut is held back, and only at night by the page's settings;
    the verdict waits up to ``wait_s`` for the clock to be set by NTP.
    """
    started = clock()

    def hold() -> bool | None:
        if not power_cut or night is None:
            return False
        if not synced() and clock() - started < wait_s:
            return None
        try:
            return bool(night())
        except Exception:
            log.debug("night check for the boot resume failed", exc_info=True)
            return False

    return hold


# -- the first sound ------------------------------------------------------------


class FirstSound:
    """Logs ``first sound at N s after boot`` when the amp's PCM first runs.

    Reads ``/proc/asound/cardN/pcm0p/sub0/status`` (``state: RUNNING`` while
    something plays) for the card whose id is ``card`` - an id such as
    ``sndrpihifiberry`` survives HDMI cards shifting the numbering; an int is
    taken as the index. ``clock`` is CLOCK_MONOTONIC, seconds since boot on
    Linux. Cheap procfs reads, only until it fires.
    """

    def __init__(
        self,
        card: int | str = AMP_CARD,
        root: Path = ASOUND,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.card = card
        self.root = Path(root)
        self.clock = clock
        self.fired = False
        self.at: float | None = None
        self._began = clock()
        self._next = 0.0
        self._status: Path | None = None

    def _find(self) -> Path | None:
        card = self.card
        if isinstance(card, int) or str(card).isdigit():
            index = str(int(card))
        else:
            index = None
            try:
                text = (self.root / "cards").read_text()
            except OSError:
                return None
            for line in text.splitlines():
                m = re.match(r"\s*(\d+)\s+\[([^\]]+)\]", line)
                if m and m.group(2).strip() == card:
                    index = m.group(1)
                    break
            if index is None:
                return None
        return self.root / f"card{index}" / "pcm0p" / "sub0" / "status"

    def tick(self, now: float) -> None:
        if self.fired or now < self._next:
            return
        slow = now - self._began >= FIRST_SOUND_PATIENCE_S
        self._next = now + (FIRST_SOUND_SLOW_S if slow else FIRST_SOUND_POLL_S)
        if self._status is None:
            self._status = self._find()
            if self._status is None:
                return
        try:
            running = "state: RUNNING" in self._status.read_text()
        except OSError:
            return
        if running:
            self.fired = True
            self.at = self.clock()
            log.info("first sound at %.1f s after boot", self.at)
