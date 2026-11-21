"""scripts/hasler-bootcfg-guard on a fake root: repair, save, the loop guard."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    loader = importlib.machinery.SourceFileLoader("bootcfg_guard", str(ROOT / "scripts/hasler-bootcfg-guard"))
    spec = importlib.util.spec_from_loader("bootcfg_guard", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


G = _load()

BLOCK = (ROOT / "config/configtxt/hasler.block").read_text()     # what this tree ships
OLD_BLOCK = "[all]\ngpio=17,22,27=op,dh\ndtoverlay=pwm-gpio,gpio=17\ndtoverlay=pwm-gpio,gpio=22\ndtoverlay=pwm-gpio,gpio=27\n"
EMBER_BLOCK = "[all]\ngpio=17=ip,pd\ngpio=22,27=op,dh\ndtoverlay=pwm-gpio,gpio=17\ndtoverlay=pwm-gpio,gpio=22\ndtoverlay=pwm-gpio,gpio=27\n"
LAMP = "\n".join([G.LAMP_BLOCK[0], BLOCK.strip("\n"), G.LAMP_BLOCK[1]])


def lamp(block: str) -> str:
    return "\n".join([G.LAMP_BLOCK[0], block.strip("\n"), G.LAMP_BLOCK[1]])
BOOT = "\n".join([G.BLOCKS[1][0], "[all]", "boot_delay=0", G.BLOCKS[1][1]])


def moode(overlay: str, extra: str = "") -> str:
    return "\n".join([
        "#########################################",
        "# This file is managed by moOde",
        "#########################################",
        "", "# Device filters", "[pi5]", "dtoverlay=vc4-kms-v3d-pi5,noaudio", "[all]",
        "", "# General settings", "dtparam=i2c_arm=on", "force_eeprom_read=0",
        "", "# Do not alter this section", "[all]",
        "", "# Audio overlays", overlay, "",
    ]) + extra


GOOD = moode("dtoverlay=hifiberry-amp4pro", "\n" + BOOT + "\n" + LAMP + "\n")
DEFAULT = moode("#dtoverlay=none")      # moOde's pristine copy
CMDLINE = "console=tty1 root=PARTUUID=abc-02 rootfstype=ext4 fsck.repair=yes rootwait quiet loglevel=3\n"


class Box:
    def __init__(self, tmp: Path, monkeypatch) -> None:
        self.root = tmp / "root"
        self.paths = G.Paths(str(self.root))
        (self.root / "boot/firmware").mkdir(parents=True)
        self.t = 1_000_000.0
        self.state = "starting"
        self.reboots = 0
        monkeypatch.setattr(G, "now", lambda: self.t)
        monkeypatch.setattr(G, "system_state", lambda: self.state)
        monkeypatch.setattr(G, "reboot", self._reboot)
        monkeypatch.setattr(G, "sync", lambda: None)
        monkeypatch.setattr(G, "boot_id", lambda: f"boot-{self.reboots}")

    def _reboot(self) -> None:
        self.reboots += 1

    @property
    def cfg(self) -> Path:
        return self.paths.config

    def write(self, cfg: str | bytes, cmdline: str | bytes | None = CMDLINE) -> None:
        self.cfg.write_bytes(cfg.encode() if isinstance(cfg, str) else cfg)
        if cmdline is not None:
            self.paths.cmdline.write_bytes(cmdline.encode() if isinstance(cmdline, str) else cmdline)

    def known_good(self, cfg: str = GOOD, cmdline: str = CMDLINE) -> None:
        self.paths.good_dir.mkdir(parents=True, exist_ok=True)
        self.paths.good("config.txt").write_text(cfg)
        self.paths.good("cmdline.txt").write_text(cmdline)

    def db(self, overlay: str) -> None:
        self.paths.moode_db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.paths.moode_db)
        con.execute("CREATE TABLE IF NOT EXISTS cfg_system (id INTEGER PRIMARY KEY, param CHAR(32), value CHAR(32))")
        con.execute("DELETE FROM cfg_system WHERE param='i2soverlay'")
        con.execute("INSERT INTO cfg_system (param, value) VALUES ('i2soverlay', ?)", (overlay,))
        con.commit()
        con.close()

    def db_set(self, **params: str) -> None:
        con = sqlite3.connect(self.paths.moode_db)
        for k, v in params.items():
            con.execute("DELETE FROM cfg_system WHERE param=?", (k,))
            con.execute("INSERT INTO cfg_system (param, value) VALUES (?, ?)", (k, v))
        con.commit()
        con.close()

    def db_get(self) -> dict:
        con = sqlite3.connect(self.paths.moode_db)
        try:
            return dict(con.execute("SELECT param, value FROM cfg_system").fetchall())
        finally:
            con.close()

    def release_block(self, block: str = BLOCK) -> None:
        self.paths.release_block.parent.mkdir(parents=True, exist_ok=True)
        self.paths.release_block.write_text(block)

    def repair(self) -> int:
        return G.cmd_repair(self.paths)

    def doc(self) -> dict:
        return json.loads(self.paths.state_file.read_text())


@pytest.fixture
def box(tmp_path, monkeypatch) -> Box:
    b = Box(tmp_path, monkeypatch)
    b.db("hifiberry-amp4pro")
    b.known_good()
    return b


# --- repair ---------------------------------------------------------------------------

@pytest.mark.parametrize("broken", [
    b"",                                            # empty after the power cut
    GOOD[:60].encode(),                             # truncated mid-header
    GOOD[:len(GOOD) // 2].encode(),                 # truncated: headers there, blocks gone
    b"\0" * 4096,                                   # a cluster of zeros
    DEFAULT.encode(),                               # moOde restored its default (the incident)
    moode("dtoverlay=hifiberry-amp4pro", "\n" + BOOT + "\n").encode(),   # lamp block missing
], ids=["empty", "truncated-head", "truncated-half", "nuls", "moode-default", "no-lamp-block"])
def test_broken_config_is_repaired_with_one_reboot(box, broken):
    box.write(broken)
    assert box.repair() == 0
    assert box.cfg.read_text() == GOOD
    assert box.reboots == 1
    doc = box.doc()
    assert doc["pending"]["file"] == "config.txt" and doc["repairs"][-1]["reboot"] is True
    assert list(box.paths.state_dir.glob("config.txt.bad-*"))
    # the next boot finds it sane: no second reboot, the repair is marked as held
    box.t += 40
    assert box.repair() == 0
    assert box.reboots == 1 and box.doc()["pending"] is None


def test_missing_config_is_repaired(box):
    box.write(GOOD)
    box.cfg.unlink()
    assert box.repair() == 0
    assert box.cfg.read_text() == GOOD and box.reboots == 1


def test_sane_config_untouched(box):
    box.write(GOOD)
    before = box.cfg.stat().st_mtime_ns
    assert box.repair() == 0
    assert box.reboots == 0
    assert box.cfg.stat().st_mtime_ns == before
    assert not box.paths.state_file.exists()


def test_sane_with_different_lamp_block_body_untouched(box):
    """A trial boot after the updater changed the block: present, so sane."""
    changed = GOOD.replace(G.LAMP_BLOCK[1], "gpio=4=op,dl\n" + G.LAMP_BLOCK[1])
    box.write(changed)
    assert box.repair() == 0
    assert box.cfg.read_text() == changed and box.reboots == 0


def test_upgraded_radio_with_the_new_block_is_sane(box):
    """The ember block on a radio whose known-good still has the old lines:
    present and marked, so sane - the old copy never puts the old lines back."""
    new = GOOD.replace(LAMP, lamp(EMBER_BLOCK))
    box.known_good(cfg=GOOD.replace(LAMP, lamp(OLD_BLOCK)))
    box.release_block(EMBER_BLOCK)
    box.write(new)
    assert box.repair() == 0
    assert box.cfg.read_text() == new and box.reboots == 0


def test_restore_carries_the_release_block_over_an_old_known_good(box):
    """A power cut after the update wrote the new block, before the known-good
    was refreshed: the restored copy has the release's block lines."""
    old = GOOD.replace(LAMP, lamp(OLD_BLOCK))
    box.known_good(cfg=old)
    box.release_block(EMBER_BLOCK)
    box.write(b"")
    assert box.repair() == 0 and box.reboots == 1
    assert box.cfg.read_text() == GOOD.replace(LAMP, lamp(EMBER_BLOCK))
    assert "gpio=17=ip,pd" in box.cfg.read_text() and "gpio=17,22,27" not in box.cfg.read_text()
    # without a release block the known-good goes back as it is
    box.paths.release_block.unlink()
    box.t += G.WINDOW_S + 1
    box.write(b"")
    assert box.repair() == 0 and box.cfg.read_text() == old


def test_shipped_block_is_the_ember_block():
    assert BLOCK == EMBER_BLOCK


def test_overlay_changed_in_moode_is_not_reverted(box):
    """A new amp chosen in moOde's UI: config.txt and the DB agree, known-good is stale."""
    box.db("hifiberry-dacplus")
    new = GOOD.replace("dtoverlay=hifiberry-amp4pro", "dtoverlay=hifiberry-dacplus")
    box.write(new)
    assert box.repair() == 0
    assert box.cfg.read_text() == new and box.reboots == 0


def test_without_db_known_good_overlay_means_some_overlay(box):
    box.paths.moode_db.unlink()
    other = GOOD.replace("dtoverlay=hifiberry-amp4pro", "dtoverlay=hifiberry-dacplus")
    box.write(other)
    assert box.repair() == 0 and box.reboots == 0          # a deliberate change stays
    box.write(moode("#dtoverlay=none", "\n" + BOOT + "\n" + LAMP + "\n"))
    assert box.repair() == 0 and box.reboots == 1          # the default's "none" does not
    assert box.cfg.read_text() == GOOD


def test_no_known_good_leaves_it(box):
    box.paths.good("config.txt").unlink()
    box.write(b"")
    assert box.repair() == 1
    assert box.cfg.read_bytes() == b"" and box.reboots == 0


def test_bad_known_good_is_not_restored(box):
    box.known_good(cfg=DEFAULT)          # stale DB says amp4pro, known-good has none
    box.write(b"")
    assert box.repair() == 1
    assert box.reboots == 0 and box.cfg.read_bytes() == b""


def test_loop_guard_one_reboot_per_window(box):
    box.write(b"")
    assert box.repair() == 0 and box.reboots == 1
    # the repair did not hold: broken again at the next boot, 50 s later
    box.t += 50
    box.write(DEFAULT)
    assert box.repair() == 1
    assert box.reboots == 1                               # no second reboot
    assert box.cfg.read_text() == DEFAULT                 # and left alone
    assert box.doc()["gave_up"]["problems"]
    # a clock that went backwards counts as "recent" too
    box.t -= 3600
    assert box.repair() == 1 and box.reboots == 1
    # a fresh breakage long after: repaired again
    box.t += 3600 + G.WINDOW_S + 1
    assert box.repair() == 0 and box.reboots == 2


def test_running_system_restores_without_reboot(box):
    box.state = "running"
    box.write(DEFAULT)
    assert box.repair() == 0
    assert box.cfg.read_text() == GOOD and box.reboots == 0
    assert box.doc()["pending"] is None


@pytest.mark.parametrize("cmd", [b"", b"\0\0\0", b"console=tty1 quiet\n"], ids=["empty", "nuls", "no-root"])
def test_cmdline_restored_without_reboot(box, cmd):
    box.write(GOOD, cmd)
    assert box.repair() == 0
    assert box.paths.cmdline.read_text() == CMDLINE
    assert box.reboots == 0 and box.cfg.read_text() == GOOD


# --- moOde stuck on HDMI ----------------------------------------------------------------

def test_guard_reasserts_amp_after_hdmi_fallback(box):
    """The 21 Nov power cut: an amp-missing boot left moOde on HDMI; repair puts
    the amp back in its database before moOde's worker starts."""
    box.db_set(adevname="Pi HDMI 1", amixname="PCM", alsa_output_mode="iec958", cardnum="0", i2sdevice="None")
    box.write(GOOD)
    assert box.repair() == 0
    db = box.db_get()
    assert (db["adevname"], db["amixname"], db["alsa_output_mode"]) == ("snd_rpi_hifiberry_dacplus", "Digital", "plughw")
    assert db["cardnum"] == "0" and db["i2soverlay"] == "hifiberry-amp4pro"     # moOde reassigns the card itself
    entry = box.doc()["repairs"][-1]
    assert entry["file"] == "moode-db" and len(entry["changed"]) == 3 and entry["reboot"] is False
    assert box.reboots == 0 and box.cfg.read_text() == GOOD                     # nothing else touched
    # the next boot: already the amp, nothing to do
    n = len(box.doc()["repairs"])
    assert box.repair() == 0 and len(box.doc()["repairs"]) == n
    # HDMI 2 is a fallback too
    box.db_set(adevname="Pi HDMI 2")
    assert box.repair() == 0 and box.db_get()["adevname"] == "snd_rpi_hifiberry_dacplus"


@pytest.mark.parametrize("overlay,adev", [
    ("hifiberry-amp4pro", "snd_rpi_hifiberry_dacplus"),     # the amp: as it should be
    ("hifiberry-amp4pro", "USB Audio Device"),              # a USB DAC chosen in moOde
    ("hifiberry-dacplus", "Pi HDMI 1"),                     # another board: not ours to judge
    ("None", "Pi HDMI 1"),                                  # HDMI on purpose
], ids=["amp", "usb", "other-overlay", "no-overlay"])
def test_guard_leaves_named_i2s_alone(box, overlay, adev):
    box.db(overlay)
    box.db_set(adevname=adev, amixname="Master", alsa_output_mode="hw", i2sdevice="None")
    before = box.db_get()
    box.write(GOOD.replace("dtoverlay=hifiberry-amp4pro", f"dtoverlay={overlay}"))
    box.repair()
    assert box.db_get() == before
    assert not box.paths.state_file.exists() or all(r["file"] != "moode-db" for r in box.doc().get("repairs", []))


def test_guard_without_moode_db_or_with_a_broken_one(box):
    box.paths.moode_db.unlink()
    box.write(GOOD)
    assert box.repair() == 0
    box.paths.moode_db.write_bytes(b"not a database")
    assert box.repair() == 0 and box.reboots == 0


# --- save ----------------------------------------------------------------------------

def test_save_only_when_sane(tmp_path, monkeypatch):
    b = Box(tmp_path, monkeypatch)
    b.db("hifiberry-amp4pro")
    b.release_block()
    b.state = "running"
    # the bench tonight: moOde's default, no lamp block -> never becomes the known-good
    b.write(DEFAULT)
    assert G.cmd_save(b.paths) == 1
    assert not b.paths.good("config.txt").exists()
    # sane: saved, cmdline too
    b.write(GOOD)
    assert G.cmd_save(b.paths) == 0
    assert b.paths.good("config.txt").read_text() == GOOD
    assert b.paths.good("cmdline.txt").read_text() == CMDLINE
    mtime = b.paths.good("config.txt").stat().st_mtime_ns
    assert G.cmd_save(b.paths) == 0                          # unchanged: not rewritten
    assert b.paths.good("config.txt").stat().st_mtime_ns == mtime
    # broken later: the known-good stays as it was
    b.write(GOOD[:100])
    assert G.cmd_save(b.paths) == 1
    assert b.paths.good("config.txt").read_text() == GOOD


def test_save_waits_for_the_boot(box):
    box.write(GOOD.replace("boot_delay=0", "boot_delay=1"))
    box.state = "starting"
    assert G.cmd_save(box.paths) == 1
    assert G.cmd_save(box.paths, force=True) == 0
    assert "boot_delay=1" in box.paths.good("config.txt").read_text()


def test_lamp_block_required_once_a_release_ships_one(tmp_path, monkeypatch):
    b = Box(tmp_path, monkeypatch)
    b.db("hifiberry-amp4pro")
    b.state = "running"
    no_lamp = moode("dtoverlay=hifiberry-amp4pro", "\n" + BOOT + "\n")
    b.write(no_lamp)
    assert G.cmd_check(b.paths) == 0          # no release block: nothing to expect
    b.release_block()
    assert G.cmd_check(b.paths) == 1


def test_main_usage():
    assert G.main([]) == 2


# --- the updater refreshes the known-good after its own changes ------------------------

def test_updater_commit_saves_known_good(world):
    world.publish("1.0.0", 1)
    assert world.checker().stage()["result"] == "ok"
    hooks_calls = []
    ap = world.applier()
    ap.hooks.calls = hooks_calls
    assert "committed" in ap.apply()
    assert ("bootcfg-save",) in hooks_calls


def test_hook_runs_the_shipped_guard(tmp_path, monkeypatch):
    from hasler_update.apply import Hooks
    from hasler_update.layout import Layout
    lay = Layout(tmp_path)
    h = Hooks(lay)
    seen = []
    monkeypatch.setattr(h, "run", lambda argv, **kw: seen.append((argv, kw.get("env", {}).get("HASLER_ROOT"))))
    h.bootcfg_save()                       # not installed: nothing
    assert seen == []
    lay.sbin.mkdir(parents=True)
    (lay.sbin / "hasler-bootcfg-guard").write_text("#!/bin/sh\n")
    h.bootcfg_save()
    assert seen and seen[0][0][-1] == "save" and seen[0][1] == str(tmp_path)
