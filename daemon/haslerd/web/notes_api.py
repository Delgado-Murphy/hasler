"""``/api/notes``: the makers' notes on the radio - "From the makers".

A maker who fixed something leaves a line on the radio itself
(``scripts/hasler-note "Replaced the memory card and put your keys back"``,
as pi or root; ``--ok`` also plays the green bloom), appended to
``<state>/maker-notes.jsonl`` as ``{"at", "by", "text"}`` and capped like the
stream events (the script trims it). The page shows the newest ones under
This radio as **From the makers**, with the date; **Seen** folds them away,
and they can be read again there. The hand-over keeps the file: the notes
are about the hardware, not the owner (docs/build-guide.md "Notes for
makers": notes name parts and settings, never people).

    GET  /api/notes        -> {"notes": [{"at", "by", "text", "seen"}, ...newest first], "unseen": n}
    POST /api/notes/seen   {"at": "<the note's at>"} | {}   (all of them)   -> {"ok": true, "unseen": 0}

"Seen" is the newest note's ``at`` the owner dismissed, kept in
``<state>/maker-notes.seen``; an older note is seen once a newer one is.
Reading never raises: a missing or odd file is "no notes".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

NOTES_FILE = "maker-notes.jsonl"
SEEN_FILE = "maker-notes.seen"
PATHS = ("/api/notes", "/api/notes/seen")
NOTES_SHOWN = 20          # the page needs the newest few; the file keeps up to 200
TEXT_MAX = 2000          # a maker's note or reply; line breaks kept (the page shows a little Markdown)


def tidy_text(text: Any, cap: int = TEXT_MAX, keep_indent: bool = False) -> str:
    """Spaces squeezed within each line, line breaks kept, at most one blank line in a row, capped.

    ``keep_indent``: the makers' Markdown as written - each line only loses its trailing spaces, so
    nested lists and code keep their indentation (and spaces inside a line stay)."""
    out: list[str] = []
    for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = line.rstrip() if keep_indent else " ".join(line.split())
        if line or (out and out[-1]):
            out.append(line)
    joined = "\n".join(out)
    return (joined.rstrip() if keep_indent else joined.strip())[:cap].rstrip()


def _epoch(stamp: Any) -> float:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def read_notes(state_dir: Path | None) -> list[dict]:
    """Every well-formed note on the card, newest first."""
    if state_dir is None:
        return []
    try:
        lines = (Path(state_dir) / NOTES_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for i, line in enumerate(lines):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not isinstance(doc, dict) or not doc.get("at") or not str(doc.get("text") or "").strip():
            continue
        out.append((_epoch(doc["at"]), i, {"at": str(doc["at"])[:40], "by": str(doc.get("by") or "")[:40],
                                           "text": tidy_text(doc["text"], keep_indent=True)}))
    # newest first; two notes in the same second keep the file's order (the later line is newer)
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [n for _, _, n in out]


class NotesApi:
    """The routes above, for ``WebApp.extra_routes``."""

    def __init__(self, app: Any, state_dir: Path | None = None) -> None:
        self.app = app
        settings_file = getattr(app, "settings_file", None)
        self.state_dir = Path(state_dir) if state_dir is not None else (
            Path(settings_file).parent if settings_file is not None else None)

    # -- routing ------------------------------------------------------------

    def route(self, method: str, path: str, body: Any) -> tuple[int, dict] | None:
        p = urlparse(path).path.rstrip("/") or "/"
        if p not in PATHS:
            return None
        method = method.upper()
        if p == "/api/notes" and method == "GET":
            return 200, self.doc()
        if p == "/api/notes/seen" and method == "POST":
            return 200, {"ok": True, **self.seen(body)}
        return 405, {"ok": False, "errors": [], "error": f"{method} not allowed here"}

    # -- reading ------------------------------------------------------------

    def _seen_at(self) -> float:
        if self.state_dir is None:
            return 0.0
        try:
            return _epoch((self.state_dir / SEEN_FILE).read_text().strip())
        except OSError:
            return 0.0

    def doc(self) -> dict:
        seen_at = self._seen_at()
        notes = []
        for n in read_notes(self.state_dir)[:NOTES_SHOWN]:
            notes.append({**n, "seen": _epoch(n["at"]) <= seen_at})
        return {"notes": notes, "unseen": sum(1 for n in notes if not n["seen"])}

    # -- doing --------------------------------------------------------------

    def seen(self, body: Any) -> dict:
        """Mark one note (``at``) and everything older as seen, or all of them."""
        body = body if isinstance(body, dict) else {}
        notes = read_notes(self.state_dir)
        if not notes:
            return {"unseen": 0}
        at = body.get("at")
        stamp = str(at) if at and any(n["at"] == str(at) for n in notes) else notes[0]["at"]
        if self.state_dir is None:
            raise ValueError("this radio keeps no notes")
        if _epoch(stamp) > self._seen_at():
            from ..boxconfig import atomic_write_text
            atomic_write_text(self.state_dir / SEEN_FILE, stamp + "\n")
            log.info("maker note of %s seen from the key page", stamp)
        return {"unseen": self.doc()["unseen"]}
