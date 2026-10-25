"""The real player: MPD for playback, ALSA for volume, moOde for renderers.

Three separate surfaces, deliberately:

* **Sources** make the sound. A small router holds one per preset kind -
  ``radio`` (MPD), ``spotify`` (go-librespot) and ``ytm`` (MPD via a local
  proxy) - and keeps exactly one of them playing, because only one process can
  hold the ALSA device. moOde's web UI reads the same MPD state, so radio and
  YTM show up there for free.
* **The hardware ALSA mixer** carries volume. This is the whole reason the BOM
  insists on a DAC with a hardware mixer: `amixer` reaches Bluetooth, AirPlay
  and Spotify exactly as it reaches MPD, so one knob governs everything.
* **moOde** owns renderer arbitration - when a renderer connects it stops MPD,
  and resumes it on disconnect. We read that state rather than reimplementing it.

Nothing here is guessed at runtime: every external command and query lives in
the config block below, defaulted but overridable, because moOde's internals
move between versions. Verify them once in Phase 1 and pin the moOde version.
"""

from __future__ import annotations

import json
import logging
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..bootresume import http_answers, mpd_answers
from .base import Preset, Source
from .bluetooth import Bluetooth
from .renderers import MoodeRenderers
from .sources.hls import HlsProxy
from .sources.radio import RadioSource, mpd_patience

log = logging.getLogger(__name__)

# moOde records active renderers as flags in its sqlite config table. Confirm
# these against the installed version once, in Phase 1. No "spotify" here:
# moOde's own librespot renderer is switched off in favour of go-librespot,
# which the router asks directly (SpotifySource.is_active).
DEFAULT_SOURCE_FLAGS = {
    "bluetooth": "btactive",
    # moOde's own names (cfg_system). "airplayactv" was a guess that matched
    # nothing, so AirPlay never registered as the active source.
    "airplay": "aplactive",
}

BTMETA_FILE = "/var/local/www/btmeta.json"   # moOde's btmeta.py: {title, artist, ...}
RENDERER_INFO_S = 3.0                         # bluetoothctl per web poll is too much

DEFAULT_RENDERER_UNITS = {
    "bluetooth": "bluealsa-aplay",
    "airplay": "shairport-sync",
}

MOODE_DB = Path("/var/local/www/db/moode-sqlite3.db")

# The boot resume's swell (bootresume.py): silent -> the knob's level in
# steps, in step with the lamp's 1.5 s fade to playing.
SWELL_S = 3.0
SWELL_STEPS = 20

# active_source() is asked by the main loop and by every key press. The answer
# costs an HTTP status call and a read of moOde's sqlite db, and a human
# cannot tell a one-second-old answer from a fresh one.
SOURCE_CACHE_S = 1.0
# ...nor is it worth waiting for: MPD busy (or hung, 2026-10-25) answers
# "not ytm" after this long rather than holding up the panel loop or the page.
SOURCE_MPD_S = 1.0


@dataclass
class SpotifyConfig:
    """go-librespot's local HTTP API. ``enabled: false`` drops the source."""

    enabled: bool = True
    base_url: str = "http://127.0.0.1:3678"
    timeout: float = 3.0
    # is_active runs on the panel loop; a wedged daemon must not stall it.
    status_timeout: float = 0.5


@dataclass
class YtmConfig:
    """The YouTube Music proxy. Shares MPD with radio."""

    enabled: bool = True
    port: int = 8099
    ytdlp: str = "yt-dlp"
    cookies: str | None = None
    # The ytmusicapi catalogue (search, names, endless radio, the owner's
    # sign-in). Off = yt-dlp only, as before. Absent ytmusicapi = off anyway.
    catalog: bool = True


@dataclass
class MoodeConfig:
    mpd_host: str = "localhost"
    mpd_port: int = 6600
    mixer: str = "Digital"          # from: moodeutl -q "select value from cfg_system where param='amixname'"
    # amixer -c takes an index or the ALSA card id. The id survives HDMI
    # cards shifting the numbering; the Amp4 Pro is "sndrpihifiberry".
    card: int | str = "sndrpihifiberry"
    moodeutl: str = "moodeutl"
    amixer: str = "amixer"
    # Ceiling on the hardware mixer, as a percentage. The Amp4 Pro can deliver
    # roughly 10 W into the Hasler's 16 ohm elliptical; the cone is good for
    # perhaps 1-3 W. Without a ceiling, "knob at maximum" means a cooked voice
    # coil on a part that cannot be bought. Calibrate by ear (see docs), then
    # the knob's full sweep maps into 0..volume_limit.
    volume_limit: int = 70
    # Mixer percentage at the knob's first step above zero. Digital on the
    # Amp4 Pro is linear in dB (0.5 dB per step, 0% = -103.5 dB), so without
    # a floor the bottom half of the knob is silent (1% is -102.5 dB). 28%
    # (-74.5 dB) is the quietest step heard on the bench radio; the key page's
    # saved volume_floor wins over this. 0 keeps the plain mapping.
    volume_floor: int = 28
    bluetoothctl: str = "bluetoothctl"
    source_flags: dict = field(default_factory=lambda: dict(DEFAULT_SOURCE_FLAGS))
    renderer_units: dict = field(default_factory=lambda: dict(DEFAULT_RENDERER_UNITS))
    # The knobs' sound effects (haslerd/dsp/): the helper that puts them into
    # CamillaDSP. While it is installed it drives every knob that carries an
    # effect and tone_commands below is not used; "" turns it off.
    dsp_command: str = "/usr/local/bin/hasler-dsp"
    # The old per-wheel commands, used only without dsp_command: `{gain}` is
    # dB, `{value}` the raw 0-100, `{control}` the wheel id. Empty means the
    # wheels do nothing.
    tone_commands: dict = field(default_factory=dict)
    # Blocks of their own in haslerd.yaml (``moode: {spotify: {...}, ytm: {...}}``).
    spotify: SpotifyConfig = field(default_factory=SpotifyConfig)
    ytm: YtmConfig = field(default_factory=YtmConfig)

    def __post_init__(self) -> None:
        # YAML hands us plain dicts; unknown keys fail loudly at startup.
        if isinstance(self.spotify, dict):
            self.spotify = SpotifyConfig(**self.spotify)
        if isinstance(self.ytm, dict):
            self.ytm = YtmConfig(**self.ytm)


def build_sources(config: MoodeConfig, state_dir: str | Path | None = None) -> dict[str, Source]:
    """Radio always; Spotify and YTM if their modules import and construct.

    Imported lazily and inside a try so the daemon still plays radio when a
    source module is missing, half-written, or its dependencies are absent -
    the box must never lose its radio because Spotify broke. ``state_dir``
    (/var/lib/haslerd) is where the YouTube Music sign-in, its state across
    reboots and the boot cache of opening tracks are kept.
    """
    radio = RadioSource(config.mpd_host, config.mpd_port, ytm_port=config.ytm.port, hls=HlsProxy())
    sources: dict[str, Source] = {"radio": radio}
    if config.spotify.enabled:
        try:
            from .sources.spotify import SpotifySource

            sources["spotify"] = SpotifySource(
                base_url=config.spotify.base_url,
                timeout=config.spotify.timeout,
                status_timeout=config.spotify.status_timeout,
            )
        except Exception:
            log.warning("spotify source unavailable; spotify presets will not play", exc_info=True)
    if config.ytm.enabled:
        try:
            from .sources.ytm import STREAM_DIR, YtmSource, stream_ok

            catalog = None
            if config.ytm.catalog:
                try:
                    from .sources.ytmusic import YtmCatalog

                    catalog = YtmCatalog(state_dir)
                except Exception:
                    log.warning("YouTube Music catalogue unavailable; yt-dlp only", exc_info=True)
            sources["ytm"] = YtmSource(
                mpd=radio.mpd,
                port=config.ytm.port,
                ytdlp=config.ytm.ytdlp,
                cookies=config.ytm.cookies or None,
                catalog=catalog,
                probe=stream_ok,
                state_dir=state_dir,
                # Ranged fetches past googlevideo's throttle (see ytm.py).
                stream_dir=(Path(state_dir) / STREAM_DIR if state_dir is not None
                            else Path(tempfile.gettempdir()) / "haslerd-ytm-stream"),
            )
            if catalog is not None:
                from .sources.ytm_playlists import PlaylistSync
                sources["ytm"].playlists = PlaylistSync(sources["ytm"], catalog, state_dir)
        except Exception:
            log.warning("ytm source unavailable; ytm presets will not play", exc_info=True)
    return sources


class MoodePlayer:
    """Implements the Player protocol against a real moOde install.

    ``sources`` and ``clock`` are injectable so the router - arbitration, the
    again-key bookkeeping, the source cache - is tested without MPD or HTTP.
    """

    def __init__(
        self,
        config: MoodeConfig | None = None,
        sources: dict[str, Source] | None = None,
        clock: Callable[[], float] = time.monotonic,
        state_dir: str | Path | None = None,
    ) -> None:
        self.config = config or MoodeConfig()
        self.sources: dict[str, Source] = (
            sources if sources is not None else build_sources(self.config, state_dir)
        )
        self.current_preset: Preset | None = None
        self._clock = clock
        self._pair_acceptor = None   # player/btaccept.PairAcceptor, on first pairing
        self._source_cache: tuple[float, str] | None = None
        self._muted = False
        self._volume: float | None = None   # mixer percent, post-ceiling
        self._knob: float | None = None     # knob scale 0-100, as last set
        self._renderer_cache: tuple[float, dict] | None = None
        self.moode_db = MOODE_DB            # read-only, for the renderer flags
        # moOde's casting targets (AirPlay, Bluetooth, UPnP): on/off, names.
        self.renderers = MoodeRenderers(db=MOODE_DB)
        # The phones themselves: paired list, reconnect, AVRCP, codec (BlueZ/BlueALSA).
        self.bluetooth = Bluetooth(state_dir)
        # The swell's bookkeeping. Every write the knob (or standby) makes
        # bumps _mixer_gen, which stops a swell in its tracks: the knob wins.
        self._mixer_lock = threading.RLock()
        self._mixer_gen = 0
        self._hold: int | None = None      # mixer percent a swell will rise to
        self._swelling = False
        # Starting and stopping, off the caller's thread (start_preset /
        # stop_soon). MPD blocks *every* client for 0.5-4 s while it stops a
        # stream (its output closes through moOde's CamillaDSP plugin; measured
        # on the bench 2026-10-21), so a key press must not wait for it. One
        # worker, one slot: a newer request replaces a pending one, and a
        # request already talking to MPD is told it has been superseded
        # (``_gen``). ``_route_lock`` keeps two arbitrations from interleaving.
        self._route_lock = threading.RLock()
        self._want_cv = threading.Condition()
        # (gen, preset or None for "stop", on_error, previous preset, cached source)
        self._want: tuple | None = None
        self._gen = 0
        self._starting: Preset | None = None      # asked to start, not yet started
        self._wanted: Preset | None = None        # the last start asked for (None: a stop)
        self._worker: threading.Thread | None = None
        self._busy = False
        self._warm_up()

    def _warm_up(self) -> None:
        """Start anything a source serves in the background, e.g. the YTM proxy.

        After a daemon restart MPD may still be partway through a YTM queue of
        proxy URLs; if the proxy is not listening, every remaining track fails
        and the playlist silently empties itself.
        """
        for name, source in self.sources.items():
            start = getattr(source, "start", None)
            if callable(start):
                try:
                    start()
                except Exception:
                    log.warning("could not start %s source", name, exc_info=True)

    # -- routing ----------------------------------------------------------

    def play_preset(self, preset: Preset) -> None:
        """Stop every other source, then start this one - and wait for it.

        ALSA arbitration: the DAC has one hardware device and only one process
        may hold it. Stopping the others *first* is what lets go-librespot and
        MPD hand the device back and forth without "device busy". Raises if the
        source is unknown or its ``play`` raises, so the caller can flash the
        lamp. Supersedes any pending ``start_preset``. Key presses use
        ``start_preset`` instead; this is for callers that need the outcome
        (the boot resume, a group handover, the page's Test button).
        """
        self._source_for(preset)
        gen, previous, cached = self._supersede(preset)
        with self._route_lock:
            self._play_now(preset, gen, previous, cached)

    def start_preset(
        self, preset: Preset, on_error: Callable[[Exception], None] | None = None
    ) -> None:
        """``play_preset`` without waiting: returns at once, the worker plays it.

        ``current()`` is ``preset`` from now on (so the same key again is
        "again"), and ``active_source()`` answers its source without asking
        MPD while the start is under way. A newer start or stop replaces this
        one if it has not begun; if it has, the worker skips whatever of it is
        left. ``on_error(exc)`` is called from the worker if it fails and is
        still the latest request. An unknown source raises here, at once.
        """
        self._source_for(preset)
        self._request(preset, on_error)

    def stop_soon(self) -> None:
        """``stop`` without waiting (standby): supersedes a pending start."""
        self._request(None, None)

    @property
    def starting(self) -> Preset | None:
        """The preset asked to start that has not started (or failed) yet."""
        return self._starting

    # -- for the stream watchdog (player/watchdog.py) ------------------------

    @property
    def route_gen(self) -> int:
        """Bumped by every start and stop: a hand has routed since ``gen`` if it differs."""
        return self._gen

    @property
    def wanted(self) -> Preset | None:
        """The preset the last routing asked for, whether or not it started
        (None after a stop). What comes back once MPD answers again."""
        return self._wanted

    @property
    def routing(self) -> bool:
        """A start or stop is pending or under way."""
        with self._want_cv:
            return self._want is not None or self._busy or self._starting is not None

    def resume_mpd(self, expect_gen: int) -> bool:
        """``play`` on MPD's queue as it stands (``mpc play``), unless anyone
        routed since ``expect_gen`` or is routing now. True if MPD was told."""
        if not self._route_lock.acquire(timeout=1.0):
            return False
        try:
            with self._want_cv:
                if self._gen != expect_gen or self._want is not None or self._busy:
                    return False
            radio = self.sources.get("radio")
            if radio is None:
                return False
            radio.mpd().play()
            return True
        finally:
            self._route_lock.release()

    def restart(
        self, preset: Preset, expect_gen: int, on_error: Callable[[Exception], None] | None = None
    ) -> int | None:
        """``start_preset(preset)`` again, unless anyone routed since ``expect_gen``,
        is routing now, or something else is current. The new generation, or None."""
        self._source_for(preset)
        with self._want_cv:
            if (self._gen != expect_gen or self._want is not None or self._busy
                    or self._starting is not None or self.current_preset not in (None, preset)):
                return None
            self._request(preset, on_error)
            return self._gen

    def _source_for(self, preset: Preset) -> Source:
        source = self.sources.get(preset.source)
        if source is None:
            raise LookupError(f"no {preset.source!r} source available for preset {preset.n}")
        return source

    def _supersede(self, preset: Preset | None) -> tuple[int, Preset | None, str | None]:
        """A new routing request: drop any pending one, record the intent."""
        with self._want_cv:
            previous = self.current_preset
            cached = self._source_cache[1] if self._source_cache else None
            self._gen += 1
            self._want = None
            self.current_preset = preset
            self._starting = preset
            self._wanted = preset
            self._source_cache = None
            return self._gen, previous, cached

    def _request(self, preset: Preset | None, on_error) -> None:
        with self._want_cv:                     # an RLock: _supersede nests
            gen, previous, cached = self._supersede(preset)
            self._want = (gen, preset, on_error, previous, cached)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="player-route", daemon=True)
                self._worker.start()
            self._want_cv.notify()

    def _work(self) -> None:
        while True:
            with self._want_cv:
                while self._want is None:
                    self._want_cv.wait()
                gen, preset, on_error, previous, cached = self._want
                self._want = None
                self._busy = True
            try:
                self._serve(gen, preset, on_error, previous, cached)
            finally:
                with self._want_cv:
                    self._busy = False
                    self._want_cv.notify_all()

    def _serve(self, gen, preset, on_error, previous, cached) -> None:
        with self._route_lock:
            if gen != self._gen:
                return                          # superseded while waiting for the lock
            if preset is None:
                self._stop_now(gen)
                return
            try:
                self._play_now(preset, gen, previous, cached)
            except Exception as exc:
                if gen != self._gen:
                    log.info("preset %s failed after it was superseded: %s", preset.n, exc)
                    return
                log.error("could not start preset %s: %s", preset.n, exc,
                          exc_info=not isinstance(exc, LookupError))
                if on_error is not None:
                    try:
                        on_error(exc)
                    except Exception:
                        log.debug("on_error hook failed", exc_info=True)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no background start or stop is pending or running."""
        with self._want_cv:
            return self._want_cv.wait_for(lambda: self._want is None and not self._busy, timeout)

    def _stoppable(self, keep: Source | None) -> list[tuple[str, Source]]:
        """One source per backend, leaving out ``keep``'s backend.

        Radio and YTM share one MPD (``backend = "mpd"``): the source about to
        play replaces MPD's queue itself, and stopping MPD first would only pay
        MPD's slow stop twice - and block on it, as ytm did on every radio press.
        """
        keep_backend = getattr(keep, "backend", None) or (keep.name if keep is not None else None)
        seen = {keep_backend} if keep is not None else set()
        out = []
        for name, other in self.sources.items():
            if other is keep:
                continue
            backend = getattr(other, "backend", None) or name
            if backend in seen:
                continue
            seen.add(backend)
            out.append((name, other))
        return out

    def _play_now(self, preset: Preset, gen: int, previous: Preset | None, cached: str | None) -> None:
        source = self._source_for(preset)
        for name, other in self._stoppable(source):
            if gen != self._gen:
                return                          # a newer press: it does its own stopping
            self._stop_source(name, other)
        if gen != self._gen:
            return
        try:
            source.play(preset)
        except Exception:
            if gen == self._gen:
                with self._want_cv:
                    if gen == self._gen:
                        self.current_preset = None
                        self._starting = None
            spotify_was_on = cached == "spotify" or (previous is not None and previous.source == "spotify")
            if preset.source != "spotify" and spotify_was_on:
                # go-librespot is paused, not stopped, to keep its session warm.
                # If it still holds the ALSA device, MPD fails with "device busy".
                log.error(
                    "%s play failed right after pausing go-librespot - if MPD says "
                    "'device busy', go-librespot is holding ALSA while paused",
                    preset.source,
                )
            raise
        with self._want_cv:
            if gen == self._gen:
                self._starting = None
                self._source_cache = None

    def _stop_source(self, name: str, source: Source) -> None:
        try:
            source.stop()
        except Exception:
            log.exception("could not stop %s", name)

    def stop(self) -> None:
        """Stop everything and wait for it. Supersedes any pending start."""
        gen, _, _ = self._supersede(None)
        with self._route_lock:
            self._stop_now(gen)

    def _stop_now(self, gen: int) -> None:
        for name, source in self._stoppable(None):
            if gen != self._gen:
                return                          # a press after the stop: it wins
            self._stop_source(name, source)

    def next_track(self) -> None:
        """Skip within the current preset. Meaningless (and a no-op) for radio."""
        preset = self.current_preset
        if preset is None:
            return
        source = self.sources.get(preset.source)
        if source is not None:
            source.next_track()

    def current(self) -> Preset | None:
        return self.current_preset

    # -- volume, via the hardware mixer -----------------------------------

    def _amixer(self, *args: str) -> None:
        subprocess.run(
            [self.config.amixer, "-c", str(self.config.card), "sset", self.config.mixer, *args],
            check=True,
            capture_output=True,
            timeout=5,
        )

    def _mixer_level(self, percent: int) -> None:
        if percent > 0:
            # Turning the knob up always un-mutes: the switch may have been
            # left off by moOde, a standby or a restart, and a level on a
            # muted mixer is silence that looks like music.
            try:
                self._amixer(f"{percent}%", "unmute")
            except subprocess.CalledProcessError:
                self._amixer(f"{percent}%")   # a mixer without a mute switch
        else:
            self._amixer(f"{percent}%")

    def set_volume(self, value: float) -> None:
        percent = scale_volume(value, self.config.volume_limit, self.config.volume_floor)
        with self._mixer_lock:
            self._mixer_gen += 1
            self._hold = None
            self._mixer_level(percent)
        self._volume = float(percent)
        self._knob = max(0.0, min(100.0, float(value)))
        if self._muted and percent > 0:
            self._muted = False

    # -- the boot resume's swell ------------------------------------------

    def hold_silent(self) -> None:
        """Mixer to 0% for a swell to come, remembering the level to rise to.

        That level is the knob's (post-ceiling); with no knob reading yet,
        the mixer's own, capped at ``volume_limit``. Unknown: left alone.
        """
        with self._mixer_lock:
            target = self._volume
            if target is None:
                percent = self._read_mixer()
                if percent is None:
                    return
                target = min(percent, max(1, min(100, int(self.config.volume_limit))))
            self._mixer_gen += 1
            self._hold = int(target)
            self._amixer("0%")

    def release_hold(self) -> None:
        """No swell after all (the start failed): back to the level at once."""
        with self._mixer_lock:
            if self._hold is None or self._swelling:
                return
            target, self._hold = self._hold, None
            self._mixer_gen += 1
            self._mixer_level(target)

    def swell(
        self,
        seconds: float = SWELL_S,
        steps: int = SWELL_STEPS,
        sleep: Callable[[float], None] = time.sleep,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        """Raise the held mixer to its level over ``seconds``, on a thread.

        From the volume floor (the knob's first audible step) up, linear in
        mixer percent - linear in dB on the Amp4 Pro, so it reads as a fade.
        Never above the level held (the knob's, under ``volume_limit``); a
        knob turned, a mute or a release meanwhile ends it where it is.
        """
        with self._mixer_lock:
            target = self._hold
            if target is None:
                return
            gen = self._mixer_gen
            self._swelling = True
        start = min(max(0, int(self.config.volume_floor)), target)
        steps = max(1, int(steps))

        def run() -> None:
            try:
                for i in range(1, steps + 1):
                    with self._mixer_lock:
                        if self._mixer_gen != gen:
                            return
                        self._mixer_level(start + (target - start) * i // steps)
                    if i < steps:
                        sleep(seconds / steps)
            except Exception:
                log.exception("volume swell failed; setting the level at once")
                with self._mixer_lock:
                    if self._mixer_gen == gen:
                        try:
                            self._mixer_level(target)
                        except Exception:
                            log.debug("could not set the level either", exc_info=True)
            finally:
                with self._mixer_lock:
                    self._swelling = False
                    if self._mixer_gen == gen:
                        self._hold = None

        if spawn is None:
            threading.Thread(target=run, name="swell", daemon=True).start()
        else:
            spawn(run)

    def reassert_volume(self) -> None:
        """moOde's startup may have set its own level (vol.sh): the knob's again."""
        with self._mixer_lock:
            if self._swelling or self._hold is not None:
                return      # the swell ends on the knob's level anyway
        if self._knob is not None:
            self.set_volume(self._knob)

    def _read_mixer(self) -> int | None:
        try:
            result = subprocess.run(
                [self.config.amixer, "-c", str(self.config.card), "sget", self.config.mixer],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            log.debug("could not read mixer volume", exc_info=True)
            return None
        return parse_amixer_percent(result.stdout)

    def get_volume(self) -> float | None:
        """Current volume on the knob scale, read back from the mixer.

        Read rather than remembered, because the moOde UI, HA and a phone over
        AirPlay all move the same hardware mixer behind our back. Falls back to
        the last value we set if amixer cannot be read.
        """
        percent = self._read_mixer()
        if percent is None:
            return self._knob
        return unscale_volume(percent, self.config.volume_limit, self.config.volume_floor)

    def set_mute(self, muted: bool) -> None:
        # Prefer the mixer's own mute switch; fall back to volume 0 on mixers
        # that have no switch (some DACs expose only a volume control).
        with self._mixer_lock:
            self._mixer_gen += 1
            self._hold = None
            try:
                self._amixer("mute" if muted else "unmute")
            except subprocess.CalledProcessError:
                self._amixer("0%" if muted else f"{int(self._volume or 40)}%")
        self._muted = muted

    # -- tone -------------------------------------------------------------

    def set_tone(self, control: str, value: float) -> None:
        """Map a wheel to a shelf gain and hand it to a configured command.

        The *mapping* is pure and tested (see ``tone_gain``). The *invocation*
        is a command template in config, empty by default, because moOde's EQ
        surface differs between versions and CamillaDSP setups - it gets filled
        in against the running box in Phase 4 rather than guessed here.

        Tone failing must never interrupt the music, so everything below is
        best-effort and swallowed.
        """
        gain = tone_gain(value)
        template = (self.config.tone_commands or {}).get(control)
        if not template:
            log.debug("no tone command configured for %s (gain would be %+.1f dB)", control, gain)
            return
        try:
            subprocess.run(
                shlex.split(template.format(gain=gain, value=value, control=control)),
                check=True,
                capture_output=True,
                timeout=5,
            )
        except Exception:
            log.debug("tone command for %s failed", control, exc_info=True)

    # -- sources ----------------------------------------------------------

    def _moodeutl(self, query: str) -> str:
        result = subprocess.run(
            [self.config.moodeutl, "-q", query],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()

    def active_source(self) -> str:
        """What is making sound: our own sources first, then moOde's renderers.

        go-librespot and the YTM proxy are ours, so they are asked directly;
        moOde knows nothing about either. Otherwise moOde's renderer flags
        decide, and "radio" is the resting answer. Cached for SOURCE_CACHE_S
        and dropped whenever we change what is playing.
        """
        starting = self._starting
        if starting is not None:
            # A start under way: MPD may be busy stopping the last stream (and
            # answers nobody meanwhile); we know what we asked for.
            return starting.source
        now = self._clock()
        if self._source_cache is not None and now - self._source_cache[0] < SOURCE_CACHE_S:
            return self._source_cache[1]
        source = self._probe_source()
        self._source_cache = (now, source)
        return source

    def _probe_source(self) -> str:
        for name in ("spotify", "ytm"):
            source = self.sources.get(name)
            if source is None:
                continue
            try:
                with mpd_patience(SOURCE_MPD_S):
                    active = source.is_active()
                if active:
                    return name
            except Exception:
                log.debug("%s is_active failed", name, exc_info=True)
        flags = self.config.source_flags
        if not flags:
            return "radio"
        values = self._moode_flags(list(flags.values()))
        for source, flag in flags.items():
            if values is not None:
                value = values.get(flag, "")
            else:
                try:
                    value = self._moodeutl(
                        f"select value from cfg_system where param='{flag}'"
                    )
                except Exception:
                    log.debug("could not read moOde flag %s", flag, exc_info=True)
                    continue
            if value not in ("", "0", "off", "None"):
                return source
        return "radio"

    def _moode_flags(self, params: list[str]) -> dict[str, str] | None:
        """``cfg_system`` values for ``params``, read straight from moOde's db.

        One read-only sqlite query (~2 ms) instead of a moodeutl per flag:
        moodeutl is a PHP script, ~120 ms of CPU each, and active_source()
        is asked every second or two. A missing row is simply absent; None
        when the db cannot be read (no moOde, locked mid-write), and the
        caller falls back to moodeutl.
        """
        try:
            con = sqlite3.connect(f"file:{self.moode_db}?mode=ro", uri=True, timeout=0.5)
            try:
                marks = ",".join("?" * len(params))
                rows = con.execute(
                    f"select param, value from cfg_system where param in ({marks})", params
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            log.debug("moOde db unreadable; asking moodeutl", exc_info=True)
            return None
        return {str(param): "" if value is None else str(value) for param, value in rows}

    # -- for the boot resume (bootresume.py), asked off the main loop ------

    def answering(self, source: str) -> bool:
        """Is the process behind ``source`` up and talking yet?"""
        if source in ("radio", "ytm"):
            return mpd_answers(self.config.mpd_host, self.config.mpd_port)
        if source == "spotify":
            return http_answers(self.config.spotify.base_url.rstrip("/") + "/status")
        return True

    def still_playing(self, preset: Preset) -> bool:
        """``preset`` is what we started and its player still plays it.

        For radio, MPD must be playing this very stream: moOde's worker
        reloading its own queue counts as "not". Never raises.
        """
        if self.current_preset != preset or self._starting is not None:
            return False
        source = self.sources.get(preset.source)
        if source is None:
            return False
        try:
            if preset.source == "radio":
                client = source.mpd()
                if client.status().get("state") != "play":
                    return False
                queued = getattr(source, "queued_uri", lambda u: u)(preset.uri)   # HLS: its proxy URL
                return str(client.currentsong().get("file", "")) == queued
            return bool(source.is_active())
        except Exception:
            log.debug("still_playing check failed", exc_info=True)
            return False

    def wrkready(self, db: Path = MOODE_DB) -> int | None:
        """moOde's worker sets cfg_system ``wrkready`` to 1 when its startup is done.

        Read-only sqlite first, moodeutl if that is refused; None if neither
        answers (no moOde, or it is mid-write).
        """
        value: str | None = None
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                row = con.execute("select value from cfg_system where param='wrkready'").fetchone()
            finally:
                con.close()
            value = None if row is None else str(row[0])
        except sqlite3.Error:
            try:
                value = self._moodeutl("select value from cfg_system where param='wrkready'")
            except Exception:
                return None
        try:
            return int(value) if value not in (None, "") else None
        except ValueError:
            return None

    def select_source(self, source: str) -> None:
        """Hand the audio path to a renderer, or take it back for MPD."""
        self._source_cache = None
        if source == "radio":
            for name in self.config.renderer_units:
                self._disconnect(name)
            # Spotify Connect lives in go-librespot now, outside moOde's
            # arbitration, so taking the path back for MPD means pausing it.
            spotify = self.sources.get("spotify")
            if spotify is not None:
                self._stop_source("spotify", spotify)
                if self.current_preset is not None and self.current_preset.source == "spotify":
                    self.current_preset = None
            return
        # moOde starts renderers on demand; selecting one mostly means getting
        # out of the way and letting the sender connect.
        self.stop()

    def _disconnect(self, source: str) -> None:
        if source != "bluetooth" or not shutil.which(self.config.bluetoothctl):
            return
        # Bluetooth off means nothing to disconnect - and bluetoothctl would
        # hang waiting for BlueZ, failing the whole source switch.
        if not self._bluetooth_up():
            return
        try:
            subprocess.run([self.config.bluetoothctl, "disconnect"], capture_output=True, timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("bluetoothctl disconnect did not answer; carrying on")

    def renderer_info(self) -> dict:
        """Who is connected over Bluetooth and what they play - for the web page.

        ``{"device": "Pixel 11", "title", "artist", "codec": "aptX HD", "rate": 48000}``,
        empty fields when nobody is connected. BlueZ's AVRCP track first, moOde's
        btmeta.json if the phone sends none. Cached briefly: the page polls every second.
        """
        now = self._clock()
        if self._renderer_cache is not None and now - self._renderer_cache[0] < RENDERER_INFO_S:
            return self._renderer_cache[1]
        info: dict = {"device": "", "title": "", "artist": "", "codec": None, "rate": None}
        try:
            playing = self.bluetooth.playing()
        except Exception:
            log.debug("bluetooth state unreadable", exc_info=True)
            playing = None
        if playing is not None:
            info.update({"device": playing["name"], "title": playing["title"],
                         "artist": playing["artist"], "codec": playing["codec"],
                         "rate": playing["rate"]})
        elif shutil.which(self.config.bluetoothctl) and self._bluetooth_up() and self.bluetooth.adapter() is None:
            # BlueZ not answering busctl (older box?): bluetoothctl as before.
            try:
                out = subprocess.run(
                    [self.config.bluetoothctl, "devices", "Connected"],
                    capture_output=True, text=True, timeout=3,
                ).stdout
                info["device"] = parse_connected_device(out)
            except (OSError, subprocess.TimeoutExpired):
                log.debug("bluetoothctl devices failed", exc_info=True)
        if info["device"] and not info["title"]:
            try:
                meta = json.loads(Path(BTMETA_FILE).read_text() or "{}")
                info["title"] = str(meta.get("title") or "")
                info["artist"] = str(meta.get("artist") or "")
            except (OSError, ValueError):
                pass
        self._renderer_cache = (now, info)
        return info

    def cast_paused(self) -> bool:
        """A phone on Bluetooth has paused (the lamp's cast-paused look)."""
        if self.active_source() != "bluetooth":
            return False
        try:
            return self.bluetooth.paused_soon()
        except Exception:
            return False

    def bluetooth_pair(self, seconds: int) -> None:
        if not shutil.which(self.config.bluetoothctl):
            log.warning("bluetoothctl not found; cannot enter pairing mode")
            return
        # With BlueZ down, bluetoothctl waits for it indefinitely - and this
        # runs on the panel loop, so every key would freeze until the timeout.
        if not self._bluetooth_up():
            log.warning("bluetooth is off - turn it on in moOde (Renderers > Bluetooth) to pair")
            return
        for args in bluetooth_pair_commands(seconds):
            try:
                result = subprocess.run(
                    [self.config.bluetoothctl, *args], capture_output=True, text=True, timeout=5
                )
            except subprocess.TimeoutExpired:
                log.warning("bluetoothctl did not answer; pairing mode not entered")
                return
            if result.returncode != 0:
                log.warning("bluetoothctl %s failed: %s", " ".join(args), (result.stdout or "").strip()[-200:])
                return
        log.info("bluetooth discoverable for %ss", seconds)
        # moOde's agent asks for a yes nobody can give on a Hasler; answer it
        # ourselves for as long as the window is open (player/btaccept.py).
        if self._pair_acceptor is None:
            from .btaccept import PairAcceptor
            self._pair_acceptor = PairAcceptor()
        self._pair_acceptor.open(seconds)

    def _bluetooth_up(self) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", "bluetooth"], capture_output=True, timeout=2
            )
        except (OSError, subprocess.TimeoutExpired):
            return True     # can't tell (no systemd?) - let bluetoothctl try
        return result.returncode == 0

    # -- misc -------------------------------------------------------------

    def route_speaker(self, position: int) -> None:
        # Excelsior only - DPDT relay on GPIO 24 plus a CamillaDSP profile swap.
        log.info("speaker route -> position %s (relay not fitted on this cabinet)", position)

    def prefetch(self, presets: list[Preset]) -> None:
        """Have sources that can warm up ahead of a press do so (today: ytm)."""
        for source in self.sources.values():
            warm = getattr(source, "warm", None)
            if warm is not None:
                try:
                    warm(list(presets))
                except Exception:
                    log.warning("could not start %s warm-up", source.name, exc_info=True)

    def shutdown(self) -> None:
        log.warning("clean shutdown requested from the panel")
        subprocess.run(["sudo", "systemctl", "poweroff"], capture_output=True, timeout=10)

    def close(self) -> None:
        # Radio last: it owns the MPD connection YTM borrows.
        for name in sorted(self.sources, key=lambda n: n == "radio"):
            try:
                self.sources[name].close()
            except Exception:
                log.debug("closing %s failed", name, exc_info=True)


def parse_connected_device(output: str) -> str:
    """``Device AA:BB:… Pixel 11 Pro XL`` -> ``Pixel 11 Pro XL`` (first one)."""
    for line in output.splitlines():
        m = re.match(r"\s*Device\s+(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\s+(.+)$", line)
        if m:
            return m.group(1).strip()
    return ""


def bluetooth_pair_commands(seconds: int) -> list[list[str]]:
    """bluetoothctl invocations, in order, for a pairing window of ``seconds``.

    One command per run: piped into an interactive bluetoothctl, commands
    arrive before it has connected to bluetoothd and are silently lost (seen
    on the bench - "pairable on" never took). The timeout goes first: BlueZ
    applies it when discoverable turns on, so the box stops advertising itself
    on its own rather than staying visible to every phone in the street.
    There is no pairable timeout on this BlueZ; moOde keeps pairable on anyway,
    and its agent asks for confirmation.
    """
    seconds = max(1, int(seconds))
    return [
        ["discoverable-timeout", str(seconds)],
        ["discoverable", "on"],
        ["pairable", "on"],
    ]


def parse_amixer_percent(output: str) -> int | None:
    """First ``[NN%]`` in ``amixer sget`` output - the left channel, in practice."""
    match = re.search(r"\[(\d{1,3})%\]", output)
    return int(match.group(1)) if match else None


def unscale_volume(percent: float, limit: int = 70, floor: int = 0) -> float:
    """Mixer percentage -> knob position. The inverse of ``scale_volume``.

    Anything above the ceiling (set by moOde's own slider, say) reads as a knob
    at maximum rather than beyond it; anything at or under the floor as zero.
    """
    limit = max(1, min(100, int(limit)))
    floor = max(0, min(limit - 1, int(floor)))
    if floor and percent <= floor:
        return 0.0
    return round(max(0.0, min(100.0, (percent - floor) * 100.0 / (limit - floor))), 1)


def scale_volume(value: float, limit: int = 70, floor: int = 0) -> int:
    """Knob position (0-100) -> mixer percentage, capped at ``limit``.

    Protects an irreplaceable 60-year-old cone from an amplifier with several
    times more power than it can take. The knob keeps its full mechanical
    sweep and full resolution - it just tops out somewhere safe, which is
    exactly how it would have behaved originally.
    """
    limit = max(1, min(100, int(limit)))
    floor = max(0, min(limit - 1, int(floor)))
    value = max(0.0, min(100.0, value))
    if value <= 0:
        return 0
    return int(round(floor + value * (limit - floor) / 100.0))


def tone_gain(value: float, span_db: float = 12.0, deadband_db: float = 0.5) -> float:
    """Wheel position (0-100) -> shelf gain in dB.

    Centre is flat, ends are +/-``span_db``. The deadband matters more than it
    looks: a 60-year-old pot parked "at noon" is never exactly at noon, and
    without it the box would sit at a permanent +0.3 dB tilt that you can
    neither hear nor get rid of.
    """
    value = max(0.0, min(100.0, value))
    gain = round((value - 50.0) * (span_db / 50.0), 2)
    return 0.0 if abs(gain) < deadband_db else gain


def treble_db(value: float) -> float:
    """What the tone knob actually does to the treble, in dB (for display).

    ``tone_gain`` is handed to scripts/hasler-tone, which halves boosts (and
    drops the master gain to match) so the bright end can't clip: dark end
    -12 dB, centre flat, bright end +6 dB. Keep the two in step.
    """
    gain = tone_gain(value)
    return round(gain / 2 if gain > 0 else gain, 1)
