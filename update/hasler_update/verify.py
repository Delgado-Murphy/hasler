"""Signatures, keyring, expiry, seq and compatibility (docs/ota-format.md).

Nothing is unpacked, and nothing is trusted, before these pass. Signatures are
OpenSSH (``ssh-keygen -Y verify``); ``allowed_signers`` is generated here from
the verified keyring, one key at a time, so a key can only vouch for the role
it is listed with.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .state import parse_iso

NS_RELEASE = "hasler-release"
NS_KEYRING = "hasler-keyring"
# Timestamp keys sign the channel index; they never vouch for code.
NS_INDEX = "hasler-channel"
CLOCK_SKEW_S = 3600


class VerifyError(Exception):
    """A signature, hash, expiry or policy check failed. The message says which."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ssh_keygen() -> str:
    exe = shutil.which("ssh-keygen") or "/usr/bin/ssh-keygen"
    if not os.path.exists(exe):
        raise VerifyError("ssh-keygen not found (install openssh-client)")
    return exe


_PUB_RE = re.compile(r"^(ssh-ed25519|sk-ssh-ed25519@openssh\.com|ecdsa-sha2-nistp256|sk-ecdsa-sha2-nistp256@openssh\.com|ssh-rsa) [A-Za-z0-9+/=]+$")


def _clean_pub(pub: str) -> str:
    """``type base64`` only - drop comments; refuse anything odd (an options
    field smuggled into allowed_signers would change what it means)."""
    parts = pub.strip().split()
    if len(parts) < 2:
        raise VerifyError("malformed public key")
    key = f"{parts[0]} {parts[1]}"
    if not _PUB_RE.match(key):
        raise VerifyError(f"unsupported public key type {parts[0]!r}")
    return key


def ssh_verify(data: bytes, sig: bytes, signers: list[tuple[str, str]], namespace: str) -> str:
    """Return the id of the signer in ``signers`` whose key made ``sig``."""
    if not signers:
        raise VerifyError(f"no usable keys for {namespace}")
    exe = _ssh_keygen()
    with tempfile.TemporaryDirectory(prefix="hasler-verify-") as tmp:
        sig_path = Path(tmp) / "data.sig"
        sig_path.write_bytes(sig)
        for key_id, pub in signers:
            if not re.fullmatch(r"[A-Za-z0-9._-]+", key_id):
                raise VerifyError(f"bad key id {key_id!r}")
            allowed = Path(tmp) / "allowed_signers"
            allowed.write_text(f'{key_id} namespaces="{namespace}" {_clean_pub(pub)}\n')
            proc = subprocess.run(
                [exe, "-Y", "verify", "-f", str(allowed), "-I", key_id, "-n", namespace, "-s", str(sig_path)],
                input=data, capture_output=True, timeout=30,
            )
            if proc.returncode == 0:
                return key_id
    raise VerifyError(f"bad signature ({namespace})")


def _expired(doc: dict, now: float, what: str, key: str = "expires") -> None:
    try:
        exp = parse_iso(str(doc[key]))
    except (KeyError, ValueError) as e:
        raise VerifyError(f"{what}: no valid {key!r}") from e
    if now >= exp:
        raise VerifyError(f"{what} expired at {doc[key]}")


# --- keyring -----------------------------------------------------------------


@dataclass
class Keyring:
    seq: int
    keys: list[dict] = field(default_factory=list)

    def signers(self, role: str, now: float) -> list[tuple[str, str]]:
        out = []
        for k in self.keys:
            if k.get("role") != role:
                continue
            try:
                if now >= parse_iso(str(k["not_after"])):
                    continue
            except (KeyError, ValueError):
                continue
            out.append((str(k["id"]), str(k["pub"])))
        return out


def root_signers(trust: Path) -> list[tuple[str, str]]:
    out = []
    for i, p in enumerate(sorted(trust.glob("root*.pub"))):
        for line in p.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                out.append((f"root{i}", line.strip()))
    return out


def parse_keyring(data: bytes, sig: bytes, trust: Path, now: float) -> Keyring:
    ssh_verify(data, sig, root_signers(trust), NS_KEYRING)
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise VerifyError("keyring is not JSON") from e
    if doc.get("schema") != 1 or not isinstance(doc.get("keys"), list):
        raise VerifyError("keyring: unknown schema")
    _expired(doc, now, "keyring")
    return Keyring(int(doc["keyring_seq"]), list(doc["keys"]))


def load_keyring(trust: Path, now: float | None = None) -> Keyring:
    now = time.time() if now is None else now
    try:
        data = (trust / "keyring.json").read_bytes()
        sig = (trust / "keyring.json.sig").read_bytes()
    except OSError as e:
        raise VerifyError(f"no keyring in {trust}") from e
    return parse_keyring(data, sig, trust, now)


def accept_keyring(data: bytes, sig: bytes, trust: Path, stored_seq: int, now: float | None = None) -> Keyring:
    """A new keyring is taken only if a root key signed it and its seq is higher."""
    now = time.time() if now is None else now
    ring = parse_keyring(data, sig, trust, now)
    if ring.seq <= stored_seq:
        raise VerifyError(f"keyring_seq {ring.seq} is not above {stored_seq}")
    return ring


# --- channel index -------------------------------------------------------------


def verify_index(data: bytes, sig: bytes, ring: Keyring, channel: str, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    ssh_verify(data, sig, ring.signers("timestamp", now), NS_INDEX)
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise VerifyError("channel index is not JSON") from e
    if doc.get("schema") != 1:
        raise VerifyError("channel index: unknown schema")
    if doc.get("channel") != channel:
        raise VerifyError(f"channel index is for {doc.get('channel')!r}, not {channel!r}")
    _expired(doc, now, "channel index (no fresh update info)")
    try:
        if parse_iso(str(doc["issued"])) > now + CLOCK_SKEW_S:
            raise VerifyError("channel index issued in the future")
    except (KeyError, ValueError) as e:
        raise VerifyError("channel index: no valid 'issued'") from e
    cur = doc.get("current")
    if not isinstance(cur, dict) or not {"version", "seq", "release_url", "release_sha256"} <= cur.keys():
        raise VerifyError("channel index: no current release")
    return doc


def ring_open(index: dict, box_id: str, now: float) -> bool:
    """Is this box's ring open yet? No rings at all = everyone."""
    rings = index.get("rings")
    if not rings:
        return True
    order = sorted(rings, key=lambda r: int(r) if str(r).isdigit() else 99)
    mine = next((r for r in order if box_id and box_id in (rings[r] or [])), None)
    if mine is None:
        mine = next((r for r in order if "*" in (rings[r] or [])), None)
    if mine is None:
        return False
    opens = (index.get("ring_open") or {}).get(mine)
    if not opens:
        return True
    try:
        return now >= parse_iso(str(opens))
    except ValueError:
        return False


# --- release manifest ------------------------------------------------------------


def verify_release(data: bytes, sig: bytes, ring: Keyring, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    ssh_verify(data, sig, ring.signers("release", now), NS_RELEASE)
    try:
        doc = json.loads(data)
    except ValueError as e:
        raise VerifyError("release.json is not JSON") from e
    if doc.get("schema") != 1 or doc.get("product") != "hasler":
        raise VerifyError("release.json: wrong schema or product")
    for key in ("version", "seq", "artifacts", "requires"):
        if key not in doc:
            raise VerifyError(f"release.json: no {key!r}")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?", str(doc["version"])):
        raise VerifyError(f"release.json: bad version {doc['version']!r}")
    if not isinstance(doc["seq"], int) or doc["seq"] <= 0:
        raise VerifyError("release.json: bad seq")
    arts = doc["artifacts"]
    if not isinstance(arts, list) or not arts or not all(
        isinstance(a, dict) and re.fullmatch(r"[0-9a-f]{64}", str(a.get("sha256", ""))) for a in arts
    ):
        raise VerifyError("release.json: artifacts need sha256")
    if "files_sha256" in doc and not re.fullmatch(r"[0-9a-f]{64}", str(doc["files_sha256"])):
        raise VerifyError("release.json: bad files_sha256")
    _expired(doc, now, f"release {doc['version']}")
    if int(doc.get("keyring_seq", 0)) > ring.seq:
        raise VerifyError(f"release needs keyring {doc.get('keyring_seq')}, box has {ring.seq}")
    return doc


# --- compatibility -----------------------------------------------------------------


def _vtuple(v: str) -> tuple:
    nums = re.findall(r"\d+", v.split("-")[0])
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def version_matches(have: str, spec: str) -> bool:
    """``spec`` like ``>=10.0,<11``; bare ``10.1`` means ==."""
    h = _vtuple(have)
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        m = re.fullmatch(r"(>=|<=|==|!=|>|<)?\s*([0-9][0-9.]*)", part)
        if not m:
            raise VerifyError(f"bad version spec {spec!r}")
        op, v = m.group(1) or "==", _vtuple(m.group(2))
        ok = {">=": h >= v, "<=": h <= v, "==": h == v, "!=": h != v, ">": h > v, "<": h < v}[op]
        if not ok:
            return False
    return True


def version_key(v: str) -> tuple:
    return _vtuple(v)


def python_abi(python: str | None = None) -> str | None:
    """``cp313`` for the given interpreter (default: this one)."""
    if python is None:
        tag = sys.implementation.cache_tag or ""
    else:
        try:
            tag = subprocess.run([python, "-c", "import sys;print(sys.implementation.cache_tag)"],
                                 capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
    m = re.fullmatch(r"cpython-(\d+)", tag)
    return f"cp{m.group(1)}" if m else None


def system_facts(root: Path = Path("/")) -> dict:
    """What a release's ``requires`` is checked against."""
    codename = None
    try:
        for line in (root / "etc/os-release").read_text().splitlines():
            if line.startswith("VERSION_CODENAME="):
                codename = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    moode = os.environ.get("HASLER_MOODE_VERSION")
    if moode is None and shutil.which("moodeutl"):
        try:
            out = subprocess.run(["moodeutl", "--mooderel"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"\d+\.\d+(\.\d+)?", out)
            moode = m.group(0) if m else None
        except (OSError, subprocess.SubprocessError):
            moode = None
    return {
        "arch": platform.machine(),
        "os": codename,
        "moode": moode,
        "python_abi": python_abi("/usr/bin/python3") if os.path.exists("/usr/bin/python3") else python_abi(),
    }


def check_compat(release: dict, facts: dict, variant: str | None = None) -> None:
    req = release.get("requires") or {}
    for key in ("arch", "os", "python_abi"):
        want = req.get(key)
        if want is None:
            continue
        if key == "python_abi" and release.get("bundles_python"):
            continue      # the release brings its own interpreter
        if facts.get(key) != want:
            raise VerifyError(f"incompatible: needs {key} {want}, box has {facts.get(key)}")
    if req.get("moode"):
        have = facts.get("moode")
        if not have:
            raise VerifyError("incompatible: moOde version unknown")
        if not version_matches(have, str(req["moode"])):
            raise VerifyError(f"incompatible: needs moOde {req['moode']}, box has {have}")
    variants = release.get("variants")
    if variant and variants and variant not in variants:
        raise VerifyError(f"release does not support variant {variant!r}")


def check_policy(release: dict, state: dict, index: dict | None, current_seq: int | None) -> None:
    """seq / halt / bad / min_from_seq rules. ``state`` is the apply state."""
    seq = int(release["seq"])
    if seq <= int(state.get("max_seq") or 0):
        raise VerifyError(f"seq {seq} is not above installed max_seq {state.get('max_seq')} (downgrade refused)")
    if seq in (state.get("bad") or []):
        raise VerifyError(f"release seq {seq} failed here before (marked bad)")
    if index is not None:
        if seq in (index.get("halted") or []):
            raise VerifyError(f"release seq {seq} is halted")
        cur = index["current"]
        if int(cur["seq"]) != seq or str(cur["version"]) != str(release["version"]):
            raise VerifyError("release does not match the channel index")
    floor = release.get("min_from_seq")
    if floor is not None and current_seq is not None and current_seq < int(floor):
        raise VerifyError(f"release needs at least seq {floor} installed first (have {current_seq})")
