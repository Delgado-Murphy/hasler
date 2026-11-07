"""``/api/support``: "Send to the makers" - the report, straight to the makers' private support list.

"Tell the makers" (web/report.py) is a few lines the owner can share or copy;
**Send to the makers** is the sheet's main button: the same report - the text
and its JSON twin, both already stripped of secrets by ``report.build`` - plus
the owner's note, POSTed to the makers' server's ``/v1/report``. The server
(tools/checkin/support.js) files it as an issue in Delgado & Murphy's PRIVATE
GitHub repo and answers the request's number; owners never need a GitHub
account, and nothing lands anywhere public. A maker's ``/owner ...`` comment
on that issue - and the issue's state: seen, done, reopened - comes back as a
note from the makers (``<state>/maker-notes.jsonl``, ``by: "makers"``, with
the issue's number; the page's **From the makers** and **Your requests**).

Where it goes: every radio can send out of the box. The makers' server is
``DEFAULT_URL`` unless box.yaml says otherwise (``checkin_url: https://…``
another server; ``checkin_url: ""`` switches sending off). ``--fake`` has no
default, so a page test never reaches the real server.

The radio's word (tools/checkin/enrol.js): box.yaml ``checkin_token`` (the
makers' shared word) when set, as before; otherwise the radio's own token,
asked for once with ``POST /v1/enrol {box_id}`` at the first send and kept in
``<state>/secrets/support.json`` (0600, kept by the hand-over: it is the
cabinet's, not the owner's). A server that no longer knows the token (403)
gets one fresh enrolment; one that knows the radio already (409) means a maker
has to clear it, and the sheet says so.

Offline: a send that can't reach the server waits in ``<state>/support-outbox.json``
and goes by itself once the radio is online (``tick``, from the main loop, with
a growing pause between tries); the page says "Waiting to send". The main loop
also fetches the makers' replies every half hour while a request is open.

    GET  /api/support          -> {"available", "requests": [{"number", "at", "deduped", "state", "title",
                                    "replies": [{"at", "text", "state", "seen"}...oldest first], "unseen"}...newest first],
                                   "pending": [{"id", "at", "title", "error"}], "unseen_replies"}
                               state: sent | seen (the makers' "seen" label, or reopened) | done (closed);
                               the thread mixes in the owner's write-backs, in time order: {"at", "text", "mine": true}
    POST /api/support/send     {"note", "wifi"} -> {"ok", "number", "at", "deduped", "requests"}
                               or {"ok", "queued": true, "pending"} when the server can't be reached;
                               409 with sending off, 429 after the server's daily limit, 502 refused,
                               503 the server answered but couldn't take it (try again).
    POST /api/support/retry    {} -> {"ok", "sent": n, "pending"}: the waiting ones, now.
    POST /api/support/forget   {"id"} -> {"ok", "pending"}: a waiting one, not sent after all.
    POST /api/support/replies  {} -> {"ok", "added"}: fetch the makers' replies into the notes; an edited
                               reply (same id, ``edited``) takes the old one's place.
    POST /api/support/reply    {"number", "note"} -> {"ok", "requests"}: the owner writes back on a request
                               (the server's ``/v1/reply``: a comment on the same issue, reopened if closed);
                               the request is "sent" again. 503 offline (nothing kept: write again), 429.

The requests sent are kept in ``<state>/support-requests.json`` (number, time,
the note's first line; the hand-over clears it and the outbox with
everything else the owner did).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .notes_api import tidy_text
from .server import Unavailable

log = logging.getLogger(__name__)

# The makers' server, for every radio whose box.yaml doesn't name another (update/hasler_update/layout.py
# has the same default for the daily check-in; a test keeps the two equal).
DEFAULT_URL = "https://hasler-checkin.delgado-murphy.workers.dev"   # the root; /v1/... are derived (base_url, endpoint)
PATHS = ("/api/support", "/api/support/send", "/api/support/retry", "/api/support/forget", "/api/support/replies",
         "/api/support/reply")
REQUESTS_FILE = "support-requests.json"
OUTBOX_FILE = "support-outbox.json"
TOKEN_FILE = "support.json"               # under secrets/: the radio's own word
NOTES_FILE = "maker-notes.jsonl"          # web/notes_api.py reads it
SEEN_FILE = "maker-notes.seen"
NOTES_KEEP = 200                          # as scripts/hasler-note trims it
REQUESTS_KEPT = 20
FOLLOWUPS_KEPT = 30                       # the owner's write-backs on one request
FOLLOWUP_MAX = 2000
OUTBOX_KEPT = 5
STATES = ("sent", "seen", "done")         # a request: sent, the makers have seen it, done (the issue closed)
MAX_BYTES = 16384                         # the server refuses more
TIMEOUT_S = 15.0
TOKEN_HEADER = "X-Hasler-Token"
USER_AGENT = "hasler-support/1"
TITLE_MAX = 120
RETRY_FIRST_S = 60.0                      # the outbox: a minute, then twice as long each time...
RETRY_MAX_S = 1800.0                      # ...up to half an hour
REPLIES_EVERY_S = 1800.0                  # the makers' replies, while a request is open
OPEN_DAYS = 14                            # a done request is still asked about this long (it may be reopened)

OFF_WORDS = "Sending straight from the radio is off here. Use Share, Copy or Email instead."
NOT_KNOWN_WORDS = ("The makers don’t recognise this radio any more. Use Email or Share for now, "
                   "and mention it: they can reset its line.")


class Refused(Exception):
    """The makers' server said no: ``status`` and the owner's words."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Offline(Exception):
    """The makers' server couldn't be reached at all: the request waits."""


def base_url(checkin_url: str) -> str:
    """The server's root, from box.yaml ``checkin_url``: the root itself or the full
    ``https://…/v1/checkin`` - both forms work (update/hasler_update/checkin.py agrees)."""
    url = str(checkin_url or "").strip().rstrip("/")
    return re.sub(r"/v1/checkin$", "", url)


def endpoint(checkin_url: str, route: str) -> str:
    """``/v1/<route>`` on the server ``checkin_url`` names (either form); "" when it names none."""
    base = base_url(checkin_url)
    return f"{base}/v1/{route}" if base else ""


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


def first_line(note: str) -> str:
    line = next((x for x in str(note or "").splitlines() if x.strip()), "")
    line = " ".join(line.split())
    return line if len(line) <= TITLE_MAX else line[:TITLE_MAX - 1] + "…"


def _epoch(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


class SupportApi:
    """The routes above, for ``WebApp.extra_routes``; ``tick`` for the main loop."""

    def __init__(self, app: Any, state_dir: Path | None = None, box: Callable[[], dict] | None = None,
                 transport: Callable[..., tuple[int, Any]] = http, clock: Callable[[], float] = time.time,
                 default_url: str = DEFAULT_URL) -> None:
        self.app = app
        settings_file = getattr(app, "settings_file", None)
        self.state_dir = Path(state_dir) if state_dir is not None else (
            Path(settings_file).parent if settings_file is not None else None)
        self._box_fn = box
        self.transport = transport
        self.clock = clock
        self.default_url = default_url
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._next_try = 0.0
        self._pause = RETRY_FIRST_S
        self._next_replies = 0.0

    # -- routing ------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PATHS:
            return None
        method = method.upper()
        if p == "/api/support" and method == "GET":
            return 200, self.doc()
        if method != "POST":
            return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}
        if p == "/api/support/send":
            try:
                return 200, {"ok": True, **self.send(body)}
            except Refused as exc:
                return exc.status, {"ok": False, "errors": [], "error": str(exc)}
        if p == "/api/support/retry":
            return 200, {"ok": True, **self.flush(force=True)}
        if p == "/api/support/forget":
            return 200, {"ok": True, **self.forget(body)}
        if p == "/api/support/reply":
            try:
                return 200, {"ok": True, **self.followup(body)}
            except Refused as exc:
                return exc.status, {"ok": False, "errors": [], "error": str(exc)}
        return 200, {"ok": True, **self.pull_replies()}

    # -- box.yaml and the radio's word ----------------------------------------

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
        """(the server's root or "" when sending is off, the shared word or "", box_id)."""
        box = self._box()
        url = box.get("checkin_url")
        url = self.default_url if url is None else url     # absent: the makers' own; "" switches it off
        return (base_url(str(url or "")), str(box.get("checkin_token") or "").strip(),
                str(box.get("box_id") or "").strip())

    def _token_file(self) -> Path | None:
        if self.state_dir is None:
            return None
        from ..boxconfig import secret_file
        return secret_file(self.state_dir, TOKEN_FILE)

    def _stored_token(self, base: str, box_id: str) -> str:
        path = self._token_file()
        try:
            doc = json.loads(path.read_text(encoding="utf-8")) if path is not None else None
        except (OSError, ValueError):
            return ""
        if isinstance(doc, dict) and doc.get("server") == base and doc.get("box_id") == box_id:
            return str(doc.get("token") or "")
        return ""

    def _forget_token(self) -> None:
        path = self._token_file()
        if path is not None:
            path.unlink(missing_ok=True)

    def _enrol(self, base: str, box_id: str) -> str:
        """Ask the server for this radio's own word, once; keep it. Raises Offline / Refused."""
        try:
            status, doc = self.transport("POST", endpoint(base, "enrol"), {"schema": 1, "box_id": box_id}, "")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise Offline(str(exc)) from None
        if status == 409:
            log.warning("support: the makers' server already knows box %s… - a maker has to clear it", box_id[:8])
            raise Refused(502, NOT_KNOWN_WORDS)
        if status == 404:
            return ""                     # an older server without enrolment: send without a word, as before
        if status != 200 or not isinstance(doc, dict) or not isinstance(doc.get("token"), str) or not doc["token"]:
            log.info("support: enrolment refused: %s %s", status, doc)
            raise Unavailable("The makers couldn’t take it just now. Try again in a while, or use Share or Email.")
        path = self._token_file()
        if path is not None:
            from ..boxconfig import atomic_write_text
            atomic_write_text(path, json.dumps({"schema": 1, "server": base, "box_id": box_id, "token": doc["token"],
                                                "at": self._now_iso()}) + "\n", mode=0o600)
        log.info("support: this radio has its own word with the makers' server now")
        return doc["token"]

    def _word(self, base: str, shared: str, box_id: str, enrol: bool = True) -> tuple[str, str]:
        """(token, kind): the shared word, the radio's own, or ("", "none")."""
        if shared:
            return shared, "shared"
        own = self._stored_token(base, box_id)
        if own:
            return own, "own"
        if enrol:
            own = self._enrol(base, box_id)
            return (own, "own") if own else ("", "none")
        return "", "none"

    # -- the requests sent ----------------------------------------------------

    def _read(self, name: str, key: str) -> list:
        if self.state_dir is None:
            return []
        try:
            doc = json.loads((self.state_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        items = doc.get(key) if isinstance(doc, dict) else None
        return items if isinstance(items, list) else []

    def requests(self) -> list[dict]:
        out = []
        for r in self._read(REQUESTS_FILE, "requests"):
            if isinstance(r, dict) and isinstance(r.get("number"), int):
                state = r.get("state") if r.get("state") in STATES else "sent"
                fus = [{"at": str(x.get("at") or "")[:40], "text": tidy_text(x.get("text"), FOLLOWUP_MAX)}
                       for x in (r.get("followups") if isinstance(r.get("followups"), list) else [])
                       if isinstance(x, dict) and str(x.get("text") or "").strip()]
                out.append({"number": r["number"], "at": str(r.get("at") or "")[:40], "deduped": bool(r.get("deduped")),
                            "state": state, "title": str(r.get("title") or "")[:TITLE_MAX], "followups": fus[-FOLLOWUPS_KEPT:]})
        return out

    def _write(self, name: str, key: str, items: list) -> None:
        if self.state_dir is not None:
            from ..boxconfig import atomic_write_text
            atomic_write_text(self.state_dir / name, json.dumps({"schema": 1, key: items}, indent=1, ensure_ascii=False) + "\n")

    def _write_requests(self, kept: list[dict]) -> None:
        self._write(REQUESTS_FILE, "requests", kept)

    def _remember(self, number: int, at: str, deduped: bool, title: str) -> list[dict]:
        with self._lock:
            old = next((r for r in self.requests() if r["number"] == number), None)
            kept = [r for r in self.requests() if r["number"] != number]
            # sent again (the same issue): keep what the makers already said about it, and its first words
            kept = ([{"number": number, "at": at, "deduped": deduped, "state": old["state"] if old else "sent",
                      "title": (old and old["title"]) or title, "followups": old["followups"] if old else []}] + kept)[:REQUESTS_KEPT]
            self._write_requests(kept)
            return kept

    def _set_states(self, replies: list) -> None:
        """A request's state from the makers' replies, in order: "seen" (the label, or reopened)
        and "done" (closed). A late "seen" label never undoes "done"; reopening does."""
        with self._lock:
            kept, changed = self.requests(), False
            for r in replies:
                if not isinstance(r, dict) or r.get("state") not in STATES or not isinstance(r.get("issue"), int):
                    continue
                req = next((x for x in kept if x["number"] == r["issue"]), None)
                if req is None:
                    continue
                new = r["state"]
                if new == "seen" and req["state"] == "done" and not str(r.get("id")).startswith("reopened:"):
                    continue
                if req["state"] != new:
                    req["state"], changed = new, True
            if changed:
                self._write_requests(kept)

    # -- the makers' replies, as the page shows them --------------------------

    def _note_lines(self) -> list[dict]:
        if self.state_dir is None:
            return []
        try:
            lines = (self.state_dir / NOTES_FILE).read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if isinstance(d, dict):
                out.append(d)
        return out

    def _seen_at(self) -> float:
        try:
            return _epoch((self.state_dir / SEEN_FILE).read_text().strip()) if self.state_dir is not None else 0.0
        except OSError:
            return 0.0

    def doc(self) -> dict:
        base, _, box_id = self._server()
        seen_at = self._seen_at()
        threads: dict[int, list] = {}
        for n in self._note_lines():
            if n.get("by") != "makers" or not isinstance(n.get("issue"), int) or not str(n.get("text") or "").strip():
                continue
            threads.setdefault(n["issue"], []).append({
                "at": str(n.get("replied_at") or n.get("at") or "")[:40],
                "text": tidy_text(n["text"]),
                "state": n.get("state") if n.get("state") in STATES else None,
                "seen": _epoch(n.get("at")) <= seen_at,
                "_k": _epoch(n.get("at"))})       # in order by the radio's own clock, as the owner's words are
        reqs = []
        for r in self.requests():
            fus = r.pop("followups", [])
            replies = threads.get(r["number"], [])
            unseen = sum(1 for x in replies if not x["seen"])
            if fus:
                mine = [{"at": x["at"], "text": x["text"], "state": None, "seen": True, "mine": True, "_k": _epoch(x["at"])} for x in fus]
                replies = sorted(replies + mine, key=lambda x: x["_k"])
            replies = [{k: v for k, v in x.items() if k != "_k"} for x in replies]
            reqs.append({**r, "replies": replies, "unseen": unseen})
        return {"available": bool(base and box_id), "requests": reqs, "pending": self.pending(),
                "unseen_replies": sum(r["unseen"] for r in reqs)}

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

    def _deliver(self, payload: dict) -> dict:
        """POST one report; the server's {"number", "deduped"}. Raises Offline, Refused, Unavailable."""
        base, shared, box_id = self._server()
        if not base or not box_id:
            raise Refused(409, OFF_WORDS)
        payload = {**payload, "box_id": box_id}
        token, kind = self._word(base, shared, box_id)
        for attempt in (1, 2):
            try:
                status, doc = self.transport("POST", endpoint(base, "report"), payload, token)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise Offline(str(exc)) from None
            if status == 403 and kind == "own" and attempt == 1:
                # the server lost or cleared this radio's word: ask once more, then try again
                self._forget_token()
                token, kind = self._word(base, shared, box_id)
                continue
            break
        if status == 429:
            raise Refused(429, "You’ve sent several today. The makers have them; for anything more, use Share or Email.")
        if status == 403:
            raise Refused(502, NOT_KNOWN_WORDS if kind != "shared" else
                          "The makers can’t take messages from this radio yet. Use Share or Email instead.")
        if status == 400 or status == 413:
            log.info("support request refused: %s %s", status, doc)
            raise Refused(502, "The makers couldn’t read this one. Use Share or Email instead.")
        if not (200 <= status < 300) or not isinstance(doc, dict) or not isinstance(doc.get("number"), int):
            log.info("support request not taken: %s %s", status, doc)
            raise Unavailable("The makers couldn’t take it just now. Try again in a while, or use Share or Email.")
        return doc

    def send(self, body: Any) -> dict:
        body = body if isinstance(body, dict) else {}
        base, _, box_id = self._server()
        if not base or not box_id:
            raise Refused(409, OFF_WORDS)
        wifi = body.get("wifi", True) not in (False, 0, "0", "false", "no", "off")
        note = str(body.get("note") or "")
        payload = self.payload(note, wifi)
        try:
            doc = self._deliver(payload)
        except Offline as exc:
            log.info("support request waits to be sent: %s", exc)
            return {"queued": True, "pending": self._queue(payload, first_line(note))}
        at = self._now_iso()
        number, deduped = doc["number"], bool(doc.get("deduped"))
        kept = self._remember(number, at, deduped, first_line(note))
        log.info("support request sent: #%s%s", number, " (again)" if deduped else "")
        return {"number": number, "at": at, "deduped": deduped, "requests": kept}

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self.clock()).astimezone().isoformat(timespec="seconds")

    def followup(self, body: Any) -> dict:
        """The owner writes back on one of their requests: a comment on the same issue (``/v1/reply``)."""
        body = body if isinstance(body, dict) else {}
        number, note = body.get("number"), tidy_text(body.get("note"), FOLLOWUP_MAX)
        if not isinstance(number, int) or not any(r["number"] == number for r in self.requests()):
            raise Refused(404, "That request isn’t on this radio.")
        if not note:
            raise Refused(400, "Write a few words first.")
        base, shared, box_id = self._server()
        if not base or not box_id:
            raise Refused(409, OFF_WORDS)
        token, kind = self._word(base, shared, box_id)
        try:
            status, doc = self.transport("POST", endpoint(base, "reply"), {"schema": 1, "box_id": box_id, "issue": number, "note": note}, token)
        except (urllib.error.URLError, OSError, ValueError):
            raise Refused(503, "The radio can’t reach the internet just now. Your words are still here; send them once it’s back.") from None
        if status == 429:
            raise Refused(429, "That’s a lot for one day. The makers have your words; write again tomorrow.")
        if status in (403, 404):
            raise Refused(502, NOT_KNOWN_WORDS if kind != "shared" else "The makers couldn’t match this to your request. Start a new one instead.")
        if not (200 <= status < 300) or not isinstance(doc, dict) or not doc.get("ok"):
            log.info("support follow-up not taken: %s %s", status, doc)
            raise Refused(503, "The makers couldn’t take it just now. Your words are still here; try again in a while.")
        with self._lock:
            kept = self.requests()
            for r in kept:
                if r["number"] == number:
                    r["followups"] = (r["followups"] + [{"at": self._now_iso(), "text": note}])[-FOLLOWUPS_KEPT:]
                    r["state"] = "sent"           # open again, waiting for the makers
            self._write_requests(kept)
        log.info("support follow-up sent on #%s", number)
        return {"requests": self.doc()["requests"]}

    # -- the outbox: sent once the radio is online again -----------------------

    def pending(self) -> list[dict]:
        return [{"id": str(x.get("id")), "at": str(x.get("at") or "")[:40], "title": str(x.get("title") or "")[:TITLE_MAX],
                 "error": str(x.get("error") or "") or None}
                for x in self._read(OUTBOX_FILE, "items") if isinstance(x, dict) and isinstance(x.get("payload"), dict)]

    def _queue(self, payload: dict, title: str) -> list[dict]:
        with self._lock:
            items = [x for x in self._read(OUTBOX_FILE, "items") if isinstance(x, dict)]
            items.append({"id": uuid.uuid4().hex[:12], "at": self._now_iso(), "title": title, "payload": payload, "error": None})
            self._write(OUTBOX_FILE, "items", items[-OUTBOX_KEPT:])
            self._next_try, self._pause = 0.0, RETRY_FIRST_S
        return self.pending()

    def forget(self, body: Any) -> dict:
        want = str((body or {}).get("id") or "") if isinstance(body, dict) else ""
        with self._lock:
            items = [x for x in self._read(OUTBOX_FILE, "items") if isinstance(x, dict) and str(x.get("id")) != want]
            self._write(OUTBOX_FILE, "items", items)
        return {"pending": self.pending()}

    def forget_checkin(self, background: bool = True) -> bool:
        """The owner switched the daily check-in off (or handed the radio over): ask
        the makers' server, once, to delete what it keeps for this radio
        (``POST /v1/forget``). Never enrols for it, never raises, never retries:
        what isn't forgotten now goes in the server's 90-day sweep. True when
        a request was started (or, with ``background=False``, answered 2xx)."""
        base, shared, box_id = self._server()
        if not base or not box_id:
            return False
        token = shared or self._stored_token(base, box_id)

        def go() -> bool:
            try:
                status, _ = self.transport("POST", endpoint(base, "forget"), {"schema": 1, "box_id": box_id}, token)
                log.info("check-in: asked the makers' server to forget this radio (%s)", status)
                return 200 <= int(status) < 300
            except Exception as exc:        # offline, refused: the sweep does it later
                log.info("check-in: couldn't ask the makers' server to forget this radio: %s", exc)
                return False
        if not background:
            return go()
        threading.Thread(target=go, name="checkin-forget", daemon=True).start()
        return True

    def flush(self, force: bool = False) -> dict:
        """Send what waits, oldest first; stop at the first that can't reach the server.
        A refusal (the server said no) stays with its words and isn't tried again by itself."""
        sent = 0
        with self._lock:
            items = [x for x in self._read(OUTBOX_FILE, "items") if isinstance(x, dict) and isinstance(x.get("payload"), dict)]
            for item in list(items):
                if item.get("error") and not force:
                    continue
                try:
                    doc = self._deliver(item["payload"])
                except (Offline, Unavailable):
                    break                         # not reached, or busy: later
                except Refused as exc:
                    item["error"] = str(exc)
                    continue
                self._remember(doc["number"], self._now_iso(), bool(doc.get("deduped")), str(item.get("title") or ""))
                items.remove(item)
                sent += 1
                log.info("support request sent after waiting: #%s", doc["number"])
            self._write(OUTBOX_FILE, "items", items)
        return {"sent": sent, "pending": self.pending()}

    def _open_requests(self) -> bool:
        now = self.clock()
        return any(r["state"] != "done" or now - _epoch(r["at"]) < OPEN_DAYS * 86400 for r in self.requests())

    def tick(self, now: float, online: bool = True) -> None:
        """From the main loop: the outbox once online (a minute, two, four… up to half an hour
        apart), and the makers' replies every half hour while a request is open. Never raises."""
        if not online or (self._thread is not None and self._thread.is_alive()):
            return
        waiting = any(not x["error"] for x in self.pending())
        due_send = waiting and now >= self._next_try
        due_replies = now >= self._next_replies and self._open_requests()
        if not (due_send or due_replies):
            return
        if due_replies:
            self._next_replies = now + REPLIES_EVERY_S

        def run() -> None:
            try:
                if due_send:
                    left = any(not x["error"] for x in self.flush()["pending"])
                    if left:
                        self._next_try, self._pause = now + self._pause, min(RETRY_MAX_S, self._pause * 2)
                    else:
                        self._next_try, self._pause = 0.0, RETRY_FIRST_S
                if due_replies:
                    self.pull_replies()
            except Exception:
                log.debug("support tick failed", exc_info=True)

        self._thread = threading.Thread(target=run, name="support", daemon=True)
        self._thread.start()

    # -- replies --------------------------------------------------------------

    def pull_replies(self) -> dict:
        """The makers' ``/owner`` replies, into the notes; then tell the server they're kept.

        Only while a request has been sent (nothing to ask otherwise), and only replies about
        this owner's requests become notes. Never raises over the network: an unreachable
        server is ``{"added": 0, "error"}``."""
        base, shared, box_id = self._server()
        if not base or not box_id or self.state_dir is None or not self.requests():
            return {"added": 0}
        token, _ = self._word(base, shared, box_id, enrol=False)
        url = endpoint(base, "replies") + "?" + urllib.parse.urlencode({"box_id": box_id})
        try:
            status, doc = self.transport("GET", url, None, token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"added": 0, "error": str(exc)[:120]}
        replies = doc.get("replies") if status == 200 and isinstance(doc, dict) else None
        if not isinstance(replies, list):
            return {"added": 0, "error": f"the makers' server answered {status}"}
        with self._lock:
            mine = {r["number"] for r in self.requests()}
            known = {str(d["reply_id"]) for d in self._note_lines() if d.get("reply_id") is not None}
            lines, ids, edits = [], [], {}
            for r in replies:
                if not isinstance(r, dict) or r.get("id") is None:
                    continue
                ids.append(r["id"])
                text = tidy_text(r.get("text"))
                issue = r.get("issue") if isinstance(r.get("issue"), int) else None
                if text and r.get("edited") and str(r["id"]) in known:
                    edits[str(r["id"])] = text    # the maker edited their reply: the new words in its place
                    continue
                if not text or str(r["id"]) in known or (issue is not None and issue not in mine):
                    continue                      # said already, or about a request of the radio's last owner
                known.add(str(r["id"]))
                # "at" is when it reached the radio, so it shows as new even after an older note was seen
                note = {"at": self._now_iso(), "by": "makers", "text": text, "reply_id": r["id"],
                        "replied_at": str(r.get("at") or "")[:40]}
                if issue is not None:
                    note["issue"] = issue
                if r.get("state") in STATES:
                    note["state"] = r["state"]
                lines.append(json.dumps(note, ensure_ascii=False))
            if lines or edits:
                path = self.state_dir / NOTES_FILE
                try:
                    old = path.read_text(encoding="utf-8").splitlines()
                except OSError:
                    old = []
                if edits:
                    old = [self._edited(line, edits) for line in old]
                from ..boxconfig import atomic_write_text
                atomic_write_text(path, "\n".join((old + lines)[-NOTES_KEEP:]) + "\n")
                log.info("%d note(s) from the makers' replies", len(lines))
            self._set_states(replies)
        if ids:
            try:
                self.transport("POST", endpoint(base, "replies") + "/ack", {"box_id": box_id, "ids": ids}, token)
            except (urllib.error.URLError, OSError, ValueError):
                pass                              # asked again next time; the ids keep it single
        return {"added": len(lines), **({"edited": len(edits)} if edits else {})}

    @staticmethod
    def _edited(line: str, edits: dict) -> str:
        try:
            d = json.loads(line)
        except ValueError:
            return line
        if not isinstance(d, dict) or str(d.get("reply_id")) not in edits:
            return line
        return json.dumps({**d, "text": edits[str(d["reply_id"])], "edited": True}, ensure_ascii=False)
