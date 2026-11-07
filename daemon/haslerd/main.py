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
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import yaml

from . import boxconfig
from . import presets as starter_sets
from .actions import RENDERERS, SHUTDOWN_PREPARE_S, Actions
from .bootresume import (AMP_CARD, RESUME_WINDOW_S, BootResume, FirstSound, night_hold, note_power_cut,
                         shutdown_kind)
from .ha.rest import HomeAssistant
from .player.bluetooth import Bluetooth
from .player.btvolume import PhoneVolume
from .lamp import faults as lamp_faults
from .lamp import handover, policy
from .lamp.contract import BoxState, LampDriver, Look
from .lamp import meanings as lamp_meanings
from .lamp import policy as lamp_policy
from .lamp.outputs import remember_period
from .lamp.policy import look_for, reply_for
from .web import lamp_api
from .web import stations
from .audio import MonoError, MonoSwitch, mono_default
from .web.server import (SETTINGS_FILE, WebApp, WebServer, load_settings, volume_floor_from, volume_limit_from,
                         volume_limit_range)
from .web.bluetooth_api import BluetoothApi
from .web.front import WebFront
from .web.bluetooth_api import apply_saved as apply_bluetooth
from .web.sources_api import SourcesApi
from .web.sleep_api import SleepApi
from .sleep import SleepTimer
from .web.checkin_api import CheckinApi
from .web.help_api import MAKERS_TAILNET, HelpApi
from .web.notes_api import NotesApi
from .web.support_api import SupportApi
from .web.report import ReportApi
from .web.spotify_account_api import SpotifyAccountApi
from .web.stations_api import StationsApi
from .web.sources_api import apply_saved as apply_sources
from .dsp.knobs import KnobFunctions, helper_runner
from .web.knobs_api import KnobFunctionsApi

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

try:    # Multiroom between Haslers (multiroom/group.py); optional likewise.
    from .multiroom.group import Group
    from .web.group_api import GroupApi
except ImportError:
    Group = GroupApi = None

try:    # Its audio, Snapcast (multiroom/audio.py): without it multiroom can't be switched on.
    from .multiroom.audio import SnapAudio
except ImportError:
    SnapAudio = None

try:    # Sunset..sunrise (and the page's fixed-hours mode) for the lamp.
    from .lamp.night import night_mode_active
except ImportError:
    night_mode_active = None
from .net import Connectivity, tcp_probe
from .wifiwatch import HotspotWatchdog
from .ytdlp_lane import YtdlpLane
from .player.base import FakePlayer, Preset
from .player.moode import SOURCE_MPD_S, MoodeConfig, MoodePlayer
from .player.sources.radio import mpd_patience
from .player.watchdog import EVENTS_FILE, StreamEvents, StreamWatchdog
from .state import StateStore
from .variant import Panel, Variant, load_variant

log = logging.getLogger("haslerd")

POLL_HZ = 50.0
SOURCE_POLL_S = 2.0     # active_source() asks go-librespot and moOde; no need to ask often
# ... but a phone casting should turn the lamp blue at once: moOde's renderer
# flags (one read-only sqlite query, ~2 ms) are watched this often, and a
# change asks active_source() afresh (LampKeeper._poll).
RENDERER_POLL_S = 0.25
LAMP_TICK_S = 0.05
# The main loop stops telling the engine for this long (a hang): the engine
# falls back to its busy ember breath (engine.watchdog), never a frozen frame.
LAMP_WATCHDOG_S = 3.0
HA_PUBLISH_S = 5.0      # status to Home Assistant at most this often (HaApi throttles too)
# Long enough for the power switch's debounce to report a knob that is off at
# boot (EndSwitch assumes "on"), so we never blip a station before going quiet.
BOOT_SETTLE_S = 0.15

# The lamp's clocks (docs/lamp.md).
LOADING_POLL_S = 0.5    # "has the preset made a sound yet?", while loading
LOADING_FAIL_S = 20.0   # no sound this long after a start: red pulses
CAST_GRACE_S = 3.0      # blue stays this long after a phone lets go
KEY_COUNTDOWN_AFTER_S = 0.5     # a shutdown key (Excelsior key 0) held this long starts the countdown
UPDATE_OK_S = 12.0      # the green bloom and Morse OK after an update or setup: one go, then gone
NIGHT_CHECK_S = 60.0    # sunset is not a thing to poll at 20 Hz
UPDATE_POLL_S = 5.0     # "is an update being installed?" (one small file read)
BOOT_MAX_S = 45.0       # warm-up breath gives way to ready by then at the latest...
BOOT_RESUME_MAX_S = RESUME_WINDOW_S + 5.0   # ...unless a boot resume is still on its way
# Restarted onto a Wi-Fi just chosen (web/wifi.py's wifi-joined note): busy while
# it connects, at most this long - by then it is online (the green OK) or in setup
# mode (the HAL eye, then CQ); the usual rules after that.
JOIN_MAX_S = 300.0


def preset_sounding(player, preset: Preset) -> bool:
    """Has ``preset`` started making sound? Our loading breath's end.

    The preset's source must be what the player reports as active (MPD
    playing our URL for radio/ytm, go-librespot playing for spotify). For the
    MPD sources that is not enough - MPD says "play" the moment it is told to,
    while it is still connecting and buffering - so MPD's ``elapsed`` must
    have moved too. Never raises: unsure reads as "not yet".
    """
    try:
        if getattr(player, "starting", None) is not None:
            return False        # still being started: MPD may be busy, don't wait on it
        if player.current() != preset or player.active_source() != preset.source:
            return False
    except Exception:
        return False
    radio = (getattr(player, "sources", None) or {}).get("radio")
    mpd = getattr(radio, "mpd", None)
    if preset.source not in ("radio", "ytm") or not callable(mpd):
        return True
    try:
        with mpd_patience(SOURCE_MPD_S):      # the panel loop: never wait long on MPD
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


def shutdown_keys(panel: Panel, actions: Actions) -> list:
    """Keys whose hold shuts the box down (the Excelsior's key 0): their hold
    draws the same countdown as the HM's tone knob held off."""
    keys = []
    for control in getattr(panel, "controls", ()):
        if getattr(control, "kind", None) != "momentary":
            continue
        spec = actions.variant.actions.get(f"{control.id}.hold") or {}
        if spec.get("do") == "shutdown":
            keys.append(control)
    return keys


# A reply word the engine doesn't know -> the nearest it does, in order.
REPLY_WORDS: dict[str, tuple[str, ...]] = {
    "ack": ("kick",), "kick": ("ack",),
    "next": ("kick", "ack"), "nudge": ("kick", "ack"),
    "confirmed": ("kick", "ack"),
    "fail": ("refuse3", "refuse"), "refuse3": ("fail", "refuse"),
    "refuse": ("refuse1", "fail"), "refuse1": ("refuse", "fail"),
    "ha": ("kick", "ack"),
    # v2's green bloom ("that worked"); an engine without it swells, or kicks.
    "bloom": ("ok", "swell", "confirmed", "kick", "ack"),
    "ok": ("bloom", "swell", "confirmed", "kick", "ack"),
    "update_ok": ("ok", "bloom", "swell", "confirmed", "kick"),
}


def _engine_knows(driver: Any, name: str) -> bool:
    """Does the lamp engine draw the reply ``name``? (Its REPLY_S table.)"""
    tables = [getattr(driver, "REPLY_S", None), getattr(driver, "REPLY_ALIASES", None),
              getattr(driver, "OVERLAY_REPLIES", None)]
    if tables == [None, None, None]:
        try:
            from .lamp import engine as lamp_engine
            tables = [getattr(lamp_engine, "REPLY_S", None), getattr(lamp_engine, "REPLY_ALIASES", None),
                      getattr(lamp_engine, "OVERLAY_REPLIES", None)]
        except Exception:
            pass
    known = [t for t in tables if t is not None]
    if not known:
        return True         # can't tell: say it as it is
    try:
        return any(name in t for t in known)
    except TypeError:
        return True


class LampKeeper:
    """Gathers a BoxState each tick and keeps the lamp engine told.

    The engine draws on its own thread, so nothing here waits on it; this side
    only calls ``show`` when the Look changes, ``set_night`` when night flips
    and ``countdown`` while the power knob is held off. Actions' hooks land
    here too, so a slow preset start or a shutdown is already showing before
    the blocking call that follows.

    Seamless hands (lamp/handover.py): ``take_over`` stops the boot breath
    and starts the engine from the frame the LED shows (``engine.seed``), the
    daemon's warm-up carries on the boot script's own breath, a hang falls
    back to the engine's busy breath (``engine.watchdog``, fed by ``show``
    every tick) and ``close`` leaves a steady ember at a shutdown, or a note
    for the boot script to breathe on through a restart or an update.
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
        self._resume_pending = False
        self._resume_done = False
        self._resume_outcome: str | None = None
        self._offline_since: float | None = None
        self._joining_since: float | None = None     # joining(): connecting to a Wi-Fi just chosen
        self._loading: Preset | None = None
        self._loading_since = 0.0
        # Started, then silent for LOADING_FAIL_S: not "playing" whatever the
        # player claims (MPD says "play" while it fails to connect).
        self._silent: Preset | None = None
        self._next_loading_poll = 0.0
        self._cast_seen: float | None = None
        self._was_standby = False
        self._next_source_poll = 0.0
        self._next_renderer_poll = 0.0
        self._renderer_flags: dict | None = None
        self._next_night = 0.0
        # The boot script's breath (lamp/handover.py), read by take_over: the
        # daemon's warm-up stage goes on breathing it.
        self.boot_breath: Look | None = None
        self.run_dir = handover.RUN_DIR
        # What the next boot breath draws (lamp/handover.py boot_doc), written on change.
        self.boot_file = handover.BOOT_FILE
        self._boot_sig: str | None = None
        # Light language v2 (BoxState.fault/setup/offline/update_ok, set only
        # where the BoxState has them): ``faults`` (lamp/faults.check, set by
        # main) answers the reasons for an SOS; ``celebrate`` raises update_ok
        # for UPDATE_OK_S (an update installed, setup done).
        self.faults: Callable[[], list] | None = None
        self.fault_reasons: list = []
        # Wall time (time.time) the last SOS cleared; None = none cleared this
        # run. /api/status ``fault_cleared_at``: the page says it once.
        self.fault_cleared_at: float | None = None
        self._next_fault_poll = 0.0
        self._ok_until = 0.0
        self._prev_state: BoxState | None = None     # the last tick's, for policy.events_for
        self._was_hotspot = False
        self._geek = lamp_api.geek_doc({})
        actions.on_egg = self.egg
        self._fed = False                   # engine.watchdog armed (first show): feed() each tick
        self._refused_panel = False         # the missing panel board said once (refuse3)
        # web/confirm.ConfirmBroker (set by main): a press on the radio is
        # asked for - the engine's beckon (BoxState.confirming).
        self.confirm = None
        # Is an update being installed (web/software.py Software.installing)?
        # Set by main; None = never.
        self.updating: Callable[[], bool] | None = None
        self._updating = False
        self._next_update_poll = 0.0
        self._counting = False
        self._settings: dict | None = None    # the page's last saved doc, if any
        # Settings -> Pilot lamp: brightness and key replies (lamp/meanings.py),
        # and which of them the engine takes itself (else this side fakes it).
        self._prefs = lamp_meanings.prefs_doc({})
        self._engine_does = {"brightness": False, "key_replies": False}
        # player/watchdog.StreamWatchdog (set by main): MPD stopped a preset by
        # itself - not "playing" then, and the loading breath while it recovers.
        self.watchdog = None
        # sleep.SleepTimer (set by main): its last minute dims the lamp, and a
        # phone it slept through is not blue (the radio is asleep, muted).
        self.sleep = None
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
        if (name in lamp_meanings.KEY_REPLIES and not self._prefs["key_replies"]
                and not self._engine_does["key_replies"]):
            return      # key replies off, and the engine can't drop them itself
        try:
            phone = False
            if name.startswith("source:"):
                try:
                    # The PU and Bluetooth keys have just asked the player, so
                    # this is its cached answer, not another round trip.
                    phone = bool(self.actions.phone_has_it())
                except Exception:
                    phone = False
            self.driver.reply(self._word(reply_for(name, phone)))
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
        """WebApp.lamp_listeners: location, night mode or colours changed - look again now.

        Runs on a web thread, so it only leaves the doc and moves the next
        night check to the next tick; the main loop does the rest. The saved
        colours go straight to the engine (its set_palette is thread-safe).
        """
        self._settings = dict(doc)
        self._next_night = 0.0
        self.apply_palette(doc)
        self.apply_prefs(doc)
        self.apply_geek(doc)

    def apply_prefs(self, doc: dict) -> None:
        """settings.json ``lamp`` (brightness, key replies) onto the engine (also at start)."""
        prefs = lamp_meanings.prefs_doc(doc)
        try:
            done = lamp_api.apply_prefs(self.driver, prefs)
        except Exception:
            log.warning("lamp: could not apply brightness / key replies", exc_info=True)
            done = {"brightness": False, "key_replies": False}
        changed = prefs["brightness"] != self._prefs["brightness"]
        self._prefs, self._engine_does = prefs, done
        if changed and not done["brightness"]:
            self.look = None        # the next refresh shows the Look at the new level

    def apply_geek(self, doc: dict) -> None:
        """settings.json ``lamp.geek`` / ``lamp.callsign`` onto the engine (also at start)."""
        self._geek = lamp_api.geek_doc(doc)
        try:
            lamp_api.apply_geek(self.driver, self._geek)
        except Exception:
            log.warning("lamp: could not apply geek mode", exc_info=True)
        set_egg = getattr(self.panel, "set_egg", None)
        if callable(set_egg):
            set_egg(self._geek["geek"])         # the hidden combination only in geek mode

    def celebrate(self, now: float | None = None) -> None:
        """Something finished well (an update installed, setup done): the
        green bloom and Morse OK (BoxState.update_ok) for UPDATE_OK_S."""
        now = self.clock() if now is None else now
        self._ok_until = now + UPDATE_OK_S
        self._joining_since = None          # a join's restart that got online ends here too

    def joining(self, now: float | None = None) -> None:
        """This start follows a Wi-Fi join (main.py, Wifi.joined_pending): the
        amber breath while it connects, never the red alert. Online ->
        consume_wifi_joined -> ``celebrate`` (the green OK, then music); setup
        mode instead -> the HAL eye once, then CQ (docs/using.md "Wi-Fi")."""
        self._joining_since = self.clock() if now is None else now

    def egg(self) -> bool:
        """The hidden key combination (or the page): an Easter egg, in geek mode only."""
        if not self._geek.get("geek"):
            log.info("lamp: no surprises outside geek mode")
            return False
        return lamp_api.play_egg(self.driver)

    def meaning(self) -> str:
        """What the lamp says now, in a word (lamp/meanings.STATUS) - /api/status ``lamp``."""
        return lamp_meanings.meaning_of(self.state, lamp_policy)

    def _scaled(self, look: Look) -> Look:
        """Today's engine has no brightness scale: dim or lift the Look itself.

        Ember is left alone - asleep must never look dark (dark = unplug).
        """
        k = lamp_meanings.BRIGHTNESS_FALLBACK.get(self._prefs["brightness"], 1.0)
        if self._engine_does["brightness"] or k == 1.0 or look.colour in ("ember", "off"):
            return look
        return dataclasses.replace(look, level=min(1.0, look.level * k), floor=min(1.0, look.floor * k))

    def apply_palette(self, doc: dict) -> None:
        """settings.json ``lamp_palette`` over the release's palette (also at start)."""
        set_palette = getattr(self.driver, "set_palette", None)
        if not callable(set_palette):
            return
        try:
            saved = doc.get("lamp_palette")
            set_palette(saved if isinstance(saved, dict) else {})
        except Exception:
            log.warning("lamp: could not apply the saved colours", exc_info=True)

    def resume_started(self) -> None:
        """A boot resume is on its way (bootresume.py): the warm-up breath
        holds until it ends, and its retries never pulse red on their own."""
        self._resume_pending = True

    def boot_done(self, outcome: str | None = None) -> None:
        """The resume finished (or a human took over): warm-up is over.

        ``outcome`` is BootResume's. "playing" has been heard already, so the
        lamp goes straight to playing (the fade lands with the swell);
        "failed" leaves a started-but-silent preset not looking like music.
        """
        self._resume_done = True
        self._resume_outcome = outcome
        player = self.actions.player
        if outcome == "playing":
            current = player.current()
            if current is not None and current == self._loading:
                self.source, self._loading = current.source, None
        elif outcome == "failed":
            self._silent, self._loading = self._loading or player.current(), None
        if self._loading is not None:
            self._loading_since = self.clock()  # a fresh LOADING_FAIL_S from here

    def _word(self, name: str) -> str:
        """A reply in the engine's own vocabulary (REPLY_WORDS): the newer
        engine says kick/refuse/confirmed, the older ack/fail."""
        kind = name.partition(":")[0]
        if _engine_knows(self.driver, kind):
            return name
        for other in REPLY_WORDS.get(kind, ()):
            if _engine_knows(self.driver, other):
                return other
        return name

    # -- the tick ---------------------------------------------------------

    def tick(self, now: float) -> None:
        """Called from the main loop every LAMP_TICK_S. Never raises."""
        try:
            self._poll(now)
            self._countdown(now)
            self.refresh(now)
            if self._fed:
                self.driver.feed()      # alive: holds off engine.watchdog's busy breath
            if not self._refused_panel and self.panel is not None and getattr(self.panel, "absent", False):
                self._refused_panel = True
                self.reply("refuse3")   # the panel board is missing: said once, at start
        except Exception:
            log.exception("lamp tick failed")

    def _poll(self, now: float) -> None:
        player = self.actions.player
        if now >= self._next_renderer_poll:
            self._next_renderer_poll = now + RENDERER_POLL_S
            if self._renderers_changed(player):
                self._next_source_poll = now    # ask afresh, this tick
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
            elif now - self._loading_since >= LOADING_FAIL_S and not self._resuming():
                log.warning("preset %s made no sound in %.0f s", self._loading.n, LOADING_FAIL_S)
                self._silent, self._loading = self._loading, None
                self.reply("fail")
        conn = self.connectivity
        if conn is not None:
            if conn.online:
                self._offline_since = None
            elif self._offline_since is None:
                self._offline_since = now
            hot = bool(conn.hotspot)
            if self._was_hotspot and not hot and conn.online:
                log.info("lamp: setup done - the radio joined the Wi-Fi")
                self.celebrate(now)
            self._was_hotspot = hot
            if self._joining_since is not None:
                if hot:
                    log.warning("lamp: the Wi-Fi just chosen didn't answer - setup mode (HAL eye, then CQ)")
                    self._joining_since = None
                    try:
                        self.driver.reply(self._word("refuse"))   # not a key reply: always shown
                    except Exception:
                        log.debug("lamp reply failed", exc_info=True)
                elif now - self._joining_since >= JOIN_MAX_S:
                    log.warning("lamp: still not on the Wi-Fi just chosen after %.0f s", JOIN_MAX_S)
                    self._joining_since = None
        if self.faults is not None and now >= self._next_fault_poll:
            self._next_fault_poll = now + lamp_faults.FAULT_POLL_S
            try:
                found = list(self.faults() or [])
            except Exception:
                log.debug("fault checks failed", exc_info=True)
                found = self.fault_reasons
            if found != self.fault_reasons:
                if found:
                    log.warning("lamp: SOS - %s needs a maker", ", ".join(found))
                else:
                    log.info("lamp: the fault cleared")
                    self.fault_cleared_at = time.time()
            self.fault_reasons = found
        if now >= self._next_update_poll:
            self._next_update_poll = now + UPDATE_POLL_S
            self._updating = self._update_now()
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
            self.note_boot()        # night, calibration or brightness changed: the boot breath's notes

    def _hold_duties(self) -> tuple | None:
        """The steady ember a shutdown leaves (day, night), as the engine draws
        standby with this lamp's calibration: levels, palette, balance, floors."""
        duty_for = getattr(self.driver, "duty_for", None)
        if not callable(duty_for):
            return None
        from .lamp.contract import EMBER_LEVEL, EMBER_NIGHT
        calibration = getattr(self.driver, "calibration", None)
        levels = (calibration() or {}).get("levels", {}) if callable(calibration) else {}
        day = float(levels.get("ember_day", EMBER_LEVEL))
        night = float(levels.get("ember_night", EMBER_NIGHT))
        return tuple(duty_for("ember", day)), tuple(duty_for("ember", night))

    def note_boot(self, force: bool = False) -> bool:
        """Leave the boot script what it should draw next time (lamp-boot.json:
        night or day, the boot and busy breaths as this engine draws them),
        when any of it changed. Never raises; True if written."""
        calibration = getattr(self.driver, "calibration", None)
        if not callable(calibration):
            return False
        try:
            spec = calibration()
            busy = getattr(policy, "UPDATING", None) or getattr(policy, "SHUTTING_DOWN")
            bright = getattr(self.driver, "brightness", 1.0)
            sig = json.dumps([spec, bool(self.state.night), bright, repr(busy)], sort_keys=True, default=str)
            if sig == self._boot_sig and not force:
                return False
            doc = handover.boot_doc(spec, bool(self.state.night), busy, bright, self._hold_duties())
            if handover.write_boot_doc(doc, self.boot_file):
                self._boot_sig = sig
                return True
        except Exception:
            log.debug("lamp: boot breath notes failed", exc_info=True)
        return False

    def _renderers_changed(self, player: Any) -> bool:
        """moOde's phone flags moved since the last look: a phone started or
        stopped casting. The player's cached source is dropped then, so the
        next active_source() asks (and stays the one answer for everyone)."""
        flags_of = getattr(player, "renderer_flags", None)
        if not callable(flags_of):
            return False
        try:
            flags = flags_of()
        except Exception:
            log.debug("renderer flags unreadable", exc_info=True)
            return False
        if flags is None:
            return False
        before, self._renderer_flags = self._renderer_flags, flags
        if before is None or flags == before:
            return False
        forget = getattr(player, "forget_source", None)
        if callable(forget):
            forget()
        return True

    def _update_now(self) -> bool:
        if self.updating is None:
            return False
        try:
            return bool(self.updating())
        except Exception:
            log.debug("update phase unreadable", exc_info=True)
            return False

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
        if progress is None and self.panel is not None and not self.actions.shutting_down:
            # A key held for shutdown (the Excelsior's key 0): the same 5 s.
            for key in shutdown_keys(self.panel, self.actions):
                since = getattr(key, "down_since", None)
                if since is None or since < self._armed_at or getattr(key, "hold_fired", False):
                    continue
                # Not on key-down: a tap's kick must play out whole (the
                # countdown outranks replies). After KEY_COUNTDOWN_AFTER_S the
                # glow runs down to land with the hold at hold_s.
                held = now - since
                if held < KEY_COUNTDOWN_AFTER_S:
                    continue
                span = max(key.hold_s - KEY_COUNTDOWN_AFTER_S, 1e-3)
                progress = max(0.0, min(1.0, (held - KEY_COUNTDOWN_AFTER_S) / span))
                break
        if progress is None and not self._counting:
            return
        self._counting = progress is not None
        try:
            self.driver.countdown(progress)
        except Exception:
            log.debug("lamp countdown failed", exc_info=True)

    def _resuming(self) -> bool:
        return self._resume_pending and not self._resume_done

    def boot_stage(self, now: float) -> str:
        conn = self.connectivity
        if not self._net_seen and conn is not None and conn.online:
            self._net_seen = bool(getattr(conn, "seen_up", True))
        # Music (or a verdict) ends the warm-up; nothing to resume, the
        # network (the next key press will work) or the clock does.
        settled = self._resume_outcome not in (None, "nothing") or self._net_seen
        limit = BOOT_RESUME_MAX_S if self._resuming() else BOOT_MAX_S
        if self._resume_done and settled or now - self._started >= limit:
            return "done"
        return "network" if self._net_seen else "daemon"

    def gather(self, now: float) -> BoxState:
        """The BoxState as of ``now`` (timers are this object's, flags Actions')."""
        actions, player, state = self.actions, self.actions.player, self.state
        sleep = self.sleep
        phone = actions.phone_has_it(self.source)
        if actions.standby and not self._was_standby:
            # Standby just started: the phone's blue grace is over now, or
            # casting (which outranks standby) would keep the lamp blue 3 s.
            self._cast_seen = None
        self._was_standby = actions.standby
        if phone and (actions.keeps_asleep(self.source)
                      or (actions.standby and sleep is not None and sleep.keeps_asleep(self.source))):
            phone = False
            self._cast_seen = None      # a phone the radio sleeps through is not blue, not even 3 s
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
        state.updating = self._updating
        state.hotspot = bool(conn is not None and conn.hotspot)
        state.pairing = actions.pairing
        state.casting = casting
        state.cast_paused = paused
        state.offline_s = 0.0 if self._offline_since is None else now - self._offline_since
        if hasattr(state, "joining"):
            state.joining = self._joining_since is not None
        watchdog = self.watchdog
        stalled = watchdog is not None and watchdog.silent
        state.loading = self._loading is not None or (watchdog is not None and watchdog.breathing)
        state.playing = (
            not state.loading and current is not None and self.source == current.source
            and current != self._silent and not stalled
        )
        state.source = current.source if current is not None else self.source
        state.panel_missing = bool(self.panel is not None and getattr(self.panel, "absent", False))
        state.sleeping = bool(sleep is not None and sleep.fading)
        # v2 flags, only where this BoxState has them (the engine engineer's contract).
        for flag, value in (("fault", bool(self.fault_reasons)), ("setup", state.hotspot),
                            ("offline", state.offline_s >= lamp_policy.OFFLINE_AFTER_S),
                            ("update_ok", now < self._ok_until)):
            if hasattr(state, flag):
                setattr(state, flag, value)
        if hasattr(state, "confirming"):
            pending = getattr(self.confirm, "pending", None)
            try:
                state.confirming = bool(pending()) if callable(pending) else False
            except Exception:
                state.confirming = False
        group = getattr(actions, "group", None)
        if group is not None:
            try:
                state.group, state.group_source, state.group_playing = group.lamp()
            except Exception:
                log.debug("multiroom lamp state failed", exc_info=True)
                state.group, state.group_playing = "", False
        return state

    def refresh(self, now: float | None = None) -> Look:
        """Recompute the Look; tell the engine only if it changed."""
        now = self.clock() if now is None else now
        state = self.gather(now)
        self._events(state)
        look, fade = look_for(state)
        if self.boot_breath is not None and state.boot == "daemon" and look == policy.BOOT.get("daemon"):
            look = self.boot_breath     # the boot script's breath, carried on
        look = self._scaled(look)
        if look != self.look:
            log.debug("lamp %s -> %s (%.1f s)", self.look, look, fade)
            self.look = look
            try:
                self.driver.show(look, fade)
            except Exception:
                log.debug("lamp show failed", exc_info=True)
            if not self._fed:
                self._arm_watchdog()
        return look

    def _events(self, state: BoxState) -> None:
        """One-shot replies for what just happened (policy.events_for: an update
        installed -> update_ok), and the geek's farewell '73' as a shutdown starts."""
        before = self._prev_state
        try:
            events = getattr(policy, "events_for", None)
            if callable(events):
                for word in events(before, state) or ():
                    self.driver.reply(self._word(word))
            if state.shutting_down and not (before is not None and before.shutting_down):
                farewell = getattr(self.driver, "farewell", None)
                if callable(farewell):
                    farewell()          # '73' in amber Morse - the engine plays it only in geek mode
        except Exception:
            log.debug("lamp events failed", exc_info=True)
        self._prev_state = dataclasses.replace(state)

    def _arm_watchdog(self) -> None:
        """After the first show: a main loop that stops ticking (feed) ends in
        the engine's busy ember breath within LAMP_WATCHDOG_S."""
        watchdog = getattr(self.driver, "watchdog", None)
        if not (callable(watchdog) and callable(getattr(self.driver, "feed", None))):
            return
        try:
            watchdog(LAMP_WATCHDOG_S)
            self._fed = True
        except Exception:
            log.debug("lamp watchdog unavailable", exc_info=True)

    def take_over(self) -> None:
        """Before the first frame: the boot breath lets go, the engine starts
        from what the LED shows (no dark gap, no jump), and the boot script
        learns this lamp's PWM period for the next boot. Never raises."""
        try:
            doc = handover.take_over(self.run_dir)
            if doc and doc.get("colour") == "amber":
                floor, peak = float(doc.get("floor", 0.25)), float(doc.get("peak", 0.60))
                period = float(doc.get("period_s", 3.0))
                if 0.0 <= floor < peak <= 1.0 and period > 0:
                    self.boot_breath = Look("amber", peak, "breath", period, floor)
            output = getattr(self.driver, "output", None)
            period_ns = getattr(output, "period_ns", None)
            if isinstance(period_ns, int):
                remember_period(period_ns)
            read = getattr(output, "read", None)
            seed = getattr(self.driver, "seed", None)
            duty = read() if callable(read) else None
            if callable(seed) and duty is not None and max(duty) > 0.0:
                seed(tuple(duty))       # linear duty; the engine glides from it
        except Exception:
            log.warning("lamp: taking over from the boot breath failed", exc_info=True)

    def close(self) -> None:
        # Dark means safe to unplug, and only the halt hook says that.
        # Shutting down: a steady ember (not a breath frozen at a random
        # phase) through the halt; the system-shutdown hook darkens it last.
        # A restart, a crash or an update being installed: the LED keeps its
        # last frame and the boot script breathes on from it (ExecStopPost,
        # lamp/handover.py) until the next haslerd takes over - amber, or the
        # busy ember breath while an update installs.
        shutting = self.actions.shutting_down
        try:
            mode = "hold" if shutting else "ember" if self._update_now() else "amber"
            self.driver.close(halt_dark=False)
            if shutting:
                output = getattr(self.driver, "output", None)
                hold = getattr(output, "hold", None)
                if callable(hold):
                    duties = self._hold_duties()
                    if duties is not None:      # the engine's standby ember: calibrated levels and floors
                        hold(*duties[1 if self.state.night else 0])
                    else:
                        palette = getattr(self.driver, "palette", None) or {}
                        ratio = palette.get("ember", (1.0, 0.10, 0.0))
                        balance = getattr(self.driver, "balance", (1.0, 1.0, 1.0))
                        hold(*handover.hold_duty(ratio, bool(self.state.night), balance))
            handover.write_handover(mode, self.run_dir)
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


# The descriptor's ``audio:`` keys that seed MoodeConfig (variant.Variant.audio).
AUDIO_SEEDS = {"volume_limit_default": "volume_limit", "volume_floor_default": "volume_floor",
               "volume_limit_max": "volume_limit_max", "card": "card"}


def audio_seed(audio: dict | None) -> dict:
    """MoodeConfig fields from a descriptor's ``audio:`` block (the cabinet's
    own defaults); a bad value is logged and left to the code's default."""
    out: dict = {}
    for key, field in AUDIO_SEEDS.items():
        if not isinstance(audio, dict) or key not in audio:
            continue
        value = audio[key]
        if field == "card":
            if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
                out[field] = value
            else:
                log.warning("ignoring audio.card %r in the descriptor", value)
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            log.warning("ignoring audio.%s %r in the descriptor: not a whole number", key, value)
        else:
            out[field] = int(value)
    return out


def moode_config(config: dict, settings: dict, audio: dict | None = None) -> MoodeConfig:
    """haslerd.yaml's ``moode:`` block, with the web page's saved settings on top.

    Lowest first: MoodeConfig's bench-HM constants, the descriptor's ``audio:``
    block (``volume_limit_default``, ``volume_floor_default``,
    ``volume_limit_max``, ``card``: audio_seed), box.yaml's ``moode:``, then
    settings.json's ``volume_limit`` / ``volume_floor``. Never above the
    maker's cap: a ceiling over ``volume_limit_max`` is pulled down to it
    (logged), so a cap lowered in a file holds at the next start whatever the
    page once saved.

    A bad saved ``volume_limit`` / ``volume_floor`` is ignored (logged), never
    fatal: the yaml's own value then stands. So is a ``moode:`` key this
    haslerd doesn't know - a release that drops a setting must still start
    (else the health check fails and the update rolls back).
    """
    known = {f.name for f in dataclasses.fields(MoodeConfig)}
    spec = dict(audio_seed(audio))
    for key, value in dict(config.get("moode") or {}).items():
        if key in known:
            spec[key] = value
        else:
            log.warning("ignoring unknown setting moode.%s in box.yaml", key)
    lo, cap = volume_limit_range(spec.get("volume_limit_max", MoodeConfig.volume_limit_max))
    spec["volume_limit_max"] = cap
    if "volume_limit" in settings:
        saved = settings["volume_limit"]
        try:
            spec["volume_limit"] = volume_limit_from(saved, cap)
        except ValueError as exc:
            if isinstance(saved, (int, float)) and not isinstance(saved, bool) and saved > cap:
                log.warning("saved volume_limit %r is above the maker's cap of %d: using %d", saved, cap, cap)
                spec["volume_limit"] = cap
            else:
                log.warning("ignoring saved volume_limit %r: %s", saved, exc)
    limit = spec.get("volume_limit", MoodeConfig.volume_limit)
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit > cap:
        log.warning("volume_limit %r is above the maker's cap of %d: using %d", limit, cap, cap)
        spec["volume_limit"] = cap
    if "volume_floor" in settings:
        limit = spec.get("volume_limit", MoodeConfig.volume_limit)
        try:
            spec["volume_floor"] = volume_floor_from(settings["volume_floor"], limit)
        except (TypeError, ValueError) as exc:
            log.warning("ignoring saved volume_floor %r: %s", settings["volume_floor"], exc)
    return MoodeConfig(**spec)


def fake_player(config: dict, settings: dict, audio: dict | None = None) -> FakePlayer:
    """``--fake``'s player, with a real player's volume ceiling, floor and cap
    (the descriptor's audio block, box.yaml, then the saved settings, else
    MoodeConfig's 70/28) so the key page's sliders work."""
    player = FakePlayer()
    player.config = moode_config(config, settings, audio)
    return player


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
    if args.fake and os.environ.get("HASLER_FAKE_CLOSED"):
        # --fake only: contacts that start closed (HASLER_FAKE_CLOSED=d10,d2),
        # so a page test sees a latched key, a switch on, a selector position.
        for pin in os.environ["HASLER_FAKE_CLOSED"].split(","):
            if pin.strip():
                panel.digital.set(pin.strip(), True)

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
        player = fake_player(config, settings, variant.audio)
        # HASLER_FAKE_LAMP=1: a lamp engine drawing to nowhere, so a page test
        # sees the lamp's meanings, Show me and Identify answer as on a radio.
        lamp = None
        if os.environ.get("HASLER_FAKE_LAMP"):
            from .lamp.engine import LampEngine
            from .lamp.outputs import NullOutput
            lamp = LampEngine(NullOutput())
    else:
        player = MoodePlayer(moode_config(config, settings, variant.audio), state_dir=state_dir)
        lamp = open_lamp(variant.io.get("lamp") or {})
    log.info("state file: %s", state.path or "(in memory)")

    # With HaApi present, its load() (in build_web) owns the one Home
    # Assistant client - from its saved pairing, else this same yaml block.
    ha = None if HaApi is not None else yaml_ha(config)

    # The built-in stations: the starter set in force (haslerd/presets.py) on
    # the cabinet's slots - box.yaml's ``starter``, the page's choice over it.
    # The descriptor came with the default set; a descriptor that lists its
    # own presets (no slots) keeps them.
    starters = starter_sets.Starters(paths.release, variant.slots) if variant.slots else None
    starter_box = starter_sets.box_starter(paths)
    if starters:
        chosen = starters.chosen(starter_box, settings)
        variant.presets = starters.specs(chosen)
        log.info("built-in stations: starter set %s (%s)", chosen,
                 starters.source(starter_box, settings))
    else:
        starters = None

    actions = Actions(variant, player, ha=ha, state=state)
    apply_sources(actions, settings)           # the PU cycle, resume after a cast
    apply_bluetooth(actions, settings)         # the Bluetooth keys, reconnecting the phone
    actions.dsp = build_knob_functions(actions, player, settings)
    web, app = build_web(config, actions, panel, state, ha is not None, paths=paths,
                         starters=starters, starter_box=starter_box)
    if args.fake:
        # POST /api/fake/phone: a page test's phone taking the speaker (web/fake_api.py).
        from .web.fake_api import FakeApi
        app.extra_routes.append(FakeApi(app))
        # Never the makers' real support server from a fake radio (a page test); box.yaml may name one.
        if getattr(app, "support", None) is not None:
            app.support.default_url = ""
        # A fake radio's install mark lives in its state dir (a page test writes
        # it to make the radio "new": /api/info installed_at); none without one.
        state_dir = Path(state.path).parent if state.path is not None else None
        app.install_done_file = state_dir / "install-done" if state_dir is not None else Path("/nonexistent/install-done")
    else:
        # The wifi_setup key job: the setup network now, forgetting nothing (web/wifi.py).
        actions.wifi_setup = app.wifi.hotspot_now
    # Background: YTM presets resolve their opening track now, not on first
    # press. After the web overlay, so it warms the keys as the owner set them.
    player.prefetch(list(actions.presets.values()))
    return panel, actions, lamp, config, web, app


def build_knob_functions(actions: Actions, player, settings: dict) -> KnobFunctions:
    """The knobs' sound effects (dsp/knobs.py), with the owner's saved choices.

    hasler-dsp drives them where it is installed; elsewhere (an older box, a
    laptop, --fake) a knob's change still reaches the player's tone command.
    """
    command = getattr(getattr(player, "config", None), "dsp_command", "") or ""
    runner = helper_runner(command) if command and Path(command).exists() else None
    if runner is None:
        log.info("knob effects: no hasler-dsp%s; tone knobs use the player's tone command",
                 f" at {command}" if command else "")
    knobs = KnobFunctions(lambda: actions.variant, runner, volume=player.get_volume,
                          legacy=player.set_tone)
    knobs.apply_saved(settings)
    return knobs


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


def build_mono(state_dir: Path | None, audio: dict | None = None) -> MonoSwitch | None:
    """The mono switch, re-applied from settings.json off-thread. The default
    is the cabinet's (``audio.speakers``, audio.mono_default: one speaker =
    mono) until the owner says otherwise.

    Re-applying at every start also refreshes the override's slave from
    moOde's current output device. Absent helper (a laptop, --fake): None.
    """
    mono = MonoSwitch()
    if not mono.available:
        return None
    settings = load_settings(state_dir / SETTINGS_FILE if state_dir is not None else None)
    default = mono_default(audio)
    want = settings.get("mono", default)
    want = want if isinstance(want, bool) else default

    def apply() -> None:
        try:
            mono.set(want, reopen=False)
        except MonoError:
            log.exception("could not apply the saved mono setting")

    threading.Thread(target=apply, name="mono", daemon=True).start()
    return mono


def build_web(
    config: dict, actions: Actions, panel: Panel, state: StateStore, ha: bool,
    paths: boxconfig.Paths | None = None, starters: Any = None, starter_box: str = "",
) -> tuple[WebServer | None, WebApp]:
    """The key page, with the owner's saved layout laid over the descriptor.

    ``keys.yaml`` lives next to state.json; ``web: {enabled: false}`` turns the
    page off but still honours a saved layout (and Home Assistant still gets
    its status). ``starters``/``starter_box``: the release's starter sets and
    box.yaml's choice (build), for Reset and "Start from a set". Returns
    (server or None, the app) - the main loop publishes ``app.status()`` to
    Home Assistant through ``app.ha_api``.
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
        mono=build_mono(state_dir, getattr(actions.variant, "audio", None)),
        starters=starters, starter_box=starter_box,
    )
    app.ha_api = ha_api
    # Physical confirmation and the "About this radio" card (web/confirm.py,
    # web/software.py). web.allow_unconfirmed: true = the bench, no key press.
    app.confirm.allow_unconfirmed = bool(spec.get("allow_unconfirmed", False))
    app.allowed_hosts.update(str(h).lower() for h in (spec.get("hostnames") or []))
    app.software.box_paths = paths
    # The yt-dlp lane (haslerd/ytdlp_lane.py): health, its canary, starting it
    # when YouTube Music tracks stop resolving.
    ytm = (sources or {}).get("ytm")
    app.ytdlp_lane = YtdlpLane(state_dir=state_dir, ytm=ytm, systemctl=app.software.start_unit)
    if ytm is not None and hasattr(ytm, "on_resolve"):
        ytm.on_resolve = app.ytdlp_lane.resolve_result
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
    # /api/spotify/account: sign the radio in to Spotify with a code (also in /api/casting)
    app.spotify_account = SpotifyAccountApi(app)
    app.extra_routes.append(SourcesApi(app))   # /api/sources, /api/casting
    # /api/sleep: the sleep timer (haslerd/sleep.py), ticked by the main loop;
    # any key or knob, from the cabinet or the page's remote, cancels it.
    app.sleep = SleepTimer(actions)
    actions.on_hand = app.sleep.hand
    actions.sleep = app.sleep                  # the ``sleep`` verb: the timer from a key
    app.extra_routes.append(SleepApi(app, app.sleep))
    app.extra_routes.append(BluetoothApi(app))  # /api/bluetooth
    # /api/help: "Let the makers help (24 h)" - off until the owner asks
    app.extra_routes.append(HelpApi(app, tailnet=spec.get("help_tailnet", MAKERS_TAILNET)))
    # /api/report ("Tell the makers"), /api/notes ("From the makers"),
    # /api/checkin/preview ("See what it sends"): support, all unprivileged.
    app.extra_routes.append(ReportApi(app))
    app.extra_routes.append(NotesApi(app, state_dir))
    # /api/support: "Send to the makers" (the makers' private support list) and their replies;
    # ticked by the main loop (a request that waited for the internet, the replies)
    app.support = SupportApi(app, state_dir)
    app.extra_routes.append(app.support)
    app.extra_routes.append(CheckinApi(app))
    app.extra_routes.append(app.spotify_account)
    # /api/stations/try, /mine, /report: the station browser (My stations in <state>/stations.json)
    app.stations_api = StationsApi(app, state_dir)
    app.extra_routes.append(app.stations_api)
    radio = (sources or {}).get("radio")
    if radio is not None and hasattr(radio, "on_play"):
        radio.on_play = app.stations_api.played
    if actions.dsp is not None:                 # /api/knobs/functions, /api/knobs/try
        app.knob_functions = actions.dsp
        app.extra_routes.append(KnobFunctionsApi(app, actions.dsp))
    build_group(app, actions, int(spec.get("port", 8080)))
    app.load_saved()
    # Spotify for guests: radios made before it existed get it here, not from
    # a release (go-librespot's config.yml is theirs after the bootstrap).
    app.apply_spotify_guests()
    app.spotify_account.resume()             # a sign-in left waiting gets its deadline back
    if not spec.get("enabled", True):
        return None, app
    server = WebServer(app, host=spec.get("host", "0.0.0.0"), port=int(spec.get("port", 8080)))
    try:
        server.start()
    except OSError:
        log.exception("key page could not listen on port %s; carrying on without it", server.port)
        return None, app
    # http://hasler.local/ through moOde's nginx (web/front.py), which hands
    # port 80 to 127.0.0.1:8080 - so only for a page on 8080. web.front: false
    # gives port 80 back to moOde.
    if server.port == 8080:
        app.front = WebFront(enabled=spec.get("front", True) is not False)
        app.front.start()
    return server, app


def build_group(app: WebApp, actions: Actions, api_port: int = 8080) -> object | None:
    """Multiroom (/api/group, /api/peer), from settings.json's ``multiroom``.

    Without Snapcast's side (multiroom/audio.py) the page still answers, but
    multiroom can't be switched on.
    """
    if Group is None:
        return None
    from .web import integration
    audio = None
    if SnapAudio is not None:
        try:
            audio = SnapAudio()
            mpd_reopen = audio._reopen
            spotify = (getattr(actions.player, "sources", None) or {}).get("spotify")

            def reopen() -> None:
                # Every playing source must reopen _audioout to follow a switch.
                mpd_reopen()
                if spotify is not None and hasattr(spotify, "reopen"):
                    spotify.reopen()
            audio._reopen = reopen
        except Exception:
            log.exception("multiroom audio unavailable")
    try:
        group = Group(actions, audio,
                      identity=lambda: (integration.radio_id(app.machine_id_file), app.room.name()),
                      api_port=api_port)
        group.actions_lock = app.lock
        group.apply_saved(load_settings(app.settings_file))
    except Exception:
        log.exception("multiroom unavailable")
        return None
    actions.group = group
    app.group = group
    app.extra_routes.append(GroupApi(app, group))
    return group


class HaPublisher:
    """Pushes ``app.status()`` to Home Assistant, off the panel loop.

    ``status()`` shells out (amixer, moOde) and HA may be slow, so each
    publish runs on a short-lived thread; a tick while one is still running is
    skipped rather than queued. Never raises into the loop. The same thread
    first lets HaApi.watch() notice HA has moved to a new address (ha/follow).
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
            watch = getattr(self.ha_api, "watch", None)
            if callable(watch):
                watch()
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


def build_wifiwatch(app: Any, conn: Connectivity, fake: bool = False) -> HotspotWatchdog | None:
    """The hotspot watchdog (wifiwatch.py): moOde never leaves its setup
    hotspot by itself, so this tries the saved Wi-Fi again now and then."""
    wifi = getattr(app, "wifi", None)
    if wifi is None or fake:
        return None
    settings_file = getattr(app, "settings_file", None)
    if settings_file is not None:
        wifi.hotspot_file = Path(settings_file).parent / "hotspot.txt"   # /var/lib/haslerd
        wifi.joined_file = Path(settings_file).parent / "wifi-joined"    # a join's note for the next start
    idle = getattr(app, "_idle", None)
    watch = HotspotWatchdog(wifi, hotspot=lambda: conn.hotspot,
                            idle=idle if callable(idle) else (lambda: True),
                            offline=lambda: not conn.online,
                            linked=lambda: getattr(conn, "linked", None))
    wifi.watch = watch
    return watch


def settle(panel: Panel, actions: Actions, settle_s: float = BOOT_SETTLE_S) -> None:
    """Restore what the box was doing and let the panel settle. Plays nothing.

    Order matters: load state, then let a knob that is off at power-up say
    ``off`` (-> standby) before anything resumes.
    """
    actions.restore()
    end = time.monotonic() + settle_s
    while time.monotonic() < end:
        actions.dispatch_all(panel.poll(time.monotonic()))
        time.sleep(0.01)
    take_downs = getattr(panel, "take_downs", None)
    if callable(take_downs):
        take_downs()    # a key held at power-up is no press to answer


def boot(panel: Panel, actions: Actions, settle_s: float = BOOT_SETTLE_S, quiet: bool = False) -> bool:
    """``settle``, then resume at once; True if the remembered preset is playing.

    The daemon itself resumes through BootResume instead (it waits for the
    player); this is the one-shot form.
    """
    settle(panel, actions, settle_s)
    return actions.resume_last(quiet=quiet)


def system_stopping() -> bool:
    """Is the whole system shutting down (not just haslerd restarting)?"""
    try:
        result = subprocess.run(["systemctl", "is-system-running"],
                                capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.stdout.strip() == "stopping"


def network_up(actions: Actions) -> None:
    """The internet answered for the first time this boot: tell YouTube Music."""
    ytm = (getattr(actions.player, "sources", None) or {}).get("ytm")
    hook = getattr(ytm, "network_up", None)
    if callable(hook):
        try:
            hook()
        except Exception:
            log.exception("YouTube Music network_up failed")


def wifi_join_pending(app: Any) -> bool:
    """Did a Wi-Fi join restart the radio onto a network it is not yet on (the
    wifi-joined note is there, unread)? Never raises."""
    peek = getattr(getattr(app, "wifi", None), "joined_pending", None)
    try:
        return bool(peek()) if callable(peek) else False
    except Exception:
        return False


def consume_wifi_joined(app: Any, keeper: Any) -> str | None:
    """The network is up: did a Wi-Fi join (the setup page, the rescue file,
    the watchdog) restart the radio onto it? web/wifi.py left its SSID in
    <state>/wifi-joined; this takes it, once. Then what docs/using.md
    promises: the lamp blooms green and taps OK, the other rooms learn the
    new address at once, Home Assistant is looked for now, and Settings says
    "New Wi-Fi: … Moved house?" for a day. Returns the SSID, or None."""
    wifi = getattr(app, "wifi", None)
    take = getattr(wifi, "take_joined", None)
    if not callable(take):
        return None
    try:
        ssid = take()
    except Exception:
        log.debug("wifi-joined check failed", exc_info=True)
        return None
    if not ssid:
        return None
    log.warning("on Wi-Fi %r after a join - the lamp says OK", ssid)
    if keeper is not None:
        try:
            keeper.celebrate()
        except Exception:
            log.debug("lamp celebrate failed", exc_info=True)
    group = getattr(app, "group", None)
    if group is not None and getattr(group, "enabled", False):
        try:
            group.peers.kick(force=True)
        except Exception:
            log.debug("peer refresh after the join failed", exc_info=True)
    ha_api = getattr(app, "ha_api", None)
    hook = getattr(ha_api, "network_up", None)
    if callable(hook):
        try:
            hook()
        except Exception:
            log.debug("Home Assistant check after the join failed", exc_info=True)
    before = getattr(wifi, "joined_from", None)
    if isinstance(before, str) and before == ssid:
        # The same network again (a password re-typed, the watchdog back on the
        # home Wi-Fi after setup mode): no "New Wi-Fi: … Moved house?".
        log.info("Wi-Fi %r was the network before too - no 'New Wi-Fi' note", ssid)
        return ssid
    note = getattr(app, "note_wifi_joined", None)
    if callable(note):
        try:
            note(ssid)
        except Exception:
            log.debug("could not note the Wi-Fi join", exc_info=True)
    return ssid


def amp_card(player) -> int | str:
    """The amp's ALSA card (MoodeConfig.card), for the first-sound line."""
    card = getattr(getattr(player, "config", None), "card", None)
    return card if card not in (None, "") else AMP_CARD


def boot_resume(
    actions: Actions, power_cut: bool, settings_file: Path | None,
    keeper: LampKeeper | None = None,
) -> BootResume:
    """The boot resume, wired to the lamp and to the page's night settings."""
    night = None
    if night_mode_active is not None:
        def night() -> bool:
            return bool(night_mode_active(datetime.now(), load_settings(settings_file)))

    resume = BootResume(
        actions,
        sounding=preset_sounding,
        night_hold=night_hold(power_cut, night),
        on_done=keeper.boot_done if keeper is not None else None,
    )
    if keeper is not None:
        keeper.resume_started()
    return resume


def build_stream_watchdog(actions: Actions, app: Any, keeper: LampKeeper | None) -> StreamWatchdog | None:
    """The stream watchdog (player/watchdog.py), on a real player: MPD stopping
    a preset by itself is noticed, said, and undone. None with --fake."""
    player = actions.player
    if not all(hasattr(player, a) for a in ("route_gen", "routing", "resume_mpd", "restart")):
        return None
    night = (lambda: bool(keeper.state.night)) if keeper is not None else None
    # On the card (/var/lib/haslerd), not /var/log: that is log2ram, lost at a power cut.
    settings_file = getattr(app, "settings_file", None)
    events = StreamEvents(Path(settings_file).parent / EVENTS_FILE if settings_file else None)
    watchdog = StreamWatchdog(actions, night=night, events=events, lock=getattr(app, "lock", None))
    actions.stalled = watchdog.stalled_on      # its key plays it again, not "again"
    if keeper is not None:
        keeper.watchdog = watchdog
    if app is not None:
        app.stream_watch = watchdog            # /api/status, /api/health
    watchdog.start()
    return watchdog


def build_phone_volume(actions: Actions) -> PhoneVolume | None:
    """The phone's Bluetooth volume as the radio's (player/btvolume.py), on a
    player with real BlueZ/BlueALSA behind it."""
    if not isinstance(actions.bluetooth, Bluetooth):
        return None
    player = actions.player
    return PhoneVolume(player.get_volume, player.set_volume, lambda: bool(actions.bt_phone_volume))


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
    # Before anything else touches the state directory: did the last run end
    # in a clean shutdown, a power cut, or is this a haslerd restart?
    kind = "unknown" if args.fake else shutdown_kind(box_paths(args).state)
    log.info("last shutdown: %s", kind)
    if kind == "power-cut":
        # Counted on the card (power-cuts.json): the diagnostics card's "the
        # power was cut 4 times this month - a plug timer?" hint.
        cuts = note_power_cut(box_paths(args).state)
        log.info("power cuts counted: %d", cuts.get("total", 0))
    first_sound = FirstSound(amp_card(actions.player)) if kind in ("clean", "power-cut") else None
    publisher = HaPublisher(app)
    confirm = getattr(app, "confirm", None)
    software = getattr(app, "software", None)
    support = getattr(app, "support", None)
    ytdlp_lane = None if args.fake else getattr(app, "ytdlp_lane", None)
    connectivity = build_connectivity(config)
    # For /api/status (online) and /api/diagnostics (web/diagnostics.py).
    app.connectivity = connectivity
    app.shutdown_kind = kind
    app.first_sound = first_sound
    wifiwatch = build_wifiwatch(app, connectivity, args.fake)
    phone_volume = build_phone_volume(actions)
    keeper = None
    if driver is not None:
        keeper = LampKeeper(driver, actions, panel, connectivity, getattr(app, "settings_file", None))
        if software is not None and callable(getattr(software, "installing", None)):
            keeper.updating = software.installing    # the ember breath while it installs
        listeners = getattr(app, "lamp_listeners", None)
        if listeners is not None:
            listeners.append(keeper.settings_changed)
        saved = load_settings(getattr(app, "settings_file", None))
        keeper.apply_palette(saved)
        keeper.apply_prefs(saved)            # brightness, key replies (Settings -> Pilot lamp)
        keeper.apply_geek(saved)             # geek mode, callsign
        if not args.fake:
            state_dir = box_paths(args).state
            update_state = getattr(software, "state_file", None) if software is not None else None
            keeper.faults = lambda: lamp_faults.check(state_dir=state_dir, update_state=update_state, panel=panel)
            try:
                from . import health
                release = str(health._version())        # the running release, as "About this radio" says
            except Exception:
                release = ""
            if (release and lamp_faults.version_changed(state_dir / "lamp-version", release)
                    and lamp_faults.update_phase(update_state) in (None, "committed", "probation")):
                log.info("lamp: first start of %s - an update installed", release)
                keeper.celebrate()              # the green bloom and Morse OK, once
            if wifi_join_pending(app):
                log.info("lamp: started after a Wi-Fi join - busy until it is on it")
                keeper.joining()                # amber while it connects, never the red alert
        app.lamp = driver       # the Lamp colours page's preview (web/server.py)
        app.lamp_meaning = keeper.meaning    # /api/status "lamp"
        app.lamp_keeper = keeper             # /api/status "fault_reasons", "fault_cleared_at"
    stream_watch = None if args.fake else build_stream_watchdog(actions, app, keeper)
    sleep = getattr(app, "sleep", None)
    if keeper is not None:
        keeper.sleep = sleep
    log.info("haslerd up: %s, %d controls", actions.variant.name, len(panel.controls))

    running = True

    def stop(signum, _frame):
        nonlocal running
        log.info("signal %s - shutting down", signum)
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    watchdog = Watchdog()
    if keeper is not None:
        keeper.confirm = confirm    # a pending press request: the lamp beckons
        if confirm is not None and hasattr(keeper.state, "confirming"):
            confirm.lamp_beckons = True     # ... so the broker sends no "ha" kicks on top
        keeper.take_over()      # before READY: ExecStartPost's stop is the fallback
    # Ready before resuming: starting a playlist can take seconds (yt-dlp),
    # and systemd should not count that against the start timeout.
    watchdog.ready()

    period = 1.0 / POLL_HZ
    net_announced = False
    next_source_poll = 0.0
    next_lamp_tick = 0.0
    source = "radio"

    try:
        if keeper is not None:
            keeper.tick(time.monotonic())     # warm-up breath before anything blocks
        settle(panel, actions)
        if keeper is not None:
            keeper.arm()
        # The remembered preset comes back as soon as its player answers
        # (moOde starts MPD late), swelling up; not after a power cut at night.
        resume = boot_resume(actions, kind == "power-cut", getattr(app, "settings_file", None), keeper)

        while running:
            now = time.monotonic()

            events = panel.poll(now)
            take_downs = getattr(panel, "take_downs", None)
            if callable(take_downs):
                # A key going down: the lamp's dip now, ~20-40 ms after the
                # contact closed; the press's tap/hold/select does the rest.
                actions.dispatch_all(take_downs())
            if any(event.kind != "change" for event in events):
                resume.cancel("a hand on the radio")    # wheels don't count
            if confirm is not None:
                # A key press that confirms a page request (web/confirm.py)
                # is taken out here: it must not also play a preset.
                events = confirm.intercept(events)
            actions.dispatch_all(events)
            if not actions.standby:
                actions.last_active_at = now        # awake = in use (health.py's idle)

            connectivity.poll(now)
            if ytdlp_lane is not None:             # the network back after a long time: check yt-dlp
                ytdlp_lane.tick(bool(getattr(connectivity, "seen_up", False) and connectivity.online))
            if wifiwatch is not None:
                wifiwatch.poll(now)                # out of setup mode when the Wi-Fi is back
            if not net_announced and getattr(connectivity, "seen_up", False):
                net_announced = True
                network_up(actions)
                consume_wifi_joined(app, keeper)   # a join's restart: the lamp says OK
            if resume.pending and getattr(connectivity, "hotspot", False):
                # Setup mode (a join that didn't work, no Wi-Fi at all): nothing
                # to resume until a phone sets it up - CQ, never the resume's red.
                resume.cancel("setup mode")
            resume.tick(now)
            if sleep is not None:
                sleep.tick(now)                     # the sleep timer: fade, then standby
            if first_sound is not None:
                first_sound.tick(now)

            if keeper is not None:
                source = keeper.source      # the lamp polls it, on its own clock
            elif now >= next_source_poll:
                next_source_poll = now + SOURCE_POLL_S
                try:
                    source = actions.player.active_source()
                except Exception:
                    log.debug("could not read active source", exc_info=True)
            if resume.pending and actions.phone_has_it(source) and (
                source in RENDERERS or not resume.attempts
            ):
                # A phone cast before we got going: it has the radio now.
                # (Spotify only before our own first attempt - a failed
                # Spotify resume would otherwise read as a phone.)
                resume.cancel(f"a phone ({source})")
            actions.watch_cast(source, now)        # resume_after_cast, when a cast ends
            if source == "bluetooth" and actions.bluetooth is not None:
                actions.bluetooth.observe_async()   # the phone's "last used", off-thread
            if phone_volume is not None:            # the phone's slider is the radio's volume
                phone_volume.tick(now, source == "bluetooth" and not actions.standby)
            if actions.group is not None:
                actions.group.tick(now, source)     # multiroom: keep in touch (on its own thread)
            actions.watch_asleep(source)
            if (actions.standby or actions.pairing) and actions.phone_has_it(source) and not (
                actions.keeps_asleep(source)
                or (actions.standby and sleep is not None and sleep.keeps_asleep(source))
            ):
                # The phone wakes the radio: standby ends, nothing of ours
                # resumes. A phone that connected also ends the pairing breath.
                # (Not the phone that was playing when the radio went to standby:
                # it didn't start anything.)
                actions.wake_for_phone(source)
                actions.end_pairing(paired=True)

            if keeper is not None and now >= next_lamp_tick:
                next_lamp_tick = now + LAMP_TICK_S
                keeper.tick(now)

            if confirm is not None:
                confirm.tick(now)                   # the lamp asks again while waiting
            if software is not None:
                software.tick(now)                  # an approved update, once the radio rests
            publisher.tick(now)
            if support is not None:                 # Send to the makers: what waited for the internet, replies
                support.tick(now, bool(getattr(connectivity, "seen_up", False) and connectivity.online))
            watchdog.ping(now)
            time.sleep(max(0.0, period - (time.monotonic() - now)))
    finally:
        if not args.fake and not actions.shutting_down and system_stopping():
            # SIGTERM because the system is going down (moOde's menu, a
            # reboot): the ember breath while YouTube Music caches the
            # next boot's opener, as the knob's own shutdown does.
            actions.shutting_down = True
            if keeper is not None:
                keeper.refresh()
            actions.prepare_for_boot(wait_s=SHUTDOWN_PREPARE_S)
        if stream_watch is not None:
            stream_watch.close()
        panel.close()
        if web is not None:
            web.close()
        if keeper is not None:
            keeper.close()
        if getattr(actions, "dsp", None) is not None:
            actions.dsp.close()
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
