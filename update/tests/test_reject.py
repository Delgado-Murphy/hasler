"""Everything that must be refused before anything is unpacked or switched."""

from __future__ import annotations

import shutil
import tarfile

import pytest
from conftest import DAY, World, sha

from hasler_update.state import iso


def _ti(name, kind=tarfile.REGTYPE, mode=0o644, link="", size=0):
    ti = tarfile.TarInfo(name)
    ti.type, ti.mode, ti.linkname, ti.size = kind, mode, link, size
    return ti


def staged_nothing(w: World) -> bool:
    return not w.layout.staged.exists() or not any(p for p in w.layout.staged.iterdir())


def test_downgrade_refused(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    w.applier().apply()
    w.publish("0.9.0", 1)        # an old (vulnerable) release re-served
    st = w.checker().check()
    assert st["available"] is None and st["message"] == "up to date"


def test_same_version_lower_seq_via_direct_staging_refused(installed_v1: World):
    """Even if something stages it, apply re-checks seq against the root-owned state."""
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    w.applier().apply()
    st = w.state()
    st["max_seq"] = 5
    w.layout.state_file.write_text(__import__("json").dumps(st))
    w.publish("1.2.0", 3)
    assert w.checker().check()["message"] == "up to date"


def test_expired_release_refused(world: World):
    world.publish("1.0.0", 1, release_patch={"expires": iso(world.now - DAY)})
    st = world.checker().check()
    assert st["result"] == "error" and "expired" in st["message"]


def test_expired_index_means_no_fresh_info(world: World):
    world.publish("1.0.0", 1, index_patch={"expires": iso(world.now - 60)})
    st = world.checker().check()
    assert st["result"] == "error" and "no fresh update info" in st["message"]


def test_bad_release_signature_refused(world: World):
    world.publish("1.0.0", 1, signer="rogue")
    st = world.checker().stage()
    assert st["result"] == "error" and "bad signature" in st["message"]
    assert staged_nothing(world)


def test_release_signed_by_timestamp_key_refused(world: World):
    """A stolen timestamp key can freeze, never ship code."""
    world.publish("1.0.0", 1, signer="timestamp")
    assert "bad signature" in world.checker().check()["message"]


def test_index_signed_by_wrong_key_refused(world: World):
    world.publish("1.0.0", 1, index_signer="rogue")
    assert "bad signature" in world.checker().check()["message"]


def test_keyring_not_signed_by_root_refused(world: World):
    world.write_keyring(1, signer="rogue")
    world.publish("1.0.0", 1)
    assert "bad signature (hasler-keyring)" in world.checker().check()["message"]


def test_tampered_artifact_refused(world: World):
    world.publish("1.0.0", 1, tamper_artifact=True)
    st = world.checker().stage()
    assert st["result"] == "error" and ("SHA-256" in st["message"] or "more than" in st["message"])
    assert staged_nothing(world)


def test_tampered_file_inside_tree_refused(world: World):
    """files.sha256 lists a hash that the file doesn't have (artifact itself re-signed)."""
    files = world.tree_files("1.0.0")
    listing = "".join(f"{sha(d)}  {m:04o}  {p}\n" for p, (d, m) in sorted(files.items()))
    listing = listing.replace(sha(files["app/haslerd/__init__.py"][0]), "0" * 64)
    world.publish("1.0.0", 1, list_override=listing.encode())
    st = world.checker().stage()
    assert st["result"] == "error" and ("files.sha256" in st["message"] or "hash mismatch" in st["message"]), st
    assert staged_nothing(world)


def test_staged_tree_tampered_after_staging_refused_by_root(world: World):
    """The unprivileged user (or an attacker as it) edits the staged tree: apply re-hashes its own copy."""
    world.publish("1.0.0", 1)
    world.checker().stage()
    f = world.layout.staged / "1" / "tree" / "app/haslerd/__init__.py"
    f.write_text("import os; os.system('evil')\n")
    with pytest.raises(Exception, match="hash mismatch"):
        world.applier().apply()
    assert world.current() is None


def test_staged_listing_rewritten_refused_by_root(world: World):
    """...even if it rewrites files.sha256 to match: that list is pinned by the signed files_sha256."""
    world.publish("1.0.0", 1)
    world.checker().stage()
    tree = world.layout.staged / "1" / "tree"
    f = tree / "app/haslerd/__init__.py"
    f.write_text("evil\n")
    lst = (tree / "files.sha256").read_text().splitlines()
    lst = [ln if not ln.endswith("app/haslerd/__init__.py") else f"{sha(b'evil' + bytes([10]))}  0644  app/haslerd/__init__.py" for ln in lst]
    (tree / "files.sha256").write_text("\n".join(lst) + "\n")
    with pytest.raises(Exception, match="does not match the signed manifest"):
        world.applier().apply()


def test_without_files_sha256_root_reextracts(world: World):
    world.publish("1.0.0", 1, files_sha256=False)
    world.checker().stage()
    (world.layout.staged / "1" / "tree" / "app/haslerd/__init__.py").write_text("evil\n")
    assert world.applier().apply() == "1.0.0 committed"
    assert "evil" not in (world.layout.release("1.0.0") / "app/haslerd/__init__.py").read_text()


@pytest.mark.parametrize("member,why", [
    ((_ti("hasler-1.0.0/../evil", size=1), b"x"), "traversal"),
    ((_ti("/etc/evil", size=1), b"x"), "unsafe path"),
    ((_ti("other-top/evil", size=1), b"x"), "outside"),
    ((_ti("hasler-1.0.0/link", tarfile.SYMTYPE, link="../../etc/passwd"), None), "escapes"),
    ((_ti("hasler-1.0.0/abs", tarfile.SYMTYPE, link="/etc/passwd"), None), "absolute symlink"),
    ((_ti("hasler-1.0.0/hard", tarfile.LNKTYPE, link="hasler-1.0.0/manifest.json"), None), "hard link"),
    ((_ti("hasler-1.0.0/suid", mode=0o4755, size=1), b"x"), "setuid"),
    ((_ti("hasler-1.0.0/dev", tarfile.CHRTYPE), None), "device"),
    ((_ti("hasler-1.0.0/fifo", tarfile.FIFOTYPE), None), "device, FIFO"),
])
def test_unsafe_tar_members_refused(world: World, member, why):
    world.publish("1.0.0", 1, members=[member])
    st = world.checker().stage()
    assert st["result"] == "error" and why in st["message"], st["message"]
    assert staged_nothing(world)
    assert not (world.tmp / "evil").exists() and not (world.root / "etc/evil").exists()


def test_unlisted_file_refused(world: World):
    world.publish("1.0.0", 1, members=[(_ti("hasler-1.0.0/extra.py", size=1), b"x")])
    st = world.checker().stage()
    assert "not in files.sha256" in st["message"]


def test_symlink_inside_tree_allowed(world: World):
    world.publish("1.0.0", 1, members=[(_ti("hasler-1.0.0/app/latest", tarfile.SYMTYPE, link="haslerd"), None)])
    assert world.checker().stage()["result"] == "ok"


def test_halted_release_skipped(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2, halted=[2])
    st = w.checker().stage()
    assert st["available"] is None and "halted" in st["message"]
    assert staged_nothing(w)


def test_halt_after_staging_unstages(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    assert (w.layout.staged / "2").exists()
    w.publish("1.1.0", 2, halted=[2])
    w.checker().check()
    assert not (w.layout.staged / "2").exists()
    assert w.applier().apply() == "nothing staged"


def test_incompatible_refused(world: World):
    world.publish("1.0.0", 1, release_patch={"requires": {"arch": "x86_64"}})
    assert "incompatible" in world.checker().check()["message"]
    world.publish("1.0.0", 1, release_patch={"requires": {"moode": ">=11"}})
    assert "moOde" in world.checker().check()["message"]
    world.publish("1.0.0", 1, release_patch={"variants": ["excelsior"]})
    assert "variant" in world.checker().check()["message"]


def test_min_from_seq(installed_v1: World):
    w = installed_v1
    w.publish("1.5.0", 5, release_patch={"min_from_seq": 3})
    assert "at least seq 3" in w.checker().check()["message"]


def test_ring_not_open_waits(world: World):
    world.publish("1.0.0", 1, index_patch={"rings": {"0": ["other-box"], "2": ["*"]},
                                           "ring_open": {"0": iso(world.now - 60), "2": iso(world.now + DAY)}})
    assert "not yet open" in world.checker().check()["message"]


def test_integration_off_allowlist_refused(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2, extra={"integration/etc/passwd": b"root::0:0::/:/bin/sh\n"})
    w.checker().stage()
    with pytest.raises(Exception, match="allowlist"):
        w.applier().apply()
    assert w.current() == "1.0.0" and not (w.root / "etc/passwd").exists()


def test_updater_from_a_failed_release_is_never_installed(installed_v1: World):
    """Its units, launcher and package only go in at commit - a broken release can't break its own rollback."""
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    w.applier(healthy=False).apply()
    assert "1.0.0" in (w.layout.systemd_dir / "hasler-update-boot.service").read_text()
    assert "1.0.0" in (w.layout.sbin / "hasler-update").read_text()
    assert "1.0.0" in (w.layout.updater / "hasler_update/__init__.py").read_text()


def test_unit_with_version_path_refused(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2, extra={"integration/systemd/haslerd.service": b"ExecStart=/opt/hasler/releases/1.1.0/venv/bin/python\n"})
    w.checker().stage()
    with pytest.raises(Exception, match="version path"):
        w.applier().apply()


def test_keyring_rotation(world: World):
    """A release that needs keyring 2: fetched, root-verified, installed by apply."""
    import json

    from conftest import sign
    new_keys = [
        {"id": "release-b", "role": "release", "pub": world.k["rogue"][1], "not_after": iso(world.now + 365 * DAY)},
        {"id": "timestamp-a", "role": "timestamp", "pub": world.k["timestamp"][1], "not_after": iso(world.now + 365 * DAY)},
    ]
    doc = json.dumps({"schema": 1, "keyring_seq": 2, "expires": iso(world.now + 365 * DAY), "keys": new_keys}).encode()
    world.server.mkdir(parents=True, exist_ok=True)
    (world.server / "keyring.json").write_bytes(doc)
    (world.server / "keyring.json.sig").write_bytes(sign(doc, world.k["root"][0], "hasler-keyring", world.work))
    world.publish("1.0.0", 1, signer="rogue", release_patch={"keyring_seq": 2})
    assert world.checker().stage()["result"] == "ok"
    assert world.applier().apply() == "1.0.0 committed"
    assert json.loads((world.layout.trust / "keyring.json").read_bytes())["keyring_seq"] == 2
    assert world.state()["keyring_seq"] == 2


def test_zstd_artifact(world: World):
    if not shutil.which("zstd"):
        pytest.skip("no zstd")
    world.publish("1.0.0", 1, fmt="zst")
    assert world.checker().stage()["result"] == "ok"
