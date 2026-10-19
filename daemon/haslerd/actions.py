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
key again, two dips for Home Assistant, red pulses for a failure),
``on_start`` hears that a preset is about to start (the loading breath must be
up *before* a slow start blocks the loop) and ``on_change`` that standby,
pairing or shutdown flipped.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .panel.controls import Event
from .player.base import PRESET_SOURCES, Player, Preset
from .state import StateStore
from .variant import Variant

log = logging.getLogger(__name__)

# Order the PU key cycles through. "radio" is MPD; the rest are moOde renderers.
SOURCE_CYCLE = ("radio", "bluetooth", "airplay", "spotify")

# Renderers a phone drives through moOde. A preset key has to take the audio
# path back from these explicitly; our own preset sources are arbitrated by the
# player's router instead.
RENDERERS = ("bluetooth", "airplay", "dlna")

# Event kinds that are a hand on a key: they get the lamp's dip. Wheels and the
# power knob get nothing - the ear is the feedback.
ACK_KINDS = ("tap", "select")
# Verbs that choose their own reply ("again" is next/stop, not a dip).
SELF_REPLYING = ("preset", "preset_by_index")
HA_VERBS = ("ha_scene", "ha_scene_by_index", "ha_script", "ha_toggle", "ha_automation")
DEFAULT_PAIR_S = 120


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
        # The lamp's one-off replies: "ack", "next", "stop", "fail", "ha".
        self.on_reply = on_reply
        # A preset is about to start; called before the (possibly slow) start.
        self.on_start: Callable[[Preset], None] | None = None
        # Standby, pairing or shutdown changed: the lamp should look again now,
        # before whatever blocking call comes next.
        self.on_change: Callable[[], None] | None = None
        self.clock = clock
        self.presets = build_presets(variant.presets, variant.name)
        self.standby = False
        self.shutting_down = False
        # Bluetooth discoverable until this clock time (the lamp's blue breath).
        self.pairing_until = 0.0
        self._last_preset: int | None = None
        # Preset numbers, most recent first - the PU cycle's memory per source.
        self._recent: list[int] = []
        # Where the PU cycle last put us, for renderers that are selected but
        # not yet connected (a phone has not cast yet, so nothing reports them).
        self._cycle_at: str | None = None
        self._verbs: dict[str, Callable[[Event, dict], None]] = {
            "preset": self._preset,
            "preset_by_index": self._preset_by_index,
            "cycle_source": self._cycle_source,
            "bt_pair": self._bt_pair,
            "standby": self._standby,
            "standby_toggle": self._standby_toggle,
            "resume": self._resume,
            "shutdown": self._shutdown,
            "set_volume": self._set_volume,
            "set_tone": self._set_tone,
            "set_treble": self._set_tone,
            "set_bass": self._set_tone,
            "mute": self._mute,
            "speaker_route": self._speaker_route,
            "volume_step": self._volume_step,
            "ha_scene": self._ha_scene,
            "ha_scene_by_index": self._ha_scene_by_index,
            "ha_script": self._ha_script,
            "ha_toggle": self._ha_toggle,
            "ha_automation": self._ha_automation,
        }
        # (kind, entity) already reported as dropped for want of HA - say it
        # once, not on every press.
        self._ha_dropped: set[tuple[str, str]] = set()

    @property
    def pairing(self) -> bool:
        """The Bluetooth pairing window is open."""
        return self.clock() < self.pairing_until

    def end_pairing(self) -> None:
        """A phone connected (or the window is moot): stop the pairing breath."""
        if self.pairing_until:
            self.pairing_until = 0.0
            self._changed()

    @property
    def cycle_position(self) -> str | None:
        """Where the PU cycle last put us - e.g. "bluetooth" before a phone connects."""
        return self._cycle_at

    # -- dispatch ---------------------------------------------------------

    def dispatch(self, event: Event) -> None:
        spec = self.variant.actions.get(f"{event.control}.{event.kind}")
        if not spec:
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
        if event.kind in ACK_KINDS and verb not in SELF_REPLYING and verb not in HA_VERBS:
            self._reply("ack")
        try:
            handler(event, spec)
        except Exception:
            # Never let a failing action break the panel loop.
            log.exception("action %r failed for %s", verb, event)
            if event.kind != "change":
                self._reply("fail")

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

    def _remember(self, n: int) -> None:
        self._last_preset = n
        self._cycle_at = None
        self._recent = [n] + [m for m in self._recent if m != n]

    def _recent_preset(self, sources: tuple[str, ...]) -> Preset | None:
        for n in self._recent:
            preset = self.presets.get(n)
            if preset is not None and preset.source in sources:
                return preset
        return None

    def _start(self, preset: Preset) -> None:
        """Start a preset; on failure tell the lamp, then let dispatch log it.

        ``on_start`` first, so the lamp is already breathing while a slow
        start (yt-dlp expanding a playlist) holds up the loop.
        """
        if self.on_start is not None:
            try:
                self.on_start(preset)
            except Exception:
                log.debug("on_start hook failed", exc_info=True)
        try:
            self.player.play_preset(preset)
        except Exception as exc:
            if self.on_play_error is not None:
                try:
                    self.on_play_error(exc)
                except Exception:
                    log.debug("on_play_error hook failed", exc_info=True)
            raise

    def _preset(self, event, spec):
        self._play(int(spec["n"]))

    def _preset_by_index(self, event, spec):
        # Interlocked banks report a 0-based position; presets are 1-based.
        self._play(int(event.value) + 1)

    # -- sources ----------------------------------------------------------

    def _cycle_source(self, event, spec):
        """PU key: radio -> bluetooth -> airplay -> spotify -> radio.

        The "radio" stop means our own MPD playback (radio or YTM presets) and
        the "spotify" stop means go-librespot. Each resumes the most recent
        preset of its kind; with none, "spotify" just clears the way for a
        phone to cast, exactly as bluetooth and airplay do.
        """
        order = spec.get("order", SOURCE_CYCLE)
        active = self.player.active_source()
        current = self.player.current()
        if current is not None and active == current.source:
            # A YTM preset sits at the "radio" stop; without this the cycle
            # would not know where it is.
            active = "spotify" if current.source == "spotify" else "radio"
        elif current is None and active == "radio" and self._cycle_at:
            active = self._cycle_at
        try:
            nxt = order[(order.index(active) + 1) % len(order)]
        except ValueError:
            nxt = order[0]
        if self.standby:
            self._wake()
            self._persist()
        self.player.select_source(nxt)
        self._cycle_at = nxt
        preset = None
        if nxt == "radio":
            preset = self._recent_preset(("radio", "ytm"))
        elif nxt == "spotify":
            preset = self._recent_preset(("spotify",))
        if preset is not None:
            self._remember(preset.n)
            self._persist()
            self._start(preset)

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
        self._wake()
        self._persist()

    def _standby(self, event, spec):
        """Silence, but stay on the network.

        Deliberately not a power cut: the box remains an AirPlay/Bluetooth
        target and an HA entity while the knob is off.
        """
        self.standby = True
        self._cycle_at = None
        self._changed()
        self._persist()
        self.player.stop()
        self.player.set_mute(True)

    def _standby_toggle(self, event, spec):
        """One key for both: asleep -> wake and play the last preset; else sleep."""
        if self.standby:
            self._wake()
            self._persist()
            self.resume_last()
        else:
            self._standby(event, spec)

    def _resume(self, event, spec):
        if not self.standby and self.phone_has_it():
            # The phone woke the box while the knob was off; turning the knob
            # on now must not snatch the audio back for a preset.
            return
        self._wake()
        self._persist()
        self.resume_last()

    def _shutdown(self, event, spec):
        # The ember breath first: poweroff below can take seconds to return.
        self.shutting_down = True
        self._changed()
        self.player.stop()
        self.player.shutdown()

    # -- persistence ------------------------------------------------------

    def _persist(self) -> None:
        if self.state is not None:
            self.state.save({"last_preset": self._last_preset, "standby": self.standby})

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
        if data.get("standby"):
            try:
                self._standby(None, {})
            except Exception:
                log.exception("could not re-apply standby at boot")

    def resume_last(self, quiet: bool = False) -> bool:
        """Play the remembered preset, unless in standby. Never raises.

        Returns False only if starting it failed, so the caller can try again
        once the network turns up; True means done, including "nothing to do".
        A failure gets the lamp's red pulses unless ``quiet`` (an attempt that
        will be retried - the pulses wait for the last one).
        """
        if self.standby or self._last_preset is None:
            return True
        preset = self.presets.get(self._last_preset)
        if preset is None:
            return True
        try:
            self._start(preset)
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
        self.player.set_volume(max(0.0, min(100.0, current + delta)))

    def _mute(self, event, spec):
        self.player.set_mute(bool(spec.get("state", True)))

    def _speaker_route(self, event, spec):
        self.player.route_speaker(int(event.value))

    # -- home assistant ---------------------------------------------------

    # ``self.ha`` is swapped at runtime by web/ha_api.HaApi when the owner
    # connects or disconnects HA on the key page: read it once per press.
    # Kinds map to services in ha/rest.SERVICES: scene -> scene.turn_on,
    # script -> script.turn_on, toggle -> homeassistant.toggle,
    # automation -> automation.trigger.

    def _fire_ha(self, kind: str, payload: dict[str, Any]) -> None:
        """Fire-and-forget. HA being down must never slow down a key press.

        The lamp gives two dips when the call goes out, red pulses when it
        cannot (not connected, or an entity this HA was seen not to have).
        A call that fails later, on the transport's own thread, is not seen.
        """
        ha = self.ha
        if ha is None:
            key = (kind, str(payload.get("entity", "")))
            level = logging.DEBUG if key in self._ha_dropped else logging.INFO
            self._ha_dropped.add(key)
            log.log(level, "Home Assistant not connected; %s %s does nothing", kind, key[1])
            self._reply("fail")
            return
        if payload.get("entity") in (getattr(ha, "missing", None) or ()):
            self._reply("fail")
        else:
            self._reply("ha")
        ha(kind, payload)

    def _ha_scene(self, event, spec):
        self._fire_ha("scene", {"entity": spec["entity"]})

    def _ha_script(self, event, spec):
        self._fire_ha("script", {"entity": spec["entity"]})

    def _ha_toggle(self, event, spec):
        self._fire_ha("toggle", {"entity": spec["entity"]})

    def _ha_automation(self, event, spec):
        self._fire_ha("automation", {"entity": spec["entity"]})

    def _ha_scene_by_index(self, event, spec):
        scenes = spec.get("scenes") or self.variant.actions.get("_scenes", [])
        index = int(event.value)
        if 0 <= index < len(scenes):
            self._fire_ha("scene", {"entity": scenes[index]})
        else:
            log.debug("no scene bound to index %s", index)
            if event.kind in ACK_KINDS:
                self._reply("ack")
