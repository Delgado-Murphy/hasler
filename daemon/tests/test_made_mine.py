"""What "Make it yours" (the Settings home's checklist) reads and writes:
/api/info ``installed_at``, settings.json ``made_mine`` through PUT /api/settings,
and POST /api/lamp/ok (the small OK once the list is done)."""

from __future__ import annotations

import json
import os
import time

import pytest

from test_web_confirm import call, rig  # noqa: F401  (fixture)


@pytest.fixture
def app(rig, tmp_path, monkeypatch):  # noqa: F811
    app = rig[0]
    app.settings_file = tmp_path / "settings.json"
    app.install_done_file = tmp_path / "done"
    monkeypatch.setattr(app.wifi, "current_ssid", lambda: "")
    return app


# -- /api/info installed_at -----------------------------------------------------------------


class TestInstalledAt:
    def test_the_install_mark_is_the_time(self, app):
        app.install_done_file.write_text("2026-11-01T10:20:30Z\n")
        assert app.info()["installed_at"] == "2026-11-01T10:20:30Z"

    def test_no_mark_means_the_radio_cannot_say(self, app):
        assert app.info()["installed_at"] is None

    def test_an_older_mark_without_a_time_falls_back_to_its_age(self, app):
        app.install_done_file.write_text("ok\n")
        then = time.time() - 3 * 86400
        os.utime(app.install_done_file, (then, then))
        got = app.info()["installed_at"]
        assert got == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(then))


# -- settings.json made_mine ----------------------------------------------------------------


class TestMadeMine:
    def test_starts_empty(self, app):
        assert app.settings()["made_mine"] == {"skipped": [], "hidden_at": None}

    def test_skipped_is_the_whole_list_and_persists(self, app, tmp_path):
        doc = app.put_settings({"made_mine": {"skipped": ["pair", "ytm", "pair"]}})
        assert doc["made_mine"] == {"skipped": ["pair", "ytm"], "hidden_at": None}
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert saved["made_mine"] == {"skipped": ["pair", "ytm"]}
        # The next PUT replaces the list (the page sends it whole), other keys leave it alone.
        app.put_settings({"night": "off"})
        assert app.settings()["made_mine"]["skipped"] == ["pair", "ytm"]
        assert app.put_settings({"made_mine": {"skipped": []}})["made_mine"]["skipped"] == []

    def test_hidden_at_is_kept_and_null_shows_the_list_again(self, app, tmp_path):
        doc = app.put_settings({"made_mine": {"hidden_at": "2026-11-07T18:00:00.000Z"}})
        assert doc["made_mine"] == {"skipped": [], "hidden_at": "2026-11-07T18:00:00.000Z"}
        app.put_settings({"made_mine": {"skipped": ["ha"]}})          # a skip keeps it hidden
        assert app.settings()["made_mine"]["hidden_at"] == "2026-11-07T18:00:00.000Z"
        assert app.put_settings({"made_mine": {"hidden_at": None}})["made_mine"]["hidden_at"] is None
        assert "hidden_at" not in json.loads((tmp_path / "settings.json").read_text())["made_mine"]
        assert app.settings()["made_mine"]["skipped"] == ["ha"]

    def test_without_a_settings_file_it_still_holds(self, rig, monkeypatch):  # noqa: F811
        app = rig[0]
        monkeypatch.setattr(app.wifi, "current_ssid", lambda: "")
        app.put_settings({"made_mine": {"skipped": ["place"], "hidden_at": "now"}})
        app.put_settings({"night": "off"})
        assert app.settings()["made_mine"] == {"skipped": ["place"], "hidden_at": "now"}

    def test_a_junk_block_in_the_file_reads_as_empty(self, app, tmp_path):
        (tmp_path / "settings.json").write_text(json.dumps({"made_mine": {"skipped": "pair", "hidden_at": 3}}))
        assert app.settings()["made_mine"] == {"skipped": [], "hidden_at": None}
        (tmp_path / "settings.json").write_text(json.dumps({"made_mine": [1, 2]}))
        assert app.settings()["made_mine"] == {"skipped": [], "hidden_at": None}

    @pytest.mark.parametrize("body", [
        {"made_mine": {}}, {"made_mine": {"other": 1}}, {"made_mine": "pair"},
        {"made_mine": {"skipped": "pair"}}, {"made_mine": {"skipped": [1]}}, {"made_mine": {"skipped": ["Pair a phone"]}},
        {"made_mine": {"skipped": ["x" * 33]}}, {"made_mine": {"skipped": ["r%d" % i for i in range(17)]}},
        {"made_mine": {"hidden_at": 3}}, {"made_mine": {"hidden_at": ""}}, {"made_mine": {"hidden_at": "t" * 41}},
    ])
    def test_refuses_what_is_not_the_block(self, app, body):
        with pytest.raises(ValueError):
            app.put_settings(body)
        assert app.settings()["made_mine"] == {"skipped": [], "hidden_at": None}


# -- POST /api/lamp/ok ----------------------------------------------------------------------


class TestLampOk:
    def test_plays_the_keepers_small_ok(self, app):
        played = []

        class Keeper:
            def celebrate(self):
                played.append("ok")
        app.lamp_keeper = Keeper()
        assert app.lamp_ok() == {"lamp": True}
        assert played == ["ok"]

    def test_without_a_keeper_it_says_so(self, app):
        app.lamp_keeper = None
        assert app.lamp_ok() == {"lamp": False}

    def test_over_http(self, app):
        from haslerd.web.server import WebServer
        played = []

        class Keeper:
            def celebrate(self):
                played.append("ok")
        app.lamp_keeper = Keeper()
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        try:
            hp = f"127.0.0.1:{server.port}"
            status, doc = call(hp, "POST", "/api/lamp/ok", {}, {"Origin": f"http://{hp}"})
            assert status == 200 and doc == {"ok": True, "lamp": True} and played == ["ok"]
            # A change like any other: a cross-site form can't send it.
            status, _ = call(hp, "POST", "/api/lamp/ok", None, {"Content-Type": "text/plain"}, raw=b"{}")
            assert status == 403 and played == ["ok"]
            status, doc = call(hp, "GET", "/api/info")
            assert status == 200 and doc["installed_at"] is None
        finally:
            server.close()
