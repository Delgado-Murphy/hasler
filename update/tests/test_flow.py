"""check -> stage -> apply, health failure rollback, boot loops, power cuts, gating."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import World

from hasler_update.apply import Deferred, PowerCut, ApplyError


def test_check_stage_apply_commit(world: World):
    world.publish("1.0.0", 1)
    st = world.checker().check()
    assert st["result"] == "ok" and st["available"]["version"] == "1.0.0"
    st = world.checker().stage()
    assert st["result"] == "ok" and "staged" in st["message"]
    staged = world.layout.staged / "1"
    assert (staged / "STAGED").exists() and (staged / "tree" / "app/haslerd/__init__.py").exists()

    hooks_app = world.applier()
    out = hooks_app.apply()
    assert out == "1.0.0 committed"
    s = world.state()
    assert s["phase"] == "probation" and s["max_seq"] == 1 and s["to"] == "1.0.0"
    assert world.current() == "1.0.0"
    rel = world.layout.release("1.0.0")
    assert (rel / ".complete").exists() and (rel / "venv/bin/python").exists()
    assert "# 1.0.0" in world.unit_text()
    assert oct((world.layout.sbin / "hasler-wifi-join").stat().st_mode & 0o777) == "0o755"
    # the updater is replaced only once its release passed
    assert "1.0.0" in (world.layout.updater / "hasler_update/__init__.py").read_text()
    assert "1.0.0" in (world.layout.systemd_dir / "hasler-update-boot.service").read_text()
    assert "1.0.0" in (world.layout.sbin / "hasler-update").read_text()
    calls = [c for c in hooks_app.hooks.calls if c[0] == "systemctl"]
    assert ("systemctl", "daemon-reload") in calls and ("systemctl", "restart", "--no-block", "haslerd.service") in calls
    # staged and incoming are cleaned after commit
    assert not any(world.layout.staged.iterdir())
    # nothing new next time
    assert world.checker().check()["message"] == "up to date"


def test_upgrade_then_probation_ends(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    assert w.checker().stage()["result"] == "ok"
    assert w.applier().apply() == "1.1.0 committed"
    assert w.current() == "1.1.0"
    assert Path(os.readlink(w.layout.previous)).name == "1.0.0"
    assert "# 1.1.0" in w.unit_text()
    assert w.state()["phase"] == "probation"
    w.now += 25 * 3600
    assert w.applier().boot() == "phase committed"     # probation over
    assert w.applier().on_failure().startswith("phase committed")   # no rollback any more
    assert w.current() == "1.1.0"


def test_failing_health_rolls_back(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2, extra={"integration/udev/99-new.rules": b'ACTION=="add"\n'})
    w.checker().stage()
    a = w.applier(healthy=False)
    out = a.apply()
    assert out.startswith("1.1.0 rolled back")
    s = w.state()
    assert s["phase"] == "rolled_back" and 2 in s["bad"] and s["max_seq"] == 1
    assert w.current() == "1.0.0"
    assert "# 1.0.0" in w.unit_text()
    assert not (w.layout.udev_dir / "99-new.rules").exists()     # added by 1.1.0 -> removed
    assert b"1.0.0" in (w.layout.sbin / "hasler-wifi-join").read_bytes()
    assert ("systemctl", "restart", "--no-block", "haslerd.service") in a.hooks.calls
    # the bad release is never offered again
    assert "failed here before" in w.checker().check()["message"]
    assert w.applier().apply() == "nothing staged"


def test_first_install_rollback_restores_preexisting_units(world: World):
    """The bench box: install.sh's haslerd.service is put back if 1.0.0 fails."""
    old = world.layout.systemd_dir / "haslerd.service"
    old.write_text("ExecStart=/home/pi/hasler/daemon/.venv/bin/python -m haslerd.main\n")
    world.publish("1.0.0", 1)
    world.checker().stage()
    out = world.applier(healthy=False).apply()
    assert "rolled back" in out
    assert "/home/pi/hasler" in old.read_text()
    assert world.current() is None and not (world.layout.sbin / "hasler-wifi-join").exists()


def test_boot_loop_rolls_back_at_second_boot(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    with pytest.raises(PowerCut):
        w.applier(crash_at="restarted").apply()       # box hangs/reboots during the health wait
    assert w.state()["phase"] == "trial" and w.current() == "1.1.0"
    assert w.applier().boot() == "trial boot 1"
    assert w.current() == "1.1.0"
    # it crashes again before resume could judge it
    assert w.applier().boot() == "rolled back after a boot loop"
    s = w.state()
    assert s["phase"] == "rolled_back" and "boot loop" in s["reason"] and w.current() == "1.0.0"
    assert "# 1.0.0" in w.unit_text()


def test_on_failure_during_probation(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    w.applier().apply()
    assert w.state()["phase"] == "probation"
    assert w.applier().on_failure().startswith("rolled back")
    assert w.current() == "1.0.0" and 2 in w.state()["bad"]


CHECKPOINTS_OK = ["copied", "venv", "trial-written", "integrated", "switched", "restarted", "health", "committed"]


@pytest.mark.parametrize("cut", CHECKPOINTS_OK)
def test_power_cut_at_each_step_lands_on_a_working_version(installed_v1: World, cut: str):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    with pytest.raises(PowerCut):
        w.applier(crash_at=cut).apply()
    # power returns: early boot, haslerd, then resume
    w.applier().boot()
    w.applier().resume()
    s = w.state()
    cur = w.current()
    assert cur in ("1.0.0", "1.1.0")
    assert (w.layout.release(cur) / ".complete").exists()
    assert f"# {cur}" in w.unit_text(), (cut, cur, w.unit_text())
    assert s["phase"] in ("committed", "probation", "rolled_back")
    if cut in ("copied", "venv"):
        # nothing live changed; a half-built release is dropped, a retry works
        assert cur == "1.0.0" and s["phase"] == "probation" and s["max_seq"] == 1
        assert not (w.layout.releases / "1.1.0.tmp").exists()
        assert w.applier().apply() == "1.1.0 committed"
    elif cut in ("trial-written", "integrated"):
        assert cur == "1.0.0" and s["phase"] == "rolled_back"
    else:
        assert cur == "1.1.0" and s["phase"] == "probation" and s["max_seq"] == 2


@pytest.mark.parametrize("cut", ["rollback-started", "rollback-restored", "rollback-switched"])
def test_power_cut_during_rollback_is_finished_at_boot(installed_v1: World, cut: str):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    with pytest.raises(PowerCut):
        w.applier(healthy=False, crash_at=cut).apply()
    assert w.state()["step"] == "rollback"
    assert w.applier().boot() == "finished an interrupted rollback"
    assert w.current() == "1.0.0" and "# 1.0.0" in w.unit_text()
    assert w.state()["phase"] == "rolled_back"


def test_power_cut_during_download_resumes(world: World):
    rel = world.publish("1.0.0", 1)
    world.checker().check()
    art = Path(rel["artifacts"][0]["url"].removeprefix("file://"))
    part = world.layout.incoming / (art.name + ".part")
    part.write_bytes(art.read_bytes()[:100])
    assert world.checker().stage()["result"] == "ok"
    assert not part.exists()


def test_power_cut_during_staging_is_discarded(world: World):
    world.publish("1.0.0", 1)
    (world.layout.staged / "1.tmp" / "junk").parent.mkdir(parents=True)
    (world.layout.staged / "1.tmp" / "junk").write_text("half")
    assert world.checker().stage()["result"] == "ok"
    assert not (world.layout.staged / "1.tmp").exists() and (world.layout.staged / "1" / "STAGED").exists()


# --- gating -----------------------------------------------------------------------------


def test_not_applied_while_radio_in_use(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    with pytest.raises(Deferred, match="radio playing"):
        w.applier(idle=False).apply(auto=True)
    assert w.current() == "1.0.0" and w.state()["phase"] == "probation"
    assert not (w.layout.releases / "1.1.0").exists()       # nothing even copied


def test_window_and_modes(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    with pytest.raises(Deferred, match="window"):
        w.applier(hour=14).apply(auto=True)
    w.layout.box_yaml.write_text(w.layout.box_yaml.read_text().replace("update_mode: auto", "update_mode: ask"))
    with pytest.raises(Deferred, match="asked"):
        w.applier().apply(auto=True)
    # "Update now" from the key page (radio-confirmed): mode ask is fine, still needs idle
    with pytest.raises(Deferred):
        w.applier(hour=14, idle=False).apply(now_=True)
    assert w.applier(hour=14).apply(now_=True) == "1.1.0 committed"


def test_major_release_always_asks(installed_v1: World):
    w = installed_v1
    w.publish("2.0.0", 2, release_patch={"kind": "major"})
    st = w.checker().stage()
    assert st["available"]["ask"] is True
    with pytest.raises(Deferred, match="major"):
        w.applier().apply(auto=True)


def test_selftest_failure_changes_nothing(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    with pytest.raises(ApplyError, match="selftest"):
        w.applier(selftest_fails=True).apply()
    assert w.current() == "1.0.0" and w.state()["phase"] == "probation" and "# 1.0.0" in w.unit_text()


def test_manual_rollback_and_back(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    w.applier().apply()
    out = w.applier().rollback()
    assert out == "1.0.0 committed" and w.current() == "1.0.0"
    s = w.state()
    assert s["phase"] == "committed" and s["max_seq"] == 2 and 2 in s["bad"]
    assert "# 1.0.0" in w.unit_text()
    out = w.applier().rollback("1.1.0")
    assert w.current() == "1.1.0"


def test_keep_three_releases(installed_v1: World):
    w = installed_v1
    for i, v in enumerate(["1.1.0", "1.2.0", "1.3.0", "1.4.0"], start=2):
        w.publish(v, i)
        w.checker().stage()
        assert "committed" in w.applier().apply()
    kept = sorted(p.name for p in w.layout.releases.iterdir())
    assert kept == ["1.2.0", "1.3.0", "1.4.0"]


def test_config_txt_block_needs_reboot_flag(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2, extra={"integration/configtxt/hasler.block": b"[all]\ndtoverlay=pwm-gpio,gpio=17\n"})
    w.checker().stage()
    with pytest.raises(Exception, match="reboot"):
        w.applier().apply()
    assert w.current() == "1.0.0" and w.state()["phase"] == "probation"   # refused before the trial
    assert "dtoverlay" not in (w.root / "boot/firmware/config.txt").read_text()
