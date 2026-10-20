"""Release builder (scripts/release/): allowlist, reproducibility, manifest, signatures.

Builds real tarballs from a throwaway git copy of this repo with --no-wheels,
so it runs on any laptop; needs git and the zstd CLI (skipped without them).
The signing tests need ssh-keygen and make throwaway ed25519 keys.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
REL = ROOT / "scripts" / "release"

pytestmark = [
    pytest.mark.skipif(not shutil.which("git"), reason="needs git"),
    pytest.mark.skipif(not shutil.which("zstd"), reason="needs the zstd CLI"),
]
needs_ssh = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")

# fixed for the run (builds comparable), recent (so the release has not expired)
COMMIT_TIME = int(time.time()) // 86400 * 86400 - 86400


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, REL / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


br = load("hasler_build_release", "build_release.py")
mm = sys.modules["make_manifest"]
kr = load("hasler_keyring", "keyring.py")
ch = load("hasler_channel", "channel.py")


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_DATE=f"@{COMMIT_TIME} +0000", GIT_COMMITTER_DATE=f"@{COMMIT_TIME} +0000")
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, env=env).stdout


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """A git copy of this repo's tracked files, pyproject bumped to 0.2.0."""
    dest = tmp_path_factory.mktemp("repo")
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
                             capture_output=True).stdout.decode().split("\0")
    for rel in filter(None, tracked):
        src = ROOT / rel
        if src.is_file() and not src.is_symlink():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    pp = dest / "daemon" / "pyproject.toml"
    pp.write_text(pp.read_text().replace('version = "0.1.0"', 'version = "0.2.0"', 1))
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "fixture")
    # Untracked junk a real checkout on the Pi has: none of it may ship.
    (dest / "config" / "haslerd.yaml").write_text("ha:\n  token: NOT-TO-SHIP-7f3a9c\n")
    (dest / "daemon" / ".venv" / "lib").mkdir(parents=True)
    (dest / "daemon" / ".venv" / "lib" / "evil.py").write_text("x = 1\n")
    (dest / "daemon" / "haslerd" / "__pycache__").mkdir(exist_ok=True)
    (dest / "daemon" / "haslerd" / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\0")
    (dest / "keys.yaml").write_text("k: v\n")
    return dest


def build(repo, out, *extra):
    args = br.parse_args(["--version", "0.2.0", "--out", str(out), "--repo", str(repo),
                          "--no-wheels", "--zstd-level", "3", *extra])
    return br.build(args)


@pytest.fixture(scope="module")
def built(repo, tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    tarball = build(repo, out)
    top, files = mm.read_tarball(tarball)
    return out, tarball, top, files


# --- allowlist ---------------------------------------------------------------------

def test_only_allowlisted_paths_ship(built):
    _, _, top, files = built
    assert top == "hasler-0.2.0"
    allowed_tops = {"app", "integration", "migrations", "wheelhouse", "bin", "manifest.json",
                    "files.sha256", "requirements.lock", "selftest.py"}
    assert {p.split("/")[0] for p in files} <= allowed_tops
    for p in files:
        assert ".venv" not in p and "__pycache__" not in p and not p.endswith(".pyc")
        assert Path(p).name not in ("haslerd.yaml", "keys.yaml", "settings.json", "state.json")
    assert "app/haslerd/main.py" in files
    assert "app/haslerd/web/static/index.html" in files
    assert "app/config/variants/hasler-hm.yaml" in files
    assert "app/config/haslerd.yaml.example" in files
    assert not any(p.startswith(("docs/", "app/docs")) for p in files)
    # the package's migrations also sit at the top level (contract)
    for p in files:
        if p.startswith("app/haslerd/migrations/") and p.split("/")[-1][:3].isdigit():
            assert files["migrations/" + p.split("/")[-1]] == files[p]
    assert not any(b"NOT-TO-SHIP-7f3a9c" in data for data, _ in files.values())


def test_integration_layout(built):
    _, _, _, files = built
    integ = {p: m for p, (_, m) in files.items() if p.startswith("integration/")}
    assert all(br.INTEGRATION_OK.match(p) for p in integ)
    assert integ["integration/systemd/haslerd.service"] == 0o644
    assert integ["integration/udev/99-hasler-pwm.rules"] == 0o644
    assert integ["integration/sbin/hasler-wifi-join"] == 0o755   # 0644 in the repo, forced
    assert integ["integration/bin/hasler-tone"] == 0o755
    assert integ["integration/shutdown/hasler-lamp-halt"] == 0o755


def test_worktree_mode_same_allowlist(repo, built, tmp_path):
    tarball = build(repo, tmp_path, "--worktree", "--source-date-epoch", str(COMMIT_TIME))
    _, files = mm.read_tarball(tarball)
    assert set(files) == set(built[3])


@pytest.mark.parametrize("rel,content,why", [
    ("daemon/haslerd/web/keys.yaml", b"a: b\n", "refused"),
    ("daemon/haslerd/hw/deploy.txt", b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n", "private key"),
    ("daemon/haslerd/blob.bin", b"\0", "unexpected file type"),
    ("daemon/haslerd/secrets/ha.txt", b"x", "refused"),
    ("integration/etc/passwd", b"x", "not an integration destination"),
    ("migrations/fix.py", b"def up(p): pass\n", "NNN_name.py"),
])
def test_refuses_bad_files_inside_allowlisted_dirs(repo, tmp_path, rel, content, why):
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        with pytest.raises(mm.ReleaseError, match=why):
            build(repo, tmp_path, "--worktree")
    finally:
        target.unlink()


def test_refuses_symlinks(repo, tmp_path):
    link = repo / "daemon" / "haslerd" / "link.py"
    link.symlink_to("main.py")
    try:
        with pytest.raises(mm.ReleaseError, match="symlink"):
            build(repo, tmp_path, "--worktree")
    finally:
        link.unlink()


def test_strict_fails_on_version_mismatch_and_home_pi_units(repo, tmp_path):
    args = br.parse_args(["--version", "0.3.0", "--out", str(tmp_path), "--repo", str(repo),
                          "--no-wheels", "--strict"])
    with pytest.raises(mm.ReleaseError, match="--strict"):
        br.build(args)


# --- reproducible, hashes --------------------------------------------------------------

def test_two_builds_identical(repo, built, tmp_path):
    out1, tar1, _, _ = built
    tar2 = build(repo, tmp_path)
    assert hashlib.sha256(tar1.read_bytes()).digest() == hashlib.sha256(tar2.read_bytes()).digest()
    assert (out1 / "release.json").read_bytes() == (tmp_path / "release.json").read_bytes()


def test_tar_is_normalised(built):
    import io
    import tarfile
    _, tarball, _, _ = built
    with tarfile.open(fileobj=io.BytesIO(mm.zstd_decompress(tarball))) as tf:
        members = tf.getmembers()
    assert [m.name for m in members] == sorted(m.name for m in members)
    assert {m.mtime for m in members} == {COMMIT_TIME}
    assert {(m.uid, m.gid) for m in members} == {(0, 0)}
    assert all(m.isfile() or m.isdir() for m in members)
    assert not any(m.mode & 0o7000 for m in members)


def test_files_sha256_matches(built):
    out, _, _, files = built
    listed = mm.parse_files_sha256(files["files.sha256"][0].decode())
    assert set(listed) == set(files) - {"files.sha256"}
    for rel, (sha, mode) in listed.items():
        assert hashlib.sha256(files[rel][0]).hexdigest() == sha
        assert files[rel][1] == mode
    assert (out / "files.sha256").read_bytes() == files["files.sha256"][0]


def test_check_catches_tampering(built, tmp_path):
    out, tarball, _, _ = built
    bad = tmp_path / tarball.name
    data = bytearray(tarball.read_bytes())
    data[len(data) // 2] ^= 0xFF
    bad.write_bytes(bytes(data))
    with pytest.raises((mm.ReleaseError, subprocess.CalledProcessError)):
        mm.check(out / "release.json", bad)


# --- manifest ------------------------------------------------------------------------------

def test_release_json_per_contract(built):
    out, tarball, _, files = built
    raw = (out / "release.json").read_bytes()
    doc = json.loads(raw)
    assert list(doc) == list(mm.KEY_ORDER)
    assert doc["schema"] == 1 and doc["product"] == "hasler" and doc["version"] == "0.2.0"
    assert doc["seq"] == mm.seq_for("0.2.0")
    assert doc["channel"] == "stable"
    assert doc["kind"] in mm.KINDS
    assert doc["created"] == datetime.fromtimestamp(COMMIT_TIME, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert mm.parse_iso(doc["expires"]) - mm.parse_iso(doc["created"]) == timedelta(days=120)
    assert doc["requires"] == {"os": "trixie", "moode": ">=10.0,<11", "python_abi": "cp313", "arch": "aarch64"}
    assert doc["variants"] == ["excelsior", "hasler-hm"]
    assert isinstance(doc["config_schema"], int) and doc["keyring_seq"] == 1
    art = doc["artifacts"][0]
    assert art["name"] == "hasler-0.2.0-arm64.tar.zst" == tarball.name
    assert art["size"] == tarball.stat().st_size
    assert art["sha256"] == hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert art["url"] == ("https://github.com/delgado-murphy/hasler-releases/releases/download/"
                          "v0.2.0/hasler-0.2.0-arm64.tar.zst")
    manifest = json.loads(files["manifest.json"][0])
    assert manifest == {k: v for k, v in doc.items() if k != "artifacts"}
    assert raw == mm.dumps(doc)
    sums = (out / "SHA256SUMS").read_text()
    assert f"{art['sha256']}  {tarball.name}" in sums


def test_seq_follows_semver():
    order = ["0.2.0", "0.2.1-rc.1", "0.2.1-rc.2", "0.2.1", "0.3.0-rc.1", "0.3.0", "1.0.0"]
    seqs = [mm.seq_for(v) for v in order]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert mm.kind_between("1.2.0", "1.2.1") == "patch"
    assert mm.kind_between("1.2.0", "1.3.0") == "minor"
    assert mm.kind_between("1.2.0", "2.0.0") == "major"
    assert mm.kind_between(None, "1.0.0") == "major"
    with pytest.raises(mm.ReleaseError):
        mm.parse_version("v1.0.0")


def test_rc_goes_to_bench(repo, tmp_path):
    args = br.parse_args(["--version", "0.2.0-rc.1", "--out", str(tmp_path), "--repo", str(repo),
                          "--no-wheels", "--zstd-level", "1"])
    br.build(args)
    doc = json.loads((tmp_path / "release.json").read_text())
    assert doc["channel"] == "bench" and doc["seq"] == mm.seq_for("0.2.0-rc.1")


def test_wheelhouse_lock(tmp_path):
    wh = tmp_path / "wh"
    wh.mkdir()
    (wh / "PyYAML-6.0.3-cp313-cp313-manylinux_2_17_aarch64.manylinux2014_aarch64.whl").write_bytes(b"a")
    (wh / "smbus2-0.5.0-py2.py3-none-any.whl").write_bytes(b"b")
    files, lock, warns = br.wheelhouse(wh, [])
    text = lock.decode()
    assert "pyyaml==6.0.3 \\\n    --hash=sha256:" + hashlib.sha256(b"a").hexdigest() in text
    assert "smbus2==0.5.0" in text and not warns
    assert set(files) == {f"wheelhouse/{p.name}" for p in wh.iterdir()}
    (wh / "lgpio-0.2.2.0-cp313-cp313-linux_aarch64.whl").write_bytes(b"c")
    assert br.wheelhouse(wh, [])[2]                    # unrepaired -> warning (error with --strict)
    assert not br.wheelhouse(wh, ["liblgpio1"])[2]
    (wh / "x-1.0-cp313-cp313-manylinux_2_17_x86_64.whl").write_bytes(b"d")
    with pytest.raises(mm.ReleaseError, match="aarch64"):
        br.wheelhouse(wh, [])


def test_changelog_notes():
    text = "# Changelog\n\n## [0.3.0] - 2026-11-01\n### Added\n- b\n\n## [0.2.0] - 2026-10-20\n- a\n"
    assert br.changelog_notes(text, "0.2.0") == "- a"
    assert br.changelog_notes(text, "0.3.0") == "### Added\n- b"


# --- signatures, keyring, channel ------------------------------------------------------------

@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    for name in ("root", "release", "timestamp", "other"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(d / name)], check=True)
    return d


def sh(script, *args, env=None):
    return subprocess.run(["bash", str(REL / script), *map(str, args)], capture_output=True, text=True,
                          env=dict(os.environ, **(env or {})))


@needs_ssh
def test_keyring_and_release_signature_round_trip(keys, built, tmp_path):
    out = built[0]
    ring = tmp_path / "keyring.json"
    assert kr.main(["new", "--seq", "1", "--key", f"release-t:release:{keys / 'release.pub'}",
                    "--key", f"timestamp-t:timestamp:{keys / 'timestamp.pub'}", "-o", str(ring)]) == 0
    assert kr.main(["sign", "--root-key", str(keys / "root"), str(ring)]) == 0
    assert kr.main(["verify", "--root", str(keys / "root.pub"), str(ring)]) == 0
    assert kr.main(["verify", "--root", str(keys / "other.pub"), str(ring)]) == 1
    assert kr.main(["verify", "--root", str(keys / "root.pub"), "--min-seq", "1", str(ring)]) == 1

    rel = tmp_path / "release.json"
    shutil.copy(out / "release.json", rel)
    r = sh("sign.sh", "-n", "hasler-release", "-k", keys / "release", "-p", keys / "release.pub", rel)
    assert r.returncode == 0, r.stderr
    assert sh("verify.sh", "-n", "hasler-release", "-r", "release", "-K", ring, rel).returncode == 0
    # wrong role / namespace / key / content all fail
    assert sh("verify.sh", "-n", "hasler-release", "-r", "timestamp", "-K", ring, rel).returncode != 0
    assert sh("verify.sh", "-n", "hasler-channel", "-r", "release", "-K", ring, rel).returncode != 0
    assert sh("verify.sh", "-n", "hasler-release", "-I", "x", "-k", keys / "other.pub", rel).returncode != 0
    assert sh("sign.sh", "-n", "hasler-release", "-k", keys / "other", "-p", keys / "release.pub",
              rel).returncode != 0          # -p catches the wrong key
    rel.write_bytes(rel.read_bytes().replace(b'"seq": ', b'"seq": 1'))
    assert sh("verify.sh", "-n", "hasler-release", "-r", "release", "-K", ring, rel).returncode != 0


@needs_ssh
def test_key_from_env_and_revocation(keys, tmp_path):
    ring = tmp_path / "keyring.json"
    kr.main(["new", "--seq", "1", "--key", f"release-a:release:{keys / 'release.pub'}",
             "--key", f"timestamp-a:timestamp:{keys / 'timestamp.pub'}", "-o", str(ring)])
    f = tmp_path / "x.json"
    f.write_text("{}\n")
    r = sh("sign.sh", "-n", "hasler-channel", "-e", "TS_KEY", f, env={"TS_KEY": (keys / "timestamp").read_text()})
    assert r.returncode == 0, r.stderr
    assert sh("verify.sh", "-n", "hasler-channel", "-r", "timestamp", "-K", ring, f).returncode == 0
    # rotate: drop the release key, seq goes up, old key no longer accepted
    ring2 = tmp_path / "keyring2.json"
    assert kr.main(["new", "--from", str(ring), "--drop", "release-a",
                    "--key", f"release-b:release:{keys / 'other.pub'}", "-o", str(ring2)]) == 0
    doc = json.loads(ring2.read_text())
    assert doc["keyring_seq"] == 2 and [k["id"] for k in doc["keys"]] == ["timestamp-a", "release-b"]
    assert "release-a" not in kr.allowed_signers(doc, "release")


def test_channel_promote_halt_refresh(built, tmp_path):
    out = built[0]
    idx = tmp_path / "channels" / "bench.json"
    url = "https://github.com/delgado-murphy/hasler-releases/releases/download/v0.2.0/release.json"
    rel = out / "release.json"
    assert ch.main(["promote", "--index", str(idx), "--release", str(rel), "--release-url", url]) == 0
    doc = json.loads(idx.read_text())
    ch.validate(doc)
    assert doc["current"]["seq"] == mm.seq_for("0.2.0")
    assert doc["current"]["release_sha256"] == hashlib.sha256(rel.read_bytes()).hexdigest()
    assert ch.parse_iso(doc["expires"]) - ch.parse_iso(doc["issued"]) == timedelta(days=14)
    assert set(doc) == {"schema", "channel", "issued", "expires", "current", "halted",
                        "min_supported_seq", "rings", "ring_open"}
    # same seq again: refused (a pull is a new, higher release)
    assert ch.main(["promote", "--index", str(idx), "--release", str(rel), "--release-url", url]) == 1
    # url for another version: refused
    assert ch.main(["promote", "--index", str(tmp_path / "channels" / "stable.json"), "--release", str(rel),
                    "--release-url", url.replace("v0.2.0", "v9.9.9")]) == 1
    assert ch.main(["halt", "--index", str(idx), "--seq", str(doc["current"]["seq"])]) == 0
    assert json.loads(idx.read_text())["halted"] == [doc["current"]["seq"]]
    # an expired index comes back with refresh
    stale = json.loads(idx.read_text())
    stale["issued"], stale["expires"] = "2020-01-01T00:00:00Z", "2020-01-15T00:00:00Z"
    idx.write_text(json.dumps(stale))
    assert ch.main(["check", str(idx)]) == 1
    assert ch.main(["refresh", str(idx)]) == 0
    assert ch.main(["check", str(idx)]) == 0


def test_multiroom_files_ship_from_the_worktree():
    """Snapcast multiroom: the helper, three never-enabled units, the server config."""
    srcs, links = br.sources_from_worktree(ROOT)
    files = br.select(srcs, links, ROOT)
    assert files["integration/sbin/hasler-multiroom"][1] == 0o755
    for unit in ("snapserver-hasler", "snapclient-hasler", "camilladsp-hasler"):
        assert files[f"integration/systemd/{unit}.service"][1] == 0o644
    assert b"[stream]" in files["app/config/snapcast/snapserver.conf"][0]
    assert b"hasler-multiroom" in files["integration/sudoers/haslerd"][0]
    assert not [p for p in br.lint_integration(files) if "hasler" in p and "snap" in p]


# --- a new integration/ kind only in a major release ----------------------------------------

def test_new_integration_kind_needs_a_major_release(repo, tmp_path, monkeypatch):
    """Boxes refuse an integration/ kind their updater has no rule for unless the
    release is major (docs/ota-format.md, 2026-10-21): the build refuses first."""
    import re
    monkeypatch.setattr(br, "INTEGRATION_OK", re.compile(br.INTEGRATION_OK.pattern[:-2] + r"|tmpfiles/hasler\.conf)$"))
    target = repo / "integration" / "tmpfiles" / "hasler.conf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("d /run/hasler 0755 root root -\n")
    try:
        for kind in ("patch", "minor"):
            with pytest.raises(mm.ReleaseError, match=r"integration/tmpfiles.*major"):
                build(repo, tmp_path / kind, "--worktree", "--kind", kind)
        tarball = build(repo, tmp_path / "major", "--worktree", "--kind", "major")
        assert "integration/tmpfiles/hasler.conf" in mm.read_tarball(tarball)[1]
    finally:
        shutil.rmtree(repo / "integration")


def test_known_kinds_pass_any_release_kind():
    known = [f"integration/{k}/x" for k in sorted(mm.KNOWN_KINDS_SINCE)] + ["app/haslerd/main.py"]
    for kind in mm.KINDS:
        mm.check_integration_kinds(known, kind)
    with pytest.raises(mm.ReleaseError, match="tmpfiles"):
        mm.check_integration_kinds(known + ["integration/tmpfiles/hasler.conf"], "minor")
    mm.check_integration_kinds(known + ["integration/tmpfiles/hasler.conf"], "major")
