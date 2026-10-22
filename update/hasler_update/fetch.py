"""Downloads: the channel index, release.json, the artifact (with resume).

HTTPS is transport only - everything fetched here is checked against a
signature or a signed hash before it is used. ``file://`` URLs work too (tests,
and a USB stick some day).
"""

from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .verify import VerifyError, sha256_file

SMALL_MAX = 1 << 20          # index, manifest, signatures, keyring
TIMEOUT_S = 60
USER_AGENT = "hasler-update/1"


class FetchError(Exception):
    pass


def _open(url: str, headers: dict | None = None):
    scheme = urllib.parse.urlparse(url).scheme
    if scheme not in ("https", "http", "file"):
        raise FetchError(f"refusing URL scheme {scheme!r}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT_S)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"{url}: {e}") from e


def get_small(url: str, limit: int = SMALL_MAX) -> bytes:
    with _open(url) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise FetchError(f"{url}: larger than {limit} bytes")
    return data


def download(url: str, dest: Path, sha256: str, size: int | None = None) -> Path:
    """Fetch ``url`` to ``dest`` (resuming a ``dest.part``), then hash-check it.

    A finished file that already matches is kept as is. A mismatch is deleted -
    never kept, never retried from the same bytes.
    """
    if dest.exists():
        if sha256_file(dest) == sha256:
            return dest
        dest.unlink()
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if size is not None and have > size:
        part.unlink()
        have = 0
    headers = {"Range": f"bytes={have}-"} if have and urllib.parse.urlparse(url).scheme != "file" else {}
    with _open(url, headers) as r:
        status = getattr(r, "status", None) or 200
        if have and status != 206:      # server ignored the range: start over
            have = 0
        with open(part, "ab" if have else "wb") as f:
            got = have
            while True:
                chunk = r.read(1 << 16)
                if not chunk:
                    break
                got += len(chunk)
                if size is not None and got > size:
                    f.close()
                    part.unlink()
                    raise FetchError(f"{url}: more than the signed {size} bytes")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
    if sha256_file(part) != sha256:
        part.unlink()
        raise VerifyError(f"{dest.name}: SHA-256 does not match the signed manifest")
    os.replace(part, dest)
    return dest


def ensure_space(path: Path, need: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path).free
    if free < need:
        raise FetchError(f"not enough space in {path}: {free} free, {need} needed")
