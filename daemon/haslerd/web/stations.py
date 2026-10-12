"""moOde's radio station list, for the page's station picker. Read-only."""

from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)

MOODE_DB = "/var/local/www/db/moode-sqlite3.db"
LIMIT = 50


def search(query: str, db: str = MOODE_DB, limit: int = LIMIT) -> list[dict]:
    """Stations whose name, genre or country contains ``query`` (all, if empty).

    Opened read-only per call: moOde owns that database and rewrites it, and a
    lookup a few times a minute costs nothing.
    """
    q = f"%{query.strip()}%"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        log.debug("no moOde station list at %s", db, exc_info=True)
        return []
    try:
        rows = con.execute(
            "SELECT name, station, genre, country FROM cfg_radio"
            " WHERE station LIKE 'http%' AND (name LIKE ? OR genre LIKE ? OR country LIKE ?)"
            " ORDER BY name COLLATE NOCASE LIMIT ?",
            (q, q, q, limit),
        ).fetchall()
    except sqlite3.Error:
        log.debug("station query failed", exc_info=True)
        return []
    finally:
        con.close()
    return [{"name": n, "url": u, "genre": g or "", "country": c or ""} for n, u, g, c in rows]
