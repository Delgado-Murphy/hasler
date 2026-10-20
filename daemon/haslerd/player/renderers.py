"""moOde's casting targets - Bluetooth, AirPlay, UPnP - read and switched.

moOde owns these renderers: their on/off flags and friendly names live in its
``cfg_system`` table, and its worker (worker.php) is what actually starts and
stops shairport-sync, BlueZ/bluealsa and upmpdcli. So we never touch the units
ourselves. Reading is one read-only sqlite query; switching goes through
moOde's own Renderers form - the very POST its web UI sends to
``ren-config.php`` - which writes the flag and queues the worker job:

    update_airplay_settings=1  airplaysvc=0|1  airplayname=...
    update_bt_settings=1       btsvc=0|1       btname=...
    update_upnp_settings=1     upnpsvc=0|1     upnpname=...

The form handler only queues a job when moOde's worker is idle (``w_queue``
empty in its PHP session); a busy worker saves the flag but never applies it.
So the session file is checked first and a busy worker is a "try again", not
a half-done switch. The request carries moOde's own session id as its cookie,
as the browser does (moOde stores it in cfg_system ``sessionid``).

Spotify is deliberately absent: moOde's librespot (``spotifysvc``) must stay
off - go-librespot is the box's Spotify Connect and holds the same ALSA card.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

MOODE_DB = Path("/var/local/www/db/moode-sqlite3.db")
SESSION_DIR = Path("/var/local/php")
REN_CONFIG_URL = "http://127.0.0.1/ren-config.php"
GO_LIBRESPOT_CONFIG = Path("/var/lib/go-librespot/config.yml")
CACHE_S = 3.0
APPLY_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    svc: str          # cfg_system on/off flag
    name: str         # cfg_system friendly-name param
    form: str         # ren-config.php's submit field
    unit: str         # the systemd unit moOde's worker starts


TARGETS: dict[str, Target] = {
    "airplay": Target("airplay", "AirPlay", "airplaysvc", "airplayname",
                      "update_airplay_settings", "shairport-sync"),
    "bluetooth": Target("bluetooth", "Bluetooth", "btsvc", "btname",
                        "update_bt_settings", "bluetooth"),
    "upnp": Target("upnp", "UPnP / DLNA", "upnpsvc", "upnpname",
                   "update_upnp_settings", "upmpdcli"),
}

# Names moOde passes to a shell (sysutil.sh chg-name "old" "new") and to
# Avahi/BlueZ: letters, digits, spaces and a little punctuation, no quotes.
NAME = re.compile(r"^[\w][\w .\-]{0,30}[\w.]$|^[\w]$", re.UNICODE)


class RendererError(Exception):
    """moOde could not do it right now (busy worker, no web server)."""


def valid_name(raw: object) -> str:
    """A friendly name moOde can take, or ValueError saying why."""
    if not isinstance(raw, str) or not NAME.match(raw.strip()) or "  " in raw:
        raise ValueError("a name is 1-32 letters, digits, spaces, dots or dashes, no quotes")
    return raw.strip()


def parse_session(text: str) -> dict[str, str]:
    """``w_queue``/``w_lock``/``w_active`` out of PHP's session serialisation."""
    out: dict[str, str] = {}
    for key in ("w_queue", "w_lock", "w_active"):
        m = re.search(rf'(?:^|[;}}]){key}\|(?:s:\d+:"([^"]*)"|i:(-?\d+)|b:(\d))', text)
        if m:
            out[key] = next(g for g in m.groups() if g is not None)
    return out


def worker_idle(session: dict[str, str]) -> bool:
    return session.get("w_queue", "") == "" and session.get("w_lock", "0") in ("", "0")


def spotify_device_name(path: Path = GO_LIBRESPOT_CONFIG) -> str | None:
    """go-librespot's ``device_name`` (read-only), or None if unreadable."""
    try:
        for line in path.read_text().splitlines():
            m = re.match(r"\s*device_name:\s*['\"]?([^'\"#]*?)['\"]?\s*(?:#.*)?$", line)
            if m:
                return m.group(1).strip() or None
    except OSError:
        return None
    return None


def _unit_active(unit: str) -> bool | None:
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                                capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _post(url: str, form: dict[str, str], cookie: str, timeout: float) -> int:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": f"PHPSESSID={cookie}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - loopback only
        return resp.status


class MoodeRenderers:
    """moOde's casting targets. Every outside touch is injectable for tests."""

    def __init__(
        self,
        db: Path = MOODE_DB,
        session_dir: Path = SESSION_DIR,
        url: str = REN_CONFIG_URL,
        post: Callable[[str, dict[str, str], str, float], int] = _post,
        unit_active: Callable[[str], bool | None] = _unit_active,
        clock: Callable[[], float] = time.monotonic,
        spotify_config: Path = GO_LIBRESPOT_CONFIG,
    ) -> None:
        self.db, self.session_dir, self.url = Path(db), Path(session_dir), url
        self._post, self._unit_active, self._clock = post, unit_active, clock
        self.spotify_config = spotify_config
        self._cache: tuple[float, dict[str, str]] | None = None

    # -- reading ----------------------------------------------------------

    def _rows(self, params: list[str]) -> dict[str, str] | None:
        try:
            con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=0.5)
            try:
                marks = ",".join("?" * len(params))
                rows = con.execute(
                    f"select param, value from cfg_system where param in ({marks})", params
                ).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            log.debug("moOde db unreadable", exc_info=True)
            return None
        return {str(p): "" if v is None else str(v) for p, v in rows}

    def _flags(self, fresh: bool = False) -> dict[str, str] | None:
        now = self._clock()
        if not fresh and self._cache is not None and now - self._cache[0] < CACHE_S:
            return self._cache[1]
        params = [p for t in TARGETS.values() for p in (t.svc, t.name)] + ["sessionid"]
        rows = self._rows(params)
        if rows is not None:
            self._cache = (now, rows)
        return rows

    def enabled(self) -> dict[str, bool]:
        """{target: moOde has it switched on}; {} when moOde can't be read."""
        rows = self._flags()
        if rows is None:
            return {}
        return {tid: rows.get(t.svc, "0") == "1" for tid, t in TARGETS.items()}

    def describe(self, units: bool = True) -> list[dict]:
        """Each target: on, name, and whether its unit really runs (None = unknown)."""
        rows = self._flags() or {}
        known = bool(rows)
        out = []
        for tid, t in TARGETS.items():
            out.append({
                "id": tid,
                "label": t.label,
                "available": known and t.svc in rows,
                "on": rows.get(t.svc, "0") == "1",
                "name": rows.get(t.name, ""),
                "running": self._unit_active(t.unit) if units and known else None,
                "managed_by": "moode",
            })
        return out

    # -- switching --------------------------------------------------------

    def _session(self, sid: str) -> dict[str, str] | None:
        if not re.fullmatch(r"[A-Za-z0-9,-]{1,128}", sid or ""):
            return None
        try:
            return parse_session((self.session_dir / f"sess_{sid}").read_text(errors="replace"))
        except OSError:
            return None

    def set(self, target: str, on: bool | None = None, name: str | None = None) -> dict:
        """Switch a target on/off and/or rename it, through moOde's own form.

        Returns the target's fresh description. Raises ValueError for a bad
        request, RendererError when moOde can't take it now (nothing changed).
        """
        t = TARGETS.get(target)
        if t is None:
            raise ValueError(f"no casting target called {target!r}")
        if on is None and name is None:
            raise ValueError("expected \"on\" and/or \"name\"")
        if on is not None and not isinstance(on, bool):
            raise ValueError("on must be true or false")
        if name is not None:
            name = valid_name(name)
        rows = self._flags(fresh=True)
        if rows is None or t.svc not in rows:
            raise RendererError("moOde's settings can't be read on this radio")
        want_on = (rows.get(t.svc) == "1") if on is None else on
        want_name = rows.get(t.name, "") if name is None else name
        if want_on == (rows.get(t.svc) == "1") and want_name == rows.get(t.name, ""):
            return self._one(target)
        sid = rows.get("sessionid", "")
        session = self._session(sid)
        if session is None:
            raise RendererError("moOde's session can't be read; switch it in moOde (Renderers)")
        if not worker_idle(session):
            raise RendererError("moOde is busy with another job - try again in a few seconds")
        # Both fields always: the Bluetooth handler passes the name to its
        # job unconditionally, and an absent one would rename it to "".
        form = {t.form: "1", t.svc: "1" if want_on else "0", t.name: want_name}
        try:
            status = self._post(self.url, form, sid, APPLY_TIMEOUT_S)
        except Exception as exc:
            raise RendererError(f"moOde's web server didn't answer: {exc}") from exc
        if status >= 400:
            raise RendererError(f"moOde refused the change (HTTP {status})")
        rows = self._flags(fresh=True) or {}
        if rows.get(t.svc) != ("1" if want_on else "0"):
            raise RendererError("moOde didn't take the change - try again, or use moOde's Renderers page")
        log.info("%s %s%s via moOde", t.label, "on" if want_on else "off",
                 f", named {want_name!r}" if name is not None else "")
        return self._one(target)

    def _one(self, target: str) -> dict:
        return next(d for d in self.describe() if d["id"] == target)
