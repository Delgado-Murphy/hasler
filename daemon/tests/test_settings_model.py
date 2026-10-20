"""Where the page's settings live and what happens to bad or old files:
settings.json (schema-stamped, durable), keys.yaml (never fatal), and the
read-only hints GET /api/settings gives the page."""

import json
import os

import pytest
from conftest import VARIANTS

from haslerd import boxconfig
from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.server import Unavailable, WebApp, load_settings, save_settings


@pytest.fixture
def app(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    return WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                  settings_file=tmp_path / "settings.json")


# -- settings.json --------------------------------------------------------------


def test_first_save_stamps_the_schema(app, tmp_path):
    app.put_settings({"night": "off"})
    doc = json.loads((tmp_path / "settings.json").read_text())
    assert doc == {"schema": boxconfig.SCHEMA, "night": "off"}
    assert oct((tmp_path / "settings.json").stat().st_mode & 0o777) == "0o644"


def test_save_keeps_what_it_does_not_know_and_a_newer_schema(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"schema": 7, "from_the_future": [1, 2]}))
    save_settings(path, {**load_settings(path), "night": "sun"})
    assert json.loads(path.read_text()) == {"schema": 7, "from_the_future": [1, 2], "night": "sun"}


def test_an_unwritable_state_dir_is_a_503_not_a_500(app, tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(boxconfig, "atomic_write_text", refuse)
    with pytest.raises(Unavailable, match="can't save settings"):
        app.put_settings({"night": "off"})


@pytest.mark.parametrize("text", ["", "not json", "[1, 2]", "null"])
def test_a_broken_settings_file_reads_as_defaults(app, tmp_path, text):
    (tmp_path / "settings.json").write_text(text)
    doc = app.settings()
    assert doc["location"]["name"] == "Madrid" and doc["night"] == "sun"


def test_get_settings_tells_the_page_the_range_and_floor(app):
    doc = app.settings()
    assert doc["volume_limit_range"] == [30, 100]
    assert "volume_floor" in doc                    # None with the fake player


def test_no_secret_in_settings(app, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "ha.json").write_text(json.dumps({"url": "http://ha:8123", "token": "SEKRIT"}))
    assert "SEKRIT" not in json.dumps(app.settings())


# -- keys.yaml -------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "- just\n- a list\n",
    "presets: {n: 1}\nactions: {}\n",
    "presets: []\nactions: [k1.tap]\n",
    "{{{ not yaml",
])
def test_a_bad_keys_file_falls_back_to_the_descriptor(app, tmp_path, text):
    (tmp_path / "keys.yaml").write_text(text)
    before = dict(app.actions.variant.actions)
    app.load_saved()                                # must not raise
    assert app.actions.variant.actions == before


def test_keys_save_is_private_and_schema_stamped(tmp_path):
    path = tmp_path / "keys.yaml"
    keymap.save(path, [{"n": 1, "name": "x", "radio": "http://a/b"}], {"k1.tap": {"do": "preset", "n": 1}})
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert path.read_text().startswith("# Written by the haslerd web page")
    assert keymap.load(path) == ([{"n": 1, "name": "x", "radio": "http://a/b"}],
                                 {"k1.tap": {"do": "preset", "n": 1}})
    assert not [p for p in os.listdir(tmp_path) if p.startswith(".")]   # no temp left behind
