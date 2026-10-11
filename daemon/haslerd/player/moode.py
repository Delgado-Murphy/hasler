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

import logging
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from .base import Preset, Source
from .sources.radio import RadioSource

log = logging.getLogger(__name__)

# moOde records active renderers as flags in its sqlite config table. Confirm
# these against the installed version once, in Phase 1. No "spotify" here:
# moOde's own librespot renderer is switched off in favour of go-librespot,
# which the router asks directly (SpotifySource.is_active).
DEFAULT_SOURCE_FLAGS = {
    "bluetooth": "btactive",
    "airplay": "airplayactv",
}

DEFAULT_RENDERER_UNITS = {
    "bluetooth": "bluealsa-aplay",
    "airplay": "shairport-sync",
}

# active_source() is asked by the main loop and by every key press. The answer
# costs an HTTP status call and up to three moodeutl shell-outs, and a human
# cannot tell a one-second-old answer from a fresh one.
SOURCE_CACHE_S = 1.0


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


@dataclass
class MoodeConfig:
    mpd_host: str = "localhost"
    mpd_port: int = 6600
    mixer: str = "Digital"          # from: moodeutl -q "select value from cfg_system where param='amixname'"
    # amixer -c takes an index or the ALSA card id. The id survives HDMI
    # cards shifting the numbering; the Amp4 Pro is "sndrpihifiberry".
    card: int | str = 0
    moodeutl: str = "moodeutl"
    amixer: str = "amixer"
    # Ceiling on the hardware mixer, as a percentage. The Amp4 Pro can deliver
    # roughly 10 W into the Hasler's 16 ohm elliptical; the cone is good for
    # perhaps 1-3 W. Without a ceiling, "knob at maximum" means a cooked voice
    # coil on a part that cannot be bought. Calibrate by ear (see docs), then
    # the knob's full sweep maps into 0..volume_limit.
    volume_limit: int = 70
    bluetoothctl: str = "bluetoothctl"
    source_flags: dict = field(default_factory=lambda: dict(DEFAULT_SOURCE_FLAGS))
    renderer_units: dict = field(default_factory=lambda: dict(DEFAULT_RENDERER_UNITS))
    # Tone wheels. Fill these in during Phase 4 against the real box - e.g. a
    # CamillaDSP helper or moOde's eqctl.php. `{gain}` is dB, `{value}` is the
    # raw 0-100, `{control}` is the wheel id. Empty means the wheels do nothing,
    # which is the correct behaviour before Phase 4.
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


def build_sources(config: MoodeConfig) -> dict[str, Source]:
    """Radio always; Spotify and YTM if their modules import and construct.

    Imported lazily and inside a try so the daemon still plays radio when a
    source module is missing, half-written, or its dependencies are absent -
    the box must never lose its radio because Spotify broke.
    """
    radio = RadioSource(config.mpd_host, config.mpd_port, ytm_port=config.ytm.port)
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
            from .sources.ytm import YtmSource

            sources["ytm"] = YtmSource(
                mpd=radio.mpd,
                port=config.ytm.port,
                ytdlp=config.ytm.ytdlp,
                cookies=config.ytm.cookies,
            )
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
    ) -> None:
        self.config = config or MoodeConfig()
        self.sources: dict[str, Source] = sources if sources is not None else build_sources(self.config)
        self.current_preset: Preset | None = None
        self._clock = clock
        self._source_cache: tuple[float, str] | None = None
        self._muted = False
        self._volume: float | None = None   # mixer percent, post-ceiling
        self._knob: float | None = None     # knob scale 0-100, as last set
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
        """Stop every other source, then start this one.

        ALSA arbitration: the DAC has one hardware device and only one process
        may hold it. Stopping the others *first* is what lets go-librespot and
        MPD hand the device back and forth without "device busy". Raises if the
        source is unknown or its ``play`` raises, so the caller can flash the
        lamp; ``current()`` is only set once play has succeeded.
        """
        source = self.sources.get(preset.source)
        if source is None:
            raise LookupError(f"no {preset.source!r} source available for preset {preset.n}")
        previous = self.current_preset
        cached = self._source_cache[1] if self._source_cache else None
        self.current_preset = None
        self._source_cache = None
        for name, other in self.sources.items():
            if other is not source:
                self._stop_source(name, other)
        try:
            source.play(preset)
        except Exception:
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
        self.current_preset = preset

    def _stop_source(self, name: str, source: Source) -> None:
        try:
            source.stop()
        except Exception:
            log.exception("could not stop %s", name)

    def stop(self) -> None:
        self.current_preset = None
        self._source_cache = None
        for name, source in self.sources.items():
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

    def set_volume(self, value: float) -> None:
        percent = scale_volume(value, self.config.volume_limit)
        self._amixer(f"{percent}%")
        self._volume = float(percent)
        self._knob = max(0.0, min(100.0, float(value)))
        if self._muted and percent > 0:
            self._muted = False

    def get_volume(self) -> float | None:
        """Current volume on the knob scale, read back from the mixer.

        Read rather than remembered, because the moOde UI, HA and a phone over
        AirPlay all move the same hardware mixer behind our back. Falls back to
        the last value we set if amixer cannot be read.
        """
        try:
            result = subprocess.run(
                [self.config.amixer, "-c", str(self.config.card), "sget", self.config.mixer],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            percent = parse_amixer_percent(result.stdout)
        except Exception:
            log.debug("could not read mixer volume", exc_info=True)
            percent = None
        if percent is None:
            return self._knob
        return unscale_volume(percent, self.config.volume_limit)

    def set_mute(self, muted: bool) -> None:
        # Prefer the mixer's own mute switch; fall back to volume 0 on mixers
        # that have no switch (some DACs expose only a volume control).
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
                if source.is_active():
                    return name
            except Exception:
                log.debug("%s is_active failed", name, exc_info=True)
        for source, flag in self.config.source_flags.items():
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
        if source == "bluetooth" and shutil.which(self.config.bluetoothctl):
            subprocess.run(
                [self.config.bluetoothctl, "disconnect"],
                capture_output=True,
                timeout=5,
            )

    def bluetooth_pair(self, seconds: int) -> None:
        if not shutil.which(self.config.bluetoothctl):
            log.warning("bluetoothctl not found; cannot enter pairing mode")
            return
        subprocess.run(
            [self.config.bluetoothctl],
            input=bluetooth_pair_script(seconds),
            text=True,
            capture_output=True,
            timeout=10,
        )
        log.info("bluetooth discoverable for %ss", seconds)

    # -- misc -------------------------------------------------------------

    def route_speaker(self, position: int) -> None:
        # Excelsior only - DPDT relay on GPIO 24 plus a CamillaDSP profile swap.
        log.info("speaker route -> position %s (relay not fitted on this cabinet)", position)

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


def bluetooth_pair_script(seconds: int) -> str:
    """bluetoothctl commands for a pairing window of ``seconds``.

    The timeouts go first: BlueZ applies them when discoverable/pairable turn
    on, so the box stops advertising itself on its own rather than staying
    visible to every phone in the street until the next reboot.
    """
    seconds = max(1, int(seconds))
    return (
        f"discoverable-timeout {seconds}\n"
        f"pairable-timeout {seconds}\n"
        "discoverable on\n"
        "pairable on\n"
    )


def parse_amixer_percent(output: str) -> int | None:
    """First ``[NN%]`` in ``amixer sget`` output - the left channel, in practice."""
    match = re.search(r"\[(\d{1,3})%\]", output)
    return int(match.group(1)) if match else None


def unscale_volume(percent: float, limit: int = 70) -> float:
    """Mixer percentage -> knob position. The inverse of ``scale_volume``.

    Anything above the ceiling (set by moOde's own slider, say) reads as a knob
    at maximum rather than beyond it.
    """
    limit = max(1, min(100, int(limit)))
    return round(max(0.0, min(100.0, percent * 100.0 / limit)), 1)


def scale_volume(value: float, limit: int = 70) -> int:
    """Knob position (0-100) -> mixer percentage, capped at ``limit``.

    Protects an irreplaceable 60-year-old cone from an amplifier with several
    times more power than it can take. The knob keeps its full mechanical
    sweep and full resolution - it just tops out somewhere safe, which is
    exactly how it would have behaved originally.
    """
    limit = max(1, min(100, int(limit)))
    return int(round(max(0.0, min(100.0, value)) * limit / 100.0))


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
