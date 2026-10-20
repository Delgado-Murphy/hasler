"""The key page's guard (CSRF / DNS rebinding), the press-on-the-radio
confirmation and the "About this radio" software endpoints."""

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection

import pytest
import yaml
from conftest import VARIANTS

from haslerd import boxconfig
from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.panel.io_base import AbsentDigital
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import confirm as confirm_mod
from haslerd.web.confirm import ConfirmBroker, ConfirmError, ConfirmUnavailable
from haslerd.web.server import WebApp, WebServer
from haslerd.web.software import Software


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    actions = Actions(variant, player)
    replies = []
    actions.on_reply = replies.append
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True}, ha=False)
    app.confirm.clock = Clock()
    return app, actions, player, panel, replies


@pytest.fixture
def base(rig):
    app = rig[0]
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"127.0.0.1:{server.port}", app
    server.close()


def call(hostport, method, path, body=None, headers=None, raw=None):
    """A request with full control over the headers (urllib would add its own)."""
    host, port = hostport.split(":")
    conn = HTTPConnection(host, int(port), timeout=5)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    hdrs = {"Host": hostport, "Content-Type": "application/json"}
    hdrs.update(headers or {})
    hdrs = {k: v for k, v in hdrs.items() if v is not None}
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in hdrs.items():
        conn.putheader(k, v)
    conn.putheader("Content-Length", str(len(data)))
    conn.endheaders(data)
    resp = conn.getresponse()
    text = resp.read()
    conn.close()
    try:
        doc = json.loads(text)
    except ValueError:
        doc = text
    return resp.status, doc


# -- the guard -----------------------------------------------------------------


class TestGuard:
    def test_same_origin_json_is_allowed(self, base):
        hp, _ = base
        status, doc = call(hp, "POST", "/api/stop", {}, {"Origin": f"http://{hp}", "Referer": f"http://{hp}/#settings"})
        assert status == 200 and doc["ok"] is True

    def test_no_origin_is_fine_for_a_plain_client(self, base):
        hp, _ = base
        assert call(hp, "POST", "/api/stop", {})[0] == 200

    @pytest.mark.parametrize("ctype", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"])
    def test_wrong_content_type_is_403(self, base, ctype):
        hp, _ = base
        status, doc = call(hp, "POST", "/api/stop", {}, {"Content-Type": ctype})
        assert status == 403 and "application/json" in doc["error"]

    def test_charset_suffix_is_still_json(self, base):
        hp, _ = base
        assert call(hp, "POST", "/api/stop", {}, {"Content-Type": "application/json; charset=utf-8"})[0] == 200

    @pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "192.0.2.66", "hasler.local.evil.example", ""])
    def test_foreign_host_is_403_dns_rebinding(self, base, host):
        hp, _ = base
        status, doc = call(hp, "PUT", "/api/settings", {"volume_limit": 60}, {"Host": host})
        assert status == 403 and "address" in doc["error"]

    @pytest.mark.parametrize("host", ["hasler.local:8080", "localhost:8080", "172.24.1.1:8080", "HASLER.LOCAL"])
    def test_own_names_are_allowed(self, base, host):
        hp, _ = base
        assert call(hp, "POST", "/api/stop", {}, {"Host": host})[0] == 200

    def test_the_hostname_and_extra_names_are_allowed(self, base):
        hp, app = base
        name = socket.gethostname().split(".")[0].lower()
        assert call(hp, "POST", "/api/stop", {}, {"Host": f"{name}.local:8080"})[0] == 200
        assert call(hp, "POST", "/api/stop", {}, {"Host": "radio.home"})[0] == 403
        app.allowed_hosts.add("radio.home")
        assert call(hp, "POST", "/api/stop", {}, {"Host": "radio.home"})[0] == 200

    @pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://hasler.local:8080", "https://127.0.0.1"])
    def test_cross_origin_is_403(self, base, origin):
        hp, _ = base
        status, doc = call(hp, "POST", "/api/stop", {}, {"Origin": origin})
        assert status == 403 and "cross-site" in doc["error"]

    def test_cross_site_referer_without_origin_is_403(self, base):
        hp, _ = base
        assert call(hp, "POST", "/api/next", {}, {"Referer": "http://evil.example/page"})[0] == 403
        assert call(hp, "POST", "/api/next", {}, {"Referer": f"http://{hp}/"})[0] == 200

    def test_every_method_and_the_ha_routes_are_guarded(self, base):
        hp, _ = base
        for method, path in [("PUT", "/api/config"), ("DELETE", "/api/ytm/account"), ("PATCH", "/api/ha/x"),
                             ("POST", "/api/ha/connect"), ("POST", "/api/reset"), ("POST", "/api/wifi")]:
            assert call(hp, method, path, {}, {"Origin": "http://evil.example"})[0] == 403, (method, path)

    def test_gets_are_not_guarded(self, base):
        hp, _ = base
        assert call(hp, "GET", "/api/status", None, {"Content-Type": None, "Host": "whatever"})[0] == 200

    def test_the_page_itself_sends_json_for_every_change(self):
        from haslerd.web.server import STATIC
        page = (STATIC / "index.html").read_text()
        assert "const send = method !== 'GET'" in page
        assert "'Content-Type':'application/json'" in page


# -- the confirmation broker ---------------------------------------------------------


class TestConfirm:
    def test_a_key_press_within_30s_confirms_and_is_swallowed(self, rig):
        app, actions, player, panel, replies = rig
        c = app.confirm
        req = c.request("update", {"version": "1.2.0"})
        assert req["state"] == "pending" and req["expires_in"] == 30
        assert replies == ["ha"]                       # the lamp asks: two dips
        c.clock.t += 12
        left = c.intercept([Event("volume", "change", 40), Event("k1", "tap")])
        assert left == [Event("volume", "change", 40)]  # knob passes, the key is eaten
        actions.dispatch_all(left)
        assert not any(call[0] == "play_preset" for call in player.calls)
        assert c.poll(req["token"])["state"] == "confirmed"
        assert replies[-1] == "ack"
        c.consume(req["token"], "update", {"version": "1.2.0"})
        assert c.poll(req["token"])["state"] == "used"
        with pytest.raises(ConfirmError):              # once only
            c.consume(req["token"], "update", {"version": "1.2.0"})

    def test_only_one_press_is_taken(self, rig):
        app, actions, *_ = rig
        app.confirm.request("reset")
        assert app.confirm.intercept([Event("k1", "tap"), Event("k2", "tap")]) == [Event("k2", "tap")]
        assert app.confirm.intercept([Event("k3", "tap")]) == [Event("k3", "tap")]

    def test_hold_and_pu_confirm_too(self, rig):
        app, *_ = rig
        app.confirm.request("reset")
        assert app.confirm.intercept([Event("pu", "hold")]) == []

    def test_nothing_pending_passes_everything(self, rig):
        app, *_ = rig
        events = [Event("k1", "tap")]
        assert app.confirm.intercept(events) == events

    def test_through_the_real_panel_poll(self, rig):
        app, actions, player, panel, _ = rig
        key = next(c for c in panel.controls if c.id == "k2")
        req = app.confirm.request("wifi", {"ssid": "Home"})
        t = 10.0
        panel.digital.set(key.pin, True)
        for _ in range(10):
            actions.dispatch_all(app.confirm.intercept(panel.poll(t)))
            t += 0.02
        panel.digital.set(key.pin, False)
        for _ in range(10):
            actions.dispatch_all(app.confirm.intercept(panel.poll(t)))
            t += 0.02
        assert app.confirm.poll(req["token"])["state"] == "confirmed"
        assert not any(call[0] == "play_preset" for call in player.calls)

    def test_expires_after_30s(self, rig):
        app, *_ = rig
        c = app.confirm
        req = c.request("rollback", {"to": "1.1.0"})
        c.clock.t += 31
        assert c.poll(req["token"])["state"] == "expired"
        assert c.intercept([Event("k1", "tap")]) == [Event("k1", "tap")]   # plays normally again
        with pytest.raises(ConfirmError):
            c.consume(req["token"], "rollback", {"to": "1.1.0"})

    def test_a_confirmed_token_must_be_used_within_a_minute(self, rig):
        app, *_ = rig
        c = app.confirm
        req = c.request("reset")
        c.intercept([Event("k1", "tap")])
        c.clock.t += 61
        with pytest.raises(ConfirmError):
            c.consume(req["token"], "reset", {})

    def test_token_is_bound_to_action_and_args(self, rig):
        app, *_ = rig
        c = app.confirm
        req = c.request("rollback", {"to": "1.1.0"})
        c.intercept([Event("k1", "tap")])
        with pytest.raises(ConfirmError):
            c.consume(req["token"], "rollback", {"to": "0.9.0"})
        with pytest.raises(ConfirmError):
            c.consume(req["token"], "reset", {})
        c.consume(req["token"], "rollback", {"to": "1.1.0"})

    def test_lamp_asks_again_while_pending(self, rig):
        app, *_, replies = rig
        c = app.confirm
        c.request("reset")
        c.tick()
        assert replies == ["ha"]
        c.clock.t += confirm_mod.ASK_EVERY_S
        c.tick()
        assert replies == ["ha", "ha"]
        c.clock.t += 40
        c.tick()
        assert replies == ["ha", "ha"]                 # expired: quiet

    def test_absent_panel_refuses(self, rig):
        app, _, _, panel, _ = rig
        panel.digital = AbsentDigital("no MCP23017")
        with pytest.raises(ConfirmUnavailable, match="keys aren't connected"):
            app.confirm.request("update", {"version": "1.2.0"})

    def test_allow_unconfirmed_for_the_bench(self, rig):
        app, _, _, panel, replies = rig
        panel.digital = AbsentDigital("bench")
        app.confirm.allow_unconfirmed = True
        req = app.confirm.request("reset")
        assert req["state"] == "confirmed" and replies == []
        app.confirm.consume(req["token"], "reset", {})
        app.confirm.consume(None, "reset", {})        # and no token at all

    def test_unknown_action_is_a_400(self, rig):
        with pytest.raises(ValueError):
            rig[0].confirm.request("format_disk")

    def test_a_new_request_replaces_the_pending_one(self, rig):
        c = rig[0].confirm
        a = c.request("reset")
        b = c.request("wifi", {"ssid": "x"})
        assert c.poll(a["token"])["state"] == "expired"
        c.intercept([Event("k1", "tap")])
        assert c.poll(b["token"])["state"] == "confirmed"


class TestConfirmHttp:
    def test_reset_needs_a_press(self, base):
        hp, app = base
        status, doc = call(hp, "POST", "/api/reset", {})
        assert status == 403 and doc["confirm_required"] is True
        status, doc = call(hp, "POST", "/api/confirm/request", {"action": "reset"})
        token = doc["token"]
        assert status == 200 and call(hp, "GET", f"/api/confirm/{token}")[1]["state"] == "pending"
        app.confirm.intercept([Event("k3", "tap")])
        assert call(hp, "GET", f"/api/confirm/{token}")[1]["state"] == "confirmed"
        status, doc = call(hp, "POST", "/api/reset", {"confirm": token})
        assert status == 200 and doc["ok"] and doc["config"]

    def test_unknown_token_reads_expired(self, base):
        hp, _ = base
        status, doc = call(hp, "GET", "/api/confirm/AAAAAAAAAAAAAAAAAAAAAAAA")
        assert status == 404 and doc["state"] == "expired"

    def test_cancel(self, base):
        hp, app = base
        token = call(hp, "POST", "/api/confirm/request", {"action": "reset"})[1]["token"]
        assert call(hp, "DELETE", f"/api/confirm/{token}", {})[1]["ok"] is True
        assert call(hp, "GET", f"/api/confirm/{token}")[1]["state"] == "expired"

    def test_absent_panel_is_a_clear_409(self, base):
        hp, app = base
        app.panel.digital = AbsentDigital("gone")
        status, doc = call(hp, "POST", "/api/confirm/request", {"action": "update", "args": {"version": "1.2.0"}})
        assert status == 409 and "keys aren't connected" in doc["error"]

    def test_wifi_from_the_lan_needs_a_press(self, base):
        hp, app = base
        joined = []
        app.wifi.join = lambda ssid, pwd, sec: joined.append(ssid)
        status, doc = call(hp, "POST", "/api/wifi", {"ssid": "Home", "password": "12345678"})
        assert status == 403 and doc["confirm_required"] and joined == []
        token = call(hp, "POST", "/api/confirm/request", {"action": "wifi", "args": {"ssid": "Home"}})[1]["token"]
        app.confirm.intercept([Event("k1", "tap")])
        status, _ = call(hp, "POST", "/api/wifi", {"ssid": "Home", "password": "12345678", "confirm": token})
        assert status == 200 and joined == ["Home"]

    def test_wifi_from_a_phone_on_the_hotspot_just_works(self, base):
        hp, app = base
        joined = []
        app.wifi.join = lambda ssid, pwd, sec: joined.append(ssid)
        app.hotspot_addrs = {"127.0.0.1"}     # this test's socket stands in for 172.24.1.1
        status, _ = call(hp, "POST", "/api/wifi", {"ssid": "Home", "password": "12345678"},
                         {"Host": "172.24.1.1:8080", "Origin": "http://172.24.1.1:8080"})
        assert status == 200 and joined == ["Home"]
        # ... and the captive page itself is served
        status, page = call(hp, "GET", "/wifi", None, {"Host": "172.24.1.1:8080"})
        assert status == 200 and b"/api/confirm/request" in page


# -- software ----------------------------------------------------------------------------


def make_release(root, version, seq, complete=True):
    d = root / "opt/hasler/releases" / version
    d.mkdir(parents=True)
    (d / "release.json").write_text(json.dumps({"version": version, "seq": seq}))
    if complete:
        (d / ".complete").write_text("")
    return d


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A fake updater layout under HASLER_ROOT: 1.1.0 previous, 1.2.0 running, 1.3.0 on offer."""
    root = tmp_path / "root"
    monkeypatch.setenv("HASLER_ROOT", str(root))
    make_release(root, "1.0.0", 1000099)
    prev = make_release(root, "1.1.0", 1010099)
    cur = make_release(root, "1.2.0", 1020099)
    make_release(root, "0.9.0", 900099, complete=False)
    (root / "opt/hasler/current").symlink_to(cur)
    (root / "opt/hasler/previous").symlink_to(prev)
    upd = root / "var/lib/hasler-update"
    (upd / "staged/1030099").mkdir(parents=True)
    (upd / "staged/1030099/STAGED").write_text("")
    now = time.time()
    (upd / "status.json").write_text(json.dumps({
        "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 3600)), "channel": "stable", "result": "ok",
        "index_expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 86400 * 10)), "message": "",
        "available": {"version": "1.3.0", "seq": 1030099, "kind": "minor", "requires_migration": False,
                      "notes": "Quieter lamp at night."}}))
    app_dir = root / "var/lib/hasler-apply"
    app_dir.mkdir(parents=True)
    (app_dir / "state.json").write_text(json.dumps({
        "phase": "probation", "from": "1.1.0", "to": "1.2.0", "bad": [1000099], "manual": False,
        "history": [{"at": "2026-10-18T03:00:00Z", "event": "switch"}, {"at": "2026-10-18T03:02:00Z", "event": "commit", "to": "1.2.0"}]}))
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "box.yaml").write_text("schema: 1\nvariant: hasler-hm\nchannel: stable\nupdate_mode: ask\n")
    paths = boxconfig.Paths(release=tmp_path, etc=etc, state=tmp_path / "state")
    started = []
    state = {"idle": True}
    sw = Software(box_paths=paths, state_dir=tmp_path / "state", systemctl=started.append,
                  idle=lambda: state["idle"], local_hour=lambda: 3)
    return sw, root, started, state, paths


class TestSoftware:
    def test_status_document(self, box):
        sw, *_ = box
        st = sw.status()
        assert st["version"] == "1.2.0" and st["channel"] == "stable" and st["update_mode"] == "ask"
        assert st["available"] == {"version": "1.3.0", "seq": 1030099, "notes": "Quieter lamp at night.",
                                   "kind": "minor", "requires_migration": False, "staged": True}
        assert st["last_result"]["ok"] is True and st["last_result"]["version"] == "1.2.0"
        assert st["last_result"]["at"] == "2026-10-18T03:02:00Z"
        assert [(r["version"], r["status"]) for r in st["releases"]] == [
            ("1.2.0", "running"), ("1.1.0", "previous"), ("1.0.0", "bad")]
        assert st["stale"] is False and st["approved"] is None

    def test_rolled_back_result_and_stale(self, box):
        sw, root, *_ = box
        state = root / "var/lib/hasler-apply/state.json"
        doc = json.loads(state.read_text())
        doc.update(phase="rolled_back", to="1.3.0", failed="1.3.0", **{"from": "1.2.0"}, reason="MPD not answering",
                   history=doc["history"] + [{"at": "2026-10-19T03:05:00Z", "event": "rollback"}])
        state.write_text(json.dumps(doc))
        status = root / "var/lib/hasler-update/status.json"
        s = json.loads(status.read_text())
        s["checked"] = "2026-01-01T00:00:00Z"
        status.write_text(json.dumps(s))
        st = sw.status()
        assert st["last_result"] == {"ok": False, "version": "1.3.0", "back_to": "1.2.0",
                                     "reason": "MPD not answering", "at": "2026-10-19T03:05:00Z"}
        assert st["stale"] is True

    def test_not_installed_is_404(self, tmp_path, monkeypatch, rig):
        monkeypatch.setenv("HASLER_ROOT", str(tmp_path / "nothing"))
        app = rig[0]
        app.software = Software()
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        try:
            status, doc = call(f"127.0.0.1:{server.port}", "GET", "/api/software")
            assert status == 404 and doc["installed"] is False
        finally:
            server.close()

    def test_check_starts_the_check_unit(self, box):
        sw, _, started, *_ = box
        sw.check()
        assert started == ["hasler-update-check.service"]

    def test_install_now_starts_once_idle(self, box):
        sw, _, started, state, _ = box
        state["idle"] = False
        sw.install("1.3.0", "now")
        sw.join()
        assert started == [] and sw.status()["approved"]["when"] == "now"
        state["idle"] = True
        sw.tick(force=True)
        sw.join()
        assert started == ["hasler-update-apply@1030099.service"]
        assert sw.status()["approved"] is None        # cleared before starting

    def test_install_tonight_waits_for_the_window(self, box):
        sw, _, started, _, _ = box
        sw.local_hour = lambda: 14
        sw.install("1.3.0", "tonight")
        sw.join()
        assert started == []
        sw.local_hour = lambda: 3
        sw.tick(force=True)
        sw.join()
        assert started == ["hasler-update-apply@1030099.service"]

    def test_install_refuses_what_is_not_on_offer_or_not_staged(self, box):
        sw, root, *_ = box
        with pytest.raises(ValueError):
            sw.install("9.9.9")
        (root / "var/lib/hasler-update/staged/1030099/STAGED").unlink()
        from haslerd.web.software import SoftwareError
        with pytest.raises(SoftwareError, match="downloaded"):
            sw.install("1.3.0")

    def test_rollback(self, box):
        sw, _, started, *_ = box
        sw.rollback("1.1.0")
        assert started == ["hasler-update-apply@rollback-1.1.0.service"]
        for bad in ("1.2.0", "0.9.0", "7.0.0", "1.1.0; rm -rf /", "../1.1.0"):
            with pytest.raises(ValueError):
                sw.rollback(bad)

    def test_settings_write_box_yaml(self, box):
        sw, _, _, _, paths = box
        assert sw.put_settings({"update_mode": "auto"}) == {"update_mode": "auto", "channel": "stable"}
        doc = yaml.safe_load(paths.box_file.read_text())
        assert doc["update_mode"] == "auto" and doc["variant"] == "hasler-hm" and doc["schema"] == 1
        with pytest.raises(ValueError):
            sw.put_settings({"update_mode": "sometimes"})
        with pytest.raises(ValueError, match="bench or stable"):
            sw.put_settings({"channel": "beta"})

    def test_two_channels_beta_reads_as_stable(self, box, caplog):
        sw, _, _, _, paths = box
        paths.box_file.write_text("schema: 1\nvariant: hasler-hm\nchannel: beta\nupdate_mode: ask\n")
        with caplog.at_level(logging.WARNING, logger="haslerd.web.software"):
            assert sw.box_settings() == {"update_mode": "ask", "channel": "stable"}
            assert sw.box_settings()["channel"] == "stable"
        assert sum("treating it as stable" in r.getMessage() for r in caplog.records) == 1
        # A new radio's box.yaml without update_mode: auto by default.
        paths.box_file.write_text("schema: 1\nvariant: hasler-hm\n")
        assert sw.box_settings() == {"update_mode": "auto", "channel": "stable"}

    def test_hand_written_off_is_off_not_ask(self, box):
        # PyYAML reads a bare ``off`` / ``no`` as False; the updater reads the word.
        sw, _, _, _, paths = box
        for word in ("off", "no", "Off"):
            paths.box_file.write_text(f"schema: 1\nvariant: hasler-hm\nupdate_mode: {word}\n")
            assert sw.box_settings()["update_mode"] == "off"

    def test_writing_drops_a_retired_channel_for_stable(self, box):
        sw, _, _, _, paths = box
        paths.box_file.write_text("schema: 1\nvariant: hasler-hm\nchannel: beta\nupdate_mode: ask\n")
        assert sw.put_settings({"update_mode": "off"}) == {"update_mode": "off", "channel": "stable"}
        doc = yaml.safe_load(paths.box_file.read_text())
        assert doc["channel"] == "stable" and doc["update_mode"] == "off"


class TestSoftwareHttp:
    @pytest.fixture
    def web(self, box, rig):
        sw = box[0]
        app = rig[0]
        app.software = sw
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        yield f"127.0.0.1:{server.port}", app, box
        server.close()

    def confirmed(self, hp, app, action, args):
        token = call(hp, "POST", "/api/confirm/request", {"action": action, "args": args})[1]["token"]
        app.confirm.intercept([Event("k1", "tap")])
        return token

    def test_get_and_check(self, web):
        hp, app, (sw, _, started, *_ ) = web
        status, doc = call(hp, "GET", "/api/software")
        assert status == 200 and doc["version"] == "1.2.0" and doc["available"]["version"] == "1.3.0"
        assert call(hp, "POST", "/api/software/check", {})[0] == 200
        assert started == ["hasler-update-check.service"]

    def test_install_and_rollback_need_a_press(self, web):
        hp, app, (sw, _, started, *_ ) = web
        assert call(hp, "POST", "/api/software/install", {"version": "1.3.0"})[0] == 403
        assert call(hp, "POST", "/api/software/rollback", {"to": "1.1.0"})[0] == 403
        token = self.confirmed(hp, app, "rollback", {"to": "1.0.0"})
        # A token for another version is no good.
        assert call(hp, "POST", "/api/software/rollback", {"to": "1.1.0", "confirm": token})[0] == 403
        assert started == []
        token = self.confirmed(hp, app, "update", {"version": "1.3.0"})
        status, doc = call(hp, "POST", "/api/software/install", {"version": "1.3.0", "when": "now", "confirm": token})
        assert status == 200 and doc["approved"]["seq"] == 1030099
        sw.join()
        assert started == ["hasler-update-apply@1030099.service"]
        token = self.confirmed(hp, app, "rollback", {"to": "1.1.0"})
        assert call(hp, "POST", "/api/software/rollback", {"to": "1.1.0", "confirm": token})[0] == 200
        assert started[-1] == "hasler-update-apply@rollback-1.1.0.service"

    def test_mode_free_channel_confirmed(self, web):
        hp, app, (sw, *_ ) = web
        status, doc = call(hp, "PUT", "/api/software/settings", {"update_mode": "off"})
        assert status == 200 and doc["update_mode"] == "off"
        assert call(hp, "PUT", "/api/software/settings", {"channel": "bench"})[0] == 403
        token = self.confirmed(hp, app, "channel", {"channel": "bench"})
        status, doc = call(hp, "PUT", "/api/software/settings", {"channel": "bench", "confirm": token})
        assert status == 200 and doc["channel"] == "bench"

    def test_a_bad_value_is_a_400_before_any_press(self, web):
        hp, app, (sw, *_ ) = web
        status, doc = call(hp, "PUT", "/api/software/settings", {"update_mode": "sometimes", "channel": "bench"})
        assert status == 400 and "update_mode" in doc["error"] and not doc.get("confirm_required")
        status, doc = call(hp, "PUT", "/api/software/settings", {"channel": "beta"})
        assert status == 400 and "bench or stable" in doc["error"]

    def test_systemd_refusal_is_a_503(self, web):
        hp, app, (sw, *_ ) = web

        def refuse(unit):
            raise RuntimeError("Interactive authentication required.")
        sw.systemctl = refuse
        status, doc = call(hp, "POST", "/api/software/check", {})
        assert status == 503 and "authentication" in doc["error"]


# -- health's idle clock ------------------------------------------------------------------


def test_keys_and_plays_touch_the_idle_clock(rig):
    app, actions, *_ = rig
    assert actions.last_active_at is None
    before = time.monotonic()
    actions.dispatch(Event("k1", "tap"))
    assert actions.last_active_at is not None and actions.last_active_at >= before
    from haslerd.health import health_report
    actions._standby(None, {})
    assert health_report(app)["idle"] is False        # just played: not idle yet
    assert health_report(app, now=time.monotonic() + 601)["idle"] is True
