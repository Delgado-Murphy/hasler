"""Find Home Assistant on the LAN, so the owner picks it instead of typing it.

Best effort, two ways at once, never more than ~4 s:

* **mDNS.** HA announces ``_home-assistant._tcp`` with TXT records such as
  ``location_name=Home``, ``internal_url=http://192.168.88.159:8123``,
  ``base_url=…``, ``version=…``, ``uuid=…``. ``avahi-browse -rtp`` (from
  avahi-utils; moOde ships it) resolves and dumps them, one ``;``-separated
  line per record, then exits.
* **The default name.** HA OS answers on ``homeassistant.local:8123``. An
  unauthenticated ``GET /api/`` that comes back 401 (or 200) means something
  HA-shaped is there - no token is sent.

The URL offered is the instance's ``internal_url`` when it announces one (the
LAN address, which is what a radio on the LAN should use), else
``http://<address>:<port>`` from the resolved record. ``base_url``/
``external_url`` are often the public https name and are used only as a last
resort. Results are ``[{"name", "url"}]`` (plus ``uuid`` when announced - the
instance's identity, used to notice the radio has moved to another home),
deduplicated by HA's uuid and URL.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from typing import Callable

from .rest import urllib_fetch

log = logging.getLogger(__name__)

AVAHI_CMD = ["avahi-browse", "-rtp", "_home-assistant._tcp"]
TIMEOUT = 4.0
FALLBACK_URL = "http://homeassistant.local:8123"

Runner = Callable[..., subprocess.CompletedProcess]
Probe = Callable[[str], bool]


def _unescape(text: str) -> str:
    """avahi -p escapes odd bytes as \\DDD (decimal): ``My\\032Home`` -> ``My Home``."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), text)


def parse_txt(field: str) -> dict[str, str]:
    """``"a=1" "b=two words"`` -> {"a": "1", "b": "two words"}."""
    out = {}
    for item in re.findall(r'"((?:[^"\\]|\\.)*)"', field):
        key, sep, value = _unescape(item).partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def parse_avahi(output: str) -> list[dict]:
    """``avahi-browse -rtp`` output -> [{"name", "url"}], resolved records only.

    Resolved lines are ``=;iface;proto;name;type;domain;host;address;port;txt``.
    IPv4 records are preferred (an IPv6 link-local URL is useless in a browser).
    """
    found: dict[str, dict] = {}
    order: list[str] = []
    for line in output.splitlines():
        parts = line.split(";", 9)
        if len(parts) < 9 or parts[0] != "=":
            continue
        _, _iface, proto, name, _stype, _domain, host, address, port = parts[:9]
        txt = parse_txt(parts[9]) if len(parts) > 9 else {}
        if txt.get("internal_url"):
            url = txt["internal_url"]
        elif address and port:
            hostpart = f"[{address}]" if ":" in address else address
            url = f"http://{hostpart}:{port}"
        elif txt.get("base_url"):
            url = txt["base_url"]
        else:
            continue
        url = url.rstrip("/")
        label = txt.get("location_name") or _unescape(name) or host
        key = txt.get("uuid") or url
        entry = {"name": label, "url": url}
        if txt.get("uuid"):
            entry["uuid"] = txt["uuid"]      # instance identity; ha_api keeps it, the page never sees it
        if key not in found:
            found[key] = entry
            order.append(key)
        elif proto == "IPv4" and not txt.get("internal_url"):
            found[key] = entry          # prefer the IPv4 address over IPv6
    seen_urls: set[str] = set()
    result = []
    for key in order:
        entry = found[key]
        if entry["url"] not in seen_urls:
            seen_urls.add(entry["url"])
            result.append(entry)
    return result


def browse(runner: Runner = subprocess.run, timeout: float = TIMEOUT) -> tuple[list[dict], str | None]:
    """mDNS via avahi-browse -> (instances, note). Never raises."""
    try:
        done = runner(AVAHI_CMD, capture_output=True, text=True, timeout=timeout)
        output = done.stdout or ""
    except FileNotFoundError:
        return [], "avahi-browse isn't installed on the radio (apt install avahi-utils), so only homeassistant.local was tried"
    except subprocess.TimeoutExpired as exc:
        # Whatever resolved before the deadline is still good.
        raw = exc.stdout or b""
        output = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    except Exception as exc:
        log.info("avahi-browse failed: %s", exc)
        return [], "Network discovery failed on the radio"
    return parse_avahi(output), None


def probe_fallback(url: str = FALLBACK_URL, fetch=urllib_fetch) -> bool:
    """Does something HA-shaped answer at ``url``? No token is sent."""
    try:
        status, _ = fetch("GET", url + "/api/", {}, None, 2.0)
    except Exception:
        return False
    return status in (200, 401)


def discover(runner: Runner = subprocess.run, probe: Probe = probe_fallback) -> tuple[list[dict], str | None]:
    """Both methods in parallel -> ([{"name", "url"}], note-or-None). Never raises."""
    fallback: dict[str, bool] = {}

    def check() -> None:
        try:
            fallback["ok"] = bool(probe(FALLBACK_URL))
        except Exception:
            fallback["ok"] = False

    worker = threading.Thread(target=check, daemon=True)
    worker.start()
    instances, note = browse(runner)
    worker.join(TIMEOUT)
    if fallback.get("ok") and not any("homeassistant.local" in i["url"] for i in instances):
        instances.append({"name": "homeassistant.local", "url": FALLBACK_URL})
    if not instances and note is None:
        note = "No Home Assistant found on this network - type its address instead"
    return instances, note
