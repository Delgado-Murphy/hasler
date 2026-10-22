"""The volume floor from the key page, the Amp4 Pro as the default card, and a
box.yaml ``moode:`` key this haslerd doesn't know (never fatal)."""

import json
import logging
import subprocess

import pytest
from conftest import VARIANTS

from haslerd import main
from haslerd.actions import Actions
from haslerd.player.moode import MoodeConfig, MoodePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, load_settings, volume_floor_from, volume_floor_range


@pytest.fixture
def amixer(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("haslerd.player.moode.subprocess.run", run)
    return calls


@pytest.fixture
def rig(tmp_path, amixer):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    player = MoodePlayer(MoodeConfig(source_flags={}, volume_limit=70, volume_floor=28), sources={})
    actions = Actions(variant, player)
    app = WebApp(actions, Panel.from_variant(variant, fake=True), tmp_path / "keys.yaml",
                 {"radio": True}, ha=False, settings_file=tmp_path / "settings.json")
    return app, actions, player


def levels(calls):
    return [c[5] for c in calls if c[3] == "sset"]


# -- defaults ---------------------------------------------------------------------


def test_the_amp4_pro_is_the_default_card(amixer):
    config = MoodeConfig()
    assert (config.card, config.mixer) == ("sndrpihifiberry", "Digital")
    MoodePlayer(MoodeConfig(source_flags={}), sources={}).set_volume(50)
    assert amixer[-1][:5] == ["amixer", "-c", "sndrpihifiberry", "sset", "Digital"]


def test_the_default_floor_makes_the_first_step_audible():
    # Digital is 0.5 dB/step from -103.5 dB: 1% would be silence.
    assert MoodeConfig().volume_floor == 28


# -- validation -------------------------------------------------------------------


def test_floor_range_follows_the_ceiling():
    assert volume_floor_range(70) == (0, 60)
    assert volume_floor_range(30) == (0, 20)
    assert volume_floor_range(None) == (0, 90)
    assert volume_floor_from(60, 70) == 60 and volume_floor_from(0, 70) == 0


@pytest.mark.parametrize("raw", [-1, 61, 28.5, "28", True, None])
def test_bad_floors_are_refused(raw):
    with pytest.raises(ValueError):
        volume_floor_from(raw, 70)


# -- /api/settings ----------------------------------------------------------------


def test_get_reports_floor_and_range(rig):
    app, *_ = rig
    doc = app.settings()
    assert doc["volume_floor"] == 28 and doc["volume_floor_range"] == [0, 60]


def test_put_floor_persists_and_remaps_the_knob_at_once(rig, tmp_path, amixer):
    app, _, player = rig
    player.set_volume(50)
    assert levels(amixer)[-1] == "49%"               # 28 + 50 * 42 / 100
    doc = app.put_settings({"volume_floor": 40})
    assert doc["volume_floor"] == 40 and doc["volume_floor_range"] == [0, 60]
    assert player.config.volume_floor == 40
    assert levels(amixer)[-1] == "55%"               # same knob, new floor
    assert load_settings(tmp_path / "settings.json") == {"schema": 1, "volume_floor": 40}


@pytest.mark.parametrize("body", [{"volume_floor": 61}, {"volume_floor": -1}, {"volume_floor": "30"},
                                  {"volume_floor": 45, "volume_limit": 50}])
def test_put_bad_floor_changes_nothing(rig, tmp_path, body):
    app, _, player = rig
    with pytest.raises(ValueError):
        app.put_settings(body)
    assert (player.config.volume_floor, player.config.volume_limit) == (28, 70)
    assert not (tmp_path / "settings.json").exists()


def test_floor_is_checked_against_a_new_ceiling_in_the_same_put(rig):
    app, _, player = rig
    doc = app.put_settings({"volume_limit": 90, "volume_floor": 75})
    assert (player.config.volume_limit, player.config.volume_floor) == (90, 75)
    assert doc["volume_floor_range"] == [0, 80]


def test_lowering_the_ceiling_pulls_a_high_floor_down(rig, tmp_path):
    app, _, player = rig
    app.put_settings({"volume_floor": 60})
    doc = app.put_settings({"volume_limit": 40})
    assert doc["volume_floor"] == 30 and player.config.volume_floor == 30
    assert load_settings(tmp_path / "settings.json")["volume_floor"] == 30


def test_floor_in_standby_is_saved_but_not_played(rig, amixer):
    app, actions, player = rig
    actions._standby(None, {})
    n = len(amixer)
    app.put_settings({"volume_floor": 35})
    assert player.config.volume_floor == 35
    assert not [c for c in amixer[n:] if c[3] == "sset"]


# -- startup: settings.json over box.yaml, unknown keys ---------------------------


def test_saved_floor_wins_over_box_yaml(tmp_path):
    assert main.moode_config({"moode": {"volume_floor": 10}}, {"volume_floor": 33}).volume_floor == 33
    assert main.moode_config({"moode": {"volume_floor": 10}}, {}).volume_floor == 10
    # bad saved floor (above limit-10): ignored, the yaml's stands
    assert main.moode_config({"moode": {"volume_floor": 10}}, {"volume_floor": 65}).volume_floor == 10
    # checked against the saved ceiling, not the yaml's
    cfg = main.moode_config({"moode": {"volume_limit": 50}}, {"volume_limit": 90, "volume_floor": 70})
    assert (cfg.volume_limit, cfg.volume_floor) == (90, 70)


def test_unknown_box_yaml_key_is_dropped_with_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=main.log.name):
        cfg = main.moode_config({"moode": {"card": 1, "retired_knob": True}}, {})
    assert cfg.card == 1
    assert "moode.retired_knob" in caplog.text


def test_get_settings_without_a_player_config_reads_the_saved_floor(tmp_path):
    from haslerd.player.base import FakePlayer

    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    (tmp_path / "settings.json").write_text(json.dumps({"volume_floor": 22}))
    app = WebApp(Actions(variant, FakePlayer()), Panel.from_variant(variant, fake=True),
                 tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    assert app.settings()["volume_floor"] == 22
