"""Signing the radio in to Spotify from the key page (web/spotify_account_api.py)
and config.yml's credentials type (player/glr_config.py). go-librespot is a
fake: its restart, its state.json writes and its /auth/code."""

from __future__ import annotations

import json
import logging

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.player.glr_config import credentials_type, set_credentials_type
from haslerd.variant import Panel, load_variant
from haslerd.web import spotify_account_api as acct_mod
from haslerd.web.confirm import ConfirmError
from haslerd.web.server import Unavailable, WebApp, WebServer
from haslerd.web.sources_api import SourcesApi
from haslerd.web.spotify_account_api import SpotifyAccountApi, mask

from test_spotify_guests import REAL
from test_web_confirm import Clock, call

SECRET = "QVFEMGtleWJsb2JzZWNyZXQ="          # stands in for the stored login token
CODE = {"url": "https://www.spotify.com/pair?code=ABCD-EFGH", "code": "ABCD-EFGH",
        "expires_at": "2026-10-21T12:00:00Z"}
ORIGINAL = {"device_id": "d" * 40, "event_manager": None,
            "credentials": {"username": "jdoe12fe", "data": SECRET}, "last_volume": None}


class FakeLibrespot:
    """systemctl try-restart + go-librespot: what the restart did, and a code
    while it signs in with device_auth and no credentials."""

    def __init__(self, cfg, state):
        self.cfg, self.state = cfg, state
        self.restarts = 0
        self.fail = None
        self.code = CODE
        self.rewrite = 0          # restarts during which it writes its old state back

    def restart(self):
        self.restarts += 1
        if self.fail:
            raise RuntimeError(self.fail)
        if self.rewrite:
            self.rewrite -= 1
            self.state.write_text(json.dumps(ORIGINAL))

    def auth_code(self):
        doc = json.loads(self.state.read_text())
        signing_in = credentials_type(self.cfg) == "device_auth" and not doc["credentials"]["data"]
        return dict(self.code) if signing_in and self.code else None

    def approve(self, user="newuser99", data="TkVXU0VDUkVU"):
        doc = json.loads(self.state.read_text())
        doc["credentials"] = {"username": user, "data": data}
        self.state.write_text(json.dumps(doc))


class Timers:
    def __init__(self):
        self.armed = []

    def __call__(self, seconds, fn):
        t = type("T", (), {"cancelled": False, "seconds": seconds, "fn": staticmethod(fn)})()
        t.cancel = lambda: setattr(t, "cancelled", True)
        self.armed.append(t)
        return t


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    app = WebApp(Actions(variant, FakePlayer()), panel, tmp_path / "keys.yaml", {"radio": True},
                 ha=False, settings_file=tmp_path / "settings.json")
    app.confirm.clock = Clock()
    glr = tmp_path / "go-librespot"
    glr.mkdir(mode=0o700)
    cfg, state = glr / "config.yml", glr / "state.json"
    cfg.write_text(REAL)
    state.write_text(json.dumps(ORIGINAL))
    state.chmod(0o600)
    fake = FakeLibrespot(cfg, state)
    now = [1000.0]
    timers = Timers()
    api = SpotifyAccountApi(app, config=cfg, restart=fake.restart, auth_code=fake.auth_code,
                            clock=lambda: now[0], timer=timers)
    app.spotify_account = api
    app.extra_routes.append(api)
    return app, api, fake, now, timers, panel, glr


def press(app, panel, do):
    token = app.confirm.request("spotify", {"do": do})["token"]
    key = next(c.id for c in panel.controls if c.kind in ("momentary", "interlocked_bank"))
    app.confirm.intercept([Event(key, "tap")])
    return token


def creds(glr, name="state.json"):
    return json.loads((glr / name).read_text())["credentials"]


# -- the pieces ------------------------------------------------------------------


def test_mask_is_go_librespots():
    assert mask("jdoe12fe") == "jd****fe"
    assert mask("jd123456fe") == "jd******fe"
    assert mask("abcd") == "abcd"
    assert mask("jdoe12fe@example.com") == "jd****fe@example.com"


class TestCredentialsType:
    def test_read_and_switch_keeps_everything_else(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text(REAL)
        p.chmod(0o600)
        assert credentials_type(p) == "interactive"
        assert set_credentials_type(p, "device_auth") is True
        assert credentials_type(p) == "device_auth"
        assert p.read_text() == REAL.replace("  type: interactive", "  type: device_auth")
        assert p.stat().st_mode & 0o777 == 0o600
        assert set_credentials_type(p, "device_auth") is False

    def test_missing_block_or_type(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text("device_name: Hasler\n")
        assert credentials_type(p) == "zeroconf"           # go-librespot's default
        set_credentials_type(p, "device_auth")
        assert p.read_text() == "device_name: Hasler\ncredentials:\n  type: device_auth\n"
        p.write_text("credentials:\n    zeroconf:\n      persist_credentials: false\nbitrate: 320\n")
        assert credentials_type(p) == "zeroconf"
        set_credentials_type(p, "device_auth")
        assert p.read_text() == ("credentials:\n    type: device_auth\n    zeroconf:\n"
                                 "      persist_credentials: false\nbitrate: 320\n")
        assert credentials_type(tmp_path / "nope.yml") is None
        with pytest.raises(ValueError):
            set_credentials_type(p, "password")

    def test_nested_type_elsewhere_is_not_it(self, tmp_path):
        p = tmp_path / "config.yml"
        p.write_text("audio:\n  type: alsa\ncredentials:\n  type: interactive # was\n")
        assert credentials_type(p) == "interactive"


# -- the account -------------------------------------------------------------------


def test_signed_in_shows_the_masked_user_and_no_secret(rig):
    _, api, fake, *_ = rig
    doc = api.doc()
    assert doc["available"] and doc["signed_in"] and doc["flow_state"] == "signed_in"
    assert doc["user"] == "jd****fe"
    assert SECRET not in json.dumps(doc) and "jdoe12fe" not in json.dumps(doc)
    assert fake.restarts == 0


def test_not_set_up(rig, tmp_path):
    app, *_ = rig
    api = SpotifyAccountApi(app, config=tmp_path / "missing" / "config.yml")
    assert api.doc()["available"] is False
    with pytest.raises(Unavailable):
        api.start({"confirm": "x"})


def test_start_needs_a_press(rig):
    app, api, fake, *_ = rig
    with pytest.raises(ConfirmError):
        api.start({})
    other = app.confirm.request("spotify", {"do": "sign_out"})["token"]
    with pytest.raises(ConfirmError):
        api.start({"confirm": other})
    assert fake.restarts == 0 and creds(rig[6])["data"] == SECRET


def test_change_account_end_to_end(rig, caplog):
    app, api, fake, now, timers, panel, glr = rig
    caplog.set_level(logging.DEBUG)
    doc = api.start({"confirm": press(app, panel, "sign_in")})
    assert fake.restarts == 1
    assert credentials_type(glr / "config.yml") == "device_auth"
    assert creds(glr) == {"username": "", "data": None}
    assert creds(glr, acct_mod.BEFORE_SIGN_IN)["data"] == SECRET
    assert (glr / acct_mod.BEFORE_SIGN_IN).stat().st_mode & 0o777 == 0o600
    assert json.loads((glr / "state.json").read_text())["device_id"] == "d" * 40
    assert doc["flow_state"] == "waiting" and doc["code"] == "ABCD-EFGH" and doc["url"] == CODE["url"]
    assert doc["previous"] == "jd****fe" and doc["signed_in"] is False
    assert doc["sign_in_until"] == 1000 + acct_mod.SIGN_IN_MAX_S
    assert timers.armed[-1].seconds == acct_mod.SIGN_IN_MAX_S

    fake.approve()
    doc = api.doc()
    assert doc["flow_state"] == "signed_in" and doc["user"] == "ne*****99" and doc["code"] is None
    assert timers.armed[-1].cancelled
    text = caplog.text
    assert "ABCD-EFGH" not in text and SECRET not in text and "jdoe12fe" not in text
    assert "jd****fe" in text and "ne*****99" in text


def test_starting_then_failed_without_a_code(rig):
    app, api, fake, now, _, panel, _ = rig
    fake.code = None
    assert api.start({"confirm": press(app, panel, "sign_in")})["flow_state"] == "starting"
    now[0] += acct_mod.NO_CODE_S + 1
    doc = api.doc()
    assert doc["flow_state"] == "failed" and "online" in doc["error"]


def test_cancel_puts_the_old_account_back_without_a_press(rig):
    app, api, fake, _, _, panel, glr = rig
    api.start({"confirm": press(app, panel, "sign_in")})
    doc = api.cancel()
    assert doc["flow_state"] == "signed_in" and doc["user"] == "jd****fe"
    assert creds(glr)["data"] == SECRET and fake.restarts == 2
    with pytest.raises(ValueError):
        api.cancel()                                   # nothing to cancel now


def test_cancel_when_there_was_no_account_is_signed_out(rig):
    app, api, fake, _, _, panel, glr = rig
    api.sign_out({"confirm": press(app, panel, "sign_out")})
    api.start({"confirm": press(app, panel, "sign_in")})
    assert api.doc()["previous"] is None
    doc = api.cancel()
    assert doc["flow_state"] == "signed_out" and credentials_type(glr / "config.yml") == "zeroconf"
    # the sign-out backup still has the old account, for a person with ssh
    assert creds(glr, acct_mod.BEFORE_SIGN_OUT)["data"] == SECRET


def test_second_start_keeps_the_first_backup(rig):
    app, api, fake, _, _, panel, glr = rig
    api.start({"confirm": press(app, panel, "sign_in")})
    api.start({"confirm": press(app, panel, "sign_in")})
    assert creds(glr, acct_mod.BEFORE_SIGN_IN)["data"] == SECRET
    assert api.cancel()["user"] == "jd****fe"


def test_nobody_approves_in_time(rig):
    app, api, fake, now, timers, panel, glr = rig
    api.start({"confirm": press(app, panel, "sign_in")})
    now[0] += acct_mod.SIGN_IN_MAX_S
    timers.armed[-1].fn()                               # the timer fires
    assert creds(glr)["data"] == SECRET
    assert api.doc()["flow_state"] == "signed_in"
    # no previous account: signed out, with the reason
    api.sign_out({"confirm": press(app, panel, "sign_out")})
    api.start({"confirm": press(app, panel, "sign_in")})
    now[0] += acct_mod.SIGN_IN_MAX_S
    doc = api.doc()                                     # a poll past the deadline does it too
    assert doc["flow_state"] == "failed" and "in time" in doc["error"]
    assert credentials_type(glr / "config.yml") == "zeroconf"


def test_haslerd_restarted_mid_sign_in_gets_a_deadline(rig):
    app, api, fake, now, timers, panel, glr = rig
    api.start({"confirm": press(app, panel, "sign_in")})
    fresh = SpotifyAccountApi(app, config=glr / "config.yml", restart=fake.restart,
                              auth_code=fake.auth_code, clock=lambda: now[0], timer=timers)
    n = len(timers.armed)
    fresh.resume()
    assert len(timers.armed) == n + 1 and fresh.doc()["flow_state"] == "waiting"


def test_sign_out(rig, caplog):
    app, api, fake, _, _, panel, glr = rig
    caplog.set_level(logging.INFO)
    with pytest.raises(ConfirmError):
        api.sign_out({})
    doc = api.sign_out({"confirm": press(app, panel, "sign_out")})
    assert doc["flow_state"] == "signed_out" and doc["user"] is None and doc["code"] is None
    assert creds(glr) == {"username": "", "data": None}
    assert credentials_type(glr / "config.yml") == "zeroconf"
    assert creds(glr, acct_mod.BEFORE_SIGN_OUT)["data"] == SECRET
    assert fake.restarts == 1 and SECRET not in caplog.text


def test_legacy_credentials_json(rig):
    app, api, fake, _, _, panel, glr = rig
    (glr / "state.json").write_text(json.dumps({**ORIGINAL, "credentials": {"username": "", "data": None}}))
    (glr / "credentials.json").write_text(json.dumps({"username": "olduser77", "data": SECRET}))
    assert api.doc()["user"] == "ol*****77"
    api.start({"confirm": press(app, panel, "sign_in")})
    assert not (glr / "credentials.json").exists() and (glr / "credentials.json.old").exists()
    assert creds(glr, acct_mod.BEFORE_SIGN_IN)["username"] == "olduser77"
    assert api.cancel()["user"] == "ol*****77"


def test_a_rewrite_during_the_restart_is_written_again(rig):
    app, api, fake, _, _, panel, glr = rig
    fake.rewrite = 1
    api.sign_out({"confirm": press(app, panel, "sign_out")})
    assert fake.restarts == 2 and creds(glr)["data"] is None
    fake.rewrite = 5                                       # never sticks: says so
    with pytest.raises(Unavailable, match="kept its old account"):
        api.start({"confirm": press(app, panel, "sign_in")})


def test_restart_failure_is_503(rig):
    app, api, fake, _, _, panel, _ = rig
    fake.fail = "Interactive authentication required."
    with pytest.raises(Unavailable, match="didn't restart"):
        api.start({"confirm": press(app, panel, "sign_in")})


def test_rides_along_in_casting(rig):
    app, api, *_ = rig
    target = next(t for t in SourcesApi(app, spotify_name=lambda: "Hasler")._targets() if t["id"] == "spotify")
    assert target["account"]["user"] == "jd****fe"


def test_over_http(rig):
    app, api, fake, _, _, panel, glr = rig
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    try:
        hp = f"127.0.0.1:{server.port}"
        status, doc = call(hp, "GET", "/api/spotify/account")
        assert status == 200 and doc["user"] == "jd****fe" and SECRET not in json.dumps(doc)
        status, doc = call(hp, "POST", "/api/spotify/account/start", {})
        assert status == 403 and doc["confirm_required"] is True
        status, doc = call(hp, "POST", "/api/spotify/account/start", {"confirm": press(app, panel, "sign_in")})
        assert status == 200 and doc["ok"] and doc["code"] == "ABCD-EFGH"
        status, doc = call(hp, "POST", "/api/spotify/account/cancel", {})
        assert status == 200 and doc["user"] == "jd****fe"
        status, doc = call(hp, "POST", "/api/spotify/account/cancel", {})
        assert status == 400
        status, doc = call(hp, "DELETE", "/api/spotify/account", {"confirm": press(app, panel, "sign_out")})
        assert status == 200 and doc["flow_state"] == "signed_out"
        assert call(hp, "PUT", "/api/spotify/account", {})[0] == 405
    finally:
        server.close()
