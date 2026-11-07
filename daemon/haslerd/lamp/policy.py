"""What the lamp should say, given what the box is doing - docs/lamp.md.

Pure: ``look_for(BoxState) -> (Look, fade seconds)``. No clocks, no hardware,
so the priority order is a table of tests rather than a thing to watch on the
bench. The main loop gathers the BoxState (and keeps the timers: loading
since when, the phone's 3 s grace); the engine draws the result, applying
night levels, the transition rules, calibration and gamma on its own. A fade
of 0 means "the engine's standard transition" (rise 0.6 s, fall 1.2 s, hue
changes through the 10 % trough); only slow glides give their own length.

Priority, highest first (docs/lamp.md "State map"):

 1. shutting down, and installing an update: the slow ember breath (busy,
    don't unplug). The knob-off countdown itself is ``LampDriver.countdown``
    drawn by the engine; halted is dark.
 2. replies (kick, refuse, swell) - the engine's overlay, not a Look
 3. a confirm request: beckon until a key is pressed
 4. setup / hotspot: the patient amber beckon
 5. a phone casting (it beats standby: a phone wakes the radio)
 6. standby
 7. boot warm-up
 8. Bluetooth pairing window
 9. loading, and joining a group (the same breath)
10. following a group whose leader plays, playing / ready - glowing down in
    the sleep timer's last minute

Offline and a missing panel board are no longer idle states: the next press
that can't work refuses (and a missing panel refuses three times at boot).
"""

from __future__ import annotations

from .contract import (
    CAST_PAUSED_LEVEL, CASTING_LEVEL, CASTING_NIGHT, EMBER_LEVEL, EMBER_NIGHT, PLAYING_LEVEL,
    PLAYING_NIGHT, READY_LEVEL, READY_NIGHT, SETUP_REST_S, SLEEP_LEVEL, BoxState, Look, beckoning,
    breathing, steady,
)

# Kept for the keeper's timers: seconds without internet before a press may refuse.
OFFLINE_AFTER_S = 60.0

PLAYING = steady("amber", PLAYING_LEVEL, PLAYING_NIGHT)
READY = steady("amber", READY_LEVEL, READY_NIGHT)
STANDBY = steady("ember", EMBER_LEVEL, EMBER_NIGHT)
SHUTTING_DOWN = breathing("ember", 0.35, night=0.22, slow=True)
# Installing an update: the shutdown's breath, never a steady ember (which
# reads as "asleep" - and the box must not be unplugged now).
UPDATING = SHUTTING_DOWN
LOADING = breathing("amber", 0.55, night=0.35)
HOTSPOT = beckoning("amber", READY_LEVEL, READY_NIGHT, rest_s=SETUP_REST_S)
PAIRING = breathing("blue", CASTING_LEVEL, night=CASTING_NIGHT)
CASTING = steady("blue", CASTING_LEVEL, CASTING_NIGHT)
CAST_PAUSED = steady("blue", CAST_PAUSED_LEVEL, CAST_PAUSED_LEVEL)

# Warm-up breaths, the floor rising stage by stage so ~40 s of boot never
# looks frozen. "firmware"/"early" match the early boot service.
BOOT = {
    "firmware": breathing("amber", 0.45, night=0.35, floor=0.12),
    "early": breathing("amber", 0.45, night=0.35, floor=0.12),
    "daemon": breathing("amber", 0.45, night=0.35, floor=0.18),
    "network": breathing("amber", 0.50, night=0.35, floor=0.225),
}
# Knob off at power-on: ember, never amber.
BOOT_STANDBY = breathing("ember", 0.30, night=0.20)

STANDARD = 0.0          # the engine's transition rule
FADE_SLEEP = 20.0       # the sleep timer's last minute: a glow the room barely notices
# Kept names (callers and tests may still read them); all standard now.
FADE_PLAYING = FADE_READY = FADE_STANDBY = FADE_CAST = FADE_CAST_PAUSE = STANDARD
FADE_PAIRED = FADE_HOTSPOT = FADE_LOADING = FADE_BOOT = FADE_SHUTDOWN = FADE_ALERT = STANDARD


def playing_look(source: str) -> Look:
    """Amber, whatever the radio plays (playlists no longer wear a tint)."""
    return PLAYING


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


def offline_shows(state: BoxState) -> bool:
    """Offline is never an idle Look any more: a press that can't work refuses."""
    return False


def look_for(state: BoxState) -> tuple[Look, float]:
    """The Look the lamp should settle into, and how long to fade into it (0: standard)."""
    if state.halted:
        return Look(), STANDARD
    if state.shutting_down:
        return SHUTTING_DOWN, STANDARD
    if state.updating:
        return UPDATING, STANDARD
    if state.confirming:
        return confirm_look(_underneath(state)), STANDARD
    return _underneath(state)


def _underneath(state: BoxState) -> tuple[Look, float]:
    if state.hotspot:
        return HOTSPOT, STANDARD
    if state.casting:
        return (CAST_PAUSED if state.cast_paused else CASTING), STANDARD
    if state.standby:
        return (BOOT_STANDBY if state.boot != "done" else STANDBY), STANDARD
    if state.boot != "done" and not state.playing:
        return BOOT.get(state.boot, BOOT["daemon"]), STANDARD
    if state.pairing:
        return PAIRING, STANDARD
    if state.loading or state.group == "joining":
        return LOADING, STANDARD
    if state.group == "following" and state.group_playing:
        return sleepy(state, playing_look(state.group_source), STANDARD)
    if state.playing:
        return sleepy(state, playing_look(state.source), STANDARD)
    return sleepy(state, READY, STANDARD)


MEANINGS = ("playing", "ready", "asleep", "phone", "busy", "asking", "setup",
            "starting", "pairing", "offline")


def meaning_for(state: BoxState) -> str:
    """What the lamp is saying, in the page's words (/api/status "lamp").

    Follows look_for's priority, so the page names what the cabinet shows.
    "offline": idle without internet for a minute (the lamp itself shows
    ready - a press would refuse), or halted.
    """
    if state.halted:
        return "offline"
    if state.shutting_down or state.updating:
        return "busy"
    if state.confirming:
        return "asking"
    if state.hotspot:
        return "setup"
    if state.casting:
        return "phone"
    if state.standby:
        return "asleep"
    if state.boot != "done" and not state.playing:
        return "starting"
    if state.pairing:
        return "pairing"
    if state.loading or state.group == "joining":
        return "starting"
    if state.playing or (state.group == "following" and state.group_playing):
        return "playing"
    # The lamp shows ready, but the page can say why a press would refuse.
    return "offline" if state.offline_s >= OFFLINE_AFTER_S else "ready"


def confirm_look(under: tuple[Look, float]) -> Look:
    """A confirm request beckons in the colour on show, at least at the ready level."""
    look = under[0]
    if look.colour == "off" or look.level <= 0.0:
        return beckoning("amber", READY_LEVEL, READY_NIGHT)
    return beckoning(look.colour, look.level, look.night)


def sleepy(state: BoxState, look: Look, fade: float) -> tuple[Look, float]:
    """``look`` as it is, or glowing down to SLEEP_LEVEL while the sleep timer fades out."""
    if not state.sleeping:
        return look, fade
    night = min(look.night, SLEEP_LEVEL) if look.night is not None else None
    return Look(look.colour, min(look.level, SLEEP_LEVEL), night=night), FADE_SLEEP
