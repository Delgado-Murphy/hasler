"""A press on the radio: physical confirmation for the page's risky buttons.

docs/ota.md section 3, "LAN attacker": anybody on the Wi-Fi can talk to the key
page, so installing or rolling back a release, changing the Wi-Fi, resetting
the keys, moving to another release channel or starting remote help (Tailscale
for 24 h, web/help_api.py) needs a hand on the cabinet.

    POST /api/confirm/request {"action": "update", "args": {"version": "1.2.0"}}
        -> {"token": "...", "state": "pending", "expires_in": 30}
    GET  /api/confirm/<token>  -> {"state": "pending" | "confirmed" | "expired" | "used"}
    DELETE /api/confirm/<token>   (the page's Cancel)

While a request is pending the lamp asks - two soft dips (the existing "ha"
reply, a double ack), again every few seconds - and the **next press of any
panel key** within 30 s confirms it. That press is swallowed: it does not also
play a preset (``intercept`` runs in the main loop *before* dispatch, and the
web's own ``/api/press`` never reaches it, so the page cannot press for
itself). The action itself then takes the token (``consume``), once, with the
same action and arguments it was asked for, within a minute.

No panel board (``AbsentDigital``): nobody can press, so requests are refused
with a clear message - security beats convenience. ``web.allow_unconfirmed:
true`` in box.yaml (the bench) confirms every request at once and lets the
actions run without a token.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

ACTIONS = ("update", "rollback", "wifi", "reset", "channel", "help")   # help: web/help_api.py
CONFIRM_S = 30.0         # to press a key after asking
USE_S = 60.0             # to use a confirmed token
ASK_EVERY_S = 5.0        # the lamp asks again this often while pending
KEEP_S = 300.0           # forget finished requests after this
# Panel controls that are keys, and the events that are a hand on one.
KEY_KINDS = ("momentary", "interlocked_bank")
PRESS_EVENTS = ("tap", "hold", "select")

ABSENT_MESSAGE = ("The radio's keys aren't connected, so it can't confirm this with a key press. "
                  "Fix the panel board, or set web.allow_unconfirmed in box.yaml on a bench radio.")


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

    # -- what can confirm --------------------------------------------------

    @property
    def can_confirm(self) -> bool:
        """A human can press a key on this radio (or the bench says don't ask)."""
        if self.allow_unconfirmed:
            return True
        return self.panel is not None and not getattr(self.panel, "absent", False) and bool(self._key_ids())

    def _key_ids(self) -> set[str]:
        return {c.id for c in getattr(self.panel, "controls", ()) if getattr(c, "kind", None) in KEY_KINDS}

    # -- web side ------------------------------------------------------------

    def request(self, action: Any, args: Any = None) -> dict:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        args = _clean_args(args)
        if not self.can_confirm:
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
            if self.allow_unconfirmed:
                req.state, req.confirmed_at, req.by = "confirmed", now, "allow_unconfirmed"
            self._requests[token] = req
        log.info("confirm requested: %s %s%s", action, args,
                 " (allowed unconfirmed)" if self.allow_unconfirmed else " - waiting for a key press")
        if not self.allow_unconfirmed:
            self._reply("ha")
        return {"token": token, "state": req.state, "action": action,
                "expires_in": int(CONFIRM_S), "unconfirmed": self.allow_unconfirmed}

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
        if not token and self.allow_unconfirmed:
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
            keys = self._key_ids()
            for i, event in enumerate(events):
                if getattr(event, "control", None) in keys and getattr(event, "kind", None) in PRESS_EVENTS:
                    req = max(waiting, key=lambda r: r.asked)
                    req.state, req.confirmed_at, req.by = "confirmed", now, event.control
                    log.info("confirm: %s pressed - %s %s confirmed", event.control, req.action, req.args)
                    rest = events[:i] + events[i + 1:]
                    break
            else:
                return events
        self._reply("ack")
        return rest

    def tick(self, now: float | None = None) -> None:
        """The lamp asks again every ASK_EVERY_S while a request waits."""
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
            self._reply("ha")

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

    def _reply(self, name: str) -> None:
        if self.reply is None:
            return
        try:
            self.reply(name)
        except Exception:
            log.debug("confirm lamp reply failed", exc_info=True)
