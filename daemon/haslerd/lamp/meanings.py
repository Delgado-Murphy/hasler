"""What the lamp means, in words - for the page, never for the LED.

The owner's side of the lamp (Settings -> Pilot lamp, the front page's
"Lamp: ..." line): a short list of *meanings*, each a name the page can show
and ask the lamp to play. The engine draws them; this file only names them
and says which one the lamp shows now, so a colour-blind owner can read it.

Two kinds:

* **looks** - states the lamp settles into (``LOOKS``);
* **replies** - one-off answers drawn over a look (``REPLIES``).

``meaning_of(BoxState)`` follows the policy's priority order
(lamp/policy.py ``look_for``) but answers a word, not a colour, so it
survives a change of palette or light language. A policy that knows better
can expose ``meaning_for(state) -> str`` and it is used instead.

``STATUS`` adds the in-between states the front page may show that have no
"Show me" row (warming up, a phone pairing, no internet, off).
"""

from __future__ import annotations

from typing import Any

from .contract import BoxState

# Looks the owner can ask to see, in the order the page lists them.
LOOKS: tuple[str, ...] = ("playing", "ready", "asleep", "phone", "busy", "asking", "setup")
# One-off replies the owner can ask to see.
REPLIES: tuple[str, ...] = ("heard", "no")
# Everything /api/status "lamp" can say.
STATUS: tuple[str, ...] = LOOKS + ("starting", "pairing", "offline", "off")

# How long "Show me" plays a look on the real lamp (seconds): long enough for
# two of setup's 3 s cycles and a whole 6 s busy breath.
SHOW_S = 8.0
# Identify: how long by default, and the most one call may ask for.
IDENTIFY_S = 10.0
IDENTIFY_MAX_S = 60.0

# Today's engine has no named looks: "Show me" falls back to a still colour
# from its palette (name, perceptual level) for a look, or an existing reply.
# The new light language (engine.preview_look / preview_reply) replaces this.
FALLBACK_LOOK: dict[str, tuple[str, float]] = {
    "playing": ("amber", 1.0),
    "ready": ("amber", 0.35),
    "asleep": ("ember", 0.22),
    "phone": ("blue", 0.80),
    "busy": ("ember", 0.35),
    "setup": ("white", 0.60),
}
FALLBACK_REPLY: dict[str, str] = {
    "heard": "ack",
    "no": "fail",
    "asking": "ha",          # the confirm broker's "press a key" double dip
}

# Replies that answer a key press: dropped when key replies are off and the
# engine can't do it itself (engine.set_key_replies).
KEY_REPLIES: tuple[str, ...] = ("ack", "next", "nudge")

# Brightness scale: what the page offers, and how today's engine (no
# set_brightness) approximates it by scaling a Look's level.
BRIGHTNESS: tuple[str, ...] = ("low", "normal", "bright")
BRIGHTNESS_FALLBACK = {"low": 0.6, "normal": 1.0, "bright": 1.25}


def meaning_of(state: BoxState, policy: Any = None) -> str:
    """The meaning the lamp shows for ``state`` - one of STATUS."""
    better = getattr(policy, "meaning_for", None)
    if callable(better):
        try:
            name = better(state)
            if name in STATUS:
                return name
        except Exception:
            pass
    if state.halted:
        return "off"
    if state.shutting_down or state.updating:
        return "busy"
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
    if state.group == "following" and state.group_playing:
        return "playing"
    if _offline(state, policy):
        return "offline"
    if state.playing:
        return "playing"
    return "ready"


def _offline(state: BoxState, policy: Any) -> bool:
    shows = getattr(policy, "offline_shows", None)
    if callable(shows):
        try:
            return bool(shows(state))
        except Exception:
            return False
    return state.offline_s >= 60.0 and not state.night and not state.casting and not state.playing


def prefs_from(raw: Any) -> dict:
    """A submitted ``lamp`` block -> {"brightness"?, "key_replies"?}, or ValueError."""
    if not isinstance(raw, dict) or not raw or not set(raw) <= {"brightness", "key_replies"}:
        raise ValueError('lamp must be {"brightness": "low"|"normal"|"bright", "key_replies": true|false}')
    out: dict = {}
    if "brightness" in raw:
        if raw["brightness"] not in BRIGHTNESS:
            raise ValueError("lamp brightness must be one of " + ", ".join(BRIGHTNESS))
        out["brightness"] = raw["brightness"]
    if "key_replies" in raw:
        if not isinstance(raw["key_replies"], bool):
            raise ValueError("lamp key_replies must be true or false")
        out["key_replies"] = raw["key_replies"]
    return out


def prefs_doc(settings: Any) -> dict:
    """settings.json ``lamp`` -> {"brightness", "key_replies"}, defaults for anything odd."""
    raw = settings.get("lamp") if isinstance(settings, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    brightness = raw.get("brightness")
    key_replies = raw.get("key_replies")
    return {
        "brightness": brightness if brightness in BRIGHTNESS else "normal",
        "key_replies": key_replies if isinstance(key_replies, bool) else True,
    }


def seconds_from(raw: Any, default: float, most: float) -> float:
    """A submitted duration in seconds, 1..most."""
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw:
        raise ValueError("seconds must be a number")
    if not 1 <= raw <= most:
        raise ValueError(f"seconds must be from 1 to {most:g}")
    return float(raw)
