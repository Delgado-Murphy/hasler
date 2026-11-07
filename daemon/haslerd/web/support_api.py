"""``/api/support``: "Send to the makers" - the report, straight to the makers' private support list.

"Tell the makers" (web/report.py) is a few lines the owner shares or copies.
When the makers have set up their check-in server (box.yaml ``checkin_url``),
the sheet also has **Send to the makers**: the same report - the text and its
JSON twin, both already stripped of secrets by ``report.build`` - plus the
owner's note, POSTed to that server's ``/v1/report`` with the check-in's
``X-Hasler-Token``. The server (tools/checkin/support.js) files it as an
issue in Delgado & Murphy's PRIVATE GitHub repo and answers the request's
number; owners never need a GitHub account, and nothing lands anywhere
public. A maker's ``/owner ...`` comment on that issue comes back as a note
from the makers (``<state>/maker-notes.jsonl``, ``by: "makers"``; the page's
**From the makers**, web/notes_api.py), fetched when the page opens (and
after the daily check-in, update/hasler_update/checkin.py), only while a
request has been sent.

    GET  /api/support          -> {"available", "requests": [{"number", "at", "deduped"}, ...newest first]}
    POST /api/support/send     {"note", "wifi"} -> {"ok", "number", "at", "deduped", "requests"}
                               409 without a checkin_url, 429 after the server's daily limit,
                               503 when the server can't be reached.
    POST /api/support/replies  {} -> {"ok", "added"}: fetch the makers' replies into the notes.

Sending is the owner's press, not the daily check-in's switch: it needs only
the makers' ``checkin_url``. The requests sent are kept in
``<state>/support-requests.json`` (number and time; the hand-over clears
the state with everything else the owner did).
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .server import Unavailable

log = logging.getLogger(__name__)

PATHS = ("/api/support", "/api/support/send", "/api/support/replies")
REQUESTS_FILE = "support-requests.json"
NOTES_FILE = "maker-notes.jsonl"          # web/notes_api.py reads it
NOTES_KEEP = 200                          # as scripts/hasler-note trims it
REQUESTS_KEPT = 20
MAX_BYTES = 16384                         # the server refuses more
TIMEOUT_S = 15.0
TOKEN_HEADER = "X-Hasler-Token"
USER_AGENT = "hasler-support/1"
TEXT_MAX = 600


class Refused(Exception):
    """The makers' server said no: ``status`` and the owner's words."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def base_url(checkin_url: str) -> str:
    """The server's root, from box.yaml ``checkin_url`` (``https://…/v1/checkin``)."""
    url = str(checkin_url or "").strip().rstrip("/")
    return re.sub(r"/v1/checkin$", "", url)


def http(method: str, url: str, body: dict | None, token: str, timeout: float = TIMEOUT_S) -> tuple[int, Any]:
    """One request to the makers' server (HTTPS only): (status, parsed JSON or None)."""
    if not url.lower().startswith("https://"):
        raise ValueError("checkin_url must be https://")
    data = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers[TOKEN_HEADER] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(1 << 20), int(r.status)
    except urllib.error.HTTPError as e:
        raw, status = e.read(1 << 16) if e.fp else b"", int(e.code)
    try:
        doc = json.loads(raw.decode("utf-8")) if raw else None
    except ValueError:
        doc = None
    return status, doc


class SupportApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any, state_dir: Path | None = None, box: Callable[[], dict] | None = None,
                 transport: Callable[..., tuple[int, Any]] = http, clock: Callable[[], float] = time.time) -> None:
        self.app = app
        settings_file = getattr(app, "settings_file", None)
        self.state_dir = Path(state_dir) if state_dir is not None else (
            Path(settings_file).parent if settings_file is not None else None)
        self._box_fn = box
        self.transport = transport
        self.clock = clock

    # -- routing ------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PATHS:
            return None
        method = method.upper()
        if p == "/api/support" and method == "GET":
            return 200, self.doc()
        if p == "/api/support/send" and method == "POST":
            try:
                return 200, {"ok": True, **self.send(body)}
            except Refused as exc:
                return exc.status, {"ok": False, "errors": [], "error": str(exc)}
        if p == "/api/support/replies" and method == "POST":
            return 200, {"ok": True, **self.pull_replies()}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    # -- box.yaml -------------------------------------------------------------

    def _box(self) -> dict:
        if self._box_fn is not None:
            return self._box_fn() or {}
        paths = getattr(self.app, "box_paths", None)
        if paths is None:
            return {}
        try:
            from .. import boxconfig
            return boxconfig.read_box_file(paths)
        except Exception:
            log.debug("could not read box.yaml", exc_info=True)
            return {}

    def _server(self) -> tuple[str, str, str]:
        box = self._box()
        return (base_url(str(box.get("checkin_url") or "")), str(box.get("checkin_token") or "").strip(),
                str(box.get("box_id") or "").strip())

    # -- the requests sent ----------------------------------------------------

    def requests(self) -> list[dict]:
        if self.state_dir is None:
            return []
        try:
            doc = json.loads((self.state_dir / REQUESTS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for r in (doc.get("requests") if isinstance(doc, dict) else None) or []:
            if isinstance(r, dict) and isinstance(r.get("number"), int):
                out.append({"number": r["number"], "at": str(r.get("at") or "")[:40], "deduped": bool(r.get("deduped"))})
        return out

    def _remember(self, number: int, at: str, deduped: bool) -> list[dict]:
        kept = [r for r in self.requests() if not (deduped and r["number"] == number)]
        kept = ([{"number": number, "at": at, "deduped": deduped}] + kept)[:REQUESTS_KEPT]
        if self.state_dir is not None:
            from ..boxconfig import atomic_write_text
            atomic_write_text(self.state_dir / REQUESTS_FILE, json.dumps({"schema": 1, "requests": kept}, indent=1) + "\n")
        return kept

    def doc(self) -> dict:
        base, _, box_id = self._server()
        return {"available": bool(base and box_id), "requests": self.requests()}

    # -- sending --------------------------------------------------------------

    def payload(self, note: str, wifi: bool) -> dict:
        """What goes: the report (text + JSON twin, secrets already stripped) and the radio's ids."""
        from . import report
        _, _, box_id = self._server()
        out = report.build(self.app, note=note, wifi=wifi)
        doc = out["json"]
        body = {"schema": 1, "box_id": box_id, "short_id": (doc.get("radio") or {}).get("short_id"),
                "note": doc.get("note") or "", "text": out["text"], "json": doc}
        if len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            body["json"] = None                   # the text alone says it all; never more than the server takes
        return body

    def send(self, body: Any) -> dict:
        body = body if isinstance(body, dict) else {}
        base, token, box_id = self._server()
        if not base or not box_id:
            raise Refused(409, "The makers haven’t set up sending from the radio. Use Share or Copy instead.")
        wifi = body.get("wifi", True) not in (False, 0, "0", "false", "no", "off")
        payload = self.payload(str(body.get("note") or ""), wifi)
        try:
            status, doc = self.transport("POST", base + "/v1/report", payload, token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.info("support request not sent: %s", exc)
            raise Unavailable("Couldn’t reach the makers just now. Try again later, or use Share or Copy.") from None
        if status == 429:
            raise Refused(429, "You’ve sent several today already. The makers have them; use Share or Copy for anything more.")
        if status == 403:
            raise Refused(502, "The makers’ server didn’t accept this radio. Use Share or Copy instead.")
        if not (200 <= status < 300) or not isinstance(doc, dict) or not isinstance(doc.get("number"), int):
            log.info("support request refused: %s %s", status, doc)
            raise Unavailable("The makers’ server couldn’t take it just now. Try again later, or use Share or Copy.")
        at = self._now_iso()
        number, deduped = doc["number"], bool(doc.get("deduped"))
        kept = self._remember(number, at, deduped)
        log.info("support request sent: #%s%s", number, " (again)" if deduped else "")
        return {"number": number, "at": at, "deduped": deduped, "requests": kept}

    def _now_iso(self) -> str:
        from datetime import datetime
        return datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec="seconds")

    # -- replies --------------------------------------------------------------

    def _known_reply_ids(self) -> set:
        ids = set()
        try:
            lines = (self.state_dir / NOTES_FILE).read_text(encoding="utf-8").splitlines()
        except (OSError, TypeError):
            return ids
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("reply_id") is not None:
                ids.add(str(d["reply_id"]))
        return ids

    def pull_replies(self) -> dict:
        """The makers' ``/owner`` replies, into the notes; then tell the server they're kept.

        Only while a request has been sent (nothing to ask otherwise). Never raises
        over the network: an unreachable server is ``{"added": 0, "error"}``."""
        base, token, box_id = self._server()
        if not base or not box_id or self.state_dir is None or not self.requests():
            return {"added": 0}
        url = base + "/v1/replies?" + urllib.parse.urlencode({"box_id": box_id})
        try:
            status, doc = self.transport("GET", url, None, token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"added": 0, "error": str(exc)[:120]}
        replies = doc.get("replies") if status == 200 and isinstance(doc, dict) else None
        if not isinstance(replies, list):
            return {"added": 0, "error": f"the makers' server answered {status}"}
        known, lines, ids = self._known_reply_ids(), [], []
        for r in replies:
            if not isinstance(r, dict) or r.get("id") is None:
                continue
            ids.append(r["id"])
            text = " ".join(str(r.get("text") or "").split())[:TEXT_MAX]
            if not text or str(r["id"]) in known:
                continue
            known.add(str(r["id"]))
            # "at" is when it reached the radio, so it shows as new even after an older note was seen
            note = {"at": self._now_iso(), "by": "makers", "text": text, "reply_id": r["id"],
                    "replied_at": str(r.get("at") or "")[:40]}
            if isinstance(r.get("issue"), int):
                note["issue"] = r["issue"]
            lines.append(json.dumps(note, ensure_ascii=False))
        if lines:
            path = self.state_dir / NOTES_FILE
            try:
                old = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                old = []
            from ..boxconfig import atomic_write_text
            atomic_write_text(path, "\n".join((old + lines)[-NOTES_KEEP:]) + "\n")
            log.info("%d note(s) from the makers' replies", len(lines))
        if ids:
            try:
                self.transport("POST", base + "/v1/replies/ack", {"box_id": box_id, "ids": ids}, token)
            except (urllib.error.URLError, OSError, ValueError):
                pass                              # asked again next time; the ids keep it single
        return {"added": len(lines)}
