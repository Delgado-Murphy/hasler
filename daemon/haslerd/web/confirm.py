"""A press on the radio: physical confirmation for the page's risky buttons.

docs/ota.md section 3, "LAN attacker": anybody on the Wi-Fi can talk to the key
page, so installing or rolling back a release, changing the Wi-Fi, resetting
the keys, moving to another release channel, starting remote help (Tailscale
for 24 h, web/help_api.py), signing the radio in to or out of Spotify
(web/spotify_account_api.py), allowing changes through another address
(web/hosts.py) or restarting the radio (``POST /api/restart``) needs a hand
on the cabinet.

    POST /api/confirm/request {"action": "update", "args": {"version": "1.2.0"}}
        -> {"token": "...", "state": "pending", "expires_in": 30}
    GET  /api/confirm/<token>  -> {"state": "pending" | "confirmed" | "expired" | "used"}
    DELETE /api/confirm/<token>   (the page's Cancel)

While a request is pending the lamp asks - the engine's confirming beckon
(two kicks, a rest, in the colour on show: ``BoxState.confirming``, which the
LampKeeper reads from ``pending()``) - and the **next press of any
panel key** within 30 s confirms it. That press is swallowed: it does not also
play a preset (``intercept`` runs in the main loop *before* dispatch, and the
web's own ``/api/press`` never reaches it, so the page cannot press for
itself). The action itself then takes the token (``consume``), once, with the
same action and arguments it was asked for, within a minute.

``lamp_beckons`` (set by main when a LampKeeper draws the beckon): the
broker sends no "ha" replies of its own then - a kick at the request and
every ASK_EVERY_S on top of the beckon broke its rhythm. Without a keeper
(an older wiring, a test) it still asks with "ha" kicks; a radio without a
lamp has nobody listening to them, which is fine.

No panel board (``AbsentDigital``): nobody can press, so requests are refused
with a clear message - security beats convenience. ``web.allow_unconfirmed:
true`` in box.yaml (the bench) confirms every request at once and lets the
actions run without a token.

One exception, because the fault that most needs a maker must not block the
way to the maker: when the key board is dead but the knobs still answer (the
pots sit on the ADS1115, not the MCP23017; variant.Panel polls them alone
then), a **knob movement** counts as the hand on the radio for ``help`` only
(``KNOB_ACTIONS``): the request answers ``by_knob: true`` and the page says
"turn the volume knob a little". Nothing else is confirmed by a knob.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

ACTIONS = ("update", "rollback", "wifi", "reset", "channel", "help",   # help: web/help_api.py
           "spotify",                                                   # web/spotify_account_api.py
           "hostname",                                                  # web/hosts.py
           "restart",                                                   # "Restart the radio" (server.py restart)
           "starter")                                                   # a starter set on every key (server.set_starter)
# Always a real press, even on a bench with web.allow_unconfirmed: the request
# comes from an address the radio doesn't trust yet.
ALWAYS_PRESS = ("hostname",)
CONFIRM_S = 30.0         # to press a key after asking
USE_S = 60.0             # to use a confirmed token
ASK_EVERY_S = 5.0        # the lamp asks again this often while pending
KEEP_S = 300.0           # forget finished requests after this
# Panel controls that are keys, and the events that are a hand on one.
KEY_KINDS = ("momentary", "interlocked_bank")
PRESS_EVENTS = ("tap", "hold", "select")
# With the key board dead and the knobs alive, a pot's ``change`` is the hand
# on the radio - for these actions only (letting the makers in to fix it).
KNOB_KINDS = ("pot",)
KNOB_ACTIONS = ("help",)

# What the owner reads (the page shows it); the box.yaml hint is for the journal.
ABSENT_MESSAGE = ("The radio's keys aren't connected, so nobody can press one to say yes. "
                  "The makers need to look at the panel board.")
ABSENT_LOG = "no panel board to confirm with (fix the panel board, or set web.allow_unconfirmed in box.yaml on a bench radio)"


class ConfirmError(Exception):
    """This action needs (a valid, confirmed) press on the radio - HTTP 403."""


class ConfirmUnavailable(Exception):
    """No way to confirm on this radio right now (no panel) - HTTP 409."""


@dataclass
class Request:
    token: str
    action: str
    args: dict
    asked: float
    state: str = "pending"          # pending | confirmed | expired | used | cancelled
    confirmed_at: float | None = None
    next_ask: float = 0.0
    by: str = field(default="")      # the key that confirmed it


def _clean_args(args: Any) -> dict:
    if args is None:
        return {}
    if not isinstance(args, dict):
        raise ValueError("args must be an object")
    out = {}
    for k, v in args.items():
        if not isinstance(k, str) or len(k) > 40:
            raise ValueError("bad argument name")
        if v is not None and not isinstance(v, (str, int, float, bool)):
            raise ValueError(f"argument {k} must be a plain value")
        if isinstance(v, str) and len(v) > 200:
            raise ValueError(f"argument {k} is too long")
        out[k] = v
    return out


class ConfirmBroker:
    """Pending requests, the key-press interception and the lamp's asking.

    Thread-safe: requests and polls come from web threads, ``intercept`` and
    ``tick`` from the main loop.
    """

    def __init__(
        self,
        panel: Any = None,
        reply: Callable[[str], None] | None = None,
        allow_unconfirmed: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.panel = panel
        self.reply = reply
        self.allow_unconfirmed = bool(allow_unconfirmed)
        self.clock = clock
        self._lock = threading.Lock()
        self._requests: dict[str, Request] = {}
        # True when the lamp draws the confirming beckon itself (main.py sets
        # it with the LampKeeper): no "ha" kicks of our own on top of it.
        self.lamp_beckons = False

    # -- what can confirm --------------------------------------------------

    @property
    def can_confirm(self) -> bool:
        """A human can press a key on this radio (or the bench says don't ask)."""
        if self.allow_unconfirmed:
            return True
        return self.panel is not None and not getattr(self.panel, "absent", False) and bool(self._key_ids())

    def _key_ids(self) -> set[str]:
        return {c.id for c in getattr(self.panel, "controls", ()) if getattr(c, "kind", None) in KEY_KINDS}

    def _knob_ids(self) -> set[str]:
        return {c.id for c in getattr(self.panel, "controls", ()) if getattr(c, "kind", None) in KNOB_KINDS}

    @property
    def knobs_answer(self) -> bool:
        """The key board is dead but the knobs are read (variant.Panel.knobs_answer)."""
        return (self.panel is not None and getattr(self.panel, "absent", False)
                and bool(getattr(self.panel, "knobs_answer", False)) and bool(self._knob_ids()))

    # -- web side ------------------------------------------------------------

    def request(self, action: Any, args: Any = None) -> dict:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        args = _clean_args(args)
        unconfirmed = self.allow_unconfirmed and action not in ALWAYS_PRESS
        has_keys = self.panel is not None and not getattr(self.panel, "absent", False) and bool(self._key_ids())
        by_knob = not has_keys and action in KNOB_ACTIONS and self.knobs_answer
        if not (unconfirmed or has_keys or by_knob):
            log.warning("confirm %s refused: %s", action, ABSENT_LOG)
            raise ConfirmUnavailable(ABSENT_MESSAGE)
        now = self.clock()
        token = secrets.token_urlsafe(18)
        req = Request(token, action, args, asked=now, next_ask=now + ASK_EVERY_S)
        with self._lock:
            self._expire(now)
            # One question at a time: a new one replaces any still pending.
            for old in self._requests.values():
                if old.state == "pending":
                    old.state = "cancelled"
            if unconfirmed:
                req.state, req.confirmed_at, req.by = "confirmed", now, "allow_unconfirmed"
            self._requests[token] = req
        log.info("confirm requested: %s %s%s", action, args,
                 " (allowed unconfirmed)" if unconfirmed else
                 " - waiting for a knob movement (the keys are dead)" if by_knob else " - waiting for a key press")
        if not unconfirmed:
            self._ask()
        return {"token": token, "state": req.state, "action": action,
                "expires_in": int(CONFIRM_S), "unconfirmed": unconfirmed, "by_knob": by_knob}

    def poll(self, token: str) -> dict | None:
        with self._lock:
            now = self.clock()
            self._expire(now)
            req = self._requests.get(token)
            if req is None:
                return None
            left = max(0.0, req.asked + CONFIRM_S - now) if req.state == "pending" else 0.0
            state = "expired" if req.state == "cancelled" else req.state
            return {"state": state, "action": req.action, "expires_in": round(left, 1)}

    def cancel(self, token: str) -> bool:
        with self._lock:
            req = self._requests.get(token)
            if req is None or req.state not in ("pending", "confirmed"):
                return False
            req.state = "cancelled"
        log.info("confirm cancelled: %s", req.action)
        return True

    def consume(self, token: Any, action: str, args: dict | None = None) -> None:
        """Use a confirmed token for ``action``/``args``, once. Raises ConfirmError."""
        args = args or {}
        if not token and self.allow_unconfirmed and action not in ALWAYS_PRESS:
            return
        if not isinstance(token, str) or not token:
            raise ConfirmError(f"{action}: press a key on the radio to confirm first")
        with self._lock:
            now = self.clock()
            self._expire(now)
            req = self._requests.get(token)
            if req is None or req.action != action or req.args != args:
                raise ConfirmError(f"{action}: that confirmation is not for this - ask again")
            if req.state != "confirmed":
                raise ConfirmError(f"{action}: not confirmed on the radio ({req.state})")
            req.state = "used"
        log.info("confirmed %s %s used", action, args)

    # -- main-loop side -------------------------------------------------------

    def pending(self) -> bool:
        with self._lock:
            self._expire(self.clock())
            return any(r.state == "pending" for r in self._requests.values())

    def intercept(self, events: Iterable[Any]) -> list[Any]:
        """The panel's events, minus the key press that confirms a request.

        Called from the main loop before dispatch. With nothing pending this
        is the events, untouched.
        """
        events = list(events)
        if not events or not self._requests:
            return events
        with self._lock:
            now = self.clock()
            self._expire(now)
            waiting = [r for r in self._requests.values() if r.state == "pending"]
            if not waiting:
                return events
            # With the board absent no key event can be real: only a knob (for help) counts then.
            keys = self._key_ids() if not getattr(self.panel, "absent", False) else set()
            knobs = self._knob_ids() if self.knobs_answer else set()
            for i, event in enumerate(events):
                control, kind = getattr(event, "control", None), getattr(event, "kind", None)
                if control in keys and kind in PRESS_EVENTS:
                    req = max(waiting, key=lambda r: r.asked)
                    req.state, req.confirmed_at, req.by = "confirmed", now, event.control
                    log.info("confirm: %s pressed - %s %s confirmed", event.control, req.action, req.args)
                    rest = events[:i] + events[i + 1:]
                    break
                if control in knobs and kind == "change":
                    answered = [r for r in waiting if r.action in KNOB_ACTIONS]
                    if not answered:
                        continue
                    req = max(answered, key=lambda r: r.asked)
                    req.state, req.confirmed_at, req.by = "confirmed", now, event.control
                    log.info("confirm: %s turned - %s %s confirmed (the keys are dead)", event.control, req.action, req.args)
                    rest = events     # the knob still does its job: the event stays
                    break
            else:
                return events
        self._reply("bloom")        # accepted: the green bloom (v2; never muted as a key dip, older engines swell)
        return rest

    def tick(self, now: float | None = None) -> None:
        """The lamp asks again every ASK_EVERY_S while a request waits (not
        when it beckons by itself: ``lamp_beckons``)."""
        if not self._requests:
            return
        ask = False
        with self._lock:
            now = self.clock() if now is None else now
            self._expire(now)
            for req in self._requests.values():
                if req.state == "pending" and now >= req.next_ask:
                    req.next_ask = now + ASK_EVERY_S
                    ask = True
        if ask:
            self._ask()

    # -- internals -------------------------------------------------------------

    def _expire(self, now: float) -> None:
        for token, req in list(self._requests.items()):
            if req.state == "pending" and now - req.asked > CONFIRM_S:
                req.state = "expired"
                log.info("confirm for %s expired: no key pressed", req.action)
            elif req.state == "confirmed" and now - (req.confirmed_at or now) > USE_S:
                req.state = "expired"
            if req.state != "pending" and now - req.asked > KEEP_S:
                del self._requests[token]

    def _ask(self) -> None:
        """The lamp asks for a press - unless it beckons on its own (``lamp_beckons``)."""
        if not self.lamp_beckons:
            self._reply("ha")

    def _reply(self, name: str) -> None:
        if self.reply is None:
            return
        try:
            self.reply(name)
        except Exception:
            log.debug("confirm lamp reply failed", exc_info=True)
