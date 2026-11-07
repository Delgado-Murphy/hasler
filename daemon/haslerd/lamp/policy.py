"""What the lamp should say, given what the box is doing - docs/lamp.md.

Pure: ``look_for(BoxState) -> (Look, fade seconds)``. No clocks, no hardware,
so the priority order is a table of tests rather than a thing to watch on the
bench. The main loop gathers the BoxState (and keeps the timers: loading
since when, the phone's 3 s grace); the engine draws the result, applying
night levels, the transition rules, calibration and gamma on its own. A fade
of 0 means "the engine's standard transition" (rise 0.6 s, fall 1.2 s, a
change to or from moon blue through the 10 % trough); only slow glides give
their own length.

The light language v2's state map, highest first (docs/lamp.md "State map";
the designed signals come from the signal table, lamp/signals.py):

 1. halted: dark - safe to unplug
 2. a fault that needs a maker: SOS in red (even in standby)
 3. busy - shutting down, installing, starting, loading, joining: the amber breath
    (the knob-off countdown itself is ``LampDriver.countdown``, drawn by the engine)
 4. setup needed (the hotspot): CQ in amber Morse
 5. a confirm request: the beckon
    (identify, update OK, the kick, the green bloom, the HAL eye, the volume
    sag and the group swell are the engine's overlays and replies, not Looks)
 6. no internet, while awake and idle: the red alert
 7. Bluetooth pairing: the blue breath
 8. a phone casting / paused: moon blue
 9. standby: ember, still
10. playing: the filament (gold 80 %, a tiny shimmer) / ready: amber 45 %, steady -
    glowing down in the sleep timer's last minute
"""

from __future__ import annotations

import dataclasses

from . import signals
from .contract import (
    CAST_PAUSED_LEVEL, CASTING_LEVEL, CASTING_NIGHT, EMBER_LEVEL, EMBER_NIGHT, NIGHT_CAP,
    PLAYING_LEVEL, PLAYING_NIGHT, READY_LEVEL, READY_NIGHT, SLEEP_LEVEL, BoxState, Look, steady,
)

# Seconds without internet before the idle lamp says so (the red alert), and
# before a press that needs it may refuse.
OFFLINE_AFTER_S = 60.0

# Playing: an incandescent filament (gold, 80 %, a tiny irregular shimmer) -
# the signal table's "playing".
PLAYING = signals.DEFAULTS["playing"]
READY = steady("amber", READY_LEVEL, READY_NIGHT)
STANDBY = steady("ember", EMBER_LEVEL, EMBER_NIGHT)
CASTING = steady("blue", CASTING_LEVEL, CASTING_NIGHT)
CAST_PAUSED = steady("blue", CAST_PAUSED_LEVEL, CAST_PAUSED_LEVEL)
# The designed signals: these names are the table's defaults (look_for asks
# the table, so a remap shows at once).
BUSY = signals.DEFAULTS["busy"]
SHUTTING_DOWN = UPDATING = LOADING = BUSY      # busy is one amber breath, never ember
HOTSPOT = SETUP = signals.DEFAULTS["setup"]
PAIRING = signals.DEFAULTS["pairing"]
NO_INTERNET = signals.DEFAULTS["no_internet"]
FAULT = signals.DEFAULTS["fault"]
# Boot is busy too: the same breath at every stage, and with the knob off
# (asleep never moves - the box is busy starting).
BOOT = {stage: BUSY for stage in ("firmware", "early", "daemon", "network")}
BOOT_STANDBY = BUSY

STANDARD = 0.0          # the engine's transition rule
FADE_SLEEP = 20.0       # the sleep timer's last minute: a glow the room barely notices
# Kept names (callers and tests may still read them); all standard now.
FADE_PLAYING = FADE_READY = FADE_STANDBY = FADE_CAST = FADE_CAST_PAUSE = STANDARD
FADE_PAIRED = FADE_HOTSPOT = FADE_LOADING = FADE_BOOT = FADE_SHUTDOWN = FADE_ALERT = STANDARD


def playing_look(source: str) -> Look:
    """The filament, whatever the radio plays (playlists wear no tint of their own)."""
    return signals.get("playing")


def source_signature(stop: str, preset_source: str | None, phone_there: bool = False) -> str:
    """The PU key's reply: a kick (the hue change to the destination is the Look's)."""
    return "kick"


def reply_for(text: str, phone_there: bool = False) -> str:
    """The rest of haslerd's reply words -> what the engine draws.

    Actions and the multiroom group speak in events, never in colours:
    ``"source:<stop>[:<preset source>]"`` (the PU key went there) is a kick,
    ``"group"`` (a radio joined or left this radio's group) a swell;
    everything else passes through unchanged (the engine maps old words).
    """
    if text == "group":
        return "swell"
    if text.startswith("source:"):
        _, stop, preset_source = (text.split(":", 2) + [""])[:3]
        return source_signature(stop, preset_source or None, phone_there)
    return text


def _busy(state: BoxState) -> bool:
    """Busy - don't unplug / wait: shutting down, installing, starting (also with the
    knob off), loading a preset or joining a group (not in standby or while a phone
    plays: then nothing of ours is loading)."""
    return (state.shutting_down or state.updating or (state.boot != "done" and not state.playing)
            or ((state.loading or state.group == "joining") and not state.standby and not state.casting))


def _fault(state: BoxState) -> bool:
    return state.fault or state.panel_missing


def _setup(state: BoxState) -> bool:
    return state.hotspot or state.setup


def idle_offline(state: BoxState) -> bool:
    """No internet, and the lamp may say so: awake and idle (never while music or a
    phone plays, never in standby, never while pairing or setting up)."""
    return ((state.offline or state.offline_s >= OFFLINE_AFTER_S) and not state.standby and not state.playing
            and not state.casting and not state.pairing and not _setup(state)
            and not (state.group == "following" and state.group_playing))


def offline_shows(state: BoxState) -> bool:
    """Whether the lamp shows the red alert for no internet now."""
    return (idle_offline(state) and not _busy(state) and not state.confirming
            and not _fault(state) and not state.halted)


def look_for(state: BoxState) -> tuple[Look, float]:
    """The Look the lamp should settle into, and how long to fade into it (0: standard)."""
    if state.halted:
        return Look(), STANDARD
    if _fault(state):
        return signals.get("fault"), STANDARD
    if _busy(state):
        return signals.get("busy"), STANDARD
    if _setup(state):
        return signals.get("setup"), STANDARD
    if state.confirming:
        return confirm_look(_underneath(state)), STANDARD
    return _underneath(state)


def _underneath(state: BoxState) -> tuple[Look, float]:
    if idle_offline(state):
        return signals.get("no_internet"), STANDARD
    if state.pairing:
        return signals.get("pairing"), STANDARD
    if state.casting:
        return (CAST_PAUSED if state.cast_paused else CASTING), STANDARD
    if state.standby:
        return STANDBY, STANDARD
    if state.group == "following" and state.group_playing:
        return sleepy(state, playing_look(state.group_source), STANDARD)
    if state.playing:
        return sleepy(state, playing_look(state.source), STANDARD)
    return sleepy(state, READY, STANDARD)


MEANINGS = ("playing", "ready", "asleep", "phone", "busy", "asking", "setup",
            "starting", "pairing", "no_internet", "fault", "offline", "off")


def meaning_for(state: BoxState) -> str:
    """What the lamp is saying, in the page's words (/api/status "lamp").

    Follows look_for's priority, so the page names what the cabinet shows.
    "off": halted, the lamp is dark. "no_internet": the red alert (idle and
    awake). "offline" is no longer said (kept in MEANINGS for old pages).
    """
    if state.halted:
        return "off"
    if _fault(state):
        return "fault"
    if state.shutting_down or state.updating:
        return "busy"
    if _busy(state):
        return "starting"
    if _setup(state):
        return "setup"
    if state.confirming:
        return "asking"
    if idle_offline(state):
        return "no_internet"
    if state.pairing:
        return "pairing"
    if state.casting:
        return "phone"
    if state.standby:
        return "asleep"
    if state.playing or (state.group == "following" and state.group_playing):
        return "playing"
    return "ready"


def events_for(before: BoxState | None, now: BoxState) -> list[str]:
    """One-shot replies for what just happened between two ticks: an update
    installed -> ``"update_ok"`` (the bloom and OK; Close Encounters in geek mode)."""
    if now.update_ok and not (before is not None and before.update_ok):
        return ["update_ok"]
    return []


def confirm_look(under: tuple[Look, float]) -> Look:
    """A confirm request beckons in the colour on show (amber over ember or a moving
    look: asleep never moves), on at most the ready level - room for the kicks."""
    ask = signals.get("asking")
    look = under[0]
    if look.colour in ("off", "ember") or look.level <= 0.0 or look.motion != "solid":
        return ask
    night = look.night if look.night is not None else min(look.level, NIGHT_CAP)
    return dataclasses.replace(ask, colour=look.colour, level=min(look.level, READY_LEVEL),
                               night=min(night, READY_NIGHT))


def sleepy(state: BoxState, look: Look, fade: float) -> tuple[Look, float]:
    """``look`` as it is, or glowing down to SLEEP_LEVEL while the sleep timer fades out."""
    if not state.sleeping:
        return look, fade
    night = min(look.night, SLEEP_LEVEL) if look.night is not None else None
    colour = "amber" if look.colour == "gold" else look.colour     # the filament cools to a steady glow
    return Look(colour, min(look.level, SLEEP_LEVEL), night=night), FADE_SLEEP
