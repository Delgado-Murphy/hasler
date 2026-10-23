"""scripts/hasler-os-update with apt and dpkg faked: what goes in, what is held
back, deferring, and the way back after a failed health check.

The real apt plumbing (private lists, repack, dpkg -i back) is exercised in a
Debian trixie container by scripts/tests/os-update-docker.sh."""

from __future__ import annotations

import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    loader = importlib.machinery.SourceFileLoader("hasler_os_update", str(ROOT / "scripts/hasler-os-update"))
    spec = importlib.util.spec_from_loader("hasler_os_update", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


U = _load()
T0 = 1_792_000_000.0     # some night


class FakeBox(U.Box):
    def __init__(self, root: Path) -> None:
        super().__init__(str(root))
        self.t = T0
        self.hour = 3
        self.inst = {("libssl3t64", "arm64"): "3.5.1-1",
                     ("openssl", "arm64"): "3.5.1-1",
                     ("linux-image-rpi-v8", "arm64"): "1:6.12.47-1+rpt1",
                     ("mpd", "arm64"): "0.24.4-1moode1",
                     ("libavcodec61", "arm64"): "7:7.1.1-1+rpt1",
                     ("bluez", "arm64"): "5.82-1",
                     ("python3.13", "arm64"): "3.13.5-2",
                     ("libfoo1", "arm64"): "1.0-1",
                     ("tzdata", "all"): "2025b-4"}
        self.avail = {("libssl3t64", "arm64"): "3.5.1-1+deb13u1",
                      ("openssl", "arm64"): "3.5.1-1+deb13u1",
                      ("linux-image-rpi-v8", "arm64"): "1:6.12.48-1",
                      ("mpd", "arm64"): "0.24.4-1+deb13u1",
                      ("libavcodec61", "arm64"): "7:7.1.2-0+deb13u1",
                      ("bluez", "arm64"): "5.82-1+deb13u1",
                      ("python3.13", "arm64"): "3.13.5-2+deb13u1",
                      ("libfoo1", "arm64"): "1.0-1+deb13u1",
                      ("tzdata", "all"): "2025b-4"}        # same: nothing to do
        self.held = {"python3.13"}
        self.deps: dict[str, list[str]] = {"openssl": ["libssl3t64"]}
        self.idle_answer = (True, "idle")
        self.network = True
        self.healthy = [(True, "healthy")]
        self.phase = "idle"
        self.calls: list[tuple] = []
        self.active_during_install = None

    # clock and gates
    def now(self):
        return self.t

    def local_hour(self):
        return self.hour

    def updater_phase(self):
        return self.phase

    def free_bytes(self):
        return 10 << 30

    def idle(self):
        self.calls.append(("idle",))
        return self.idle_answer

    # apt / dpkg
    def apt_update(self, tailscale):
        self.calls.append(("update", tailscale))
        return (True, "ok") if self.network else (False, "Debian-Security not reachable: Err:1 http://deb.debian.org")

    def available(self):
        return dict(self.avail)

    def installed(self):
        return dict(self.inst)

    def holds(self):
        return set(self.held)

    def snapshot(self):
        return "".join(f"{n}\t{v}\n" for (n, _a), v in sorted(self.inst.items()))

    def _arch(self, name):
        return next(a for (n, a) in list(self.inst) + list(self.avail) if n == name)

    def simulate(self, names):
        self.calls.append(("simulate", tuple(names)))
        todo, seen = list(names), []
        while todo:
            n = todo.pop(0)
            if n in seen:
                continue
            seen.append(n)
            todo.extend(self.deps.get(n, []))
        lines = []
        for n in seen:
            a = self._arch(n)
            old = self.inst.get((n, a))
            new = self.avail[(n, a)]
            lines.append(f"Inst {n} [{old}] ({new} Debian-Security:13/stable-security [{a}])" if old
                         else f"Inst {n} ({new} Debian-Security:13/stable-security [{a}])")
        return True, "\n".join(lines) + "\n"

    def download(self, names):
        self.calls.append(("download", tuple(names)))
        return True, ""

    def install(self, names):
        self.calls.append(("install", tuple(names)))
        self.active_during_install = (self.run_dir / "active").exists()
        ok, out = self.simulate(names)
        for c in U.parse_simulation(out)[0]:
            self.inst[(c["name"], self._arch(c["name"]))] = c["new"]
        return True, ""

    def apt_clean(self):
        self.calls.append(("clean",))

    def dpkg_audit(self):
        return ""

    def save_deb(self, name, version, arch, out):
        f = out / f"{name}_{version}_{arch}.deb"
        f.write_text(json.dumps([name, arch, version]))
        return f

    def dpkg_install(self, debs):
        self.calls.append(("dpkg-i", tuple(sorted(p.name for p in debs))))
        for p in debs:
            name, arch, version = json.loads(p.read_text())
            self.inst[(name, arch)] = version
        return True, ""

    # services, health
    def unit_files(self, names):
        return []

    def mask_inactive(self, units):
        return []

    def unmask(self, units):
        pass

    def main_pid(self, unit):
        return 0

    def stale(self):
        return []

    def boot_id(self):
        return "boot-1"

    def baseline(self):
        return {"api": {"version": "1.0.3", "ok": True, "checks": {}}}

    def health_wait(self, baseline, appear_s=90.0, hold_s=60.0):
        self.calls.append(("health",))
        return self.healthy.pop(0) if len(self.healthy) > 1 else self.healthy[0]


@pytest.fixture
def box(tmp_path):
    return FakeBox(tmp_path / "root")


def run(box, **kw):
    lane = U.Lane(box)
    rc = lane.run(**{"auto": True, **kw})
    return rc, U.Lane(box).st


def events(box):
    return [json.loads(x) for x in box.events_file.read_text().splitlines()]


def names(box, kind):
    return [c for c in box.calls if c[0] == kind]


# --- what goes in ---------------------------------------------------------------------------


def test_upgrades_only_what_is_allowed(box):
    rc, st = run(box)
    assert rc == 0 and st["last_result"] == "ok"
    assert names(box, "install") == [("install", ("libfoo1", "libssl3t64", "openssl"))]
    assert box.inst[("libssl3t64", "arm64")] == "3.5.1-1+deb13u1"
    # never: kernel, moOde's build, Raspberry Pi's build, held, Bluetooth
    assert box.inst[("linux-image-rpi-v8", "arm64")] == "1:6.12.47-1+rpt1"
    assert box.inst[("mpd", "arm64")] == "0.24.4-1moode1"
    assert box.inst[("libavcodec61", "arm64")] == "7:7.1.1-1+rpt1"
    assert box.inst[("python3.13", "arm64")] == "3.13.5-2"
    assert box.inst[("bluez", "arm64")] == "5.82-1"
    held = " ".join(st["held_back"])
    for n in ("linux-image-rpi-v8 (never", "mpd (never", "libavcodec61 (Raspberry", "python3.13 (held", "bluez (never"):
        assert n in held
    assert st["last_upgrade"] == {"at": U.iso(T0), "count": 3}
    assert "libssl3t64 3.5.1-1 -> 3.5.1-1+deb13u1" in st["upgraded"]
    assert names(box, "clean"), "apt-get clean after success (SD wear)"
    assert box.active_during_install is True and not (box.run_dir / "active").exists()
    snaps = list(box.state.glob("pre-*.list"))
    assert len(snaps) == 1 and "libssl3t64\t3.5.1-1\n" in snaps[0].read_text()
    assert [e["event"] for e in events(box)] == ["upgrading", "upgraded"]
    assert box.status_file.stat().st_mode & 0o777 == 0o644      # haslerd reads it


def test_a_package_that_drags_in_a_held_one_is_held_back(box):
    box.deps["libfoo1"] = ["python3.13"]
    rc, st = run(box)
    assert rc == 0
    assert ("libfoo1", "arm64") in box.inst and box.inst[("libfoo1", "arm64")] == "1.0-1"
    assert any(h.startswith("libfoo1 (would also change python3.13") for h in st["held_back"])
    assert box.inst[("openssl", "arm64")] == "3.5.1-1+deb13u1"


def test_nothing_to_do_is_a_quiet_night(box):
    box.avail = dict(box.inst)
    rc, st = run(box)
    assert rc == 0 and st["last_result"] == "none" and not names(box, "install")
    assert st["last_night"] == U.night_key(T0)


# --- not now --------------------------------------------------------------------------------


def test_not_idle_defers_before_touching_apt(box):
    box.idle_answer = (False, "radio in use (playing=True, standby=False)")
    rc, st = run(box)
    assert rc == U.EX_DEFERRED and not names(box, "update")
    assert "radio in use" in st["deferred"]["why"] and st["last_result"] is None
    assert st["last_night"] is None     # tried again at the second slot


def test_woke_up_after_download_defers_before_install(box):
    answers = [(True, "idle"), (False, "radio in use")]
    box.idle = lambda: answers.pop(0)
    rc, _ = run(box)
    assert rc == U.EX_DEFERRED and names(box, "download") and not names(box, "install")


def test_no_network_is_a_no_op(box):
    rc, st = run(box)
    assert rc == 0
    box.network = False
    box.t += 86400
    box.calls.clear()
    rc, st = run(box)
    assert rc == U.EX_DEFERRED and not names(box, "simulate") and not names(box, "install")
    assert st["last_result"] == "ok" and st["deferred"]["why"].startswith("no network")


@pytest.mark.parametrize("hour", [1, 5, 14, 23])
def test_outside_the_window(box, hour):
    box.hour = hour
    assert run(box)[0] == U.EX_DEFERRED and not box.calls


def test_once_a_night(box):
    assert run(box)[0] == 0
    box.calls.clear()
    box.t += 40 * 60      # the 04:15 slot
    assert run(box)[0] == 0 and not names(box, "update")
    assert U.Lane(box).st["last_result"] == "ok"


@pytest.mark.parametrize("phase", ["trial", "probation"])
def test_never_during_a_release_trial_or_probation(box, phase):
    box.phase = phase
    rc, st = run(box)
    assert rc == U.EX_DEFERRED and phase in st["deferred"]["why"] and not box.calls


def test_never_during_a_release_apply(box):
    lock = box.p("/var/lib/hasler-apply/lock")
    lock.parent.mkdir(parents=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, st = run(box)
    finally:
        os.close(fd)
    assert rc == U.EX_DEFERRED and "busy" in st["deferred"]["why"]


def test_off_in_box_yaml(box):
    f = box.p("/etc/hasler/box.yaml")
    f.parent.mkdir(parents=True)
    f.write_text("schema: 1\nupdate_mode: auto\nos_updates: off\n")
    rc, st = run(box)
    assert rc == 0 and st["last_result"] == "off" and st["enabled"] is False and not box.calls


# --- the way back ---------------------------------------------------------------------------


def test_health_failure_goes_back_and_pauses(box):
    before = dict(box.inst)
    box.healthy = [(False, "check mpd failed (False)"), (True, "healthy")]
    rc, st = run(box)
    assert rc == 1 and st["last_result"] == "rolled_back"
    assert box.inst == before, "every replaced package is back at its old version"
    (call,) = names(box, "dpkg-i")
    assert "libssl3t64_3.5.1-1_arm64.deb" in call[1]
    assert st["paused"]["reason"] == "check mpd failed (False)"
    assert "libssl3t64=3.5.1-1+deb13u1" in st["bad"]
    ev = events(box)[-1]
    assert ev["event"] == "rolled-back" and ev["healthy_after"] is True
    assert not (box.run_dir / "active").exists()

    # the next nights: paused
    box.t += 86400
    box.calls.clear()
    rc, st = run(box)
    assert rc == U.EX_DEFERRED and "paused" in st["deferred"]["why"] and not names(box, "update")

    # after 14 days it runs again, but never the versions that failed
    box.t += 14 * 86400
    box.healthy = [(True, "healthy")]
    rc, st = run(box)
    assert rc == 0 and st["paused"] is None and not names(box, "install")
    assert any("failed here before" in h for h in st["held_back"])
    assert box.inst[("libssl3t64", "arm64")] == "3.5.1-1"

    # ... while a newer fix is taken
    box.avail[("libssl3t64", "arm64")] = box.avail[("openssl", "arm64")] = "3.5.1-1+deb13u2"
    box.t += 86400
    rc, st = run(box)
    assert rc == 0 and box.inst[("libssl3t64", "arm64")] == "3.5.1-1+deb13u2"


def test_resume_by_hand(box):
    box.healthy = [(False, "haslerd not active")]
    assert run(box)[0] == 1
    lane = U.Lane(box)
    assert lane.resume() == "resumed" and U.Lane(box).st["paused"] is None
    assert events(box)[-1]["event"] == "resumed"


def test_rollback_by_hand_puts_back_the_last_run(box):
    before = dict(box.inst)
    assert run(box)[0] == 0 and box.inst != before
    assert U.Lane(box).rollback() == 0
    assert box.inst == before
    st = U.Lane(box).st
    assert st["last_result"] == "rolled_back" and "openssl=3.5.1-1+deb13u1" in st["bad"]


def test_no_health_probe_means_no_upgrade(box):
    def boom():
        raise ImportError("no hasler_update")
    box.baseline = boom
    rc, st = run(box)
    assert rc == 1 and st["last_result"] == "error" and not names(box, "install")


def test_an_interrupted_run_is_finished_first(box):
    lane = U.Lane(box)
    lane.st["in_progress"] = {"at": "x", "changes": []}
    lane.save()
    done = []
    box.dpkg_configure_pending = lambda: (done.append(1), (True, ""))[1]
    assert run(box)[0] == 0 and done
    assert events(box)[0]["event"] == "interrupted-run-finished"


# --- pieces ---------------------------------------------------------------------------------


def test_events_are_capped(box):
    lane = U.Lane(box)
    for i in range(260):
        lane.event("x", i=i, pad="p" * 100)
    lines = box.events_file.read_text().splitlines()
    assert len(lines) <= U.EVENTS_MAX_LINES and box.events_file.stat().st_size <= U.EVENTS_MAX_BYTES
    assert json.loads(lines[-1])["i"] == 259


@pytest.mark.parametrize("a,b,want", [
    ("1.0", "1.0", 0), ("1.0-1", "1.0-1+deb13u1", -1), ("3.5.1-1+deb13u2", "3.5.1-1+deb13u10", -1),
    ("1:0.1", "9.9", 1), ("1.0~rc1", "1.0", -1), ("2.30-1", "2.4-1", 1), ("1.0a", "1.0", 1),
    ("7:7.1.1-1+rpt1", "7:7.1.2-0+deb13u1", -1), ("1.0-1", "1.0", 1), ("0.24.4-1moode1", "0.24.4-1+deb13u1", -1),
])
def test_vercmp(a, b, want):
    got = U.vercmp(a, b)
    assert (got > 0) - (got < 0) == want


def test_parse_simulation():
    out = ("Inst libssl3t64 [3.5.1-1] (3.5.1-1+deb13u1 Debian-Security:13/stable-security [arm64])\n"
           "Inst libnew2:arm64 (2.0-1 Debian-Security:13/stable-security [arm64])\n"
           "Conf libssl3t64 (3.5.1-1+deb13u1 Debian-Security:13/stable-security [arm64])\n"
           "Remv oldthing [1.0]\n")
    inst, remv = U.parse_simulation(out)
    assert inst == [{"name": "libssl3t64", "old": "3.5.1-1", "new": "3.5.1-1+deb13u1"},
                    {"name": "libnew2", "old": None, "new": "2.0-1"}]
    assert remv == ["oldthing"]


def test_denylist():
    for n in ("linux-image-rpi-2712", "raspi-firmware", "firmware-brcm80211", "rpi-eeprom", "mpd",
              "camilladsp", "moode-player", "alsa-utils", "libasound2t64", "bluez", "initramfs-tools"):
        assert U.denied(n), n
    for n in ("libssl3t64", "openssl", "libc6", "systemd", "python3.13", "tailscale", "nginx", "mpdris2"):
        assert not U.denied(n), n


def test_parse_packages_keeps_the_highest():
    text = ("Package: a\nVersion: 1.0-1+deb13u1\nArchitecture: arm64\n\n"
            "Package: a\nVersion: 1.0-1+deb13u2\nArchitecture: arm64\nDescription: x\n continued\n\n"
            "Package: b\nVersion: 2\nArchitecture: all\n")
    assert U.parse_packages(text) == {("a", "arm64"): "1.0-1+deb13u2", ("b", "all"): "2"}


def test_units_ship_and_are_pulled_in():
    svc = (ROOT / "config/hasler-os-update.service").read_text()
    timer = (ROOT / "config/hasler-os-update.timer").read_text()
    assert "ExecStart=/usr/bin/python3 -I /usr/local/sbin/hasler-os-update run --auto" in svc
    assert "SuccessExitStatus=75" in svc and "RuntimeDirectory=hasler-os-update" in svc
    assert "OnCalendar=*-*-* 03:35,04:15" in timer
    assert "Wants=hasler-os-update.timer" in (ROOT / "config/haslerd.service").read_text().splitlines()
    assert 'Rule("scripts/hasler-os-update", "integration/sbin/", mode=0o755)' in \
        (ROOT / "scripts/release/build_release.py").read_text()
