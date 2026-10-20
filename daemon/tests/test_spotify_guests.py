"""Spotify for guests (player/glr_config.py): settings.json ``spotify_guests``
keeps go-librespot's ``zeroconf_enabled`` in step - on radios that already
exist, whose config.yml no release rewrites. Temp files, a fake restart."""

from __future__ import annotations

import json
import logging

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.player.glr_config import SpotifyGuests, guests_from, set_zeroconf, zeroconf_enabled
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, load_settings

# The shape of the real radio's /var/lib/go-librespot/config.yml (2026-10-20).
REAL = """# go-librespot - Spotify Connect device for haslerd to drive over its local API.
#
# zeroconf_enabled: false   <- a comment, not the key
device_name: Hasler
device_type: speaker
log_level: info
audio_backend: alsa
zeroconf_enabled: true   # owner: guests on the Wi-Fi see "Hasler" in Spotify too
credentials:
  type: interactive
  interactive:
    callback_port: 36842
server:
  enabled: true
  address: 127.0.0.1
"""


class Restarts:
    def __init__(self, fail=None):
        self.n, self.fail = 0, fail

    def __call__(self):
        self.n += 1
        if self.fail:
            raise RuntimeError(self.fail)


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(REAL)
    p.chmod(0o600)
    return p


class TestFile:
    def test_already_on_is_untouched(self, cfg):
        before = cfg.stat().st_mtime_ns
        assert set_zeroconf(cfg, True) is False
        assert cfg.read_text() == REAL and cfg.stat().st_mtime_ns == before

    def test_off_changes_one_line_and_keeps_the_rest(self, cfg):
        assert set_zeroconf(cfg, False) is True
        old, new = REAL.splitlines(), cfg.read_text().splitlines()
        assert [i for i, (a, b) in enumerate(zip(old, new)) if a != b] == [old.index(
            'zeroconf_enabled: true   # owner: guests on the Wi-Fi see "Hasler" in Spotify too')]
        assert "zeroconf_enabled: false" in new and len(new) == len(old)
        assert "# zeroconf_enabled: false   <- a comment, not the key" in new
        assert cfg.stat().st_mode & 0o777 == 0o600
        assert zeroconf_enabled(cfg) is False
        assert set_zeroconf(cfg, False) is False

    def test_missing_key_is_appended(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text("device_name: Hasler\nserver:\n  zeroconf_enabled: nested\n")
        assert zeroconf_enabled(p) is False                 # go-librespot's default
        assert set_zeroconf(p, True) is True
        assert p.read_text().splitlines()[-1] == "zeroconf_enabled: true"
        assert "  zeroconf_enabled: nested" in p.read_text()
        assert zeroconf_enabled(p) is True

    @pytest.mark.parametrize("raw,on", [("yes", True), ("True", True), ("'true'", True),
                                        ("no", False), ("FALSE", False)])
    def test_yaml_spellings_count_as_the_same(self, tmp_path, raw, on):
        p = tmp_path / "config.yml"
        p.write_text(f"zeroconf_enabled: {raw}\n")
        assert set_zeroconf(p, on) is False
        assert set_zeroconf(p, not on) is True

    def test_missing_file(self, tmp_path):
        assert zeroconf_enabled(tmp_path / "nope.yml") is None
        with pytest.raises(OSError):
            set_zeroconf(tmp_path / "nope.yml", True)


class TestApply:
    def test_restart_only_when_the_file_changed(self, cfg):
        r = Restarts()
        g = SpotifyGuests(cfg, restart=r)
        assert g.apply(True) == "same" and r.n == 0
        assert g.apply(False) == "changed" and r.n == 1
        assert g.apply(False) == "same" and r.n == 1
        assert g.apply(True) == "changed" and r.n == 2

    def test_missing_file_skips_and_logs_once(self, tmp_path, caplog):
        r = Restarts()
        g = SpotifyGuests(tmp_path / "config.yml", restart=r)
        with caplog.at_level(logging.INFO, logger="haslerd.player.glr_config"):
            assert g.apply(True) == "missing"
            assert g.apply(False) == "missing"
        assert r.n == 0
        assert len([m for m in caplog.messages if "not applied" in m]) == 1
        assert not (tmp_path / "config.yml").exists()

    def test_failed_restart_is_reported_not_raised(self, cfg):
        g = SpotifyGuests(cfg, restart=Restarts(fail="sudo: a password is required"))
        assert g.apply(False) == "failed"
        assert zeroconf_enabled(cfg) is False                # the file is still right

    def test_leaves_state_json_alone(self, cfg):
        state = cfg.parent / "state.json"
        state.write_text('{"credentials": "x"}')
        SpotifyGuests(cfg, restart=Restarts()).apply(False)
        assert state.read_text() == '{"credentials": "x"}'

    @pytest.mark.parametrize("doc,want", [({}, True), ({"spotify_guests": False}, False),
                                          ({"spotify_guests": "no"}, True)])
    def test_setting_default_on(self, doc, want):
        assert guests_from(doc) is want


@pytest.fixture
def app(tmp_path, cfg):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, Panel.from_variant(variant, fake=True), tmp_path / "keys.yaml",
                 {"radio": True}, ha=False, settings_file=tmp_path / "settings.json")
    app.spotify_guests = SpotifyGuests(cfg, restart=Restarts())
    return app


class TestSettings:
    def test_default_on_and_start_apply(self, app, cfg):
        assert app.settings()["spotify_guests"] is True
        cfg.write_text(REAL.replace("zeroconf_enabled: true", "zeroconf_enabled: false"))
        app.apply_spotify_guests().join(5)
        assert zeroconf_enabled(cfg) is True and app.spotify_guests.restart.n == 1

    def test_put_saves_and_applies(self, app, cfg):
        doc = app.put_settings({"spotify_guests": False})
        assert doc["spotify_guests"] is False
        assert load_settings(app.settings_file)["spotify_guests"] is False
        app.apply_spotify_guests().join(5)       # same lock: waits for put's own apply
        assert zeroconf_enabled(cfg) is False and app.spotify_guests.restart.n == 1
        json.dumps(app.settings())

    def test_put_rejects_a_non_bool(self, app):
        with pytest.raises(ValueError, match="spotify_guests"):
            app.put_settings({"spotify_guests": "yes"})
        assert "spotify_guests" not in load_settings(app.settings_file)
