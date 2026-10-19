"""A fake box (root dir) and a fake release server, signed with throwaway keys."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "update"))
sys.path.insert(0, str(ROOT / "daemon"))

from hasler_update.apply import Applier, Hooks, PowerCut  # noqa: E402
from hasler_update.check import Checker  # noqa: E402
from hasler_update.layout import Layout  # noqa: E402
from hasler_update.state import iso  # noqa: E402

DAY = 86400
FACTS = {"arch": "aarch64", "os": "trixie", "moode": "10.0.2", "python_abi": "cp313"}


def keygen(path: Path) -> str:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", path.name, "-f", str(path)], check=True)
    return path.with_suffix(".pub").read_text().strip() if path.suffix else Path(str(path) + ".pub").read_text().strip()


def sign(data: bytes, key: Path, ns: str, work: Path) -> bytes:
    f = work / f"tosign-{hashlib.sha256(data).hexdigest()[:12]}"
    f.write_bytes(data)
    sig = Path(str(f) + ".sig")
    sig.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", ns, str(f)], check=True,
                   capture_output=True)
    return sig.read_bytes()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class World:
    """One fake box + one fake release server."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.work = tmp / "work"
        self.work.mkdir()
        self.root = tmp / "root"
        self.server = tmp / "server"
        self.layout = Layout(self.root)
        keys = tmp / "keys"
        keys.mkdir()
        self.k = {}
        for name in ("root", "release", "timestamp", "rogue"):
            p = keys / name
            self.k[name] = (p, keygen(p))
        self.now = time.time()
        trust = self.layout.trust
        trust.mkdir(parents=True)
        (trust / "root.pub").write_text(self.k["root"][1] + "\n")
        self.write_keyring(1)
        self.layout.etc.mkdir(parents=True, exist_ok=True)
        self.layout.box_yaml.write_text(
            "schema: 1\nvariant: hasler-hm\nchannel: stable\nupdate_mode: auto\nbox_id: box-1\n"
            f"update_url: {self.server.as_uri()}\n"
        )
        cfg = self.root / "boot/firmware/config.txt"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("arm_64bit=1\n")
        (self.root / "etc/systemd/system").mkdir(parents=True)
        self.layout.haslerd_state.mkdir(parents=True)
        (self.layout.haslerd_state / "settings.json").write_text('{"schema": 1}\n')
        self.releases: dict[str, dict] = {}

    # --- keys ---------------------------------------------------------------
    def write_keyring(self, seq: int, keys: list[dict] | None = None, signer: str = "root") -> tuple[bytes, bytes]:
        keys = keys or [
            {"id": "release-a", "role": "release", "pub": self.k["release"][1], "not_after": iso(self.now + 365 * DAY)},
            {"id": "timestamp-a", "role": "timestamp", "pub": self.k["timestamp"][1], "not_after": iso(self.now + 365 * DAY)},
        ]
        doc = json.dumps({"schema": 1, "keyring_seq": seq, "expires": iso(self.now + 365 * DAY), "keys": keys}).encode()
        sig = sign(doc, self.k[signer][0], "hasler-keyring", self.work)
        (self.layout.trust / "keyring.json").write_bytes(doc)
        (self.layout.trust / "keyring.json.sig").write_bytes(sig)
        return doc, sig

    # --- releases ---------------------------------------------------------------
    def tree_files(self, version: str, extra: dict[str, bytes] | None = None) -> dict[str, tuple[bytes, int]]:
        files = {
            "app/haslerd/__init__.py": (f'__version__ = "{version}"\n'.encode(), 0o644),
            "requirements.lock": (b"# nothing to install in tests\n", 0o644),
            "integration/systemd/haslerd.service": (
                f"[Service]\nExecStart=/opt/hasler/current/venv/bin/python -m haslerd.main  # {version}\n".encode(), 0o644),
            "integration/sbin/hasler-wifi-join": (b"#!/bin/sh\necho " + version.encode() + b"\n", 0o755),
            "app/hasler_update/__init__.py": (f'__version__ = "{version}"\n'.encode(), 0o644),
            "integration/systemd/hasler-update-boot.service": (f"[Service]\nExecStart=/bin/true # {version}\n".encode(), 0o644),
            "integration/sbin/hasler-update": (f"#!/usr/bin/python3 -I\n# {version}\n".encode(), 0o755),
            "manifest.json": (json.dumps({"version": version}).encode(), 0o644),
        }
        for k, v in (extra or {}).items():
            if v is None:
                files.pop(k, None)
            else:
                files[k] = (v, 0o755 if k.startswith(("integration/sbin", "bin/")) else 0o644)
        return files

    def build_tar(self, version: str, files: dict[str, tuple[bytes, int]], list_override: bytes | None = None,
                  members: list[tuple[tarfile.TarInfo, bytes | None]] | None = None, fmt: str = "gz") -> tuple[bytes, bytes]:
        top = f"hasler-{version}"
        listing = "".join(f"{sha(d)}  {m:04o}  {p}\n" for p, (d, m) in sorted(files.items())).encode()
        if list_override is not None:
            listing = list_override
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            def add(name: str, data: bytes | None, mode: int, kind=tarfile.REGTYPE):
                ti = tarfile.TarInfo(name)
                ti.type, ti.mode, ti.mtime, ti.uid, ti.gid = kind, mode, 1_700_000_000, 0, 0
                if data is not None:
                    ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data) if data is not None else None)
            add(top, None, 0o755, tarfile.DIRTYPE)
            dirs = sorted({str(Path(p).parent) for p in files} - {"."})
            for d in dirs:
                parts = Path(d).parts
                for i in range(1, len(parts) + 1):
                    sub = "/".join(parts[:i])
                    if sub not in getattr(tf, "_seen", set()):
                        tf._seen = getattr(tf, "_seen", set()) | {sub}
                        add(f"{top}/{sub}", None, 0o755, tarfile.DIRTYPE)
            for p, (d, m) in sorted(files.items()):
                add(f"{top}/{p}", d, m)
            add(f"{top}/files.sha256", listing, 0o644)
            for ti, data in members or []:
                tf.addfile(ti, io.BytesIO(data) if data is not None else None)
        raw = buf.getvalue()
        if fmt == "gz":
            import gzip
            return gzip.compress(raw, mtime=0), listing
        if fmt == "zst":
            return subprocess.run(["zstd", "-q", "-c"], input=raw, capture_output=True, check=True).stdout, listing
        return raw, listing

    def publish(self, version: str, seq: int, *, extra=None, list_override=None, members=None, fmt="gz",
                release_patch: dict | None = None, signer: str = "release", index_signer: str = "timestamp",
                halted: list[int] | None = None, index_patch: dict | None = None, files_sha256: bool = True,
                tamper_artifact: bool = False) -> dict:
        files = self.tree_files(version, extra)
        art, listing = self.build_tar(version, files, list_override, members, fmt)
        ext = {"gz": "tar.gz", "zst": "tar.zst", "tar": "tar"}[fmt]
        name = f"hasler-{version}-arm64.{ext}"
        rdir = self.server / "releases" / f"v{version}"
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / name).write_bytes(art + (b"x" if tamper_artifact else b""))
        rel = {
            "schema": 1, "product": "hasler", "version": version, "seq": seq, "channel": "stable",
            "created": iso(self.now - 3600), "expires": iso(self.now + 120 * DAY), "kind": "minor",
            "requires_migration": False, "min_from_seq": None,
            "requires": {"os": "trixie", "moode": ">=10.0,<11", "python_abi": "cp313", "arch": "aarch64"},
            "config_schema": 1, "variants": ["hasler-hm", "excelsior"],
            "artifacts": [{"name": name, "sha256": sha(art), "size": len(art), "url": (rdir / name).as_uri()}],
            "notes": f"release {version}", "keyring_seq": 1,
        }
        if files_sha256:
            rel["files_sha256"] = sha(listing)
        rel.update(release_patch or {})
        rel_bytes = json.dumps(rel).encode()
        (rdir / "release.json").write_bytes(rel_bytes)
        (rdir / "release.json.sig").write_bytes(sign(rel_bytes, self.k[signer][0], "hasler-release", self.work))
        index = {
            "schema": 1, "channel": "stable", "issued": iso(self.now - 60), "expires": iso(self.now + 14 * DAY),
            "current": {"version": version, "seq": seq, "release_url": (rdir / "release.json").as_uri(),
                        "release_sha256": sha(rel_bytes)},
            "halted": halted or [], "min_supported_seq": 1,
        }
        index.update(index_patch or {})
        ib = json.dumps(index).encode()
        cdir = self.server / "channels"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "stable.json").write_bytes(ib)
        (cdir / "stable.json.sig").write_bytes(sign(ib, self.k[index_signer][0], "hasler-channel", self.work))
        self.releases[version] = {"files": files, "seq": seq}
        return rel

    # --- box side ---------------------------------------------------------------
    def checker(self) -> Checker:
        return Checker(self.layout, now=self.now, facts=FACTS)

    def applier(self, **kw) -> "Applier":
        return Applier(self.layout, FakeHooks(self, **kw))

    def current(self) -> str | None:
        c = self.layout.current
        return Path(os.readlink(c)).name if c.is_symlink() else None

    def unit_text(self) -> str:
        p = self.layout.systemd_dir / "haslerd.service"
        return p.read_text() if p.exists() else ""

    def state(self) -> dict:
        return json.loads(self.layout.state_file.read_text())


class FakeHooks(Hooks):
    def __init__(self, world: World, healthy: bool = True, idle: bool = True, crash_at: str | None = None,
                 hour: int = 3, selftest_fails: bool = False) -> None:
        super().__init__(world.layout)
        self.world = world
        self.healthy = healthy
        self.idle_ok = idle
        self.crash_at = crash_at
        self.hour = hour
        self.selftest_fails = selftest_fails
        self.calls: list[tuple] = []

    def now(self) -> float:
        return self.world.now

    def local_hour(self) -> int:
        return self.hour

    def checkpoint(self, name: str) -> None:
        self.calls.append(("checkpoint", name))
        if name == self.crash_at:
            self.crash_at = None
            raise PowerCut(name)

    def systemctl(self, *args: str) -> None:
        self.calls.append(("systemctl", *args))

    def udev_reload(self) -> None:
        self.calls.append(("udev",))

    def idle(self):
        return (self.idle_ok, "idle" if self.idle_ok else "radio playing")

    def baseline(self) -> dict:
        return {"haslerd": True}

    def health_wait(self, version, baseline):
        self.calls.append(("health", version))
        return (True, "healthy") if self.healthy else (False, "haslerd not active")

    def facts(self) -> dict:
        return dict(FACTS)

    def chown_root(self, path) -> None:
        pass

    def python_abi(self, python) -> str:
        return "cp313"

    def build_venv(self, release, python) -> None:
        (release / "venv" / "bin").mkdir(parents=True)
        (release / "venv" / "bin" / "python").write_text("#!/bin/sh\n")

    def selftest(self, release, etc, state) -> None:
        if self.selftest_fails:
            from hasler_update.apply import ApplyError
            raise ApplyError("selftest failed: config schema")

    def check_item(self, item) -> None:
        pass

    def migrate(self, release, etc, state, from_schema):
        return []


@pytest.fixture
def world(tmp_path) -> World:
    return World(tmp_path)


@pytest.fixture
def installed_v1(world) -> World:
    """A box that already runs 1.0.0 (seq 1), committed."""
    world.publish("1.0.0", 1)
    st = world.checker().stage()
    assert st["result"] == "ok", st
    out = world.applier().apply()
    assert "committed" in out, out
    return world
