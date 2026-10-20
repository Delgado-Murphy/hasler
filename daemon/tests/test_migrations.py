"""Config migrations, and 001: the bench box's rsync tree -> /etc/hasler."""

import json
import os
import shutil
import stat
from pathlib import Path

import pytest
import yaml

from haslerd import boxconfig, migrations
from haslerd.boxconfig import Paths
from haslerd.migrations.__main__ import main as cli

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "config" / "variants" / "hasler-hm.yaml"
EDITED_CURVE = "curve: [[30, 100], [2000, 87.5], [11000, 75], [21000, 62.5], [24000, 50], [26100, 0]]"

M001 = "001_from_rsync_tree"


@pytest.fixture
def box(tmp_path):
    """A release, an old rsync tree edited on the box, and its state dir."""
    release = tmp_path / "release"
    (release / "config" / "variants").mkdir(parents=True)
    shutil.copy(SHIPPED, release / "config" / "variants" / "hasler-hm.yaml")

    legacy = tmp_path / "legacy"
    (legacy / "config" / "variants").mkdir(parents=True)
    text = SHIPPED.read_text()
    old_curve = next(line.strip() for line in text.splitlines() if line.strip().startswith("curve: [[25"))
    text = text.replace(old_curve, EDITED_CURVE).replace("addr: auto", "addr: 0x27")
    text = text.replace("off_hold_ms: 5000", "off_hold_ms: 4000")
    (legacy / "config" / "variants" / "hasler-hm.yaml").write_text(text)
    (legacy / "config" / "haslerd.yaml").write_text(yaml.safe_dump({
        "moode": {"mpd_host": "localhost", "mixer": "Digital", "card": "sndrpihifiberry",
                  "volume_limit": 65, "spotify": {"enabled": True},
                  "ytm": {"enabled": True, "port": 8099, "cookies": ""}},
        "state_file": str(tmp_path / "state" / "state.json"),
        "web": {"enabled": True, "port": 8080},
        "home_assistant": {"url": "http://ha.local:8123", "token": "SECRET"},
    }))

    state = tmp_path / "state"
    state.mkdir()
    (state / "ytm-cookies.txt").write_text("cookies")
    (state / "ytm-browser.json").write_text("{}")
    (state / "settings.json").write_text(json.dumps({"volume_limit": 60}))
    (state / "keys.yaml").write_text("# Written by the haslerd web page.\npresets: []\nactions: {}\n")
    paths = Paths(release=release, etc=tmp_path / "etc", state=state)
    return paths, legacy, tmp_path / "no-unit"


def _run(paths, legacy, unit, apply):
    return migrations.run(paths, apply=apply, legacy=str(legacy), unit=str(unit))


def test_framework_finds_001():
    assert M001 in migrations.available()


def test_dry_run_plans_and_writes_nothing(box):
    paths, legacy, unit = box
    before = {p: p.read_bytes() for p in paths.state.iterdir()}
    [(mid, lines)] = _run(paths, legacy, unit, apply=False)
    assert mid == M001
    plan = "\n".join(lines)
    assert "would write" in plan and str(paths.box_file) in plan
    assert "controls.volume.curve" in plan
    assert "would move" in plan
    assert not paths.etc.exists()
    assert {p: p.read_bytes() for p in paths.state.iterdir()} == before
    assert migrations.pending(paths) == [M001]


def test_apply_writes_box_overlay_secrets_and_schema(box):
    paths, legacy, unit = box
    _run(paths, legacy, unit, apply=True)

    doc = yaml.safe_load(paths.box_file.read_text())
    assert doc["schema"] == 1 and doc["variant"] == "hasler-hm"
    assert doc["channel"] == "bench" and doc["update_mode"] == "auto"
    assert len(doc["box_id"]) == 36
    assert doc["migrations"] == [M001]
    # only what differs from the code defaults; no secrets, no default state_file
    assert doc["moode"] == {"volume_limit": 65}     # the Amp4 Pro card is the code default
    assert "web" not in doc and "state_file" not in doc and "home_assistant" not in doc
    assert "SECRET" not in paths.box_file.read_text()

    overlay = yaml.safe_load(paths.overlay_file("hasler-hm").read_text())
    assert overlay["schema"] == 1
    assert overlay["controls"] == {
        "volume": {"curve": yaml.safe_load(EDITED_CURVE)["curve"]},
        "tone": {"end_switch": {"off_hold_ms": 4000}},
    }
    assert overlay["io"] == {"digital": {"addr": 0x27}}

    v = boxconfig.load_effective_variant(paths, "hasler-hm")
    edited = yaml.safe_load((legacy / "config" / "variants" / "hasler-hm.yaml").read_text())
    assert v.controls == edited["controls"] and v.io == edited["io"]

    secrets = paths.secrets
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o700
    for name in ("ytm-cookies.txt", "ytm-browser.json"):
        assert (secrets / name).is_file() and not (secrets / name).is_symlink()
        assert (paths.state / name).is_symlink()
        assert os.readlink(paths.state / name) == f"secrets/{name}"
    assert (paths.state / "ytm-cookies.txt").read_text() == "cookies"
    ha = json.loads((secrets / "ha.json").read_text())
    assert ha == {"url": "http://ha.local:8123", "token": "SECRET", "publish": False}
    assert stat.S_IMODE((secrets / "ha.json").stat().st_mode) == 0o600
    assert boxconfig.secret_file(paths.state, "ha.json") == secrets / "ha.json"

    assert json.loads((paths.state / "settings.json").read_text()) == {"schema": 1, "volume_limit": 60}
    keys = (paths.state / "keys.yaml").read_text()
    assert keys.startswith("# Written by the haslerd web page.")
    assert yaml.safe_load(keys) == {"schema": 1, "presets": [], "actions": {}}

    assert migrations.pending(paths) == []
    assert _run(paths, legacy, unit, apply=True) == []


def test_up_is_idempotent_and_existing_values_win(box):
    paths, legacy, unit = box
    boxconfig.write_box_file(paths, {"variant": "hasler-hm", "channel": "stable", "box_id": "mine"})
    boxconfig.write_overlay(paths, "hasler-hm", {"controls": {"volume": {"curve": [[0, 0], [1, 100]]}}})
    mod = migrations.load(M001)
    mod.up(paths, legacy=legacy, unit=unit)
    first = (paths.box_file.read_text(), paths.overlay_file("hasler-hm").read_text())
    doc = yaml.safe_load(first[0])
    assert doc["channel"] == "stable" and doc["box_id"] == "mine"
    assert boxconfig.read_overlay(paths, "hasler-hm")["controls"]["volume"]["curve"] == [[0, 0], [1, 100]]
    again = mod.up(paths, legacy=legacy, unit=unit)
    assert not [line for line in again if line.startswith(("write", "move", "add"))]
    assert (paths.box_file.read_text(), paths.overlay_file("hasler-hm").read_text()) == first


def test_variant_from_the_systemd_unit(box, tmp_path):
    paths, legacy, _ = box
    unit = tmp_path / "haslerd.service"
    unit.write_text(f"ExecStart=/x/python -m haslerd.main \\\n    --variant {legacy}/config/variants/hasler-hm.yaml \\\n")
    mod = migrations.load(M001)
    name, path = mod.find_variant(legacy, None, {}, unit)
    assert name == "hasler-hm" and path == legacy / "config" / "variants" / "hasler-hm.yaml"


def test_same_tree_needs_no_overlay(box):
    paths, _, unit = box
    lines = migrations.load(M001).up(paths, dry_run=True, legacy=paths.release, unit=unit)
    assert not any("variant.d" in line and "write" in line for line in lines)


def test_cli_check_then_apply(box, capsys):
    paths, legacy, _ = box
    common = ["--etc", str(paths.etc), "--state", str(paths.state), "--release", str(paths.release),
              "--legacy", str(legacy)]
    assert cli(["--check", *common]) == 0
    assert "dry run" in capsys.readouterr().out
    assert not paths.box_file.exists()
    assert cli(["--apply", *common]) == 0
    assert paths.box_file.exists()
    assert cli(["--list", *common]) == 0
    assert f"applied  {M001}" in capsys.readouterr().out


def test_missing_shipped_variant_fails(box):
    paths, legacy, unit = box
    with pytest.raises(FileNotFoundError):
        migrations.load(M001).up(paths, legacy=legacy, variant="excelsior", unit=unit)
