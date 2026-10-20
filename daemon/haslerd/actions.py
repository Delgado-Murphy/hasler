"""Panel events -> things that happen.

The mapping lives in the variant descriptor, keyed ``<control>.<kind>``:

    k3.tap:   {do: preset, n: 3}
    k4.hold:  {do: ha_scene, entity: scene.relax}
    pu.tap:   {do: cycle_source}

so rebinding a key is a config edit, and the Excelsior's interlocked bank binds
to the same verbs as the Hasler's momentary keys without either knowing about
the other.

Two robustness rules, both load-bearing:

* **An unmapped event is not an error.** Descriptors are written by hand and
  will be incomplete; a key that does nothing yet should do nothing, quietly.
* **A handler that raises must not take the panel down with it.** Home
  Assistant being unreachable, MPD restarting, a renderer misbehaving - none of
  that should make the next key press fail. Errors are logged and swallowed
  here, at the boundary.

Pressing the key that is already playing means "again": for a radio station
that is "turn it off" (a station has nothing else to offer), for a Spotify or
YouTube Music playlist it is "next track".

The pilot lamp is told through hooks, never imported: ``on_reply`` gets the
one-off replies of docs/lamp.md (a dip for a key, "next"/"stop" for the same
key again, two dips for Home Assistant, red pulses for a failure,
"source:<stop>[:<preset source>]" for the PU key - the lamp turns that into a
signature of where it went - "nudge" for a volume key at its end and "swell"
for a phone paired),
``on_start`` hears that a preset is about to start (the loading breath must be
up *before* a slow start blocks the loop) and ``on_change`` that standby,
pairing or shutdown flipped.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .ha import behaviour
from .panel.controls import Event
from .player.base import PRESET_SOURCES, Player, Preset
from .state import StateStore
from .variant import Variant

log = logging.getLogger(__name__)

# A repeat tap this soon after starting a preset is contact bounce, not "again".
AGAIN_GUARD_S = 0.8

# Order the PU key cycles through by default (``Actions.source_cycle``; the
# owner's own lives in settings.json ``sources.cycle``). "radio" is our MPD
# playback, "spotify" go-librespot, the rest moOde renderers.
SOURCE_CYCLE = ("radio", "bluetooth", "airplay", "spotify")

# Every stop the cycle can make. "radio", "ytm" and "spotify" resume the most
# recent preset of that kind; "radio" also covers YouTube Music presets unless
# "ytm" is a stop of its own. "spotify" with no preset, "bluetooth" and
# "airplay" clear the way for a phone to cast.
CYCLE_STOPS = ("radio", "ytm", "spotify", "bluetooth", "airplay")
CYCLE_LABELS = {
    "radio": "Radio",
    "ytm": "YouTube Music",
    "spotify": "Spotify",
    "bluetooth": "Bluetooth",
    "airplay": "AirPlay",
}

# A cast that ended stays ended this long before the radio takes the speaker
# back (``Actions.resume_after_cast``): a phone pausing must not count.
CAST_END_GRACE_S = 20.0

# Renderers a phone drives through moOde. A preset key has to take the audio
# path back from these explicitly; our own preset sources are arbitrated by the
# player's router instead.
RENDERERS = ("bluetooth", "airplay", "dlna")

# Event kinds that are a hand on a key: they get the lamp's dip. Wheels and the
# power knob get nothing - the ear is the feedback.
ACK_KINDS = ("tap", "select")
# Verbs that choose their own reply ("again" is next/stop, not a dip).
SELF_REPLYING = ("preset", "preset_by_index", "join_group", "cycle_source", "bluetooth")
HA_VERBS = ("ha_scene", "ha_scene_by_index", "ha_script", "ha_toggle", "ha_automation", "ha_light", "ha_service")
DEFAULT_PAIR_S = 120
# A held volume key repeats every 0.15 s (web/keymap.py) and a new press
# takes the 0.8 s hold to fire: a gap longer than this is a new press, which
# nudges again at the end of the scale.
VOLUME_NUDGE_GAP_S = 0.6
SHUTDOWN_PREPARE_S = 20.0   # YouTube Music's next-boot opener, before poweroff


# The owner's Bluetooth settings (settings.json ``bluetooth``, web/bluetooth_api.py).
# keys: while a phone plays over Bluetooth, the key whose tap is the
# ``bluetooth`` verb pressed again does ``again`` and held does ``hold``,
# both on the phone (AVRCP). reconnect: selecting Bluetooth, or waking a
# radio that slept on Bluetooth, connects the last phone used.
BT_AGAIN = ("next", "previous", "none")
BT_HOLD = ("pause", "next", "previous", "none")
BT_KEYS_DEFAULT = {"enabled": True, "again": "next", "hold": "pause"}
BT_RECONNECT_DEFAULT = {"on_select": True, "on_wake": True}


def bt_settings_from(raw: Any, current: dict | None = None) -> dict:
    """A submitted (partial) ``bluetooth`` settings doc merged onto ``current``,
    or ValueError saying why. -> {"keys": {...}, "reconnect": {...}}"""
    current = current or {}
    if not isinstance(raw, dict) or not raw or set(raw) - {"keys", "reconnect"}:
        raise ValueError("expected {\"keys\": {...}} and/or {\"reconnect\": {...}}")
    keys = dict(current.get("keys") or BT_KEYS_DEFAULT)
    reconnect = dict(current.get("reconnect") or BT_RECONNECT_DEFAULT)
    if "keys" in raw:
        k = raw["keys"]
        if not isinstance(k, dict) or set(k) - set(BT_KEYS_DEFAULT):
            raise ValueError("keys takes enabled, again and hold")
        if "enabled" in k and not isinstance(k["enabled"], bool):
            raise ValueError("keys.enabled must be true or false")
        if "again" in k and k["again"] not in BT_AGAIN:
            raise ValueError(f"keys.again must be one of: {', '.join(BT_AGAIN)}")
        if "hold" in k and k["hold"] not in BT_HOLD:
            raise ValueError(f"keys.hold must be one of: {', '.join(BT_HOLD)}")
        keys.update(k)
    if "reconnect" in raw:
        r = raw["reconnect"]
        if not isinstance(r, dict) or set(r) - set(BT_RECONNECT_DEFAULT):
            raise ValueError("reconnect takes on_select and on_wake")
        for name, value in r.items():
            if not isinstance(value, bool):
                raise ValueError(f"reconnect.{name} must be true or false")
        reconnect.update(r)
    return {"keys": keys, "reconnect": reconnect}


def cycle_from(raw: Any) -> tuple[str, ...]:
    """A submitted cycle -> tuple of known, distinct stops, or ValueError saying why."""
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("cycle must be a non-empty list of sources")
    out: list[str] = []
    for stop in raw:
        if stop not in CYCLE_STOPS:
            raise ValueError(f"unknown source {stop!r} (one of: {', '.join(CYCLE_STOPS)})")
        if stop in out:
            raise ValueError(f"{stop!r} is in the cycle twice")
        out.append(stop)
    return tuple(out)


def build_presets(specs: list[dict], variant_name: str = "") -> dict[int, Preset]:
    """Parse the ``presets:`` list, skipping (loudly) any entry that is broken.

    One typo in one preset must cost that key, not the whole box.
    """
    presets: dict[int, Preset] = {}
    for spec in specs:
        try:
            preset = Preset.from_spec(spec)
        except ValueError as exc:
            log.error("%s: ignoring preset: %s", variant_name, exc)
            continue
        if preset.source not in PRESET_SOURCES:
            log.error("%s: preset %s has unknown source %r", variant_name, preset.n, preset.source)
            continue
        presets[preset.n] = preset
    return presets


class Actions:
    """Dispatches panel events against a player, optionally poking HA."""

    def __init__(
        self,
        variant: Variant,
        player: Player,
        ha: Callable[[str, dict], None] | None = None,
        state: StateStore | None = None,
        on_play_error: Callable[[Exception], None] | None = None,
        on_reply: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.variant = variant
        self.player = player
        self.ha = ha
        self.state = state
        # Called when starting a preset raises (the lamp drops its loading breath).
        self.on_play_error = on_play_error
        # The lamp's one-off replies: "ack", "next", "stop", "fail", "ha",
        # "nudge", "swell", "source:<stop>[:<preset source>]" (and "group",
        # from multiroom/group.py).
        self.on_reply = on_reply
        # A preset is about to start; called before the (possibly slow) start.
        self.on_start: Callable[[Preset], None] | None = None
        # Standby, pairing or shutdown changed: the lamp should look again now,
        # before whatever blocking call comes next.
        self.on_change: Callable[[], None] | None = None
        self.clock = clock
        self._started_at = float("-inf")   # clock() when a preset last started
        # Wall clock (time.time) of the last start: which Hasler started most
        # recently, for another radio's join_group (multiroom/group.py).
        self.started_wall: float | None = None
        # multiroom.group.Group when multiroom is installed, else None. A
        # follower's preset keys, standby and the PU key go through it.
        self.group: Any = None
        # dsp.knobs.KnobFunctions when wired (main.py): the knobs' sound
        # effects. Without it set_tone & co. go to the player's tone command.
        self.dsp: Any = None
        # Booted after following a group (state.json "follower"): the boot
        # resume plays nothing - a follower comes back alone and silent.
        self._resume_blocked = False
        self.presets = build_presets(variant.presets, variant.name)
        self.standby = False
        self.shutting_down = False
        # time.monotonic() of the last hand on the radio or preset started;
        # health.py's "nothing played for 10 min" (the updater's idle) reads
        # it, and the main loop keeps it fresh while the radio is awake.
        self.last_active_at: float | None = None
        # Bluetooth discoverable until this clock time (the lamp's blue breath).
        self.pairing_until = 0.0
        self._last_preset: int | None = None
        # volume_step at an end: which (0/100) and when last, for one nudge per press.
        self._volume_end: float | None = None
        self._volume_end_at = float("-inf")
        # Preset numbers, most recent first - the PU cycle's memory per source.
        self._recent: list[int] = []
        # Where the PU cycle last put us, for renderers that are selected but
        # not yet connected (a phone has not cast yet, so nothing reports them).
        self._cycle_at: str | None = None
        # The PU cycle's stops, in order (settings.json ``sources.cycle``).
        self.source_cycle: tuple[str, ...] = SOURCE_CYCLE
        # Take the speaker back for the last preset once a phone's Bluetooth
        # or AirPlay cast has ended (settings.json ``sources.resume_after_cast``).
        self.resume_after_cast = True   # owner: back to the station 20 s after a cast ends
        # Phones over Bluetooth (settings.json ``bluetooth``; bt_settings_from).
        self.bt_keys: dict = dict(BT_KEYS_DEFAULT)
        self.bt_reconnect: dict = dict(BT_RECONNECT_DEFAULT)
        # What standby interrupted: "bluetooth" if the radio slept on it, so
        # waking goes back there (and calls the phone) rather than to a preset.
        self.standby_source: str | None = None
        self._bt_selected_at = float("-inf")
        # Bumped by every hand on the radio that chooses what plays (a preset,
        # the cycle, standby): a cast that ends after one of those is not ours
        # to follow with a resume.
        self._hand = 0
        self._cast: tuple[str, int] | None = None    # (renderer, _hand) while a phone has it
        self._cast_ended_at: float | None = None
        self._verbs: dict[str, Callable[[Event, dict], None]] = {
            "preset": self._preset,
            "preset_by_index": self._preset_by_index,
            "cycle_source": self._cycle_source,
            "bt_pair": self._bt_pair,
            "bluetooth": self._bluetooth,
            "standby": self._standby,
            "standby_toggle": self._standby_toggle,
            "resume": self._resume,
            "shutdown": self._shutdown,
            "set_volume": self._set_volume,
            "set_tone": self._set_tone,
            "set_treble": self._set_tone,
            "set_bass": self._set_tone,
            "knob_effect": self._set_tone,
            "mute": self._mute,
            "speaker_route": self._speaker_route,
            "volume_step": self._volume_step,
            "ha_scene": self._ha_scene,
            "ha_scene_by_index": self._ha_scene_by_index,
            "ha_script": self._ha_script,
            "ha_toggle": self._ha_toggle,
            "ha_automation": self._ha_automation,
            "ha_light": self._ha_light,
            "ha_service": self._ha_service,
            "join_group": self._join_group,
        }
        # (kind, entity) already reported as dropped for want of HA - say it
        # once, not on every press.
        self._ha_dropped: set[tuple[str, str]] = set()

    @property
    def pairing(self) -> bool:
        """The Bluetooth pairing window is open."""
        return self.clock() < self.pairing_until

    def end_pairing(self, paired: bool = False) -> None:
        """A phone connected (or the window is moot): stop the pairing breath.

        ``paired``: a phone connected while the window was open - one swell
        on the blue that follows.
        """
        if self.pairing_until:
            was_open = self.pairing
            self.pairing_until = 0.0
            self._changed()
            if paired and was_open:
                self._reply("swell")

    @property
    def cycle_position(self) -> str | None:
        """Where the PU cycle last put us - e.g. "bluetooth" before a phone connects."""
        return self._cycle_at

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, event: Event) -> None:
        if event.kind == "hold" and self._bt_hold(event):
            return
        spec = self.variant.actions.get(f"{event.control}.{event.kind}")
        if not spec:
            # A pot the descriptor gives an effect but no binding: its knob
            # still plays the effect (dsp/knobs.py), no code per cabinet.
            if event.kind == "change" and self.dsp is not None and self.dsp.handles(event.control):
                self._set_tone(event, {})
                return
            log.debug("unmapped event %s", event)
            return
        spec = dict(spec)
        verb = spec.pop("do", None)
        handler = self._verbs.get(verb)
        if handler is None:
            log.warning("unknown action verb %r for %s", verb, event)
            return
        # Keys at INFO so "did it see my press?" is answered by the journal;
        # wheel changes stream while turning, so they stay at DEBUG.
        log.log(logging.DEBUG if event.kind == "change" else logging.INFO, "%s -> %s", event, verb)
        if event.kind != "change":
            self.touch()
        if event.kind in ACK_KINDS and verb not in SELF_REPLYING and verb not in HA_VERBS:
            self._reply("ack")
        try:
            handler(event, spec)
        except Exception:
            # Never let a failing action break the panel loop.
            log.exception("action %r failed for %s", verb, event)
            if event.kind != "change":
                self._reply("fail")

    def touch(self) -> None:
        """Someone is using the radio now (health.py's idle clock)."""
        self.last_active_at = time.monotonic()

    def reply(self, name: str) -> None:
        """One of the lamp's replies, from outside a key press (the boot resume)."""
        self._reply(name)

    def _reply(self, name: str) -> None:
        if self.on_reply is None:
            return
        try:
            self.on_reply(name)
        except Exception:
            log.debug("on_reply hook failed", exc_info=True)

    def _changed(self) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception:
            log.debug("on_change hook failed", exc_info=True)

    def dispatch_all(self, events: list[Event]) -> None:
        for event in events:
            self.dispatch(event)

    # -- playback ---------------------------------------------------------

    def _play(self, n: int) -> None:
        preset = self.presets.get(n)
        if preset is None:
            log.warning("no preset %s in %s", n, self.variant.name)
            self._reply("ack")
            return
        group = self.group
        if group is not None and group.following:
            if group.follower_keys == "group":
                # The whole group changes: the leader plays it (and judges
                # "same key again" against what it plays).
                self._reply("ack")
                self._hand += 1
                group.forward_preset(preset)
                return
            group.leave("a preset key (follower_keys: leave)")
        # Asked once: on the real player this is an HTTP call plus shell-outs.
        active = self.player.active_source()
        current = self.player.current()
        # "Again": this key's preset is what we started, and its source is
        # still the one making sound (not ended, not taken over by a phone).
        if (
            not self.standby
            and current is not None
            and current.n == preset.n
            and current.source == preset.source
            and active == current.source
        ):
            # An old key's contacts bounce: on the bench one press arrived as
            # two taps ~0.2 s apart, and the second stopped the station it had
            # just started. Nobody means "stop" that fast.
            if self.clock() - self._started_at < AGAIN_GUARD_S:
                log.info("k-tap on preset %s %.2f s after starting it: ignored (bounce)",
                         preset.n, self.clock() - self._started_at)
                return
            if preset.source == "radio":
                self._reply("stop")
                self._standby(None, {})
            else:
                self._reply("next")
                self.player.next_track()
            return
        self._reply("ack")
        # A renderer holds the audio path; take it back before starting ours.
        if active in RENDERERS:
            self.player.select_source("radio")
        if self.standby:
            self._wake()
        self._remember(n)
        self._persist()
        self._start(preset)

    def play_forwarded(self, preset: Preset) -> None:
        """A follower's preset key, played here for the whole group (the leader).

        "Again" is judged by what plays (source and link, not the key number:
        the follower numbers its presets its own way): a radio station again
        is standby - which ends the group - and a playlist again is the next
        track. A preset that is also one of this radio's own is remembered as
        if pressed here; a foreign one plays without changing what this radio
        resumes after a power cut.
        """
        active = self.player.active_source()
        current = self.player.current()
        if (
            not self.standby
            and current is not None
            and current.source == preset.source
            and current.uri == preset.uri
            and active == current.source
        ):
            if self.clock() - self._started_at < AGAIN_GUARD_S:
                return
            if preset.source == "radio":
                self._reply("stop")
                self._standby(None, {})
            else:
                self._reply("next")
                self.player.next_track()
            return
        if active in RENDERERS:
            self.player.select_source("radio")
        if self.standby:
            self._wake()
        own = next((p for p in self.presets.values()
                    if p.source == preset.source and p.uri == preset.uri), None)
        if own is not None:
            self._remember(own.n)
            self._persist()
            preset = own
        else:
            self._cycle_at = None
        self._start(preset)

    def lead_preset(self, preset: Preset) -> None:
        """This radio takes its group over and plays its own preset for it
        (multiroom/group.py's handover: the leader lacked the source).

        As a press here: remembered, persisted (no longer a follower), the
        lamp's loading breath via ``on_start``. Raises if the start fails.
        """
        if self.player.active_source() in RENDERERS:
            self.player.select_source("radio")
        if self.standby:
            self._wake()
        if self.presets.get(preset.n) == preset:
            self._remember(preset.n)
        self._persist()
        self._start(preset, wait=True)

    def join_group_playback(self) -> None:
        """This radio now plays a leader's stream (multiroom/group.py).

        Awake (a sleeping radio added to a group wakes, as a phone wakes it),
        and state.json says "follower", so a power cut brings it back silent.
        The group's worker stops the local players and unmutes.
        """
        self.standby = False
        self._cycle_at = None
        self._hand += 1
        self.touch()
        self._changed()
        self._persist()

    def _remember(self, n: int) -> None:
        self._resume_blocked = False
        self._last_preset = n
        self._cycle_at = None
        self._recent = [n] + [m for m in self._recent if m != n]

    def _recent_preset(self, sources: tuple[str, ...]) -> Preset | None:
        for n in self._recent:
            preset = self.presets.get(n)
            if preset is not None and preset.source in sources:
                return preset
        return None

    def _start(self, preset: Preset, wait: bool = False) -> None:
        """Start a preset; on failure tell the lamp, then let dispatch log it.

        ``on_start`` first, so the lamp is already breathing while a slow
        start (yt-dlp expanding a playlist, MPD stopping the last stream)
        is under way. A player that can start in the background
        (``start_preset``, the real one) does so, so a key press returns at
        once and a quicker press supersedes it; failures then reach the lamp
        from its worker. ``wait`` is for callers that need the outcome (the
        boot resume, a group handover): it raises on failure.
        """
        self._started_at = self.clock()
        self.started_wall = time.time()
        self._hand += 1
        self.touch()
        if self.on_start is not None:
            try:
                self.on_start(preset)
            except Exception:
                log.debug("on_start hook failed", exc_info=True)
        start_soon = None if wait else getattr(self.player, "start_preset", None)
        if callable(start_soon):
            try:
                start_soon(preset, on_error=self._start_failed)
            except Exception as exc:
                self._play_failed(exc)
                raise
            return
        try:
            self.player.play_preset(preset)
        except Exception as exc:
            self._play_failed(exc)
            raise

    def _play_failed(self, exc: Exception) -> None:
        if self.on_play_error is not None:
            try:
                self.on_play_error(exc)
            except Exception:
                log.debug("on_play_error hook failed", exc_info=True)

    def _start_failed(self, exc: Exception) -> None:
        """A background start failed (the player's worker already logged it)."""
        self._play_failed(exc)
        self._reply("fail")

    def _preset(self, event, spec):
        self._play(int(spec["n"]))

    def _preset_by_index(self, event, spec):
        # Interlocked banks report a 0-based position; presets are 1-based.
        self._play(int(event.value) + 1)

    # -- sources ----------------------------------------------------------

    def cycle_availability(self, order: tuple[str, ...] | None = None) -> dict[str, str | None]:
        """Each stop -> None if the cycle can use it now, else why it is skipped.

        "radio" is always there. A preset source missing from the player, a
        stop whose presets are all gone, or a renderer switched off in moOde
        is skipped. A renderer is assumed on when moOde can't be read.
        """
        order = tuple(order or self.source_cycle)
        sources = getattr(self.player, "sources", None)
        renderers = getattr(self.player, "renderers", None)
        try:
            enabled = renderers.enabled() if renderers is not None else {}
        except Exception:
            log.debug("could not read moOde's renderer flags", exc_info=True)
            enabled = {}
        kinds = {p.source for p in self.presets.values()}
        out: dict[str, str | None] = {}
        for stop in CYCLE_STOPS:
            reason = None
            if stop in ("ytm", "spotify") and sources is not None and stop not in sources:
                reason = f"{CYCLE_LABELS[stop]} isn't set up on this radio"
            elif stop == "ytm" and "ytm" not in kinds:
                reason = "no YouTube Music presets"
            elif stop in ("bluetooth", "airplay") and enabled and not enabled.get(stop):
                reason = f"{CYCLE_LABELS[stop]} is switched off (Casting)"
            out[stop] = reason
        return out

    def _cycle_pool(self, stop: str, order: tuple[str, ...]) -> tuple[str, ...]:
        if stop == "radio":
            return ("radio",) if "ytm" in order else ("radio", "ytm")
        if stop in ("ytm", "spotify"):
            return (stop,)
        return ()

    def _cycle_position(self, order: tuple[str, ...]) -> str:
        active = self.player.active_source()
        current = self.player.current()
        if current is not None and active == current.source:
            # A YTM preset sits at the "radio" stop unless it has its own;
            # without this the cycle would not know where it is.
            if current.source == "spotify":
                return "spotify"
            return "ytm" if current.source == "ytm" and "ytm" in order else "radio"
        if current is None and active == "radio" and self._cycle_at:
            return self._cycle_at
        return active

    def _cycle_source(self, event, spec):
        """PU key: the next stop of ``source_cycle`` (default radio ->
        bluetooth -> airplay -> spotify -> radio), skipping stops that can't
        be used now (``cycle_availability``).

        The "radio" stop means our own MPD playback (radio, and YTM presets
        unless "ytm" is a stop of its own) and the "spotify" stop means
        go-librespot. Each resumes the most recent preset of its kind; with
        none, "spotify" just clears the way for a phone to cast, exactly as
        bluetooth and airplay do.
        """
        group = self.group
        if group is not None and group.following:
            # Sources and casting are this radio's own: out of the group first.
            group.leave("the source key")
        order = tuple(spec.get("order") or self.source_cycle) or SOURCE_CYCLE
        position = self._cycle_position(order)
        skip = self.cycle_availability(order)
        start = order.index(position) if position in order else -1
        nxt = "radio"
        for step in range(1, len(order) + 1):
            candidate = order[(start + step) % len(order)]
            if skip.get(candidate) is None:
                nxt = candidate
                break
        else:
            log.info("no source in the cycle %s can be used now; radio", list(order))
        self._hand += 1
        if self.standby:
            self._wake()
            self._persist()
        self.player.select_source("radio" if nxt == "ytm" else nxt)
        self._cycle_at = nxt
        if nxt == "bluetooth" and self.bt_reconnect.get("on_select"):
            self._bt_selected_at = self.clock()
            self.reconnect_phone()
        pool = self._cycle_pool(nxt, order)
        preset = self._recent_preset(pool) if pool else None
        # The lamp's signature of where we went (before a slow start blocks).
        self._reply(f"source:{nxt}" + (f":{preset.source}" if preset is not None else ""))
        if preset is not None:
            self._remember(preset.n)
            self._persist()
            self._start(preset)

    # -- a phone casting ending -------------------------------------------

    def watch_cast(self, source: str | None, now: float | None = None) -> bool:
        """The main loop's active source, every poll. True if it resumed.

        With ``resume_after_cast`` on, a Bluetooth or AirPlay cast that has
        ended for CAST_END_GRACE_S brings back the last preset - unless a
        hand on the radio chose something since the cast began, or it sleeps.
        Spotify Connect is left alone: go-librespot reports a pause exactly
        as it reports an end.
        """
        now = self.clock() if now is None else now
        if source in RENDERERS:
            if self._cast is None:
                self._cast = (source, self._hand)
            self._cast_ended_at = None
            return False
        if self._cast is None:
            return False
        if self._cast_ended_at is None:
            self._cast_ended_at = now
            return False
        if now - self._cast_ended_at < CAST_END_GRACE_S:
            return False
        renderer, hand = self._cast
        self._cast = self._cast_ended_at = None
        if not self.resume_after_cast or self.standby or hand != self._hand:
            return False
        if self._last_preset is None or self._last_preset not in self.presets:
            return False
        log.info("%s cast ended - back to preset %s", renderer, self._last_preset)
        self._cycle_at = None
        return self.resume_last(wait=False)   # on the main loop: don't hold it up

    # -- Bluetooth phones -------------------------------------------------

    @property
    def bluetooth(self) -> Any:
        """The player's phones (player/bluetooth.py), None where there are none."""
        return getattr(self.player, "bluetooth", None)

    def _in_bluetooth(self) -> bool:
        """Bluetooth is what the radio is on: a phone plays, or the owner chose it."""
        active = self.player.active_source()
        return active == "bluetooth" or (
            active == "radio" and self.player.current() is None and self._cycle_at == "bluetooth")

    def _phone_plays(self) -> bool:
        return not self.standby and self.player.active_source() == "bluetooth"

    def _bt_hold(self, event: Event) -> bool:
        """The hold of the key whose tap is ``bluetooth``, while a phone plays:
        the phone's pause/play (keys.hold) instead of the hold's own binding."""
        keys = self.bt_keys
        if not keys.get("enabled") or keys.get("hold", "none") == "none":
            return False
        tap = self.variant.actions.get(f"{event.control}.tap")
        if not isinstance(tap, dict) or tap.get("do") != "bluetooth":
            return False
        bt = self.bluetooth
        try:
            if bt is None or not self._phone_plays():
                return False
        except Exception:
            return False
        command = "toggle" if keys["hold"] == "pause" else keys["hold"]
        log.info("%s -> bluetooth %s (a phone plays)", event, command)
        self.touch()
        self._reply("ack")
        bt.control_async(command)
        return True

    def _bluetooth(self, event, spec):
        """A key for Bluetooth: selects it (and calls the last phone), and
        while a phone plays there, pressed again is the phone's next track
        (keys.again)."""
        group = self.group
        if group is not None and group.following:
            group.leave("the Bluetooth key")
        bt = self.bluetooth
        again = self.bt_keys.get("again", "none") if self.bt_keys.get("enabled") else "none"
        if bt is not None and again != "none" and self._phone_plays():
            if self.clock() - self._bt_selected_at < AGAIN_GUARD_S:
                return      # contact bounce, as for a preset key
            self._reply("next")
            bt.control_async(again)
            return
        if self.cycle_availability(("bluetooth",)).get("bluetooth") is not None:
            log.info("Bluetooth key: Bluetooth is switched off (Casting)")
            self._reply("fail")
            return
        self._select_bluetooth()

    def _select_bluetooth(self, reconnect: bool | None = None) -> None:
        """Hand the speaker to Bluetooth and, unless told not to, call the phone."""
        self._hand += 1
        if self.standby:
            self._wake()
            self._persist()
        self.player.select_source("bluetooth")
        self._cycle_at = "bluetooth"
        self._bt_selected_at = self.clock()
        self._reply("source:bluetooth")
        if self.bt_reconnect.get("on_select") if reconnect is None else reconnect:
            self.reconnect_phone()

    def reconnect_phone(self) -> bool:
        """Call the most recently used phone, off-thread, while Bluetooth stays
        chosen. The lamp shows its waiting blue meanwhile, as for any cast."""
        bt = self.bluetooth
        if bt is None:
            return False
        hand = self._hand

        def cancelled() -> bool:
            return self._hand != hand or self.standby or self._cycle_at != "bluetooth"
        try:
            return bool(bt.reconnect_async(cancelled))
        except Exception:
            log.exception("could not start the bluetooth reconnect")
            return False

    def _bt_pair(self, event, spec):
        seconds = int(spec.get("seconds", DEFAULT_PAIR_S))
        # Breathing blue before bluetoothctl's round trips, not after.
        self.pairing_until = self.clock() + seconds
        self._changed()
        try:
            self.player.bluetooth_pair(seconds)
        except Exception:
            self.pairing_until = 0.0
            self._changed()
            raise

    # -- power ------------------------------------------------------------

    def _wake(self) -> None:
        self.standby = False
        self._changed()
        self.player.set_mute(False)

    def phone_has_it(self, source: str | None = None) -> bool:
        """A phone renderer holds the audio: Bluetooth, AirPlay, DLNA, or
        Spotify Connect (go-librespot playing with none of our presets on)."""
        active = self.player.active_source() if source is None else source
        if active in RENDERERS:
            return True
        current = self.player.current()
        return active == "spotify" and (current is None or current.source != "spotify")

    def wake_for_phone(self, source: str) -> None:
        """A phone started casting while asleep: the phone wakes the radio.

        Standby ends and the mixer unmutes - and nothing of ours resumes: the
        phone is in charge (docs/lamp.md, "Phone casts while in standby").
        """
        if not self.standby:
            return
        log.info("%s started casting - waking from standby", source)
        self._cycle_at = None
        # The radio was off: when this cast ends it stays quiet, whatever
        # resume_after_cast says (-1 never matches _hand).
        self._cast = (source, -1)
        self._cast_ended_at = None
        self._wake()
        self._persist()

    def _standby(self, event, spec):
        """Silence, but stay on the network.

        Deliberately not a power cut: the box remains an AirPlay/Bluetooth
        target and an HA entity while the knob is off.
        """
        self.enter_standby()

    def enter_standby(self, persist: bool = True) -> None:
        """Standby; ``persist=False`` keeps it out of state.json.

        The boot resume's "power cut at night" sleeps without remembering it,
        so the next daytime power cut still comes back playing.
        """
        try:
            # No phones without the player's Bluetooth: no source read then.
            on_bt = self.bluetooth is not None and self._in_bluetooth()
        except Exception:
            on_bt = False
        self.standby_source = "bluetooth" if on_bt else None
        self.standby = True
        self._cycle_at = None
        self._hand += 1
        if persist:
            self._resume_blocked = False
        self._group_standby()
        self._changed()
        if persist:
            self._persist()
        # Silent at once; MPD's stop (seconds, sometimes) happens off the
        # press, and a key pressed meanwhile supersedes it.
        stop_soon = getattr(self.player, "stop_soon", None)
        if callable(stop_soon):
            self.player.set_mute(True)
            stop_soon()
        else:
            self.player.stop()
            self.player.set_mute(True)
        self.prepare_for_boot()

    def _group_standby(self) -> None:
        """Standby/shutdown: a follower leaves its group, a leader ends it."""
        group = self.group
        if group is None:
            return
        try:
            group.standby_entered()
        except Exception:
            log.exception("multiroom: leaving the group failed")

    def prepare_for_boot(self, wait_s: float = 0.0) -> bool:
        """Have YouTube Music put the next boot's opening track on disk.

        The preset is the one the box would resume (a radio preset is a no-op
        there; None = its last-played ytm preset). ``wait_s`` 0 returns at
        once (standby); shutdown waits. True if cached. Never raises.
        """
        ytm = (getattr(self.player, "sources", None) or {}).get("ytm")
        prepare = getattr(ytm, "prepare_for_boot", None)
        if not callable(prepare):
            return False
        preset = self.presets.get(self._last_preset) if self._last_preset is not None else None
        try:
            return bool(prepare(preset, wait_s=wait_s) if wait_s else prepare(preset))
        except Exception:
            log.exception("could not prepare YouTube Music for the next boot")
            return False

    def _standby_toggle(self, event, spec):
        """One key for both: asleep -> wake and play the last preset; else sleep."""
        if self.standby:
            self._wake()
            self._persist()
            self._resume_after_wake()
        else:
            self._standby(event, spec)

    def _resume(self, event, spec):
        if not self.standby and self.phone_has_it():
            # The phone woke the box while the knob was off; turning the knob
            # on now must not snatch the audio back for a preset.
            return
        self._wake()
        self._persist()
        self._resume_after_wake()

    def _resume_after_wake(self) -> None:
        """Back to what standby interrupted: Bluetooth (calling the last phone)
        if it slept there and reconnect.on_wake, else the last preset."""
        was, self.standby_source = self.standby_source, None
        if was == "bluetooth" and self.bt_reconnect.get("on_wake") and self.bluetooth is not None:
            log.info("waking into Bluetooth: calling the last phone")
            self._select_bluetooth(reconnect=True)
            return
        self.resume_last(wait=False)       # a hand on the radio: don't hold it up

    def _shutdown(self, event, spec):
        # The ember breath first: poweroff below can take seconds to return.
        self.shutting_down = True
        self._group_standby()
        self._changed()
        self.player.stop()
        # The lamp breathes on its own thread meanwhile.
        self.prepare_for_boot(wait_s=SHUTDOWN_PREPARE_S)
        self.player.shutdown()

    # -- persistence ------------------------------------------------------

    def _persist(self) -> None:
        if self.state is not None:
            data: dict[str, Any] = {"last_preset": self._last_preset, "standby": self.standby}
            if self.standby and self.standby_source:
                data["standby_source"] = self.standby_source
            group = self.group
            if group is not None and group.following:
                data["follower"] = True
            self.state.save(data)

    def restore(self) -> None:
        """Load what the box was doing before it lost power. Plays nothing.

        Standby is re-applied (stop + mute) because moOde may itself resume MPD
        at boot, and a box left off at the key must come back silent. Playing
        is a separate step, ``resume_last``, so the daemon can first let the
        power switch report a knob that is physically off.
        """
        if self.state is None:
            return
        data = self.state.load()
        last = data.get("last_preset")
        if isinstance(last, int) and last in self.presets:
            self._last_preset = last
            self._recent = [last]
        if data.get("follower") and not data.get("standby"):
            # It was playing a group's music: it comes back alone and silent,
            # not with this radio's own old preset (docs/using.md, Multiroom).
            log.info("was following a group before the restart: not resuming")
            self._resume_blocked = True
        if data.get("standby"):
            try:
                self._standby(None, {})
            except Exception:
                log.exception("could not re-apply standby at boot")
            if data.get("standby_source") == "bluetooth":
                self.standby_source = "bluetooth"
                self._persist()

    def resume_preset(self) -> Preset | None:
        """What ``resume_last`` would play now (None: standby, nothing kept, or
        it was following a group when it lost power)."""
        if self.standby or self._last_preset is None or self._resume_blocked:
            return None
        return self.presets.get(self._last_preset)

    def resume_last(self, quiet: bool = False, wait: bool = True) -> bool:
        """Play the remembered preset, unless in standby. Never raises.

        Returns False only if starting it failed, so the caller can try again
        once the network turns up; True means done, including "nothing to do".
        A failure gets the lamp's red pulses unless ``quiet`` (an attempt that
        will be retried - the pulses wait for the last one). ``wait=False``
        (a key, a cast ending) starts it in the background: True at once, and
        a failure reaches the lamp from the player's worker.
        """
        if self.standby or self._last_preset is None or self._resume_blocked:
            return True
        preset = self.presets.get(self._last_preset)
        if preset is None:
            return True
        try:
            self._start(preset, wait=wait)
        except Exception:
            log.exception("could not resume preset %s", preset.n)
            if not quiet:
                self._reply("fail")
            return False
        return True

    # -- wheels -----------------------------------------------------------

    def _set_volume(self, event, spec):
        self.player.set_volume(float(event.value))

    def _set_tone(self, event, spec):
        """set_tone / set_treble / set_bass / knob_effect: the knob's effect."""
        if self.dsp is not None:
            self.dsp.knob_moved(event.control, float(event.value))
        else:
            self.player.set_tone(event.control, float(event.value))

    def _volume_step(self, event, spec):
        """Nudge the volume relative to where it is now, on the knob scale.

        Starts from the player's reading, not from what we last set, because
        the moOde UI or a phone may have moved it since. If nobody knows the
        volume yet, step from ``from`` (default 40) - a guess, but a quiet one.
        """
        delta = float(spec.get("delta", 0))
        if not delta:
            return
        current = self.player.get_volume()
        if current is None:
            current = float(spec.get("from", 40.0))
        target = max(0.0, min(100.0, current + delta))
        self.player.set_volume(target)
        # At the floor or ceiling: one nudge as the key gets there, not one
        # per repeat while it is held (a new press after a pause nudges again).
        end = 100.0 if delta > 0 else 0.0
        now = self.clock()
        if target == end:
            if self._volume_end != end or now - self._volume_end_at > VOLUME_NUDGE_GAP_S:
                self._reply("nudge")
            self._volume_end = end
            self._volume_end_at = now
        else:
            self._volume_end = None

    def _mute(self, event, spec):
        self.player.set_mute(bool(spec.get("state", True)))

    def _speaker_route(self, event, spec):
        self.player.route_speaker(int(event.value))

    # -- home assistant ---------------------------------------------------

    # ``self.ha`` is swapped at runtime by web/ha_api.HaApi when the owner
    # connects or disconnects HA on the key page: read it once per press.
    # Kinds map to services in ha/rest.SERVICES: scene -> scene.turn_on,
    # script -> script.turn_on, toggle -> homeassistant.toggle,
    # automation -> automation.trigger; "light" and "service" carry a whole
    # behaviour (ha/behaviour.py: light.turn_on/off/toggle with brightness,
    # colour, white temperature and fade; a fenced generic service call).

    def _fire_ha(self, kind: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget. HA being down must never slow down a key press.

        The lamp gives two dips when the call goes out, red pulses when it
        cannot (not connected, or an entity this HA was seen not to have).
        A call that fails later, on the transport's own thread, is not seen.
        """
        ha = self.ha
        entities = behaviour.entity_ids(payload)
        if ha is None:
            key = (kind, ",".join(entities))
            level = logging.DEBUG if key in self._ha_dropped else logging.INFO
            self._ha_dropped.add(key)
            log.log(level, "Home Assistant not connected; %s %s does nothing", kind, key[1])
            self._reply("fail")
            return
        missing = getattr(ha, "missing", None) or ()
        if entities and all(e in missing for e in entities):
            self._reply("fail")
        else:
            self._reply("ha")
        ha(kind, payload)

    def _ha_scene(self, event, spec):
        payload = {"entity": spec["entity"]}
        if spec.get("transition") is not None:
            payload["transition"] = spec["transition"]
        self._fire_ha("scene", payload)

    def _ha_script(self, event, spec):
        self._fire_ha("script", {"entity": spec["entity"]})

    def _ha_toggle(self, event, spec):
        self._fire_ha("toggle", {"entity": spec["entity"]})

    def _ha_automation(self, event, spec):
        self._fire_ha("automation", {"entity": spec["entity"]})

    def _ha_light(self, event, spec):
        self._fire_ha("light", dict(spec))

    def _ha_service(self, event, spec):
        self._fire_ha("service", dict(spec))

    # -- multiroom ---------------------------------------------------------

    def _join_group(self, event, spec):
        """Join the group of the Hasler leading one (else the one playing);
        on a follower, leave. Red pulses with no multiroom (multiroom/group.py)."""
        group = self.group
        if group is None:
            log.info("join_group: multiroom isn't set up on this radio")
            self._reply("fail")
            return
        group.join_key()

    def _ha_scene_by_index(self, event, spec):
        scenes = spec.get("scenes") or self.variant.actions.get("_scenes", [])
        index = int(event.value)
        if 0 <= index < len(scenes):
            self._fire_ha("scene", {"entity": scenes[index]})
        else:
            log.debug("no scene bound to index %s", index)
            if event.kind in ACK_KINDS:
                self._reply("ack")
