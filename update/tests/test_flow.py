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
    # a crash-looped release must not leave the start limit blocking the next start
    assert calls.index(("systemctl", "reset-failed", "haslerd.service")) < calls.index(("systemctl", "restart", "--no-block", "haslerd.service"))
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


def test_a_haslerd_restart_is_not_a_boot(installed_v1: World, monkeypatch):
    """Found on the bench: restarting haslerd mid-trial also started the boot
    unit, which counted a 'boot' and rolled the trial back. Same boot id: no-op."""
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    monkeypatch.setenv("HASLER_BOOT_ID", "boot-A")
    with pytest.raises(PowerCut):
        w.applier(crash_at="restarted").apply()
    assert w.state()["boot_id"] == "boot-A"
    assert w.applier().boot() == "same boot: nothing to count"
    assert w.state()["phase"] == "trial" and w.current() == "1.1.0"
    monkeypatch.setenv("HASLER_BOOT_ID", "boot-B")      # a real reboot
    assert w.applier().boot() == "trial boot 1"


def test_on_failure_during_probation(installed_v1: World):
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    w.applier().apply()
    assert w.state()["phase"] == "probation"
    assert w.applier().on_failure().startswith("rolled back")
    assert w.current() == "1.0.0" and 2 in w.state()["bad"]


CHECKPOINTS_OK = ["copied", "venv", "trial-written", "integrated", "switched", "restarted", "health", "committed"]


def test_a_trial_judged_after_a_reboot_gets_the_long_window(installed_v1: World):
    """Bench power-cut drill: after a cold boot moOde starts MPD later than 90 s,
    so the post-reboot judgement uses the boot window (BOOT_APPEAR_S)."""
    w = installed_v1
    w.publish("1.1.0", 2)
    w.checker().stage()
    with pytest.raises(PowerCut):
        w.applier(crash_at="restarted").apply()
    w.applier().boot()
    a = w.applier()
    a.resume()
    assert ("health-after-boot", "1.1.0") in a.hooks.calls


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


# --- a major release with system files of a kind the running updater has no rule for --------

PKG = Path(__file__).resolve().parents[1] / "hasler_update"
TMPFILES = "integration/tmpfiles/hasler.conf"


def new_updater(knows_tmpfiles: bool = True) -> dict[str, bytes]:
    """This updater's package as the next release ships it: with a rule for a
    kind (``tmpfiles``) the running one doesn't have."""
    files = {f"app/hasler_update/{p.name}": p.read_bytes() for p in PKG.glob("*.py")}
    if knows_tmpfiles:
        src = files["app/hasler_update/integrate.py"].decode()
        anchor = '        ("avahi", re.compile('
        assert anchor in src
        src = src.replace(anchor, '        ("tmpfiles", re.compile(r"^hasler\\.conf$"), '
                                  'lambda n: layout.root / "etc/tmpfiles.d" / n, 0o644),\n' + anchor, 1)
        files["app/hasler_update/integrate.py"] = src.encode()
    return files


@pytest.fixture
def fake_systemctl(tmp_path, monkeypatch):
    """The child updater uses the real Hooks: systemctl/udevadm from PATH, logged."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    for tool in ("systemctl", "udevadm"):
        (bindir / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        (bindir / tool).chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return log


def publish_major_with_new_kind(w: World, knows: bool = True, kind: str = "major") -> Path:
    extra = {**new_updater(knows), TMPFILES: b"d /run/hasler 0755 root root -\n"}
    w.publish("2.0.0", 2, release_patch={"kind": kind}, extra=extra)
    assert w.checker().stage()["result"] == "ok"
    return w.root / "etc/tmpfiles.d/hasler.conf"


def test_major_new_kind_installed_after_commit_by_the_new_updater(installed_v1: World, fake_systemctl):
    w = installed_v1
    dest = publish_major_with_new_kind(w)
    ap = w.applier()
    seen = {}
    real = ap.hooks.health_wait

    def health(version, baseline, after_boot=False):
        seen["during_trial"] = dest.exists()
        return real(version, baseline, after_boot)
    ap.hooks.health_wait = health
    assert ap.apply(now_=True) == "2.0.0 committed"
    assert seen["during_trial"] is False          # the old updater has no destination for it
    assert dest.read_text() == "d /run/hasler 0755 root root -\n"
    s = w.state()
    assert s["phase"] == "probation" and s["pending_integration"] is None
    assert str(dest) in s["integrated"]
    events = [h["event"] for h in s["history"]]
    assert events[-4:] == ["switch", "integration-deferred", "commit", "integrate-pending"]
    assert "systemctl daemon-reload" in fake_systemctl.read_text()   # by the child (new updater)
    # idempotent: nothing pending, nothing done
    assert w.applier().integrate_pending() == "nothing pending"
    # a rollback in probation takes the release's late files out as well
    assert w.applier().on_failure().startswith("rolled back")
    assert not dest.exists() and w.current() == "1.0.0" and w.state()["pending_integration"] is None


@pytest.mark.parametrize("kind", ["minor", "patch"])
def test_non_major_new_kind_still_refused(installed_v1: World, kind):
    w = installed_v1
    dest = publish_major_with_new_kind(w, kind=kind)
    with pytest.raises(ApplyError, match="allowlist"):
        w.applier().apply(now_=True)
    assert w.current() == "1.0.0" and not dest.exists()
    assert "1.0.0" in (w.layout.updater / "hasler_update/__init__.py").read_text()


def test_pending_failure_recorded_then_retried(installed_v1: World, fake_systemctl):
    w = installed_v1
    dest = publish_major_with_new_kind(w)
    ap = w.applier()

    def broken(args):
        raise OSError("exec failed")
    ap.hooks.run_updater = broken
    assert ap.apply(now_=True) == "2.0.0 committed"      # the release stays committed
    s = w.state()
    assert s["phase"] == "probation" and w.current() == "2.0.0" and not dest.exists()
    pend = s["pending_integration"]
    assert pend["version"] == "2.0.0" and pend["items"] == ["tmpfiles/hasler.conf"]
    assert pend["attempts"] == 1 and "exec failed" in pend["error"]
    assert s["history"][-1]["event"] == "integrate-pending-failed"
    # next boot (resume, after haslerd) retries through the installed (new) updater
    assert w.applier().resume() == "phase probation"
    assert dest.exists() and w.state()["pending_integration"] is None


def test_pending_failure_in_the_new_updater_is_recorded_and_retried_by_apply(installed_v1: World, fake_systemctl):
    """The release's own updater has no rule for the kind either: recorded,
    the release stays, and the nightly apply tries again."""
    w = installed_v1
    dest = publish_major_with_new_kind(w, knows=False)
    assert w.applier().apply(now_=True) == "2.0.0 committed"
    pend = w.state()["pending_integration"]
    assert pend["attempts"] == 1 and "no rule in this updater either" in pend["error"]
    assert not dest.exists() and w.current() == "2.0.0"
    assert w.applier().apply() == "nothing staged"
    assert w.state()["pending_integration"]["attempts"] == 2
    assert w.state()["phase"] == "probation"


def test_major_still_refuses_bad_names_symlinks_and_strays(tmp_path):
    from hasler_update import integrate as integ
    from hasler_update.layout import Layout

    layout = Layout(tmp_path / "root")

    def plan_of(rel_path: str, data: bytes | None = b"x", link: str | None = None):
        rel = tmp_path / "rel"
        import shutil
        shutil.rmtree(rel, ignore_errors=True)
        p = rel / "integration" / rel_path
        p.parent.mkdir(parents=True)
        if link is not None:
            p.symlink_to(link)
        else:
            p.write_bytes(data)
        deferred: list[str] = []
        return integ.plan(layout, rel, major=True, deferred=deferred), deferred

    assert plan_of("tmpfiles/hasler.conf") == ([], ["tmpfiles/hasler.conf"])
    for bad in ("avahi/other.service", "sudoers/root", "stray", "Tmp/x", "tmpfiles/.hidden"):
        with pytest.raises(integ.IntegrateError, match="allowlist"):
            plan_of(bad)
    with pytest.raises(integ.IntegrateError, match="not a regular file"):
        plan_of("tmpfiles/hasler.conf", link="/etc/shadow")
    with pytest.raises(integ.IntegrateError, match="not a regular file"):
        plan_of("tmpfiles", link="/etc")
    # not major: refused as before
    rel = tmp_path / "rel2"
    (rel / "integration/tmpfiles").mkdir(parents=True)
    (rel / "integration/tmpfiles/hasler.conf").write_text("x")
    with pytest.raises(integ.IntegrateError, match="allowlist"):
        integ.plan(layout, rel)
