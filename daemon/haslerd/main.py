"""The daemon. Poll the panel, dispatch what it says, keep the lamp honest.

Runs under systemd with a watchdog. The loop is deliberately dull: everything
interesting lives in the control primitives and the action table, both of which
are tested without hardware.

    haslerd                       # /etc/hasler/box.yaml names the variant
    haslerd --variant hasler-hm   # before box.yaml exists (the bench box)

Configuration (docs/ota.md section 1): with /etc/hasler/box.yaml present the
variant comes from its ``variant:`` and haslerd's settings are the code
defaults with box.yaml's override blocks on top; the descriptor gets this
box's calibration overlay (/etc/hasler/variant.d). Without box.yaml the old
``--variant`` / ``--config`` files are read exactly as before (overlay still
applied if one exists), so an unmigrated box is unaffected.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from . import boxconfig
from .actions import Actions
from .ha.rest import HomeAssistant
from .lamp.contract import BoxState, LampDriver, Look
from .lamp.policy import look_for
from .web import stations
from .audio import DEFAULT as MONO_DEFAULT, MonoError, MonoSwitch
from .web.server import SETTINGS_FILE, WebApp, WebServer, load_settings, volume_limit_from

try:    # Home Assistant's page API; optional so the radio runs without it.
    from .web.ha_api import HaApi
except ImportError:
    HaApi = None

try:    # The curated Browse library; optional likewise.
    from .web.catalog import Catalog
except ImportError:
    Catalog = None
try:    # The pilot lamp's engine; without it the radio runs dark.
    from .lamp.engine import open_lamp
except ImportError:
    def open_lamp(spec: dict) -> LampDriver | None:
        log.warning("lamp engine unavailable; no lamp")
        return None

try:    # Sunset..sunrise (and the page's fixed-hours mode) for the lamp.
    from .lamp.night import night_mode_active
except ImportError:
    night_mode_active = None
from .net import Connectivity, tcp_probe
from .player.base import FakePlayer, Preset
from .player.moode import MoodeConfig, MoodePlayer
from .state import StateStore
from .variant import Panel, Variant, load_variant

log = logging.getLogger("haslerd")

POLL_HZ = 50.0
SOURCE_POLL_S = 2.0     # active_source() shells out; no need to ask often
LAMP_TICK_S = 0.05
HA_PUBLISH_S = 5.0      # status to Home Assistant at most this often (HaApi throttles too)
# Long enough for the power switch's debounce to report a knob that is off at
# boot (EndSwitch assumes "on"), so we never blip a station before going quiet.
BOOT_SETTLE_S = 0.15
RESUME_RETRY_S = 10.0
RESUME_ATTEMPTS = 6

# The lamp's clocks (docs/lamp.md).
LOADING_POLL_S = 0.5    # "has the preset made a sound yet?", while loading
LOADING_FAIL_S = 20.0   # no sound this long after a start: red pulses
CAST_GRACE_S = 3.0      # blue stays this long after a phone lets go
NIGHT_CHECK_S = 60.0    # sunset is not a thing to poll at 20 Hz
BOOT_MAX_S = 45.0       # warm-up breath gives way to ready by then at the latest


def preset_sounding(player, preset: Preset) -> bool:
    """Has ``preset`` started making sound? Our loading breath's end.

    The preset's source must be what the player reports as active (MPD
    playing our URL for radio/ytm, go-librespot playing for spotify). For the
    MPD sources that is not enough - MPD says "play" the moment it is told to,
    while it is still connecting and buffering - so MPD's ``elapsed`` must
    have moved too. Never raises: unsure reads as "not yet".
    """
    try:
        if player.current() != preset or player.active_source() != preset.source:
            return False
    except Exception:
        return False
    radio = (getattr(player, "sources", None) or {}).get("radio")
    mpd = getattr(radio, "mpd", None)
    if preset.source not in ("radio", "ytm") or not callable(mpd):
        return True
    try:
        status = mpd().status()
        return status.get("state") == "play" and float(status.get("elapsed") or 0) > 0
    except Exception:
        log.debug("MPD status for the loading lamp failed", exc_info=True)
        return False


def power_switch(panel: Panel, actions: Actions):
    """The end switch whose long off shuts the box down (for the countdown)."""
    for control in getattr(panel, "controls", ()):
        if getattr(control, "kind", None) != "end_switch":
            continue
        spec = actions.variant.actions.get(f"{control.id}.off_held") or {}
        if spec.get("do") == "shutdown":
            return control
    return None


class LampKeeper:
    """Gathers a BoxState each tick and keeps the lamp engine told.

    The engine draws on its own thread, so nothing here waits on it; this side
    only calls ``show`` when the Look changes, ``set_night`` when night flips
    and ``countdown`` while the power knob is held off. Actions' hooks land
    here too, so a slow preset start or a shutdown is already showing before
    the blocking call that follows.
    """

    def __init__(
        self,
        driver: LampDriver,
        actions: Actions,
        panel: Panel | None = None,
        connectivity: Connectivity | None = None,
        settings_file: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = datetime.now,
        night: Callable[[datetime, dict], bool] | None = None,
        sounding: Callable[[object, Preset], bool] = preset_sounding,
    ) -> None:
        self.driver = driver
        self.actions = actions
        self.panel = panel
        self.connectivity = connectivity
        self.settings_file = settings_file
        self.clock = clock
        self.wall = wall
        self.night_fn = night if night is not None else night_mode_active
        self.sounding = sounding
        self.state = BoxState(boot="daemon")
        self.source = "radio"
        self.look: Look | None = None
        self._started = clock()
        self._net_seen = False
        self._resume_done = False
        self._offline_since: float | None = None
        self._loading: Preset | None = None
        self._loading_since = 0.0
        # Started, then silent for LOADING_FAIL_S: not "playing" whatever the
        # player claims (MPD says "play" while it fails to connect).
        self._silent: Preset | None = None
        self._next_loading_poll = 0.0
        self._cast_seen: float | None = None
        self._next_source_poll = 0.0
        self._next_night = 0.0
        self._counting = False
        self._settings: dict | None = None    # the page's last saved doc, if any
        # A knob that reads off at power-up never counts down: only an off
        # after boot() (see arm) is a hand asking for shutdown.
        self._armed_at = float("inf")
        self._switch = power_switch(panel, actions) if panel is not None else None
        actions.on_reply = self.reply
        actions.on_start = self.loading_started
        actions.on_play_error = self.play_failed
        actions.on_change = self.refresh

    # -- hooks from Actions -----------------------------------------------

    def reply(self, name: str) -> None:
        try:
            self.driver.reply(name)
        except Exception:
            log.debug("lamp reply failed", exc_info=True)

    def loading_started(self, preset: Preset) -> None:
        now = self.clock()
        self._loading = preset
        self._loading_since = now
        self._silent = None
        # Ours now: no blue grace for the phone we just took the audio from.
        self._cast_seen = None
        self.source = preset.source
        self._next_loading_poll = now + LOADING_POLL_S
        self.refresh()

    def play_failed(self, _exc: Exception) -> None:
        self._loading = None
        self.refresh()

    def arm(self) -> None:
        """Boot has settled: from now on a knob turned off counts down."""
        self._armed_at = self.clock()

    def settings_changed(self, doc: dict) -> None:
        """WebApp.lamp_listeners: location or night mode changed - look again now.

        Runs on a web thread, so it only leaves the doc and moves the next
        night check to the next tick; the main loop does the rest.
        """
        self._settings = dict(doc)
        self._next_night = 0.0

    def boot_done(self) -> None:
        """The resume finished (or a human took over): warm-up is over."""
        self._resume_done = True

    # -- the tick ---------------------------------------------------------

    def tick(self, now: float) -> None:
        """Called from the main loop every LAMP_TICK_S. Never raises."""
        try:
            self._poll(now)
            self._countdown(now)
            self.refresh(now)
        except Exception:
            log.exception("lamp tick failed")

    def _poll(self, now: float) -> None:
        player = self.actions.player
        if now >= self._next_source_poll:
            self._next_source_poll = now + SOURCE_POLL_S
            try:
                self.source = player.active_source()
            except Exception:
                log.debug("could not read active source", exc_info=True)
        if self._loading is not None and now >= self._next_loading_poll:
            self._next_loading_poll = now + LOADING_POLL_S
            if self.sounding(player, self._loading):
                self.source = self._loading.source
                self._loading = None
            elif now - self._loading_since >= LOADING_FAIL_S:
                log.warning("preset %s made no sound in %.0f s", self._loading.n, LOADING_FAIL_S)
                self._silent, self._loading = self._loading, None
                self.reply("fail")
        conn = self.connectivity
        if conn is not None:
            if conn.online:
                self._offline_since = None
            elif self._offline_since is None:
                self._offline_since = now
        if now >= self._next_night:
            self._next_night = now + NIGHT_CHECK_S
            night = self._night()
            if night != self.state.night:
                log.info("lamp: %s", "night" if night else "day")
                self.state.night = night
                try:
                    self.driver.set_night(night)
                except Exception:
                    log.debug("lamp set_night failed", exc_info=True)

    def _night(self) -> bool:
        if self.night_fn is None:
            return False
        try:
            settings = self._settings
            if settings is None or self.settings_file is not None:
                settings = load_settings(self.settings_file)
            return bool(self.night_fn(self.wall(), settings))
        except Exception:
            log.debug("night check failed", exc_info=True)
            return self.state.night

    def _countdown(self, now: float) -> None:
        sw = self._switch
        progress = None
        if sw is not None and not self.actions.shutting_down and not sw.state():
            off_at = getattr(sw, "_off_at", 0.0)
            if off_at >= self._armed_at and not getattr(sw, "_off_held_fired", False):
                progress = max(0.0, min(1.0, (now - off_at) / max(sw.off_hold_s, 1e-3)))
        if progress is None and not self._counting:
            return
        self._counting = progress is not None
        try:
            self.driver.countdown(progress)
        except Exception:
            log.debug("lamp countdown failed", exc_info=True)

    def boot_stage(self, now: float) -> str:
        conn = self.connectivity
        if not self._net_seen and conn is not None and conn.online:
            self._net_seen = bool(getattr(conn, "seen_up", True))
        if self._resume_done and self._net_seen or now - self._started >= BOOT_MAX_S:
            return "done"
        return "network" if self._net_seen else "daemon"

    def gather(self, now: float) -> BoxState:
        """The BoxState as of ``now`` (timers are this object's, flags Actions')."""
        actions, player, state = self.actions, self.actions.player, self.state
        phone = actions.phone_has_it(self.source)
        if phone:
            self._cast_seen = now
        casting = self._cast_seen is not None and now - self._cast_seen < CAST_GRACE_S
        paused = False
        cast_paused = getattr(player, "cast_paused", None)
        if casting and callable(cast_paused):
            try:
                paused = bool(cast_paused())
            except Exception:
                paused = False
        try:
            current = player.current()
        except Exception:
            current = None
        conn = self.connectivity
        state.boot = self.boot_stage(now)
        state.standby = actions.standby
        state.shutting_down = actions.shutting_down
        state.hotspot = bool(conn is not None and conn.hotspot)
        state.pairing = actions.pairing
        state.casting = casting
        state.cast_paused = paused
        state.offline_s = 0.0 if self._offline_since is None else now - self._offline_since
        state.loading = self._loading is not None
        state.playing = (
            not state.loading and current is not None and self.source == current.source
            and current != self._silent
        )
        state.source = current.source if current is not None else self.source
        state.panel_missing = bool(self.panel is not None and getattr(self.panel, "absent", False))
        return state

    def refresh(self, now: float | None = None) -> Look:
        """Recompute the Look; tell the engine only if it changed."""
        now = self.clock() if now is None else now
        look, fade = look_for(self.gather(now))
        if look != self.look:
            log.debug("lamp %s -> %s (%.1f s)", self.look, look, fade)
            self.look = look
            try:
                self.driver.show(look, fade)
            except Exception:
                log.debug("lamp show failed", exc_info=True)
        return look

    def close(self) -> None:
        # Shutting down: leave the ember on through the halt - the
        # system-shutdown hook darkens it last, so dark means safe to unplug.
        # Any other exit (a restart, a crash) goes dark at once.
        try:
            self.driver.close(halt_dark=not self.actions.shutting_down)
        except Exception:
            log.debug("lamp close failed", exc_info=True)


class Watchdog:
    """systemd sd_notify, without pulling in a dependency for six lines."""

    def __init__(self) -> None:
        self.address = os.environ.get("NOTIFY_SOCKET")
        interval = int(os.environ.get("WATCHDOG_USEC", "0") or 0)
        self.period = (interval / 1_000_000) / 2 if interval else None
        self._last = 0.0
        self._sock = None
        if self.address:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def _send(self, message: str) -> None:
        if not self._sock or not self.address:
            return
        path = "\0" + self.address[1:] if self.address.startswith("@") else self.address
        try:
            self._sock.sendto(message.encode(), path)
        except OSError:
            pass

    def ready(self) -> None:
        self._send("READY=1")

    def ping(self, now: float) -> None:
        if self.period and now - self._last >= self.period:
            self._last = now
            self._send("WATCHDOG=1")


def moode_config(config: dict, settings: dict) -> MoodeConfig:
    """haslerd.yaml's ``moode:`` block, with the web page's saved settings on top.

    A bad saved ``volume_limit`` is ignored (logged), never fatal: the yaml's
    own ceiling then stands.
    """
    spec = dict(config.get("moode") or {})
    if "volume_limit" in settings:
        try:
            spec["volume_limit"] = volume_limit_from(settings["volume_limit"])
        except ValueError as exc:
            log.warning("ignoring saved volume_limit %r: %s", settings["volume_limit"], exc)
    return MoodeConfig(**spec)


DEFAULT_VARIANT = "hasler-hm"


def box_paths(args) -> boxconfig.Paths:
    """Paths from the environment (HASLER_ETC/STATE/RELEASE), then --etc/--state."""
    paths = boxconfig.Paths.from_env()
    if getattr(args, "etc", None):
        paths = dataclasses.replace(paths, etc=Path(args.etc))
    if getattr(args, "state", None):
        paths = dataclasses.replace(paths, state=Path(args.state))
    return paths


def variant_file(arg: str | None, paths: boxconfig.Paths) -> Path:
    """``--variant``: a descriptor path, or a bare name the release ships."""
    if not arg:
        return paths.shipped_variant(DEFAULT_VARIANT)
    if "/" not in arg and not arg.endswith((".yaml", ".yml")):
        return paths.shipped_variant(arg)
    return Path(arg)


def load_config(args, paths: boxconfig.Paths) -> tuple[Variant, dict]:
    """The effective variant and haslerd's settings (see the module docstring)."""
    box = boxconfig.load_box_config(paths)
    if box:
        name = box.get("variant") or variant_file(args.variant, paths).stem
        variant = boxconfig.load_effective_variant(paths, name)
        config = boxconfig.haslerd_settings(box)
        if not args.fake:
            config.setdefault("state_file", str(paths.state / "state.json"))
            try:     # secrets go here from now on (boxconfig.secret_file)
                paths.secrets.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError:
                log.warning("cannot create %s; secrets stay beside the state", paths.secrets)
        log.info("config: %s, variant %s%s", paths.box_file, name,
                 f" + {variant.overlay_path}" if variant.overlay_path else "")
        return variant, config
    variant = boxconfig.overlay_variant(paths, load_variant(variant_file(args.variant, paths)))
    config = {}
    legacy = Path(args.config) if args.config else paths.release / "config" / "haslerd.yaml"
    if legacy.exists():
        config = yaml.safe_load(legacy.read_text()) or {}
    if getattr(args, "state", None) and not args.fake:
        config.setdefault("state_file", str(paths.state / "state.json"))
    log.info("config: %s (no %s yet), variant %s", legacy, paths.box_file, variant.path)
    return variant, config


def apply_overlay(
    panel: Panel | None, paths: boxconfig.Paths, variant_name: str, actions: Actions | None = None
) -> Variant:
    """Re-read this box's calibration overlay and apply it to the running panel.

    The knob page calls this (as ``app.reload_variant()``) after
    ``boxconfig.write_overlay``: pots pick up their new curve / range /
    hysteresis at the next poll, without a restart (``Panel.apply_specs``).
    With ``actions`` its variant's controls and io follow too; its key table
    and presets (the key page's) are left alone. Raises ValueError for an
    overlay that doesn't fit the descriptor - nothing is changed then. io
    changes (MCP address, lamp palette) still need a restart.
    """
    variant = boxconfig.load_effective_variant(paths, variant_name)
    if panel is not None:
        changed = panel.apply_specs(variant.controls)
        if changed:
            log.info("calibration applied live: %s", ", ".join(changed))
    if actions is not None:
        actions.variant.controls = variant.controls
        actions.variant.io = variant.io
        actions.variant.overlay_path = variant.overlay_path
    return variant


def build(args) -> tuple[Panel, Actions, LampDriver | None, dict, WebServer | None, WebApp]:
    paths = box_paths(args)
    variant, config = load_config(args, paths)

    panel = Panel.from_variant(variant, fake=args.fake)

    if args.fake:
        # In-memory unless a state file is asked for: a dry run on a laptop
        # should not quietly overwrite what the real box remembers.
        state = StateStore.at(config["state_file"]) if config.get("state_file") else StateStore(None)
    else:
        state = StateStore.at(config.get("state_file"))
    # The state directory also keeps the YouTube Music sign-in, the web
    # page's settings.json and Home Assistant's pairing.
    state_dir = Path(state.path).parent if state.path is not None else None
    settings = load_settings(state_dir / SETTINGS_FILE if state_dir is not None else None)
    if args.fake:
        player = FakePlayer()
        lamp = None
    else:
        player = MoodePlayer(moode_config(config, settings), state_dir=state_dir)
        lamp = open_lamp(variant.io.get("lamp") or {})
    log.info("state file: %s", state.path or "(in memory)")

    # With HaApi present, its load() (in build_web) owns the one Home
    # Assistant client - from its saved pairing, else this same yaml block.
    ha = None if HaApi is not None else yaml_ha(config)

    actions = Actions(variant, player, ha=ha, state=state)
    web, app = build_web(config, actions, panel, state, ha is not None, paths=paths)
    # Background: YTM presets resolve their opening track now, not on first
    # press. After the web overlay, so it warms the keys as the owner set them.
    player.prefetch(list(actions.presets.values()))
    return panel, actions, lamp, config, web, app


def yaml_ha(config: dict) -> HomeAssistant | None:
    """The Home Assistant client haslerd.yaml describes, or None."""
    ha_config = config.get("home_assistant") or {}
    if ha_config.get("url") and ha_config.get("token"):
        return HomeAssistant(ha_config["url"], ha_config["token"])
    log.info("no Home Assistant transport configured; hold actions are inert")
    return None


def build_browse(actions: Actions, state_dir: Path | None) -> object | None:
    """The curated Browse library, or None if its module or data won't load."""
    if Catalog is None:
        return None
    ytm = (getattr(actions.player, "sources", None) or {}).get("ytm")
    try:
        return Catalog(
            ytm_catalog=getattr(ytm, "catalog", None),
            radio_search=lambda q: stations.search(q),
            data_dir=state_dir,
        )
    except Exception:
        log.exception("browse catalogue unavailable")
        return None


def build_ha_api(actions: Actions, state_dir: Path | None, config: dict) -> object | None:
    """Home Assistant's page API (pairing, status publishing), or None."""
    if HaApi is None:
        return None
    try:
        ha_api = HaApi(actions, state_dir, config.get("home_assistant"))
        ha_api.load()
    except Exception:
        log.exception("Home Assistant API unavailable")
        return None
    return ha_api


def build_mono(state_dir: Path | None) -> MonoSwitch | None:
    """The mono switch, re-applied from settings.json (default on) off-thread.

    Re-applying at every start also refreshes the override's slave from
    moOde's current output device. Absent helper (a laptop, --fake): None.
    """
    mono = MonoSwitch()
    if not mono.available:
        return None
    settings = load_settings(state_dir / SETTINGS_FILE if state_dir is not None else None)
    want = settings.get("mono", MONO_DEFAULT)
    want = want if isinstance(want, bool) else MONO_DEFAULT

    def apply() -> None:
        try:
            mono.set(want, reopen=False)
        except MonoError:
            log.exception("could not apply the saved mono setting")

    threading.Thread(target=apply, name="mono", daemon=True).start()
    return mono


def build_web(
    config: dict, actions: Actions, panel: Panel, state: StateStore, ha: bool,
    paths: boxconfig.Paths | None = None,
) -> tuple[WebServer | None, WebApp]:
    """The key page, with the owner's saved layout laid over the descriptor.

    ``keys.yaml`` lives next to state.json; ``web: {enabled: false}`` turns the
    page off but still honours a saved layout (and Home Assistant still gets
    its status). Returns (server or None, the app) - the main loop publishes
    ``app.status()`` to Home Assistant through ``app.ha_api``.
    """
    spec = config.get("web") or {}
    if config.get("keys_file"):
        keys_file: Path | None = Path(config["keys_file"])
    elif state.path is not None:
        keys_file = Path(state.path).parent / "keys.yaml"
    else:
        keys_file = None
    sources = getattr(actions.player, "sources", None)
    available = {name: (sources is None or name in sources) for name in ("radio", "spotify", "ytm")}
    state_dir = Path(state.path).parent if state.path is not None else None
    ha_api = build_ha_api(actions, state_dir, config)
    if ha_api is None and HaApi is not None and actions.ha is None:
        # HaApi is installed but broke: fall back to the yaml's client.
        actions.ha = yaml_ha(config)
        ha = actions.ha is not None
    ha_flag = (lambda: bool(ha_api.connected)) if ha_api is not None else ha
    app = WebApp(
        actions, panel, keys_file, available, ha_flag,
        settings_file=state_dir / SETTINGS_FILE if state_dir is not None else None,
        browse=build_browse(actions, state_dir),
        mono=build_mono(state_dir),
    )
    app.ha_api = ha_api
    # For the knob page: where the overlay lives, and how to apply a new one.
    app.box_paths = paths
    app.reload_variant = (
        (lambda: apply_overlay(panel, paths, actions.variant.name, actions=actions))
        if paths is not None else None
    )
    if ha_api is not None:
        # clear-missing rewrites keys.yaml: same file, same lock as the page
        ha_api.keys_file, ha_api.panel, ha_api.layout_lock = keys_file, panel, app.lock
        app.extra_routes.append(ha_api)
    app.load_saved()
    if not spec.get("enabled", True):
        return None, app
    server = WebServer(app, host=spec.get("host", "0.0.0.0"), port=int(spec.get("port", 8080)))
    try:
        server.start()
    except OSError:
        log.exception("key page could not listen on port %s; carrying on without it", server.port)
        return None, app
    return server, app


class HaPublisher:
    """Pushes ``app.status()`` to Home Assistant, off the panel loop.

    ``status()`` shells out (amixer, moOde) and HA may be slow, so each
    publish runs on a short-lived thread; a tick while one is still running is
    skipped rather than queued. Never raises into the loop.
    """

    def __init__(self, app: WebApp | None, period_s: float = HA_PUBLISH_S) -> None:
        self.ha_api = getattr(app, "ha_api", None)
        self.app = app
        self.period_s = period_s
        self._next = 0.0
        self._thread: threading.Thread | None = None

    def tick(self, now: float) -> None:
        if self.ha_api is None or now < self._next:
            return
        self._next = now + self.period_s
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._publish, name="ha-publish", daemon=True)
        self._thread.start()

    def _publish(self) -> None:
        try:
            self.ha_api.publish(self.app.status())
        except Exception:
            log.debug("Home Assistant publish failed", exc_info=True)


def build_connectivity(config: dict) -> Connectivity:
    """The internet probe; ``seen_up`` turns True at the first probe that answers.

    ``online`` starts optimistic, so the lamp's boot warm-up needs this to know
    the network is really there.
    """
    net = config.get("connectivity") or {}
    host, port = net.get("host", "1.1.1.1"), int(net.get("port", 53))
    timeout_s = float(net.get("timeout_s", 2.0))

    def probe() -> bool:
        up = tcp_probe(host, port, timeout_s)
        if up:
            conn.seen_up = True
        return up

    conn = Connectivity(
        host=host, port=port, interval_s=float(net.get("interval_s", 10.0)),
        timeout_s=timeout_s, probe=probe,
    )
    conn.seen_up = False
    return conn


def boot(panel: Panel, actions: Actions, settle_s: float = BOOT_SETTLE_S, quiet: bool = False) -> bool:
    """Restore what the box was doing; True if the remembered preset is playing.

    Order matters: load state, let the panel settle so a knob that is off at
    power-up gets to say ``off`` (-> standby), and only then resume.
    """
    actions.restore()
    end = time.monotonic() + settle_s
    while time.monotonic() < end:
        actions.dispatch_all(panel.poll(time.monotonic()))
        time.sleep(0.01)
    return actions.resume_last(quiet=quiet)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="haslerd", description=__doc__.splitlines()[0])
    ap.add_argument("--variant", help="descriptor path or shipped name (ignored once box.yaml names one)")
    ap.add_argument("--config", help="legacy haslerd.yaml, read only while there is no box.yaml "
                    "(default: config/haslerd.yaml in the release)")
    ap.add_argument("--etc", help="box config directory (default /etc/hasler, or $HASLER_ETC)")
    ap.add_argument("--state", help="state directory (default /var/lib/haslerd, or $HASLER_STATE)")
    ap.add_argument("--fake", action="store_true", help="in-memory panel and player")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    panel, actions, driver, config, web, app = build(args)
    publisher = HaPublisher(app)
    connectivity = build_connectivity(config)
    keeper = None
    if driver is not None:
        keeper = LampKeeper(driver, actions, panel, connectivity, getattr(app, "settings_file", None))
        listeners = getattr(app, "lamp_listeners", None)
        if listeners is not None:
            listeners.append(keeper.settings_changed)
    log.info("haslerd up: %s, %d controls", actions.variant.name, len(panel.controls))

    running = True

    def stop(signum, _frame):
        nonlocal running
        log.info("signal %s - shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    watchdog = Watchdog()
    # Ready before resuming: starting a playlist can take seconds (yt-dlp),
    # and systemd should not count that against the start timeout.
    watchdog.ready()

    period = 1.0 / POLL_HZ
    next_source_poll = 0.0
    next_lamp_tick = 0.0
    source = "radio"

    try:
        if keeper is not None:
            keeper.tick(time.monotonic())     # warm-up breath before anything blocks
        # Quiet: a failed first resume is retried below; the red pulses wait
        # for the last attempt.
        resumed = boot(panel, actions, quiet=True)
        if keeper is not None:
            keeper.arm()
        # A box that boots before its Wi-Fi fails to resume; retry for a
        # minute or so, until it works or someone presses a key.
        resume_attempts = 0 if resumed else 1
        next_resume = time.monotonic() + RESUME_RETRY_S

        while running:
            now = time.monotonic()

            events = panel.poll(now)
            if events:
                resume_attempts = RESUME_ATTEMPTS   # the human is in charge now
            actions.dispatch_all(events)

            online = connectivity.poll(now)

            if 0 < resume_attempts < RESUME_ATTEMPTS and online and now >= next_resume:
                next_resume = now + RESUME_RETRY_S
                last = resume_attempts == RESUME_ATTEMPTS - 1
                resume_attempts = 0 if actions.resume_last(quiet=not last) else resume_attempts + 1
            if keeper is not None and not 0 < resume_attempts < RESUME_ATTEMPTS:
                keeper.boot_done()

            if keeper is not None:
                source = keeper.source      # the lamp polls it, on its own clock
            elif now >= next_source_poll:
                next_source_poll = now + SOURCE_POLL_S
                try:
                    source = actions.player.active_source()
                except Exception:
                    log.debug("could not read active source", exc_info=True)
            if (actions.standby or actions.pairing) and actions.phone_has_it(source):
                # The phone wakes the radio: standby ends, nothing of ours
                # resumes. A phone that connected also ends the pairing breath.
                actions.wake_for_phone(source)
                actions.end_pairing()

            if keeper is not None and now >= next_lamp_tick:
                next_lamp_tick = now + LAMP_TICK_S
                keeper.tick(now)

            publisher.tick(now)
            watchdog.ping(now)
            time.sleep(max(0.0, period - (time.monotonic() - now)))
    finally:
        panel.close()
        if web is not None:
            web.close()
        if keeper is not None:
            keeper.close()
        close = getattr(actions.player, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log.debug("player close failed", exc_info=True)
        log.info("haslerd down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
