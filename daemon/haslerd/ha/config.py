"""Where the Home Assistant connection is kept: ``ha.json`` in the state dir.

The owner connects HA from the key page (Settings -> Home Assistant), so the
connection has to live somewhere the daemon can write: next to state.json and
keys.yaml, normally ``/var/lib/haslerd/ha.json``::

    {"url": "http://192.168.1.10:8123", "token": "eyJ…", "publish": false,
     "instance": {"uuid": "…", "name": "Home", "where": "3f2a…", "time_zone": "Europe/Madrid"}}

``instance`` is which HA this is (see ``fingerprint``). A second, non-secret
file, ``ha-instance.json``, remembers the HA the *key layout* was made for; it
survives Disconnect on purpose, so that when the radio moves to another home
and is connected to that home's HA the page can say "your keys were set up
with Home Assistant 'Casa Vieja'" and offer to clear the actions that no
longer resolve.

Rules, in order of how much they matter:

* **The token is a password.** The file is created ``0600`` (before any byte
  of it is written, via ``mkstemp``), the token never reaches a log line, and
  ``HaSettings``' repr masks it - so even ``log.debug("%r", settings)`` is safe.
* **Writes are atomic** (temp file in the same directory, fsync, rename), like
  state.json: the box is switched off at the wall, and a half-written file
  must never cost the owner their connection.
* **Backwards compatible.** With no ha.json, the old ``haslerd.yaml``
  ``home_assistant: {url, token}`` block is used, exactly as before. Saving
  from the page writes ha.json, which then wins.
* **"Disconnect" must stick.** If the yaml block still holds a token, simply
  deleting ha.json would bring the yaml connection back at the next boot. So
  ``forget`` deletes the file when there is nothing to fall back to, and
  otherwise leaves a tombstone (``{"url": ""}``) that means "deliberately off".
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILENAME = "ha.json"
LAYOUT_FILENAME = "ha-instance.json"

# http(s)://host[:port] with an optional path prefix (HA behind a reverse
# proxy at /ha is rare but real). Host: a name, an IPv4 address or [IPv6].
URL_RE = re.compile(
    r"^(https?)://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?::(\d{1,5}))?(/[^\s?#]*)?$"
)


@dataclass
class HaSettings:
    """One HA connection. ``source`` says where it came from: "file" or "yaml"."""

    url: str
    token: str = field(repr=False)
    publish: bool = False
    source: str = "file"
    instance: dict = field(default_factory=dict)

    def __repr__(self) -> str:  # explicit, so the token can never leak via repr
        masked = "***" if self.token else "''"
        return f"HaSettings(url={self.url!r}, token={masked}, publish={self.publish}, source={self.source!r})"


def normalise_url(raw: Any) -> str:
    """What people paste -> ``http(s)://host[:port][/prefix]``. Raises ValueError.

    Accepts a bare ``host:8123`` (assumes http://, HA's LAN default), strips
    whitespace, trailing slashes and a pasted ``/api`` or ``/lovelace/…``
    suffix - people copy the address bar.
    """
    url = str(raw or "").strip()
    if not url:
        raise ValueError("Enter Home Assistant's address, e.g. http://homeassistant.local:8123")
    if "://" not in url:
        url = "http://" + url
    url = url.rstrip("/")
    # The frontend's own paths are never part of the base URL.
    url = re.sub(r"/(api|lovelace|config|dashboard-[\w-]+|profile|history|logbook|map)(/.*)?$", "", url)
    m = URL_RE.match(url)
    if not m:
        raise ValueError("That doesn't look like an address - try http://homeassistant.local:8123")
    port = m.group(3)
    if port is not None and not 0 < int(port) < 65536:
        raise ValueError("That port number isn't valid")
    return url


def path_for(state_dir: Path | None) -> Path | None:
    return None if state_dir is None else Path(state_dir) / FILENAME


def _from_yaml(yaml_conf: dict | None) -> HaSettings | None:
    conf = yaml_conf or {}
    url, token = conf.get("url"), conf.get("token")
    if not (url and token):
        return None
    try:
        url = normalise_url(url)
    except ValueError:
        log.error("haslerd.yaml home_assistant.url %r is not a valid address; ignoring it", conf.get("url"))
        return None
    return HaSettings(url=url, token=str(token), publish=bool(conf.get("publish", False)), source="yaml")


def load(state_dir: Path | None, yaml_conf: dict | None = None) -> HaSettings | None:
    """The saved connection: ha.json if present, else the yaml block, else None.

    Never raises: an unreadable or corrupt ha.json is logged and treated as
    absent (falling back to yaml), because a broken settings file must not
    stop the radio from starting.
    """
    path = path_for(state_dir)
    if path is not None and path.exists():
        try:
            doc = json.loads(path.read_text())
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            log.error("could not read %s (%s); ignoring it", path, exc)
        else:
            url, token = str(doc.get("url") or ""), str(doc.get("token") or "")
            if not url or not token:
                log.info("Home Assistant was disconnected from the key page (%s)", path)
                return None          # tombstone: deliberately off, don't fall back
            instance = doc.get("instance") if isinstance(doc.get("instance"), dict) else {}
            return HaSettings(url=url, token=token, publish=bool(doc.get("publish", False)),
                              source="file", instance=instance)
    return _from_yaml(yaml_conf)


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600, so the token is never world-readable,
    # not even for the instant before a chmod.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ha-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(state_dir: Path | None, settings: HaSettings) -> bool:
    """Persist ``settings`` to ha.json. False (logged) if there is nowhere to write.

    Raises OSError on a failed write - the caller tells the page, since the
    owner believes they just saved something.
    """
    path = path_for(state_dir)
    if path is None:
        log.warning("no state directory: the Home Assistant connection lasts until restart")
        return False
    _write(path, {"url": settings.url, "token": settings.token, "publish": bool(settings.publish),
                  "instance": settings.instance})
    log.info("Home Assistant connection saved to %s (%s)", path, settings.url)
    return True


def forget(state_dir: Path | None, yaml_conf: dict | None = None) -> None:
    """Disconnect for good: delete ha.json, or tombstone it if yaml would win."""
    path = path_for(state_dir)
    if path is None:
        return
    if _from_yaml(yaml_conf) is not None:
        _write(path, {"url": "", "token": "", "publish": False})
        log.info("Home Assistant disconnected (tombstone in %s overrides haslerd.yaml)", path)
    else:
        path.unlink(missing_ok=True)
        log.info("Home Assistant disconnected (%s removed)", path)


# -- instance identity ------------------------------------------------------


def fingerprint(info: dict, uuid: str | None = None) -> dict:
    """Which HA this is, from check()'s answer (+ the zeroconf uuid if known).

    The REST API has no instance id, so: the uuid HA announces over mDNS when
    discovery saw it, else location name + time zone + a hash of the home's
    coordinates rounded to ~1 km (hashed: the file needn't hold where the
    owner lives). Good enough to tell "another home" from "the same HA, new IP".
    """
    where = ""
    lat, lon = info.get("latitude"), info.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        where = hashlib.sha256(f"{lat:.2f},{lon:.2f}".encode()).hexdigest()[:16]
    return {
        "uuid": uuid or "",
        "name": str(info.get("location_name") or ""),
        "where": where,
        "time_zone": str(info.get("time_zone") or ""),
    }


def same_instance(a: dict | None, b: dict | None) -> bool:
    """True unless the two fingerprints positively disagree (unknown = same)."""
    if not a or not b:
        return True
    if a.get("uuid") and b.get("uuid"):
        return a["uuid"] == b["uuid"]
    for key in ("where", "time_zone", "name"):
        if a.get(key) and b.get(key) and a[key] != b[key]:
            return False
    return True


def load_layout_instance(state_dir: Path | None) -> dict | None:
    """The HA the key layout was made for, or None if never recorded."""
    if state_dir is None:
        return None
    path = Path(state_dir) / LAYOUT_FILENAME
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def save_layout_instance(state_dir: Path | None, instance: dict) -> None:
    """Record that the key layout now matches ``instance``. Never raises."""
    if state_dir is None:
        return
    try:
        _write(Path(state_dir) / LAYOUT_FILENAME, dict(instance))
    except OSError:
        log.exception("could not record which Home Assistant the keys belong to")
