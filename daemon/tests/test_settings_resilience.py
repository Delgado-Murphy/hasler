"""Settings -> Sound's two groups through PUT /api/settings: ``after_power_cut``
(bootresume.py) and ``when_music_fails`` / ``fallback_preset`` (player/watchdog.py)."""

from __future__ import annotations

import json

import pytest

from test_web_confirm import base, call, rig  # noqa: F401  (fixtures)


@pytest.fixture
def app(rig, tmp_path, monkeypatch):  # noqa: F811
    app = rig[0]
    app.settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(app.wifi, "current_ssid", lambda: "")
    return app


def saved(tmp_path) -> dict:
    return json.loads((tmp_path / "settings.json").read_text())


def radio_and_other(app) -> tuple[int, int | None]:
    presets = app.actions.presets
    radio = min(n for n, p in presets.items() if p.source == "radio")
    other = next((n for n, p in sorted(presets.items()) if p.source != "radio"), None)
    return radio, other


def test_settings_default_after_power_cut(app):
    doc = app.settings()
    assert doc["after_power_cut"] == "night_only"            # today's behaviour, key absent
    assert doc["when_music_fails"] == "radio" and doc["fallback_preset"] is None


def test_put_after_power_cut_validates(app, tmp_path):
    for mode in ("resume", "standby", "night_only"):
        assert app.put_settings({"after_power_cut": mode})["after_power_cut"] == mode
        assert saved(tmp_path)["after_power_cut"] == mode
    for bad in ("always", "", None, 1, True):
        with pytest.raises(ValueError, match="after_power_cut"):
            app.put_settings({"after_power_cut": bad})
    assert app.settings()["after_power_cut"] == "night_only"


def test_unknown_values_in_the_file_read_as_the_default(app, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps(
        {"after_power_cut": "sometimes", "when_music_fails": 3, "fallback_preset": "2"}))
    assert app.resilience_settings() == {"after_power_cut": "night_only", "when_music_fails": "radio",
                                         "fallback_preset": None}


def test_put_when_music_fails(app, tmp_path):
    for mode in ("standby", "keep_trying", "radio"):
        assert app.put_settings({"when_music_fails": mode})["when_music_fails"] == mode
    with pytest.raises(ValueError, match="when_music_fails"):
        app.put_settings({"when_music_fails": "spotify"})
    radio, other = radio_and_other(app)
    doc = app.put_settings({"fallback_preset": radio})
    assert doc["fallback_preset"] == radio and saved(tmp_path)["fallback_preset"] == radio
    for bad in (99, "1", True, 1.5) + ((other,) if other is not None else ()):
        with pytest.raises(ValueError, match="fallback_preset"):
            app.put_settings({"fallback_preset": bad})
    doc = app.put_settings({"fallback_preset": None})        # back to automatic
    assert doc["fallback_preset"] is None and "fallback_preset" not in saved(tmp_path)
    assert saved(tmp_path)["when_music_fails"] == "radio"


def test_without_a_settings_file_they_live_in_memory(rig):  # noqa: F811
    app = rig[0]
    app.settings_file = None
    app.wifi.current_ssid = lambda: ""
    app.put_settings({"after_power_cut": "standby", "when_music_fails": "keep_trying"})
    assert app.resilience_settings()["after_power_cut"] == "standby"
    assert app.resilience_settings()["when_music_fails"] == "keep_trying"


def test_put_over_http(base, tmp_path):  # noqa: F811
    hostport, app = base
    app.settings_file = tmp_path / "settings.json"
    app.wifi.current_ssid = lambda: ""
    status, doc = call(hostport, "PUT", "/api/settings", {"after_power_cut": "standby"},
                       headers={"Origin": f"http://{hostport}"})
    assert status == 200 and doc["after_power_cut"] == "standby"
    status, doc = call(hostport, "PUT", "/api/settings", {"after_power_cut": "never"},
                       headers={"Origin": f"http://{hostport}"})
    assert status == 400
