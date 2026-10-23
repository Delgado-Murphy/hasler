#!/usr/bin/env python3
"""Probe every radio stream the radio ships (workflow stations-check.yml).

Stdlib + curl. Reads config/stations/*.json (``stations[].url``) and
config/catalog/catalog.json (items with ``source: radio``, ``uri``), fetches
the first bytes of each (redirects followed; an HLS playlist must look like
one and its first entry must answer too), tries twice, and writes a report.

    stations_check.py [--json report.json] [--markdown report.md] [--only SUBSTR]

Exit 0 always (the workflow reads the report). Verdicts:
    ok        audio (or a playlist) came back
    blocked   HTTP 401/403/451 - often geo-blocking: GitHub's runners are in
              the US, the stations were checked from Spain. Listed apart.
    dead      anything else: DNS, refused, timeout, 404/410/5xx, HTML, empty
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UA = "Mozilla/5.0 (X11; Linux aarch64) hasler-stations-check (+https://github.com/Delgado-Murphy/hasler)"
TIMEOUT = 20
READ = 16384
AUDIO_CT = re.compile(r"audio/|mpegurl|application/octet-stream|application/ogg|video/mp2t|"
                      r"application/x-scpls|application/pls|application/dash\+xml|application/vnd\.apple", re.I)


def gather(root: Path = ROOT) -> list[dict]:
    """[{name, url, where}] - one entry per distinct URL, every file it appears in."""
    seen: dict[str, dict] = {}

    def add(name: str, url: str, where: str) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return
        e = seen.setdefault(url, {"name": name, "url": url, "where": []})
        if where not in e["where"]:
            e["where"].append(where)

    for f in sorted((root / "config" / "stations").glob("*.json")):
        for s in json.loads(f.read_text()).get("stations", []):
            add(s.get("name", "?"), s.get("url"), f.relative_to(root).as_posix())
    cat = root / "config" / "catalog" / "catalog.json"
    if cat.is_file():
        def walk(node):
            if isinstance(node, dict):
                if node.get("source") == "radio" and node.get("uri"):
                    add(node.get("name") or node.get("title") or "?", node["uri"], cat.relative_to(root).as_posix())
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(json.loads(cat.read_text()))
    return list(seen.values())


class HTTPStatus(Exception):
    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


def fetch(url: str, n: int = READ) -> tuple[int, str, bytes, str]:
    """(status, content-type, first n bytes, final url) via curl - the same TLS
    and HTTP stack (libcurl) mpd plays the stream with, ICY answers included.
    Raises HTTPStatus for >= 400 and OSError for network failures."""
    with tempfile.TemporaryDirectory(prefix="stations-") as tmp:
        hdr = Path(tmp) / "h"
        p = subprocess.Popen(["curl", "-sS", "-L", "--max-redirs", "8", "-A", UA, "-H", "Icy-MetaData: 0",
                              "--connect-timeout", "10", "--max-time", str(TIMEOUT), "-D", str(hdr), "-o", "-",
                              url], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        body = b""
        try:
            while len(body) < n:
                chunk = p.stdout.read1(n - len(body)) if hasattr(p.stdout, "read1") else p.stdout.read(n - len(body))
                if not chunk:
                    break
                body += chunk
        finally:
            if p.poll() is None:
                p.kill()
            _, err = p.communicate(timeout=30)
        heads = hdr.read_text(errors="replace") if hdr.exists() else ""
    blocks = [b for b in re.split(r"\r?\n\r?\n", heads) if b.strip()]
    status, ct, final = 0, "", url
    for blk in blocks:                       # the last response after redirects counts
        lines = blk.splitlines()
        m = re.match(r"^(?:HTTP/\S+|ICY)\s+(\d{3})", lines[0].strip())
        if not m:
            continue
        status = int(m.group(1))
        ct = next((ln.split(":", 1)[1].strip() for ln in lines[1:] if ln.lower().startswith("content-type:")), "")
        loc = next((ln.split(":", 1)[1].strip() for ln in lines[1:] if ln.lower().startswith("location:")), None)
        if loc:
            final = urllib.parse.urljoin(final, loc)
    if status >= 400:
        raise HTTPStatus(status)
    if not body and (status == 0 or p.returncode not in (0, 28)):
        msg = err.decode(errors="replace").strip().splitlines()[-1:] or [f"curl exit {p.returncode}"]
        raise OSError(msg[0].removeprefix("curl: ")[:160])
    return status, ct, body, final


def probe_once(url: str) -> tuple[str, str]:
    try:
        status, ct, body, final = fetch(url)
    except HTTPStatus as e:
        return ("blocked" if e.code in (401, 403, 451) else "dead"), f"HTTP {e.code}"
    except OSError as e:
        return "dead", str(e)[:160]
    head = body[:2048].lstrip()
    is_m3u = head.startswith(b"#EXTM3U") or "mpegurl" in ct.lower()
    if is_m3u:
        entry = next((ln.strip() for ln in body.decode("utf-8", "replace").splitlines()
                      if ln.strip() and not ln.startswith("#")), None)
        if not entry:
            return "dead", f"empty playlist ({ct})"
        try:
            s2, ct2, b2, _ = fetch(urllib.parse.urljoin(final, entry), 4096)
        except HTTPStatus as e:
            return ("blocked" if e.code in (401, 403, 451) else "dead"), f"playlist ok, its entry: HTTP {e.code}"
        except OSError as e:
            return "dead", f"playlist ok, its entry: {e}"[:160]
        return ("ok", f"HLS/M3U, entry {s2} {ct2}") if b2 else ("dead", "playlist entry is empty")
    if not body:
        return "dead", f"HTTP {status}, no bytes ({ct})"
    if "text/html" in ct.lower() or head[:15].lower().startswith((b"<!doctype html", b"<html")):
        return "dead", f"HTTP {status} returns a web page, not audio"
    if not AUDIO_CT.search(ct):
        return "ok", f"HTTP {status}, {len(body)} bytes, unusual type {ct!r}"
    return "ok", f"HTTP {status} {ct}"


def probe(entry: dict) -> dict:
    verdict, detail = probe_once(entry["url"])
    if verdict != "ok":
        time.sleep(5)
        verdict, detail = probe_once(entry["url"])
    return dict(entry, verdict=verdict, detail=detail)


def markdown(results: list[dict], run_url: str = "") -> str:
    dead = [r for r in results if r["verdict"] == "dead"]
    blocked = [r for r in results if r["verdict"] == "blocked"]
    out = [f"Weekly check of every stream URL in `config/stations/` and `config/catalog/catalog.json` "
           f"({len(results)} URLs, each tried twice from a GitHub runner in the US).", ""]
    if run_url:
        out += [f"Run: {run_url}", ""]

    def table(rows):
        t = ["| station | URL | result | in |", "|---|---|---|---|"]
        t += [f"| {r['name']} | {r['url']} | {r['detail']} | {', '.join(r['where'])} |" for r in rows]
        return t
    if dead:
        out += [f"### Dead ({len(dead)})", "", *table(dead), ""]
    if blocked:
        out += [f"### Refused from GitHub ({len(blocked)})", "",
                "HTTP 401/403/451 - usually geo-blocking; check from the radio (Spain) before removing.", "",
                *table(blocked), ""]
    if not dead and not blocked:
        out += ["All streams answered."]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json")
    ap.add_argument("--markdown")
    ap.add_argument("--only", help="probe only URLs containing this")
    ap.add_argument("--run-url", default="")
    a = ap.parse_args(argv)
    entries = [e for e in gather() if not a.only or a.only in e["url"]]
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(probe, entries))
    for r in results:
        print(f"{r['verdict']:8} {r['name'][:32]:32} {r['detail'][:70]:70} {r['url']}")
    counts = {v: sum(r["verdict"] == v for r in results) for v in ("ok", "blocked", "dead")}
    print(f"{len(results)} URLs: {counts}")
    if a.json:
        Path(a.json).write_text(json.dumps({"counts": counts, "results": results}, indent=1) + "\n")
    if a.markdown:
        Path(a.markdown).write_text(markdown(results, a.run_url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
