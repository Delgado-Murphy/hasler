"""scripts/hasler-install: a new radio from a signed release (docs/new-install.md).

The end-to-end test builds a real (--no-wheels) release from this checkout,
publishes it on a file:// dev channel (dev-keys.sh + dev-channel.py), lets
hasler-install download + verify + unpack it and lay out the setup tree, runs
that tree's ota-bootstrap.sh on a fake box with the trust hasler-install
produced, and has the real updater stage the same release from it - the path a
new radio takes, minus apt, moOde and the hardware.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import DAY, FACTS, sign
from hasler_update import verify as hv
from hasler_update.check import Checker
from hasler_update.layout import Layout
from hasler_update.state import iso

ROOT = Path(__file__).resolve().parents[2]
REL = ROOT / "scripts" / "release"
INSTALLER = ROOT / "scripts" / "hasler-install"
needs = lambda *tools: pytest.mark.skipif(  # noqa: E731
    not all(shutil.which(t) for t in tools), reason=f"needs {' '.join(tools)}")


def load_installer():
    loader = importlib.machinery.SourceFileLoader("hasler_install", str(INSTALLER))
    spec = importlib.util.spec_from_loader("hasler_install", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


hi = load_installer()


@pytest.fixture(autouse=True)
def no_card_log(monkeypatch):
    monkeypatch.setenv("HASLER_INSTALL_NO_CARD_LOG", "1")


# --- a real release on a dev channel -----------------------------------------------------

@pytest.fixture(scope="module")
def channel(tmp_path_factory):
    if not all(shutil.which(t) for t in ("ssh-keygen", "zstd")):
        pytest.skip("needs ssh-keygen and zstd")
    tmp = tmp_path_factory.mktemp("chan")
    keys = tmp / "keys"
    subprocess.run(["bash", str(REL / "dev-keys.sh"), str(keys)], check=True, capture_output=True)
    dist = tmp / "dist"
    r = subprocess.run([sys.executable, str(REL / "build_release.py"), "--version", "0.9.0", "--out", str(dist),
                        "--repo", str(ROOT), "--worktree", "--no-wheels", "--zstd-level", "3", "--channel", "bench"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    site = tmp / "site"
    r = subprocess.run([sys.executable, str(REL / "dev-channel.py"), "publish", "--keys", str(keys),
                        "--dist", str(dist), "--site", str(site), "--base-url", site.as_uri()],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return {"keys": keys, "site": site, "root_pub": (keys / "trust/root.pub").read_text().strip()}


def fetch(channel, out: Path, **kw):
    return hi.fetch_release(channel["site"].as_uri(), "bench", out, [channel["root_pub"]], **kw)


@needs("ssh-keygen", "zstd")
def test_fetch_verifies_unpacks_and_lays_out_the_setup_tree(channel, tmp_path):
    got = fetch(channel, tmp_path / "work")
    assert got["version"] == "0.9.0"
    setup = got["setup"]
    # every $REPO/... path the two box-setup scripts use is in the setup tree
    for script in ("scripts/bootstrap.sh", "scripts/ota-bootstrap.sh"):
        text = (setup / script).read_text()
        assert os.access(setup / script, os.X_OK), script
        for ref in sorted(set(re.findall(r'\$REPO/([A-Za-z0-9_./ -]+?)(?=["\'\s)]|$)', text))):
            ref = ref.strip()
            if ref.endswith("/*"):
                assert list((setup / ref[:-2]).iterdir()), ref
            elif ref == "config/$u":
                for u in ("yt-dlp-update.service", "yt-dlp-update.timer"):
                    assert (setup / "config" / u).is_file(), u
            elif ref == "config/camilladsp/Hasler":       # "Hasler Tone.yml" (a space)
                assert (setup / "config/camilladsp/Hasler Tone.yml").is_file()
            elif ref in ("update", "scripts", "config"):
                assert (setup / ref).is_dir()
            else:
                assert (setup / ref).exists(), f"{script}: $REPO/{ref} missing from the setup tree"
    # what it copied is the release's, byte for byte (the release's files are verified)
    tree = got["root"]
    assert (setup / "update/hasler_update/verify.py").read_bytes() == (tree / "app/hasler_update/verify.py").read_bytes()
    assert (setup / "scripts/hasler-install").read_bytes() == INSTALLER.read_bytes()
    assert {p.name for p in (setup / "update/systemd").iterdir()} == \
        {p.name for p in (ROOT / "update/systemd").iterdir()}
    # trust for ota-bootstrap: the pinned root + the published keyring
    assert sorted(p.name for p in got["trust"].iterdir()) == ["keyring.json", "keyring.json.sig", "root.pub"]


@needs("ssh-keygen", "zstd", "git")
def test_setup_tree_bootstraps_a_box_and_the_updater_stages_the_same_release(channel, tmp_path):
    got = fetch(channel, tmp_path / "work")
    box = tmp_path / "box"
    (box / "boot/firmware").mkdir(parents=True)
    (box / "boot/firmware/config.txt").write_text("arm_64bit=1\n")
    env = dict(os.environ, HASLER_ROOT=str(box))
    r = subprocess.run(["bash", str(got["setup"] / "scripts/ota-bootstrap.sh"), "--trust", str(got["trust"])],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box / "opt/hasler/updater/hasler_update/cli.py").is_file()
    assert (box / "etc/hasler/trust/root.pub").read_text().strip() == channel["root_pub"]
    s = hi.validate(dict(hi.DEFAULTS, channel="bench", user="pi", update_url=channel["site"].as_uri()))
    (box / "etc/hasler/box.yaml").write_text(hi.box_yaml(s, box_id="new-1"))
    status = Checker(Layout(box), facts=dict(FACTS, moode="10.3.6")).stage()
    assert status["result"] == "ok", status
    assert status["available"]["version"] == "0.9.0"
    assert (Layout(box).staged / str(got["seq"]) / "STAGED").is_file()


# --- refusals -----------------------------------------------------------------------------

@needs("ssh-keygen", "zstd")
def test_another_root_key_is_refused(channel, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    with pytest.raises(hi.InstallError, match="bad signature \\(hasler-keyring\\)"):
        hi.fetch_release(channel["site"].as_uri(), "bench", tmp_path / "w", [Path(str(other) + ".pub").read_text()])
    # and the shipped default is the real Hasler root, which dev keys are not
    with pytest.raises(hi.InstallError, match="bad signature"):
        hi.fetch_release(channel["site"].as_uri(), "bench", tmp_path / "w2", [hi.ROOT_PUB])


@needs("ssh-keygen", "zstd")
def test_tampered_tarball_is_refused_before_unpacking(channel, tmp_path):
    rel = json.loads(next(channel["site"].glob("releases/*/release.json")).read_text())
    tar = Path(rel["artifacts"][0]["url"][len("file://"):])
    saved = tar.read_bytes()
    try:
        tar.write_bytes(saved[:-1] + bytes([saved[-1] ^ 1]))   # same size, one bit off
        with pytest.raises(hi.InstallError, match="SHA-256 does not match"):
            fetch(channel, tmp_path / "w")
        assert not (tmp_path / "w/tree").exists()
    finally:
        tar.write_bytes(saved)


def _world_channel(world, **publish):
    world.publish("0.5.0", 50099, **publish)
    return world.server.as_uri(), world.k["root"][1]


def test_index_signed_by_the_release_key_is_refused(world, tmp_path):
    base, root = _world_channel(world, index_signer="release")
    (world.server / "keyring.json").write_bytes((world.layout.trust / "keyring.json").read_bytes())
    (world.server / "keyring.json.sig").write_bytes((world.layout.trust / "keyring.json.sig").read_bytes())
    with pytest.raises(hi.InstallError, match="bad signature \\(hasler-channel\\)"):
        hi.fetch_release(base, "stable", tmp_path / "w", [root])


def test_release_signed_by_a_rogue_key_is_refused(world, tmp_path):
    base, root = _world_channel(world, signer="rogue")
    shutil.copy(world.layout.trust / "keyring.json", world.server)
    shutil.copy(world.layout.trust / "keyring.json.sig", world.server)
    with pytest.raises(hi.InstallError, match="bad signature \\(hasler-release\\)"):
        hi.fetch_release(base, "stable", tmp_path / "w", [root])


def test_expired_index_is_refused(world, tmp_path):
    base, root = _world_channel(world, index_patch={"expires": iso(world.now - 60)})
    shutil.copy(world.layout.trust / "keyring.json", world.server)
    shutil.copy(world.layout.trust / "keyring.json.sig", world.server)
    with pytest.raises(hi.InstallError, match="channel index expired"):
        hi.fetch_release(base, "stable", tmp_path / "w", [root])


def test_halted_current_is_refused(world, tmp_path):
    base, root = _world_channel(world, halted=[50099])
    shutil.copy(world.layout.trust / "keyring.json", world.server)
    shutil.copy(world.layout.trust / "keyring.json.sig", world.server)
    with pytest.raises(hi.InstallError, match="halted"):
        hi.fetch_release(base, "stable", tmp_path / "w", [root])


def test_a_release_without_app_setup_cannot_set_up_a_new_radio(world, tmp_path):
    base, root = _world_channel(world)
    shutil.copy(world.layout.trust / "keyring.json", world.server)
    shutil.copy(world.layout.trust / "keyring.json.sig", world.server)
    with pytest.raises(hi.InstallError, match="no app/setup"):
        hi.fetch_release(base, "stable", tmp_path / "w", [root])
    # ...but it was verified and unpacked first (fetch only fails at the layout)
    assert (tmp_path / "w/tree/hasler-0.5.0/files.sha256").is_file()


def test_missing_channel_says_so(world, tmp_path):
    base, root = _world_channel(world)
    shutil.copy(world.layout.trust / "keyring.json", world.server)
    shutil.copy(world.layout.trust / "keyring.json.sig", world.server)
    with pytest.raises(hi.InstallError, match="no 'bench' channel yet"):
        hi.fetch_release(base, "bench", tmp_path / "w", [root])


def test_file_not_in_listing_is_refused(world, tmp_path):
    import tarfile
    ti = tarfile.TarInfo("hasler-0.5.0/app/extra.py")
    ti.size = 3
    base, root = _world_channel(world, members=[(ti, b"x=1")])
    shutil.copy(world.layout.trust / "keyring.json", world.server)
    shutil.copy(world.layout.trust / "keyring.json.sig", world.server)
    with pytest.raises(hi.InstallError, match="not in files.sha256"):
        hi.fetch_release(base, "stable", tmp_path / "w", [root])


# --- the same contract as the updater's verify.py -------------------------------------------

def test_verification_agrees_with_the_updater(world):
    rel = world.publish("0.5.0", 50099)
    ring_b, ring_s = (world.layout.trust / "keyring.json").read_bytes(), (world.layout.trust / "keyring.json.sig").read_bytes()
    ring = hi.verify_keyring(ring_b, ring_s, [world.k["root"][1]], world.now)
    ring_u = hv.parse_keyring(ring_b, ring_s, world.layout.trust, world.now)
    assert ring["keyring_seq"] == ring_u.seq
    assert hi.signers(ring, "release", world.now) == ring_u.signers("release", world.now)
    idx_b = (world.server / "channels/stable.json").read_bytes()
    idx_s = (world.server / "channels/stable.json.sig").read_bytes()
    assert hi.verify_index(idx_b, idx_s, ring, "stable", world.now) == hv.verify_index(idx_b, idx_s, ring_u, "stable", world.now)
    rdir = world.server / "releases/v0.5.0"
    rb, rs = (rdir / "release.json").read_bytes(), (rdir / "release.json.sig").read_bytes()
    index = json.loads(idx_b)
    assert hi.verify_release(rb, rs, ring, index, world.now) == hv.verify_release(rb, rs, ring_u, world.now) == rel
    # a timestamp key can't vouch for a release, in either
    bad = sign(rb, world.k["timestamp"][0], "hasler-release", world.work)
    with pytest.raises(hi.InstallError):
        hi.verify_release(rb, bad, ring, index, world.now)
    with pytest.raises(hv.VerifyError):
        hv.verify_release(rb, bad, ring_u, world.now)
    # expiry, both
    late = world.now + 400 * DAY
    with pytest.raises(hi.InstallError, match="expired"):
        hi.verify_keyring(ring_b, ring_s, [world.k["root"][1]], late)
    with pytest.raises(hv.VerifyError, match="expired"):
        hv.parse_keyring(ring_b, ring_s, world.layout.trust, late)


def test_pinned_root_is_the_published_one():
    """The key in the installer matches its fingerprint (docs/new-install.md)."""
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    r = subprocess.run(["ssh-keygen", "-lf", "-"], input=hi.ROOT_PUB, capture_output=True, text=True)
    assert r.returncode == 0 and hi.ROOT_FPR in r.stdout
    assert hi.ROOT_FPR in (ROOT / "docs/new-install.md").read_text()
    assert hashlib.sha256(hi.ROOT_PUB.encode()).hexdigest()   # (a str, one line)
    assert "\n" not in hi.ROOT_PUB


# --- settings and the files on the SD card ---------------------------------------------------

def test_settings_file(tmp_path):
    shipped = hi.read_settings(ROOT / "config/firstboot/hasler.txt")
    assert shipped == dict(hi.DEFAULTS, user="pi")
    p = tmp_path / "hasler.txt"
    p.write_text("channel = bench   # makers\nPANEL_OPTIONAL = Yes\nmoode=no\n")
    s = hi.read_settings(p)
    assert (s["channel"], s["panel_optional"], s["moode"]) == ("bench", "yes", "no")
    p.write_text("chanel = bench\n")
    with pytest.raises(hi.InstallError, match="unknown setting 'chanel'"):
        hi.read_settings(p)
    p.write_text("channel = beta\n")
    with pytest.raises(hi.InstallError, match="stable or bench"):
        hi.read_settings(p)
    assert hi.read_settings(tmp_path / "absent.txt") == hi.DEFAULTS


def test_box_yaml_is_what_the_updater_and_haslerd_read(tmp_path):
    sys.path.insert(0, str(ROOT / "daemon"))
    from haslerd.boxconfig import BOX_KEYS
    from haslerd.migrations import available
    from hasler_update.layout import read_box_yaml
    s = hi.validate(dict(hi.DEFAULTS, channel="bench", update_url="http://laptop.local:8000"))
    text = hi.box_yaml(s, box_id="abc")
    doc = yaml.safe_load(text)
    assert set(doc) <= set(BOX_KEYS)
    assert doc["schema"] == 1 and doc["channel"] == "bench" and doc["update_mode"] == "auto"
    assert doc["update_url"] == "http://laptop.local:8000"
    assert doc["migrations"] == [m for m in available() if m.startswith("001_")]
    (tmp_path / "box.yaml").write_text(text)
    flat = read_box_yaml(tmp_path / "box.yaml")
    assert flat["channel"] == "bench" and flat["box_id"] == "abc"


# moOde r1036 www/inc/autocfg.php: the keys it knows (the subset Hasler uses must be among them)
MOODE_KEYS = {"browsertitle", "hostname", "btname", "airplayname", "spotifyname", "upnpname", "dlnaname",
              "adevname", "mpdmixer", "i2soverlay", "i2sdevice", "cardnum", "amixname", "alsavolume_max",
              "autoplay", "volume_mpd_max", "btsvc", "rsmafterbt", "airplaysvc", "rsmafterapl", "spotifysvc",
              "upnpsvc", "bt_pairing_confirm", "alsavolume_max_bt"}


def test_moodecfg_ini_uses_only_moode_keys():
    keys = {}
    for line in (ROOT / "config/firstboot/moodecfg.ini").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(";"):
            k, v = (s.strip() for s in line.split("=", 1))
            assert v.startswith('"') and v.endswith('"'), line
            keys[k] = v.strip('"')
    assert set(keys) <= MOODE_KEYS
    assert keys["i2soverlay"] == "hifiberry-amp4pro" and keys["i2sdevice"] == "None"
    assert keys["spotifysvc"] == "0"           # go-librespot is the Spotify player
    assert not any(k.startswith(("wlan", "apd")) for k in keys)   # no Wi-Fi secrets on the card


def test_cloud_init_seed_files():
    fb = ROOT / "config/firstboot"
    ud = yaml.safe_load((fb / "user-data").read_text())
    assert (fb / "user-data").read_text().startswith("#cloud-config\n")
    assert ud["users"] == [] and ud["preserve_hostname"] is True and ud["locale"] is False
    assert yaml.safe_load((fb / "meta-data").read_text())["instance-id"]
    assert yaml.safe_load((fb / "network-config").read_text()) == {"network": {"config": "disabled"}}
    vd = (fb / "vendor-data").read_text()
    assert vd.startswith("#!/bin/sh\n") and "/boot/firmware/hasler-install hook" in vd
    r = subprocess.run(["sh", "-n", str(fb / "vendor-data")])
    assert r.returncode == 0


def test_unit_never_holds_the_boot():
    unit = hi.UNIT_TEXT
    assert "Type=exec" in unit and "network-online" not in unit
    assert "ConditionPathExists=!/var/lib/hasler-install/done" in unit
    assert "ExecStart=/usr/bin/python3 -I /usr/local/sbin/hasler-install resume" in unit
