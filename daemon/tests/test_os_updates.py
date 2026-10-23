"""haslerd's side of the Debian security lane (scripts/hasler-os-update):
the lamp's busy signal and the os_updates block of /api/health."""

from __future__ import annotations

import json
from types import SimpleNamespace

from haslerd import boxconfig
from haslerd.health import health_report
from haslerd.web.software import Software


def make(tmp_path, box="schema: 1\n"):
    root = tmp_path / "root"
    (root / "usr/local/sbin").mkdir(parents=True)
    (root / "usr/local/sbin/hasler-os-update").write_text("#!/usr/bin/python3\n")
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "box.yaml").write_text(box)
    paths = boxconfig.Paths(release=tmp_path, etc=etc, state=tmp_path / "state")
    return Software(root=root, box_paths=paths, systemctl=lambda u: None), root


def test_lamp_breathes_while_packages_change(tmp_path):
    sw, root = make(tmp_path)
    assert sw.installing() is False
    active = root / "run/hasler-os-update/active"
    active.parent.mkdir(parents=True)
    active.write_text("123\n")
    assert sw.installing() is True
    active.unlink()
    assert sw.installing() is False


def test_status_block(tmp_path):
    sw, root = make(tmp_path)
    assert sw.os_updates()["enabled"] is True and sw.os_updates()["last_upgrade"] is None
    st = root / "var/lib/hasler-os/status.json"
    st.parent.mkdir(parents=True)
    (root / "proc/sys/kernel/random").mkdir(parents=True)
    (root / "proc/sys/kernel/random/boot_id").write_text("boot-1\n")
    st.write_text(json.dumps({"last_run": "2026-10-21T03:40:00Z", "last_result": "ok",
                              "upgraded": ["openssl 3.5.1-1 -> 3.5.1-1+deb13u1"], "held_back": ["mpd (held)"],
                              "reboot_advisable": True, "boot_id": "boot-1", "paused": None, "bad": ["x=1"],
                              "last_upgrade": {"at": "2026-10-21T03:40:00Z", "count": 3}}))
    doc = sw.os_updates()
    assert doc["last_upgrade"]["count"] == 3 and doc["reboot_advisable"] is True
    assert doc["held_back"] == ["mpd (held)"] and "bad" not in doc
    # a power-on since then finished the job
    (root / "proc/sys/kernel/random/boot_id").write_text("boot-2\n")
    assert sw.os_updates()["reboot_advisable"] is False
    # in /api/health and the key page's /api/software
    app = SimpleNamespace(software=sw)
    assert health_report(app)["os_updates"]["last_result"] == "ok"


def test_off_in_box_yaml_and_absent_lane(tmp_path):
    sw, root = make(tmp_path, "schema: 1\nos_updates: off\n")
    assert sw.os_updates()["enabled"] is False
    assert "os_updates" not in boxconfig.haslerd_settings({"os_updates": False})
    (root / "usr/local/sbin/hasler-os-update").unlink()
    assert sw.os_updates() is None
    assert health_report(SimpleNamespace())["os_updates"] is None
