"""The daemon. Poll the panel, dispatch what it says, keep the lamp honest.

Runs under systemd with a watchdog. The loop is deliberately dull: everything
interesting lives in the control primitives and the action table, both of which
are tested without hardware.

    haslerd --variant config/variants/hasler-hm.yaml
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

import yaml

from .actions import Actions
from .ha.rest import HomeAssistant
from .hw.gpio_lamp import GpioRgbController
from .hw.powerctl import Lamp, PowerController
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
from .net import Connectivity
from .player.base import FakePlayer
from .player.moode import MoodeConfig, MoodePlayer
from .state import StateStore
from .variant import Panel, load_variant

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

SOURCE_TO_LAMP = {
    "radio": "radio",
    "bluetooth": "bluetooth",
    "airplay": "airplay",
    "spotify": "spotify",
    "ytm": "ytm",
    "dlna": "dlna",
}

# Sources that play happily with no internet; "offline" would be a lie for them.
LOCAL_SOURCES = ("bluetooth",)


def lamp_state(standby: bool, online: bool, source: str, hotspot: bool = False) -> str:
    """What the lamp should say. Pure, so the priority order is testable.

    Standby wins over offline: a knob turned off means a quiet lamp, not a red
    one flashing in the bedroom all night because the router is rebooting.
    The hotspot outranks both: it means "I can't find your Wi-Fi - come and
    tell me a new one", which is the one thing the owner has to act on.
    """
    if hotspot:
        return "hotspot"
    if standby:
        return "standby"
    if not online and source not in LOCAL_SOURCES:
        return "offline"
    return SOURCE_TO_LAMP.get(source, "radio")


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


def build(args) -> tuple[Panel, Actions, Lamp | None, dict, WebServer | None, WebApp]:
    variant = load_variant(args.variant)
    config = {}
    if args.config and Path(args.config).exists():
        config = yaml.safe_load(Path(args.config).read_text()) or {}

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
    web, app = build_web(config, actions, panel, state, ha is not None)
    # Background: YTM presets resolve their opening track now, not on first
    # press. After the web overlay, so it warms the keys as the owner set them.
    player.prefetch(list(actions.presets.values()))
    if lamp is not None:
        actions.on_play_error = lambda _exc: lamp.flash("error", time.monotonic())
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
    config: dict, actions: Actions, panel: Panel, state: StateStore, ha: bool
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


def open_lamp(spec: dict) -> Lamp | None:
    """The pilot lamp from the descriptor's ``io.lamp`` block, or None.

    ``backend: gpio`` drives a bare RGB LED on three Pi pins; ``powerctl`` (the
    default, for descriptors that predate the choice) the HiFiBerry Power
    Controller; ``none`` no lamp at all.
    """
    backend = spec.get("backend", "powerctl")
    if backend == "none":
        return None
    if backend == "gpio":
        controller = GpioRgbController(
            pins=spec.get("pins"), common=spec.get("common", "cathode"), balance=spec.get("balance")
        )
    elif backend == "powerctl":
        controller = PowerController()
    else:
        log.warning("unknown lamp backend %r; no lamp", backend)
        return None
    return Lamp(controller) if controller.available else None


def build_connectivity(config: dict) -> Connectivity:
    net = config.get("connectivity") or {}
    return Connectivity(
        host=net.get("host", "1.1.1.1"),
        port=int(net.get("port", 53)),
        interval_s=float(net.get("interval_s", 10.0)),
        timeout_s=float(net.get("timeout_s", 2.0)),
    )


def boot(panel: Panel, actions: Actions, settle_s: float = BOOT_SETTLE_S) -> bool:
    """Restore what the box was doing; True if the remembered preset is playing.

    Order matters: load state, let the panel settle so a knob that is off at
    power-up gets to say ``off`` (-> standby), and only then resume.
    """
    actions.restore()
    end = time.monotonic() + settle_s
    while time.monotonic() < end:
        actions.dispatch_all(panel.poll(time.monotonic()))
        time.sleep(0.01)
    return actions.resume_last()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="haslerd", description=__doc__.splitlines()[0])
    here = Path(__file__).resolve().parents[2]
    ap.add_argument("--variant", default=str(here / "config/variants/hasler-hm.yaml"))
    ap.add_argument("--config", default=str(here / "config/haslerd.yaml"))
    ap.add_argument("--fake", action="store_true", help="in-memory panel and player")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    panel, actions, lamp, config, web, app = build(args)
    publisher = HaPublisher(app)
    connectivity = build_connectivity(config)
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
    online = True

    try:
        resumed = boot(panel, actions)
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
                resume_attempts = 0 if actions.resume_last() else resume_attempts + 1

            if now >= next_source_poll:
                next_source_poll = now + SOURCE_POLL_S
                try:
                    source = actions.player.active_source()
                except Exception:
                    log.debug("could not read active source", exc_info=True)

            if lamp is not None and now >= next_lamp_tick:
                next_lamp_tick = now + LAMP_TICK_S
                lamp.set_state(lamp_state(actions.standby, online, source, connectivity.hotspot))
                lamp.tick(now)

            publisher.tick(now)
            watchdog.ping(now)
            time.sleep(max(0.0, period - (time.monotonic() - now)))
    finally:
        panel.close()
        if web is not None:
            web.close()
        if lamp is not None:
            try:
                lamp.controller.close()
            except Exception:
                log.debug("lamp close failed", exc_info=True)
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
