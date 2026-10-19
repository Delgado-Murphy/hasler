"""Migrations on a snapshot, the real offline venv, the CLI, small parsers."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import ROOT, FakeHooks, World

from hasler_update.apply import Applier, Hooks
from hasler_update.integrate import config_block, render_config_txt
from hasler_update.layout import Layout, read_box_yaml
from hasler_update.verify import version_matches


class MigratingHooks(FakeHooks):
    def migrate(self, release, etc, state, from_schema):
        (etc / "box.yaml").write_text((etc / "box.yaml").read_text().replace("schema: 1", "schema: 2"))
        (state / "settings.json").write_text('{"schema": 2}\n')
        return ["002_x.py"]


def test_migration_runs_on_snapshot_and_rollback_restores(installed_v1: World):
    w = installed_v1
    w.publish("2.0.0", 2, release_patch={"config_schema": 2, "requires_migration": True, "kind": "major"},
              extra={"migrations/002_x.py": b"def up(paths): pass\n"})
    w.checker().stage()
    hooks = MigratingHooks(w, healthy=False)
    Applier(w.layout, hooks).apply()
    assert "schema: 1" in w.layout.box_yaml.read_text()           # restored from the snapshot
    assert json.loads((w.layout.haslerd_state / "settings.json").read_text())["schema"] == 1
    assert (w.layout.trust / "keyring.json").exists()               # trust never part of a snapshot

    w.publish("2.0.1", 3, release_patch={"config_schema": 2, "requires_migration": True, "kind": "major"},
              extra={"migrations/002_x.py": b"def up(paths): pass\n"})
    w.checker().stage()
    assert Applier(w.layout, MigratingHooks(w)).apply() == "2.0.1 committed"
    assert "schema: 2" in w.layout.box_yaml.read_text()
    assert oct(w.layout.snapshots.stat().st_mode & 0o777) == "0o700"


def test_real_offline_venv_build(tmp_path):
    rel = tmp_path / "opt/hasler/releases/1.0.0"
    (rel / "app/haslerd").mkdir(parents=True)
    (rel / "app/haslerd/__init__.py").write_text("X = 42\n")
    (rel / "requirements.lock").write_text("# empty\n")
    Hooks(Layout(tmp_path)).build_venv(rel, sys.executable)
    out = subprocess.run([str(rel / "venv/bin/python"), "-c", "import haslerd; print(haslerd.X)"],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "42"


def test_cli_status_and_check(world: World):
    world.publish("1.0.0", 1)
    env = {**os.environ, "HASLER_ROOT": str(world.root), "HASLER_MOODE_VERSION": "10.0.2"}
    exe = [sys.executable, str(ROOT / "update/bin/hasler-update")]
    out = subprocess.run(exe + ["status"], env=env, capture_output=True, text=True)
    assert out.returncode == 0 and json.loads(out.stdout)["state"]["phase"] == "idle"
    out = subprocess.run(exe + ["check"], env=env, capture_output=True, text=True)
    doc = json.loads(out.stdout)
    assert doc["available"] is not None or "incompatible" in doc["message"]   # laptop arch differs
    out = subprocess.run(exe + ["instance", "rm -rf"], env=env, capture_output=True, text=True)
    assert out.returncode == 1


def test_config_txt_block_render():
    cur = "a=1\n# >>> hasler pilot lamp (scripts/install.sh; edits here are replaced) >>>\nold\n# <<< hasler pilot lamp <<<\n"
    new = render_config_txt(cur, "[all]\ngpio=17=op,dh\n")
    assert "old" not in new and new.startswith("a=1\n") and config_block(new) == "[all]\ngpio=17=op,dh"


def test_small_parsers(tmp_path):
    p = tmp_path / "box.yaml"
    p.write_text("schema: 1\nchannel: \"bench\"\nupdate_mode: off\nmetered: yes\nnested:\n  x: 1\n")
    d = read_box_yaml(p)
    assert d == {"schema": "1", "channel": "bench", "update_mode": "off", "metered": True}
    assert version_matches("10.0.2", ">=10.0,<11") and not version_matches("11.0", ">=10.0,<11")
