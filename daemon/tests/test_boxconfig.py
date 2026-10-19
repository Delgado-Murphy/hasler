"""The config layer: what a release ships vs. what is this box's own."""

import os
import shutil
import stat
from argparse import Namespace
from pathlib import Path

import pytest
import yaml

from haslerd import boxconfig, main
from haslerd.boxconfig import Paths
from haslerd.variant import Panel

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "config" / "variants" / "hasler-hm.yaml"


@pytest.fixture
def paths(tmp_path):
    release = tmp_path / "release"
    (release / "config" / "variants").mkdir(parents=True)
    shutil.copy(SHIPPED, release / "config" / "variants" / "hasler-hm.yaml")
    return Paths(release=release, etc=tmp_path / "etc", state=tmp_path / "state")


# -- paths ----------------------------------------------------------------------


def test_release_root_is_the_checkout_when_running_from_the_repo():
    assert boxconfig.find_release() == ROOT
    assert Paths.from_env().shipped_variant("hasler-hm") == SHIPPED


def test_release_root_in_a_tarball_is_the_dir_beside_the_package(tmp_path):
    (tmp_path / "app" / "haslerd").mkdir(parents=True)
    (tmp_path / "app" / "config" / "variants").mkdir(parents=True)
    assert boxconfig.find_release(tmp_path / "app" / "haslerd") == tmp_path / "app"


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HASLER_ETC", str(tmp_path / "e"))
    monkeypatch.setenv("HASLER_STATE", str(tmp_path / "s"))
    monkeypatch.setenv("HASLER_RELEASE", str(tmp_path / "r"))
    p = Paths.from_env()
    assert (p.etc, p.state, p.release) == (tmp_path / "e", tmp_path / "s", tmp_path / "r")
    assert p.secrets == tmp_path / "s" / "secrets"
    assert p.box_file == tmp_path / "e" / "box.yaml"
    assert p.overlay_file("x") == tmp_path / "e" / "variant.d" / "x.yaml"


def test_defaults_without_env(monkeypatch):
    for name in ("HASLER_ETC", "HASLER_STATE", "HASLER_RELEASE"):
        monkeypatch.delenv(name, raising=False)
    p = Paths.from_env()
    assert p.etc == Path("/etc/hasler") and p.state == Path("/var/lib/haslerd")


def test_variant_names_cannot_escape(paths):
    for bad in ("", "../x", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            paths.shipped_variant(bad)


def test_shipped_data_is_found_without_parents():
    from haslerd.web import catalog, stations

    assert catalog.STATIC_FILE.exists()
    assert stations.CURATED_FILE.exists()


def test_secret_file_prefers_secrets_and_falls_back_for_the_bench_box(tmp_path):
    assert boxconfig.secret_file(tmp_path, "ha.json") == tmp_path / "ha.json"     # nothing yet
    (tmp_path / "ha.json").write_text("{}")
    assert boxconfig.secret_file(tmp_path, "ha.json") == tmp_path / "ha.json"     # legacy file
    (tmp_path / "secrets").mkdir()
    assert boxconfig.secret_file(tmp_path, "ytm-cookies.txt") == tmp_path / "secrets" / "ytm-cookies.txt"
    assert boxconfig.secret_file(tmp_path, "ha.json") == tmp_path / "ha.json"     # not moved yet
    (tmp_path / "secrets" / "ha.json").write_text("{}")
    assert boxconfig.secret_file(tmp_path, "ha.json") == tmp_path / "secrets" / "ha.json"


def test_ha_config_saves_into_secrets_once_it_exists(tmp_path):
    from haslerd.ha import config as ha_config

    (tmp_path / "secrets").mkdir()
    ha_config.save(tmp_path, ha_config.HaSettings(url="http://ha:8123", token="t"))
    assert (tmp_path / "secrets" / "ha.json").exists()
    assert not (tmp_path / "ha.json").exists()
    assert ha_config.load(tmp_path).token == "t"


# -- merging ----------------------------------------------------------------------


def test_deep_merge_dicts_recursive_lists_replaced_and_no_mutation():
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    over = {"a": {"c": [3]}, "e": {"f": 1}}
    out = boxconfig.deep_merge(base, over)
    assert out == {"a": {"b": 1, "c": [3]}, "d": 1, "e": {"f": 1}}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    out["e"]["f"] = 2
    assert over["e"]["f"] == 1


def test_merge_controls_by_id_keeps_order():
    shipped = [{"id": "a", "kind": "pot", "channel": 0, "curve": [[0, 0], [9, 100]]},
               {"id": "b", "kind": "momentary", "pin": "d0"}]
    out = boxconfig.merge_controls(shipped, {"a": {"curve": [[1, 0], [8, 100]]}})
    assert [c["id"] for c in out] == ["a", "b"]
    assert out[0]["curve"] == [[1, 0], [8, 100]] and out[0]["channel"] == 0
    assert shipped[0]["curve"] == [[0, 0], [9, 100]]


def test_merge_controls_unknown_id_is_an_error():
    with pytest.raises(ValueError, match="nope"):
        boxconfig.merge_controls([{"id": "a"}], {"nope": {"x": 1}})
    with pytest.raises(ValueError):
        boxconfig.merge_controls([{"id": "a"}], {"a": {"id": "b"}})


# -- box.yaml and the overlay -----------------------------------------------------


def test_load_box_config_absent_is_empty(paths):
    assert boxconfig.load_box_config(paths) == {}


def test_haslerd_settings_drop_updater_keys():
    box = {"schema": 1, "variant": "v", "channel": "bench", "box_id": "x", "moode": {"card": "c"}}
    assert boxconfig.haslerd_settings(box) == {"moode": {"card": "c"}}


def test_overlay_round_trip_schema_and_none_deletes(paths):
    assert boxconfig.read_overlay(paths, "hasler-hm") == {"controls": {}, "io": {}}
    boxconfig.write_overlay(paths, "hasler-hm", {"controls": {"volume": {"hysteresis_counts": 40,
                                                                          "curve": [[0, 0], [9, 100]]}},
                                                  "io": {"digital": {"addr": 0x27}}})
    raw = yaml.safe_load(paths.overlay_file("hasler-hm").read_text())
    assert raw["schema"] == boxconfig.SCHEMA
    new = boxconfig.write_overlay(paths, "hasler-hm", {"controls": {"volume": {"hysteresis_counts": None}},
                                                        "io": {"digital": None}})
    assert new == {"controls": {"volume": {"curve": [[0, 0], [9, 100]]}}, "io": {}}
    assert boxconfig.read_overlay(paths, "hasler-hm") == new
    assert yaml.safe_load(paths.overlay_file("hasler-hm").read_text())["schema"] == boxconfig.SCHEMA
    assert stat.S_IMODE(os.stat(paths.overlay_file("hasler-hm")).st_mode) == 0o664
    assert stat.S_IMODE(os.stat(paths.overlay_file("hasler-hm").parent).st_mode) == 0o775
    assert not [p for p in paths.overlay_file("x").parent.iterdir() if p.name.startswith(".")]


def test_write_overlay_rejects_unknown_controls_and_keeps_the_file(paths):
    boxconfig.write_overlay(paths, "hasler-hm", {"controls": {"volume": {"hysteresis_counts": 40}}})
    before = paths.overlay_file("hasler-hm").read_text()
    with pytest.raises(ValueError):
        boxconfig.write_overlay(paths, "hasler-hm", {"controls": {"wobble": {"x": 1}}})
    with pytest.raises(ValueError):
        boxconfig.write_overlay(paths, "hasler-hm", {"presets": []})
    assert paths.overlay_file("hasler-hm").read_text() == before


def test_effective_variant_applies_the_overlay(paths):
    plain = boxconfig.load_effective_variant(paths, "hasler-hm")
    assert plain.overlay_path is None
    boxconfig.write_overlay(paths, "hasler-hm", {
        "controls": {"volume": {"curve": [[0, 0], [100, 100]]}, "tone": {"end_switch": {"off_hold_ms": 3000}}},
        "io": {"lamp": {"balance": {"red": 0.9}}},
    })
    v = boxconfig.load_effective_variant(paths, "hasler-hm")
    assert v.overlay_path == paths.overlay_file("hasler-hm")
    specs = {c["id"]: c for c in v.controls}
    assert specs["volume"]["curve"] == [[0, 0], [100, 100]]
    assert specs["volume"]["channel"] == 1
    assert specs["tone"]["end_switch"] == {"id": "power", "pin": "d8", "off_hold_ms": 3000}
    assert v.io["lamp"]["balance"] == {"red": 0.9} and v.io["lamp"]["backend"] == "pwm-sysfs"
    assert v.actions == plain.actions


# -- main: choosing the config, and the live reload ---------------------------------


def _args(**kw):
    base = dict(variant=None, config=None, fake=True, etc=None, state=None)
    base.update(kw)
    return Namespace(**base)


def test_legacy_config_without_box_yaml(paths, tmp_path):
    legacy = tmp_path / "haslerd.yaml"
    legacy.write_text("moode: {card: sndrpihifiberry}\n")
    v, config = main.load_config(_args(variant=str(SHIPPED), config=str(legacy)), paths)
    assert v.path == SHIPPED and v.overlay_path is None
    assert config == {"moode": {"card": "sndrpihifiberry"}}


def test_legacy_config_still_gets_the_overlay(paths):
    boxconfig.write_overlay(paths, "hasler-hm", {"controls": {"volume": {"hysteresis_counts": 41}}})
    v, _ = main.load_config(_args(variant="hasler-hm"), paths)
    assert {c["id"]: c for c in v.controls}["volume"]["hysteresis_counts"] == 41


def test_box_yaml_names_the_variant_and_overrides_settings(paths, tmp_path):
    (paths.release / "config" / "variants" / "other.yaml").write_text(
        SHIPPED.read_text().replace("variant: hasler-hm", "variant: other"))
    boxconfig.write_box_file(paths, {"variant": "other", "channel": "bench", "moode": {"volume_limit": 50}})
    legacy = tmp_path / "haslerd.yaml"
    legacy.write_text("moode: {card: ignored}\n")
    v, config = main.load_config(_args(variant="hasler-hm", config=str(legacy), fake=False), paths)
    assert v.name == "other"
    assert config == {"moode": {"volume_limit": 50}, "state_file": str(paths.state / "state.json")}
    assert paths.secrets.is_dir() and stat.S_IMODE(paths.secrets.stat().st_mode) == 0o700


def test_box_paths_flags(monkeypatch, tmp_path):
    monkeypatch.delenv("HASLER_ETC", raising=False)
    p = main.box_paths(_args(etc=str(tmp_path / "e"), state=str(tmp_path / "s")))
    assert p.etc == tmp_path / "e" and p.state == tmp_path / "s"


def test_apply_overlay_changes_the_running_pot(paths):
    v = boxconfig.load_effective_variant(paths, "hasler-hm")
    panel = Panel.from_variant(v, fake=True)
    volume = next(c for c in panel.controls if c.id == "volume")
    power = next(c for c in panel.controls if c.id == "power")
    panel.analog.set(1, 13000)
    events = []
    for i in range(20):
        events += panel.poll(i * 0.02)
    before = volume.state()
    assert before is not None

    boxconfig.write_overlay(paths, "hasler-hm", {"controls": {
        "volume": {"curve": [[0, 0], [26000, 100]], "hysteresis_counts": 5},
        "tone": {"end_switch": {"off_hold_ms": 2000}},
    }})

    class FakeActions:
        variant = v

    effective = main.apply_overlay(panel, paths, "hasler-hm", actions=FakeActions)
    assert FakeActions.variant.overlay_path == paths.overlay_file("hasler-hm")
    assert {c["id"]: c for c in effective.controls}["volume"]["hysteresis_counts"] == 5
    assert volume.hysteresis == 5 and power.off_hold_s == 2.0
    assert next(c for c in panel.controls if c.id == "volume") is volume     # same object
    changes = [e for e in panel.poll(1.0) if e.control == "volume"]
    assert changes and changes[0].value == 50.0


def test_apply_overlay_with_a_bad_overlay_changes_nothing(paths):
    v = boxconfig.load_effective_variant(paths, "hasler-hm")
    panel = Panel.from_variant(v, fake=True)
    volume = next(c for c in panel.controls if c.id == "volume")
    curve = volume.curve
    paths.overlay_file("hasler-hm").parent.mkdir(parents=True)
    paths.overlay_file("hasler-hm").write_text("controls: {wobble: {x: 1}}\n")
    with pytest.raises(ValueError):
        main.apply_overlay(panel, paths, "hasler-hm")
    assert volume.curve == curve
