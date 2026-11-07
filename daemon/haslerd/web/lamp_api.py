"""The owner's lamp calls: "Show me", brightness, key replies, Identify.

Settings -> Pilot lamp lists what the lamp means (lamp/meanings.py) with a
"Show me" for each; This radio and Rooms have "Identify this radio". These
are the engine calls behind them, each guarded so a radio running an older
engine still answers (a still colour instead of the real behaviour, say):

==========================  ==================================================
engine (new light language)  today's engine (fallback)
==========================  ==================================================
preview_look(name, seconds)  preview(palette[colour], level, hold_s=seconds)
preview_reply(name)          reply("ack" | "fail" | "ha")
stop_preview()               preview(None)
set_brightness(name)         the LampKeeper scales each Look's level (main.py)
set_key_replies(on)          the LampKeeper drops key-press replies (main.py)
identify(seconds)            preview(palette["white"], 1.0, hold_s=seconds)
==========================  ==================================================

HTTP (web/server.py, behind the usual Host/Origin guard):

    POST /api/lamp/preview   {"look": name} | {"reply": name} | {"look": null}
    POST /api/lamp/identify  {"seconds": 1..60 (10)} [+ {"peer": id}: another Hasler's]
    PUT  /api/settings       {"lamp": {"brightness": "low"|"normal"|"bright", "key_replies": bool}}
"""

from __future__ import annotations

import logging
from typing import Any

from ..lamp import meanings

log = logging.getLogger(__name__)

# Our meaning name -> the engine's look/reply name. The same words unless the
# engine's light language names them differently; change it here, once.
ENGINE_LOOK: dict[str, str] = {name: name for name in meanings.LOOKS}
ENGINE_REPLY: dict[str, str] = {name: name for name in meanings.REPLIES}


def _palette_rgb(lamp: Any, colour: str) -> Any:
    palette = getattr(lamp, "palette", None)
    if isinstance(palette, dict) and colour in palette:
        return palette[colour]
    from ..lamp import engine as lamp_engine
    # Colours the palette no longer has (the old warm white) show as amber.
    return lamp_engine.DEFAULT_PALETTE.get(colour) or lamp_engine.DEFAULT_PALETTE["amber"]


def preview_named(lamp: Any, body: dict) -> dict:
    """``{"look": name}`` / ``{"reply": name}`` on the lamp for a few seconds."""
    if "look" in body and "reply" in body:
        raise ValueError('send {"look": name} or {"reply": name}, not both')
    if "look" in body:
        name, kind, known = body["look"], "look", meanings.LOOKS
        if name is None:
            stop_preview(lamp)
            return {"lamp": lamp is not None, "showing": False, "expires_s": 0}
    else:
        name, kind, known = body["reply"], "reply", meanings.REPLIES
    if name not in known:
        raise ValueError(f"{kind} must be one of " + ", ".join(known))
    seconds = meanings.SHOW_S
    if lamp is not None:
        try:
            _show(lamp, kind, name, seconds)
        except Exception:
            log.warning("lamp: could not show %s %s", kind, name, exc_info=True)
            raise
    log.info("lamp: showing the %s %r from the web page", kind, name)
    return {"lamp": lamp is not None, "showing": lamp is not None, kind: name,
            "expires_s": seconds if kind == "look" else 0}


def _show(lamp: Any, kind: str, name: str, seconds: float) -> None:
    if kind == "look":
        fn = getattr(lamp, "preview_look", None)
        if callable(fn):
            fn(ENGINE_LOOK[name], seconds)
            return
    else:
        fn = getattr(lamp, "preview_reply", None)
        if callable(fn):
            fn(ENGINE_REPLY[name])
            return
    # Today's engine: a still colour for a look, an existing reply otherwise.
    if name in meanings.FALLBACK_REPLY:
        reply = getattr(lamp, "reply", None)
        if callable(reply):
            reply(meanings.FALLBACK_REPLY[name])
        return
    colour, level = meanings.FALLBACK_LOOK[name]
    preview = getattr(lamp, "preview", None)
    if callable(preview):
        preview(_palette_rgb(lamp, colour), level, hold_s=seconds)


def stop_preview(lamp: Any) -> None:
    if lamp is None:
        return
    stop = getattr(lamp, "stop_preview", None)
    if callable(stop):
        stop()
        return
    preview = getattr(lamp, "preview", None)
    if callable(preview):
        preview(None)


def identify(lamp: Any, seconds: float) -> bool:
    """The lamp says "it's me" for ``seconds``. False: no lamp here."""
    if lamp is None:
        return False
    fn = getattr(lamp, "identify", None)
    if callable(fn):
        fn(seconds)
        return True
    preview = getattr(lamp, "preview", None)
    if callable(preview):
        preview(_palette_rgb(lamp, "white"), 1.0, hold_s=seconds)
        return True
    return False


def apply_prefs(lamp: Any, prefs: dict) -> dict:
    """Brightness and key replies onto the engine. Returns what it couldn't
    take itself ({"brightness": bool, "key_replies": bool} = done by the engine)."""
    done = {"brightness": False, "key_replies": False}
    if lamp is None:
        return done
    fn = getattr(lamp, "set_brightness", None)
    if callable(fn):
        fn(prefs["brightness"])
        done["brightness"] = True
    fn = getattr(lamp, "set_key_replies", None)
    if callable(fn):
        fn(bool(prefs["key_replies"]))
        done["key_replies"] = True
    return done
