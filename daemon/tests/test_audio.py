"""Mono output: the switch around the root helper, and its place in /api/settings."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import VARIANTS

from haslerd import main
from haslerd.actions import Actions
from haslerd.audio import MonoError, MonoSwitch
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, load_settings

REPO = Path(__file__).resolve().parents[2]


class Runner:
    """Stands in for subprocess.run: the helper (touching a fake override) and mpc."""

    def __init__(self, override, playing=False, answer=None):
        self.override = override
        self.playing = playing
        self.answer = answer
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "sudo":
            if self.answer is not None:
                return SimpleNamespace(stdout=self.answer, stderr="", returncode=1)
            if argv[-1] == "on":
                self.override.write_text("pcm.!_audioout {}")
            else:
                self.override.unlink(missing_ok=True)
            return SimpleNamespace(stdout=json.dumps({"ok": True, "mono": self.override.exists()}),
                                   stderr="", returncode=0)
        if argv[:2] == ["mpc", "status"]:
            return SimpleNamespace(stdout="x\n[playing] #1/3\n" if self.playing else "x\n", stderr="")
        return SimpleNamespace(stdout="", stderr="")


@pytest.fixture
def helper(tmp_path):
    path = tmp_path / "hasler-audio-mono"
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def switch(tmp_path, helper, **kw):
    run = Runner(tmp_path / "zz-hasler-mono.conf", **kw)
    return MonoSwitch(helper=helper, override=run.override, run=run), run


def test_on_and_off_follow_the_override_file(tmp_path, helper):
    mono, run = switch(tmp_path, helper)
    assert mono.available and not mono.is_on()
    assert mono.set(True) is True and mono.is_on()
    assert mono.set(False) is False and not mono.is_on()
    assert ["sudo", "-n", helper, "on"] in run.calls


def test_playing_mpd_is_restarted_so_it_reopens_the_device(tmp_path, helper):
    mono, run = switch(tmp_path, helper, playing=True)
    mono.set(True)
    assert run.calls[-2:] == [["mpc", "-q", "stop"], ["mpc", "-q", "play"]]


def test_idle_mpd_is_left_alone(tmp_path, helper):
    mono, run = switch(tmp_path, helper)
    mono.set(True)
    assert ["mpc", "-q", "play"] not in run.calls


def test_helper_failure_is_a_readable_error(tmp_path, helper):
    mono, _ = switch(tmp_path, helper, answer='{"ok": false, "error": "must run as root"}')
    with pytest.raises(MonoError, match="must run as root"):
        mono.set(True)


def test_missing_helper_is_unavailable(tmp_path):
    mono = MonoSwitch(helper=str(tmp_path / "nope"), override=tmp_path / "x")
    assert not mono.available
    with pytest.raises(MonoError, match="install.sh"):
        mono.set(True)


# -- /api/settings -----------------------------------------------------------

@pytest.fixture
def app(tmp_path, helper):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    player = FakePlayer()
    player.config = SimpleNamespace(volume_limit=70)
    mono, _ = switch(tmp_path, helper)
    return WebApp(Actions(variant, player), Panel.from_variant(variant, fake=True),
                  tmp_path / "keys.yaml", {"radio": True}, ha=False,
                  settings_file=tmp_path / "settings.json", mono=mono)


def test_settings_report_mono(app):
    assert app.settings()["mono"] == {"available": True, "on": False}


def test_put_mono_applies_and_persists(app, tmp_path):
    doc = app.put_settings({"mono": True})
    assert doc["mono"]["on"] is True
    assert load_settings(tmp_path / "settings.json") == {"schema": 1, "mono": True}
    assert doc["volume_limit"] == 70                 # untouched


def test_put_both_at_once(app, tmp_path):
    app.put_settings({"mono": False, "volume_limit": 60})
    assert load_settings(tmp_path / "settings.json") == {"schema": 1, "mono": False, "volume_limit": 60}


@pytest.mark.parametrize("body", [{"mono": "yes"}, {"mono": 1}, {}, {"other": True}])
def test_bad_bodies_are_refused(app, body):
    with pytest.raises(ValueError):
        app.put_settings(body)


def test_without_the_helper_mono_is_unavailable(app):
    app.mono = None
    assert app.settings()["mono"] == {"available": False, "on": False}
    with pytest.raises(ValueError, match="install.sh"):
        app.put_settings({"mono": True})


# -- startup -----------------------------------------------------------------

def test_startup_reapplies_the_saved_setting_default_on(tmp_path, helper, monkeypatch):
    seen = []
    monkeypatch.setattr(main.MonoSwitch, "available", property(lambda self: True))
    monkeypatch.setattr(main.MonoSwitch, "set", lambda self, on, reopen=True: seen.append((on, reopen)))
    monkeypatch.setattr(main.threading, "Thread",
                        lambda target, **kw: SimpleNamespace(start=target))
    assert main.build_mono(tmp_path) is not None
    (tmp_path / "settings.json").write_text('{"mono": false}')
    main.build_mono(tmp_path)
    assert seen == [(True, False), (False, False)]


# -- the helper script itself ---------------------------------------------------

def test_helper_script_parses_and_refuses_non_root():
    script = REPO / "scripts" / "hasler-audio-mono"
    assert os.access(script, os.X_OK)
    subprocess.run(["sh", "-n", str(script)], check=True)
    if os.geteuid() != 0:
        out = subprocess.run([str(script), "status"], capture_output=True, text=True)
        assert json.loads(out.stdout) == {"ok": False, "error": "must run as root"}
