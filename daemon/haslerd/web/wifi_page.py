"""The Wi-Fi setup page (static/wifi.html), rendered on the server.

Phones show it in their captive-portal sheet, and some of those run no
JavaScript, so the page arrives complete: the network list, the phone's
language (Accept-Language, or ?lang=) and a plain form that posts to /wifi.
With JavaScript the same form is sent to /api/wifi instead (web/server.py).

In the page, ``{{key}}`` is a string from its own table (the
``<script id="strings">`` JSON, one table for the server and the page's
script) and ``{{=name}}`` a value filled in here. Strings are ours and may
carry a little markup; everything from the network side is escaped.
"""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any
from urllib.parse import parse_qs

LANGS = ("en", "es")
SECURITIES = ("auto", "none", "wpa-psk", "sae")
HOTSPOT_SSID = "Hasler"

_STRINGS = re.compile(r'<script type="application/json" id="strings">(.*?)</script>', re.S)
_SLOT = re.compile(r"\{\{(=?)(\w+)\}\}")
_VAR = re.compile(r"\{(\w+)\}")
CURRENT = ' aria-current="true"'
_LOCK = ('<svg class="lock" viewBox="0 0 12 14" role="img" aria-label="{label}"><rect x="0" y="6" width="12" height="8" '
         'rx="2" fill="currentColor"/><path d="M3 6V4.5a3 3 0 0 1 6 0V6" stroke="currentColor" stroke-width="2" '
         'fill="none"/></svg>')


class FormError(ValueError):
    """A form mistake, named by its key in the string table."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


def strings(page: str) -> dict[str, dict[str, str]]:
    m = _STRINGS.search(page)
    return json.loads(m.group(1)) if m else {"en": {}}


def pick_lang(chosen: str | None, accept_language: str | None) -> str:
    """?lang= / the form's lang field, else the first of Accept-Language we speak, else English."""
    if chosen in LANGS:
        return chosen
    prefs = []
    for i, part in enumerate((accept_language or "").split(",")):
        tag, _, params = part.strip().partition(";")
        q = 1.0
        for p in params.split(";"):
            k, _, v = p.strip().partition("=")
            if k == "q":
                try:
                    q = float(v)
                except ValueError:
                    q = 0.0
        prefs.append((-q, i, tag.strip()[:2].lower()))
    for _, _, code in sorted(prefs):
        if code in LANGS:
            return code
    return "en"


def parse_form(raw: bytes) -> dict[str, str]:
    fields = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)
    return {k: v[0] for k, v in fields.items()}


def join_body(form: dict[str, str]) -> dict[str, str]:
    """The form's fields -> the body /api/wifi takes. Raises FormError."""
    net = form.get("net", "")
    if net == "other":
        ssid, security = form.get("other_ssid", "").strip(), form.get("security", "auto")
        if not ssid:
            raise FormError("err_name")
    elif ":" in net:
        security, ssid = net.split(":", 1)
    else:
        raise FormError("err_choose")
    if security not in SECURITIES:
        security = "auto"
    password = "" if security == "none" else form.get("password", "")
    if (password or security != "auto") and security != "none" and not re.fullmatch(r"[0-9a-fA-F]{64}", password):
        if len(password) < 8:
            raise FormError("err_short")
        if len(password) > 63:
            raise FormError("err_long")
    return {"ssid": ssid, "password": password, "security": security}


def _fmt(s: str, **values: str) -> str:
    return _VAR.sub(lambda m: values.get(m.group(1), m.group(0)), s)


def _bars(signal: int, label: str) -> str:
    n = 4 if signal >= 75 else 3 if signal >= 50 else 2 if signal >= 25 else 1
    return (f'<span class="bars s{n}" role="img" data-n="{signal}" aria-label="{escape(label)}">'
            "<i></i><i></i><i></i><i></i></span>")


def render(page: str, status: dict[str, Any], lang: str, *, state: str = "form", error: str = "",
           form: dict[str, str] | None = None, saved_ssid: str = "") -> str:
    """The page for this phone. ``status`` is Wifi.status(); ``form`` the fields
    sent back after a mistake (the choice is kept, the password never is)."""
    table = strings(page)
    s = {**table.get("en", {}), **table.get(lang, {})}
    form = form or {}
    hotspot, current = bool(status.get("hotspot")), str(status.get("current") or "")
    networks = [n for n in status.get("networks") or [] if n.get("ssid")]
    chosen = form.get("net", "")
    if not chosen and not networks:
        chosen = "other"

    rows, chosen_ssid = [], ""
    for i, net in enumerate(networks):
        ssid, sec = str(net["ssid"]), str(net.get("security") or "auto")
        value = f"{sec}:{ssid}"
        checked = value == chosen
        if checked:
            chosen_ssid = ssid
        signal = int(net.get("signal") or 0)
        lock = _LOCK.format(label=escape(s.get("locked", ""))) if sec != "none" else ""
        rows.append(
            f'<li><label class="net"><input type="radio" name="net" value="{escape(value)}" data-sec="{escape(sec)}"'
            f'{" checked" if checked else ""}{" required" if i == 0 else ""}><span class="name">{escape(ssid)}</span>'
            f'{lock}{_bars(signal, _fmt(s.get("signal", ""), n=str(signal)))}</label></li>')
    if not networks:
        rows.append(f'<li class="note" style="padding:12px 14px;margin:0">{s.get("empty", "")}</li>')
    if chosen == "other":
        chosen_ssid = form.get("other_ssid", "").strip()

    if state == "done":
        lede_key, lede_ssid = "lede_done", saved_ssid
        chosen_ssid = saved_ssid
    elif current and hotspot:
        lede_key, lede_ssid = "lede_retry", current
    elif current:
        lede_key, lede_ssid = "lede_change", current
    else:
        lede_key, lede_ssid = "lede_first", ""
    note_key = "note_hotspot" if hotspot else "note_lan"
    security = form.get("security", "auto")
    shown = escape(chosen_ssid) if chosen_ssid else escape(s.get("ssid_none", ""))
    back = ("" if hotspot or not current or state == "done" else
            f'<a class="back" href="/#settings" data-t="back">{s.get("back", "")}</a>')
    langs = "".join(
        f'<a href="/wifi?lang={code}" lang="{code}"{CURRENT if code == lang else ""}>{name}</a>'
        for code, name in (("en", "English"), ("es", "Español")))

    values = {
        "lang": lang,
        "state": "done" if state == "done" else "form",
        "back": back,
        "lede_key": lede_key,
        "lede_ssid": escape(lede_ssid),
        "lede": _fmt(s.get(lede_key, ""), v=escape(lede_ssid)),
        "networks": "\n        ".join(rows),
        "other_checked": " checked" if chosen == "other" else "",
        "other_ssid": escape(form.get("other_ssid", "")),
        "note_key": note_key,
        "note": s.get(note_key, ""),
        "error": escape(error),
        "langs": langs,
        **{f"sec_{k}": (" selected" if security == v else "")
           for k, v in (("auto", "auto"), ("wpa", "wpa-psk"), ("sae", "sae"), ("none", "none"))},
    }

    def slot(m: re.Match) -> str:
        if m.group(1):
            return values.get(m.group(2), "")
        return _fmt(s.get(m.group(2), m.group(0)), ssid=shown)

    # One pass, so nothing filled in (an SSID with braces, say) is read as a slot.
    return _SLOT.sub(slot, page)
