"""What the lamp should say, given what the box is doing - docs/lamp.md.

Pure: ``look_for(BoxState) -> (Look, fade seconds)``. No clocks, no hardware,
so the priority order is a table of tests rather than a thing to watch on the
bench. The main loop gathers the BoxState (and keeps the timers: offline for
how long, loading since when, the phone's 3 s grace); the engine draws the
result, applying night caps, gamma and colour balance on its own.

Priority, highest first (docs/lamp.md "Priority"):

 1. shutting down (the knob-off countdown itself is ``LampDriver.countdown``,
    drawn by the engine over the standby ember), and installing an update
    (the same ember breath: busy, don't unplug)
 2. short replies - the engine's overlay, not a Look
 3. setup / hotspot
 4. a phone casting (it beats standby: a phone wakes the radio)
 5. standby
 6. boot warm-up
 7. Bluetooth pairing window
 8. loading, and joining a group (the same breath)
 9. following a group whose leader plays (the leader's tint; it plays, so
    no idle red)
10. offline idle red (only when the next key press would fail), and the
    missing panel board
11. playing / ready
"""

from __future__ import annotations

from .contract import BoxState, Colour, Look, signature

# Seconds without internet before the lamp admits it: blips stay invisible.
OFFLINE_AFTER_S = 60.0

# Sources whose presets wear the pale amber tint; radio stays plain amber.
PLAYLIST_SOURCES = ("ytm", "spotify")

READY = Look("amber", 0.35)
LOADING = Look("amber", 0.70, "breath", 1.2, 0.25)
STANDBY = Look("ember", 0.06)
SHUTTING_DOWN = Look("ember", 0.20, "breath", 2.5, 0.04)
# Installing an update: the shutdown's breath, never a solid ember (which
# reads as "asleep" - and the box must not be unplugged now).
UPDATING = SHUTTING_DOWN
HOTSPOT = Look("white", 0.60, "breath", 3.0, 0.15)
PAIRING = Look("blue", 0.60, "breath", 2.0, 0.15)
CASTING = Look("blue", 0.80)
CAST_PAUSED = Look("blue", 0.30)
WAITING_FOR_PHONE = 0.30    # the PU key's blue signature before a phone is there
OFFLINE = Look("red", 0.30, "breath", 4.0, 0.10)
PANEL_MISSING = Look("red", 0.30, "breath", 6.0, 0.10)

# Warm-up breaths, 3 s period, the floor rising stage by stage so ~40 s of
# boot never looks frozen. "firmware"/"early" match the early boot service.
BOOT = {
    "firmware": Look("amber", 0.35, "breath", 3.0, 0.10),
    "early": Look("amber", 0.35, "breath", 3.0, 0.10),
    "daemon": Look("amber", 0.35, "breath", 3.0, 0.20),
    "network": Look("amber", 0.50, "breath", 3.0, 0.30),
}
# Knob off at power-on: ember, never amber.
BOOT_STANDBY = Look("ember", 0.08, "breath", 3.0, 0.02)

# Fades into each look (seconds), docs/lamp.md "Behaviour".
FADE_PLAYING = 1.5
FADE_READY = 1.5
FADE_STANDBY = 1.5
FADE_CAST = 1.5
FADE_CAST_PAUSE = 0.8      # blue 80 % <-> 30 % as the phone pauses/plays
FADE_PAIRED = 1.0          # pairing breath -> solid blue on success
FADE_HOTSPOT = 2.0
FADE_LOADING = 0.3
FADE_BOOT = 1.0
FADE_SHUTDOWN = 0.5
FADE_ALERT = 1.0


def playing_look(source: str) -> Look:
    colour: Colour = "amber_pale" if source in PLAYLIST_SOURCES else "amber"
    return Look(colour, 1.0)


def source_signature(stop: str, preset_source: str | None, phone_there: bool = False) -> str:
    """The PU key's reply: a signature in the colour of where the cycle went.

    ``stop`` is the cycle stop, ``preset_source`` the source of the preset it
    resumes (None: nothing to resume - a phone's turn). Our own music is
    amber (pale for playlists); a phone's stop is blue, dim (30 %) while no
    phone is there yet.
    """
    if preset_source is not None:
        return signature("amber_pale" if preset_source in PLAYLIST_SOURCES else "amber")
    if stop in ("radio", "ytm"):
        return signature("amber_pale" if stop in PLAYLIST_SOURCES else "amber")
    return signature("blue") if phone_there else signature("blue", WAITING_FOR_PHONE)


def reply_for(text: str, phone_there: bool = False) -> str:
    """The rest of haslerd's reply words -> what the engine draws.

    Actions and the multiroom group speak in events, never in colours:
    ``"source:<stop>[:<preset source>]"`` (the PU key went there) and
    ``"group"`` (a radio joined or left this radio's group) become
    signatures here; everything else passes through unchanged.
    """
    if text == "group":
        return signature("magenta")
    if text.startswith("source:"):
        _, stop, preset_source = (text.split(":", 2) + [""])[:3]
        return source_signature(stop, preset_source or None, phone_there)
    return text


def offline_shows(state: BoxState) -> bool:
    """Idle red: only when the next key press would fail, and never at night."""
    return (
        state.offline_s >= OFFLINE_AFTER_S
        and not state.night
        and not state.casting
        and not state.playing
    )


def look_for(state: BoxState) -> tuple[Look, float]:
    """The Look the lamp should settle into, and how long to fade into it."""
    if state.shutting_down:
        return SHUTTING_DOWN, FADE_SHUTDOWN
    if state.updating:
        return UPDATING, FADE_SHUTDOWN
    if state.hotspot:
        return HOTSPOT, FADE_HOTSPOT
    if state.casting:
        if state.cast_paused:
            return CAST_PAUSED, FADE_CAST_PAUSE
        return CASTING, FADE_PAIRED if state.pairing else FADE_CAST
    if state.standby:
        if state.boot != "done":
            return BOOT_STANDBY, FADE_BOOT
        return STANDBY, FADE_STANDBY
    if state.boot != "done" and not state.playing:
        return BOOT.get(state.boot, BOOT["daemon"]), FADE_BOOT
    if state.pairing:
        return PAIRING, FADE_LOADING
    if state.loading or state.group == "joining":
        return LOADING, FADE_LOADING
    if state.group == "following" and state.group_playing:
        return playing_look(state.group_source), FADE_PLAYING
    if offline_shows(state):
        return OFFLINE, FADE_ALERT
    if state.panel_missing and not state.playing:
        return PANEL_MISSING, FADE_ALERT
    if state.playing:
        return playing_look(state.source), FADE_PLAYING
    return READY, FADE_READY
