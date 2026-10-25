"""The stream watchdog: MPD stopped by itself while the radio says "playing".

Seen on the radio (2026-10-21, 21 h up): /api/status said preset 2 was
playing, the lamp was amber, and MPD sat in ``stop`` with the stream still
queued - silently. ``mpc play`` brought it straight back. Nothing in
haslerd's journal, nothing in MPD's.

So, every ``POLL_S`` on a thread of its own, while the radio believes one of
its MPD presets (radio, ytm) is playing - awake, no phone casting, nothing
being started or stopped - this asks MPD for its status through the shared,
locked client (radio.py; no second connection) and:

* logs each MPD state change it sees, with MPD's own ``error`` when it gives
  one - the record the next incident needs;
* counts MPD as **stalled** when it has been in ``stop`` for ``STOP_CONFIRM_S``
  or in ``play`` without ``elapsed`` moving for ``STALL_S``. Only once the
  preset has been heard: a start that never makes sound is the lamp's
  loading timeout's business (main.LampKeeper), not this;
* recovers it: ``play`` on the queue first (cheap, what ``mpc play`` did);
  if MPD still isn't moving ``VERIFY_S`` later, the preset is started again
  through the player's router. An attempt that fails both waits
  ``BACKOFF_S`` (5 s, 15 s, 60 s, then every 5 min) before the next. After
  ``PULSE_AFTER`` failed attempts the lamp shows its red pulses once (not at
  night) and the watchdog keeps trying, quietly.

It never fights a hand. Every start and stop goes through the player's
router, which numbers them (``MoodePlayer.route_gen``): a key again, standby,
the PU key, the page's stop, a group, the boot resume - any routing this
watchdog did not ask for ends whatever it was doing, and it waits to hear
the new preset before watching again. A phone taking the speaker (moOde
stops MPD for Bluetooth and AirPlay; Spotify Connect) stands it down until
the next start. MPD ``pause`` is somebody's choice (moOde's UI), never a stall.
A YouTube Music playlist that played to its last track has *ended*, not
stalled: nothing is restarted, but the radio stops claiming it plays.

On-card record. /var/log is log2ram (a tmpfs synced daily and at a clean
shutdown), so the journal of a power-cut day is gone. Each MPD stop or
error, each stall, attempt and recovery is also appended as one JSON line to
``stream-events.jsonl`` in the state directory (/var/lib/haslerd, ext4),
kept to the last ``EVENTS_KEEP`` lines / ``EVENTS_MAX_BYTES`` by rewriting it
in place; the last few are in /api/health ``stream_events_recent``.

MPD not answering at all (2026-10-25: MPD 0.24's ffmpeg decoder stuck in an
HLS retry loop, its main thread waiting on it, port 6600 open and silent) is
``MpdRecovery``'s, on the same thread: the shared connection fails fast once
MPD has timed out (radio.py ``MpdUnresponsive``), this probes it on a
connection of its own every poll, and after ``MPD_HANG_S`` with mpd.service
still "active" restarts it - ``sudo -n systemctl restart mpd.service``, the
one line sudoers allows (config/sudoers/haslerd), at most
``MPD_RESTARTS_PER_H`` an hour, spaced by ``MPD_RESTART_GAP_S``. Normally
systemd gets there first: mpd.service's own watchdog (the hasler-watchdog.conf
drop-in) kills a main loop that stopped pinging. Either way, once MPD
answers again the preset last asked for (``MoodePlayer.wanted``) is started
again, unless MPD plays already or a hand, standby or a phone has it.

``/api/status`` and ``/api/health`` ask ``silent`` / ``status_doc()`` so they
tell the truth meanwhile ("playing" off, ``recovering`` and ``stalled_s``),
and the lamp leaves its playing look for the loading breath while it tries.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .base import Preset

log = logging.getLogger(__name__)

MPD_SOURCES = ("radio", "ytm")
POLL_S = 2.0
STOP_CONFIRM_S = 4.0      # MPD in "stop" this long (a phone's moOde stop shows up first)
STALL_S = 15.0            # "play" with elapsed frozen this long (a dead stream buffering forever)
VERIFY_S = 8.0            # after `play`: moving by then, or start the preset again
RESTART_VERIFY_S = 25.0   # after a re-start (a YTM resolve can take a while)
BACKOFF_S = (5.0, 15.0, 60.0, 300.0)   # between failed attempts; the last repeats
PULSE_AFTER = 3           # failed attempts before the lamp's one red pulse
EVENTS_FILE = "stream-events.jsonl"
EVENTS_KEEP = 200         # lines kept on the card...
EVENTS_MAX_BYTES = 64 * 1024   # ...and bytes, whichever is reached first
EVENTS_RECENT = 5         # in /api/health
# MPD not answering at all (MpdRecovery)
MPD_HANG_S = 30.0         # unanswered this long, mpd.service "active": restart it (systemd's own
                          # watchdog, 20 s + a 10 s stop, normally has by then)
MPD_RESTARTS_PER_H = 3
MPD_RESTART_GAP_S = (60.0, 300.0)   # after the 1st restart, then after each later one
MPD_RESTART = ("sudo", "-n", "/usr/bin/systemctl", "restart", "mpd.service")
MPD_RESTART_TIMEOUT_S = 60.0
MPD_PROBE_S = 2.0


class StreamEvents:
    """A small JSON-lines record on the card, rewritten in place to stay small.

    Never raises: a full or read-only card costs the record, not the radio.
    ``recent()`` is the last few, kept in memory (and read back at start).
    """

    def __init__(self, path: str | Path | None, keep: int = EVENTS_KEEP,
                 max_bytes: int = EVENTS_MAX_BYTES, wall: Callable[[], datetime] = datetime.now) -> None:
        self.path = Path(path) if path is not None else None
        self.keep, self.max_bytes, self.wall = keep, max_bytes, wall
        self._lock = threading.Lock()
        self._recent: deque = deque(maxlen=EVENTS_RECENT)
        self._lines = 0
        if self.path is not None:
            try:
                lines = self.path.read_text().splitlines()
            except OSError:
                lines = []
            self._lines = len(lines)
            for line in lines[-EVENTS_RECENT:]:
                try:
                    self._recent.append(json.loads(line))
                except ValueError:
                    pass

    def add(self, action: str, preset: Preset | None, state: str | None, error: str | None,
            **extra: Any) -> None:
        doc = {"t": self.wall().isoformat(timespec="seconds"), "action": action,
               "preset": None if preset is None else {"n": preset.n, "name": preset.name,
                                                     "source": preset.source},
               "mpd_state": state or None, "error": error or None}
        doc.update(extra)
        with self._lock:
            self._recent.append(doc)
            if self.path is None:
                return
            line = json.dumps(doc, ensure_ascii=False) + "\n"
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                self._lines += 1
                if self._lines > self.keep or self.path.stat().st_size > self.max_bytes:
                    self._trim()
            except OSError:
                log.debug("stream events: could not write %s", self.path, exc_info=True)

    def _trim(self) -> None:
        """Keep the newest lines: 3/4 of the cap, so it isn't rewritten every time."""
        lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        kept, size = [], 0
        for line in reversed(lines):
            if len(kept) >= self.keep * 3 // 4 or size + len(line) > self.max_bytes * 3 // 4:
                break
            kept.append(line)
            size += len(line)
        kept.reverse()
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        os.replace(tmp, self.path)
        self._lines = len(kept)

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self._recent)


class MpdRecovery:
    """MPD alive but not answering: wait, restart it (rate-limited), start the
    preset again once it answers. ``tick(now)`` is called by the stream
    watchdog's thread every poll; True while MPD is out (the watchdog then
    leaves its own stream checks alone).

    ``conn`` is radio.py's MpdConnection (``unresponsive_s``, ``probe``).
    ``run`` runs a command (subprocess.run); ``replay()`` starts the preset
    last asked for again: its number, or None if it wasn't (``_replay``).
    """

    def __init__(self, conn: Any, record: Callable[..., None], replay: Callable[[], str | None],
                 run: Callable[..., Any] = subprocess.run, hang_s: float = MPD_HANG_S) -> None:
        self.conn = conn
        self.record = record
        self.replay = replay
        self.run = run
        self.hang_s = hang_s
        self.since: float | None = None     # out since (monotonic)
        self.reason = ""
        self._restarted = False             # by us, this outage
        self._withheld = False              # the rate limit said no, this outage (noted once)
        self._restarts: deque = deque()     # our restarts in the last hour (monotonic)

    @property
    def out(self) -> bool:
        return self.since is not None

    def restarts_last_hour(self, now: float) -> int:
        while self._restarts and now - self._restarts[0] >= 3600.0:
            self._restarts.popleft()
        return len(self._restarts)

    def tick(self, now: float) -> bool:
        if self.since is None:
            down = self.conn.unresponsive_s()
            suspected = getattr(self.conn, "suspected", None)
            if down is None and callable(suspected) and suspected():
                # A quick question timed out somewhere: a full-length probe
                # here decides (MPD busy stopping a stream answers in time).
                self.conn.probe(float(getattr(self.conn, "timeout", 8.0)))
                down = self.conn.unresponsive_s()
            if down is None:
                return False
            self.since = now - down
            self._restarted = self._withheld = False
            self.reason = "MPD not answering"
            log.warning("MPD not answering (%s); watching it", getattr(self.conn, "down_reason", "") or "timeout")
            self.record("mpd unresponsive", None, getattr(self.conn, "down_reason", "") or None)
        result = self.conn.probe(MPD_PROBE_S)
        if result == "ok":
            self._back(now)
            return False
        out_s = now - self.since
        if result == "refused":
            self.reason = "MPD restarting"
            return True
        self.reason = f"MPD not answering for {out_s:.0f} s"
        if out_s >= self.hang_s:
            self._maybe_restart(now)
        return True

    def _may_restart(self, now: float) -> bool:
        n = self.restarts_last_hour(now)
        if n >= MPD_RESTARTS_PER_H:
            return False
        if n:
            gap = MPD_RESTART_GAP_S[min(n - 1, len(MPD_RESTART_GAP_S) - 1)]
            if now - self._restarts[-1] < gap:
                return False
        return True

    def _active(self) -> bool:
        """mpd.service is "active": its process is there (not stopping or
        starting - systemd's own watchdog may be restarting it right now)."""
        try:
            out = self.run(["systemctl", "is-active", "mpd.service"], capture_output=True,
                           text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return False
        return str(getattr(out, "stdout", "") or "").strip() == "active"

    def _maybe_restart(self, now: float) -> None:
        if not self._may_restart(now):
            if not self._withheld:
                self._withheld = True
                log.warning("MPD still not answering; not restarting it again yet (%d restarts in the "
                            "last hour)", self.restarts_last_hour(now))
                self.record("mpd restart withheld", None, None, reason="rate limit",
                            restarts_last_hour=self.restarts_last_hour(now))
            self.reason = "MPD not answering; restart limit reached"
            return
        if not self._active():
            return                          # stopping or starting already: give it the time
        self._restarts.append(now)
        self._restarted = True
        log.warning("MPD has not answered for %.0f s; restarting it", now - (self.since or now))
        self.reason = "MPD not answering - restarting it"
        try:
            done = self.run(list(MPD_RESTART), capture_output=True, text=True, timeout=MPD_RESTART_TIMEOUT_S)
            code = getattr(done, "returncode", 1)
            err = (str(getattr(done, "stderr", "") or "").strip()[-200:]) or None
        except (OSError, subprocess.SubprocessError) as exc:
            code, err = None, str(exc)
        if code == 0:
            log.info("MPD restarted")
        else:
            log.error("restarting MPD failed (%s): %s", code, err)
        self.record("mpd unresponsive -> restarted", None, err if code != 0 else None,
                    ok=code == 0, after_s=round(now - (self.since or now), 1),
                    restarts_last_hour=self.restarts_last_hour(now))

    def _back(self, now: float) -> None:
        out_s = now - (self.since or now)
        log.info("MPD answering again after %.0f s%s", out_s, " (restarted by haslerd)" if self._restarted else "")
        replayed = None
        try:
            replayed = self.replay()
        except Exception:
            log.exception("could not start the preset again after MPD came back")
        self.record("mpd back", None, None, after_s=round(out_s, 1), restarted_by_haslerd=self._restarted,
                    replayed=replayed)
        self.since = None
        self.reason = ""


class StreamWatchdog:
    """Watches MPD for a preset that stopped sounding by itself; restarts it.

    ``actions`` is the Actions table (standby, phones, the lamp's replies);
    its ``player`` must be a MoodePlayer (``route_gen``, ``routing``,
    ``resume_mpd``, ``restart``). ``night()`` True = no red pulses.
    ``check(now)`` is one poll; ``start()`` runs it every POLL_S on a thread.
    """

    def __init__(
        self,
        actions: Any,
        clock: Callable[[], float] = time.monotonic,
        night: Callable[[], bool] | None = None,
        poll_s: float = POLL_S,
        events: StreamEvents | None = None,
        lock: Any = None,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.actions = actions
        self.events = events if events is not None else StreamEvents(None)
        # The web page's lock (WebApp.lock), held while the preset is started
        # again after an MPD outage, as a key press from the page would be.
        self.lock = lock
        self.clock = clock
        self.night = night
        self.poll_s = poll_s
        self._gen: int | None = None        # the routing generation being watched
        self._preset: Preset | None = None
        self._armed = False                 # heard since that generation began
        self._cast = False                  # a phone took over: stand down till the next start
        self._finished = False              # a YTM playlist played to its end
        # MPD as last sampled
        self._state: str | None = None
        self._elapsed: float | None = None
        self._song: int | None = None
        self._error: str | None = None
        self._noted_error: str | None = None   # the last error put in the record
        self._progress_at: float | None = None
        self._stop_since: float | None = None
        # a stall and its recovery
        self._stalled_at: float | None = None
        self._reason = ""
        self._attempts = 0                  # failed attempts so far
        self._phase = "wait"                # wait -> play -> preset -> wait ...
        self._deadline = 0.0
        self._restart_failed = False
        self._pulsed = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        radio = (getattr(actions.player, "sources", None) or {}).get("radio")
        conn = getattr(radio, "mpd", None)
        self.mpd_recovery: MpdRecovery | None = None
        if callable(getattr(conn, "unresponsive_s", None)) and callable(getattr(conn, "probe", None)):
            self.mpd_recovery = MpdRecovery(conn, self._record_mpd, self._replay, run=run)

    # -- what the page, health and lamp ask --------------------------------

    @property
    def mpd_out(self) -> bool:
        """MPD is not answering at all (MpdRecovery)."""
        return self.mpd_recovery is not None and self.mpd_recovery.out

    @property
    def recovering(self) -> bool:
        """MPD should be playing and isn't, or isn't answering: the watchdog is bringing it back."""
        return self._stalled_at is not None or self.mpd_out

    @property
    def reason(self) -> str | None:
        """Why it is recovering, in a few words (None when it isn't)."""
        if self.mpd_out:
            return self.mpd_recovery.reason or "MPD not answering"
        if self._stalled_at is not None:
            return self._reason or "stalled"
        return None

    @property
    def silent(self) -> bool:
        """The preset the radio names is not actually sounding (stalled, ended, or MPD out)."""
        return self._stalled_at is not None or self._finished or self.mpd_out

    @property
    def breathing(self) -> bool:
        """The lamp's loading breath: recovering, and not yet given up on loudly."""
        if self.mpd_out:
            return True
        return self._stalled_at is not None and self._attempts < PULSE_AFTER

    def stalled_s(self, now: float | None = None) -> float | None:
        since = self._stalled_at
        if self.mpd_out:
            out = self.mpd_recovery.since
            since = out if since is None or out is None else min(since, out)
        if since is None:
            return None
        return max(0.0, (self.clock() if now is None else now) - since)

    def stalled_on(self, preset: Preset | None) -> bool:
        """``preset`` is the one that has gone quiet (its key then means "play", not "again")."""
        return preset is not None and self.silent and preset == self._preset

    def status_doc(self) -> dict:
        s = self.stalled_s()
        return {"recovering": self.recovering, "stalled_s": None if s is None else round(s, 1),
                "reason": self.reason}

    # -- the thread -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stream-watchdog", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.poll_s):
            try:
                self.check()
            except Exception:
                log.exception("stream watchdog check failed")

    # -- one poll -----------------------------------------------------------

    def check(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        actions, player = self.actions, self.actions.player
        if self.mpd_recovery is not None and self.mpd_recovery.tick(now):
            return                      # MPD out: nothing to sample; MpdRecovery has it
        gen = player.route_gen
        if gen != self._gen:
            if self._stalled_at is not None:
                log.info("stream watchdog: preset %s changed hands (a key, standby, the page) "
                         "- recovery stands down after %.0f s", self._name(), now - self._stalled_at)
                self._record("stood down", self._state, self._error, reason="routed by a hand")
            self._watch(gen, player.current())
        if actions.standby or getattr(actions, "shutting_down", False):
            return self._quiet()
        if player.routing:
            return                      # a start or stop under way (ours included): wait for it
        group = getattr(actions, "group", None)
        if group is not None and getattr(group, "following", False):
            return self._quiet()
        if self._cast:
            return
        try:
            phone = bool(actions.phone_has_it())
        except Exception:
            phone = False
        if phone:
            if self._stalled_at is not None:
                log.info("stream watchdog: a phone has the speaker - not restarting preset %s", self._name())
                self._record("stood down", self._state, self._error, reason="a phone has the speaker")
            self._cast = True
            return self._quiet()
        preset = self._preset
        if preset is None or preset.source not in MPD_SOURCES:
            return
        status = self._status()
        if status is None:
            return
        progressed = self._sample(status, now)
        if self._finished:
            if progressed:                  # moOde's UI, say, played it again
                self._finished = False
            return
        if self._stalled_at is None:
            if progressed:
                self._armed = True
                self._stop_since = None
            if not self._armed:
                return                      # not heard yet: the loading lamp's job
            self._detect(status, now)
            if self._stalled_at is None:
                return
        self._recover(status, progressed, now)

    # -- pieces -------------------------------------------------------------

    def _watch(self, gen: int, preset: Preset | None) -> None:
        """A new routing generation: forget everything about the last one."""
        self._gen, self._preset = gen, preset
        self._armed = self._cast = self._finished = False
        self._progress_at = self._stop_since = self._elapsed = None
        self._clear_stall()

    def _quiet(self) -> None:
        self._clear_stall()
        self._finished = False
        self._armed = False

    def _clear_stall(self) -> None:
        self._stalled_at = None
        self._attempts = 0
        self._phase = "wait"
        self._restart_failed = self._pulsed = False

    def _record(self, action: str, state: str | None, error: str | None, **extra: Any) -> None:
        try:
            self.events.add(action, self._preset, state, error, **extra)
        except Exception:
            log.debug("stream event not recorded", exc_info=True)

    def _record_mpd(self, action: str, _preset: Any, error: str | None, **extra: Any) -> None:
        try:
            self.events.add(action, getattr(self.actions.player, "wanted", None), None, error, **extra)
        except Exception:
            log.debug("stream event not recorded", exc_info=True)

    def _replay(self) -> str | None:
        """MPD answers again: start the preset last asked for, as it was asked.
        Not when a hand, standby or a phone has the radio, or MPD plays already.
        The preset's number (as a string) if it was started, else None."""
        actions, player = self.actions, self.actions.player
        preset = getattr(player, "wanted", None)
        if preset is None or preset.source not in MPD_SOURCES:
            return None
        if actions.standby or getattr(actions, "shutting_down", False) or player.routing:
            return None
        group = getattr(actions, "group", None)
        if group is not None and getattr(group, "following", False):
            return None
        try:
            if actions.phone_has_it():
                return None
        except Exception:
            pass
        status = self._status()
        if status is not None and status.get("state") == "play":
            return None                 # it came back playing (moOde, its state file): leave it
        lock = self.lock
        if lock is not None and not lock.acquire(timeout=2.0):
            return None
        try:
            gen = player.route_gen
            if player.current() not in (None, preset):
                return None
            on_start = getattr(actions, "on_start", None)
            if callable(on_start):
                try:
                    on_start(preset)        # the lamp's loading breath, as for a key
                except Exception:
                    log.debug("on_start hook failed", exc_info=True)
            if player.restart(preset, gen, on_error=getattr(actions, "_start_failed", None)) is None:
                return None
        finally:
            if lock is not None:
                lock.release()
        log.info("MPD is back: starting preset %s (%s) again", preset.n, preset.name)
        return str(preset.n)

    def recent_events(self) -> list[dict]:
        return self.events.recent()

    def _name(self) -> str:
        p = self._preset
        return f"{p.n} ({p.name})" if p is not None else "?"

    def _status(self) -> dict | None:
        radio = (getattr(self.actions.player, "sources", None) or {}).get("radio")
        mpd = getattr(radio, "mpd", None)
        if not callable(mpd):
            return None
        try:
            return dict(mpd().status())
        except Exception as exc:
            log.debug("stream watchdog: MPD status unreadable (%s)", exc)
            return None

    def _sample(self, status: dict, now: float) -> bool:
        """Take in one status; True if MPD played on since the last one."""
        state = str(status.get("state") or "")
        error = str(status.get("error") or "") or None
        try:
            elapsed = float(status["elapsed"]) if status.get("elapsed") is not None else None
        except (TypeError, ValueError):
            elapsed = None
        if state != self._state:
            log.info("MPD %s -> %s (preset %s%s)", self._state or "?", state or "?",
                     self._name(), f"; MPD says: {error}" if error else "")
            if state == "stop" and self._state is not None:
                self._record("mpd stopped", state, error, previous=self._state)
                self._noted_error = error
            elif error and error != self._noted_error:
                self._record("mpd error", state, error)
                self._noted_error = error
        elif error and error != self._error:
            log.info("MPD reports: %s (preset %s, %s)", error, self._name(), state)
            if error != self._noted_error:
                self._record("mpd error", state, error)
                self._noted_error = error
        progressed = (
            state == "play" and self._state == "play"
            and elapsed is not None and self._elapsed is not None and elapsed != self._elapsed
        )
        if state == "play" and self._state != "play":
            self._progress_at = now         # the stall clock starts with play
        if progressed:
            self._progress_at = now
        if state == "stop":
            if self._stop_since is None:
                self._stop_since = now
        else:
            self._stop_since = None
        if status.get("song") is not None and state in ("play", "pause"):
            try:
                self._song = int(status["song"])
            except (TypeError, ValueError):
                pass
        self._state, self._elapsed, self._error = state, elapsed, error
        return progressed

    def _detect(self, status: dict, now: float) -> None:
        state = self._state
        if state == "stop" and self._stop_since is not None and now - self._stop_since >= STOP_CONFIRM_S:
            if self._ended(status):
                log.info("YouTube Music preset %s played to its end; nothing to restart", self._name())
                self._record("ended", self._state, self._error)
                self._finished = True
                return
            why = "MPD stopped by itself" + (f" ({self._error})" if self._error else "")
            self._stall(why, now)
        elif state == "play" and self._progress_at is not None and now - self._progress_at >= STALL_S:
            self._stall(f"MPD plays but elapsed stuck at {self._elapsed} s for {now - self._progress_at:.0f} s", now)

    def _ended(self, status: dict) -> bool:
        """A YTM queue that reached its last track and stopped cleanly."""
        if self._preset is None or self._preset.source != "ytm" or self._error:
            return False
        try:
            length = int(status.get("playlistlength") or 0)
        except (TypeError, ValueError):
            return False
        return status.get("song") is None and self._song is not None and length > 0 and self._song >= length - 1

    def _stall(self, why: str, now: float) -> None:
        log.info("stream watchdog: preset %s should be playing but isn't - %s; recovering",
                 self._name(), why)
        self._stalled_at = now
        self._reason = why
        self._record("stalled", self._state, self._error, reason=why)
        self._attempts = 0
        self._phase = "wait"
        self._deadline = now                # the first attempt at once
        self._pulsed = self._restart_failed = False

    def _recover(self, status: dict, progressed: bool, now: float) -> None:
        if progressed:
            log.info("stream watchdog: preset %s playing again after %.0f s (%s, %d failed attempt%s)",
                     self._name(), now - (self._stalled_at or now),
                     "play" if self._phase == "play" else "restart" if self._phase == "preset" else "by itself",
                     self._attempts, "" if self._attempts == 1 else "s")
            self._record("recovered", self._state, self._error,
                         after_s=round(now - (self._stalled_at or now), 1), failed_attempts=self._attempts)
            self._clear_stall()
            self._stop_since = None
            return
        if self._phase == "wait":
            if now < self._deadline:
                return
            self._phase, self._deadline = "play", now + VERIFY_S
            log.info("stream watchdog: attempt %d for preset %s: play", self._attempts + 1, self._name())
            self._record("play", self._state, self._error, attempt=self._attempts + 1)
            if not self._resume():
                self._deadline = now        # refused: straight to a re-start next poll
            return
        if self._phase == "play":
            if now < self._deadline:
                return
            self._phase, self._deadline = "preset", now + RESTART_VERIFY_S
            log.info("stream watchdog: attempt %d for preset %s: play did not bring it back; "
                     "starting the preset again", self._attempts + 1, self._name())
            self._restart_failed = False
            self._record("restart preset", self._state, self._error, attempt=self._attempts + 1)
            if not self._restart():
                self._deadline = now
            return
        if now < self._deadline and not self._restart_failed:
            return
        self._attempts += 1
        wait = BACKOFF_S[min(self._attempts - 1, len(BACKOFF_S) - 1)]
        log.info("stream watchdog: attempt %d for preset %s failed; next in %.0f s",
                 self._attempts, self._name(), wait)
        self._phase, self._deadline = "wait", now + wait
        pulse = self._attempts >= PULSE_AFTER and not self._pulsed and not self._is_night()
        self._record("attempt failed", self._state, self._error, attempt=self._attempts,
                     next_in_s=wait, lamp_fail=pulse)
        if self._attempts >= PULSE_AFTER and not self._pulsed:
            self._pulsed = True
            if self._is_night():
                log.info("stream watchdog: still silent after %d attempts (night: no red pulses); "
                         "trying on quietly", self._attempts)
            else:
                log.info("stream watchdog: still silent after %d attempts; red pulses once, "
                         "trying on quietly", self._attempts)
                try:
                    self.actions.reply("fail")
                except Exception:
                    log.debug("lamp fail reply failed", exc_info=True)

    def _resume(self) -> bool:
        try:
            return bool(self.actions.player.resume_mpd(self._gen))
        except Exception as exc:
            log.info("stream watchdog: MPD refused play: %s", exc)
            return False

    def _restart(self) -> bool:
        preset = self._preset
        if preset is None:
            return False

        def failed(exc: Exception) -> None:
            self._restart_failed = True     # the player's worker already logged it

        try:
            gen = self.actions.player.restart(preset, self._gen, on_error=failed)
        except Exception as exc:
            log.info("stream watchdog: could not start preset %s again: %s", self._name(), exc)
            return False
        if gen is None:
            return False                    # somebody routed meanwhile: the next poll sees it
        self._gen = gen                     # ours: keep watching through it
        self._elapsed = None                # a new stream: two fresh samples to count as moving
        return True

    def _is_night(self) -> bool:
        if self.night is None:
            return False
        try:
            return bool(self.night())
        except Exception:
            return False
