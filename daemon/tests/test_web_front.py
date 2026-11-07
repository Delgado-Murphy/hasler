"""The key page on port 80: web/front.py (the check), web/server.py (what it
believes from the front) and scripts/hasler-web-front (the root helper, run
with fakes for nginx and systemctl). No network beyond 127.0.0.1."""

import http.client
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import front as front_mod
from haslerd.web.front import WebFront
from haslerd.web.server import WebApp, WebServer, front_addrs

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "hasler-web-front"


# -- what the server believes ----------------------------------------------------

class TestFrontAddrs:
    H = {"X-Hasler-Peer": "192.168.1.30", "X-Hasler-Local": "192.168.1.50"}

    def test_believed_from_loopback_only(self):
        assert front_addrs("127.0.0.1", self.H) == ("192.168.1.30", "192.168.1.50")
        assert front_addrs("::ffff:127.0.0.1", self.H) == ("192.168.1.30", "192.168.1.50")
        assert front_addrs("::1", self.H) == ("192.168.1.30", "192.168.1.50")
        assert front_addrs("192.168.1.30", {"X-Hasler-Peer": "127.0.0.1", "X-Hasler-Local": "127.0.0.1"}) is None

    def test_both_headers_or_the_radio_itself(self):
        assert front_addrs("127.0.0.1", {}) is None
        assert front_addrs("127.0.0.1", {"X-Hasler-Peer": "192.168.1.30"}) is None
        assert front_addrs("127.0.0.1", {"X-Hasler-Peer": "fe80::1%wlan0", "X-Hasler-Local": "fe80::2"}) == \
            ("fe80::1", "fe80::2")


@pytest.fixture
def app(tmp_path, monkeypatch):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    player = FakePlayer()
    player.config = SimpleNamespace(volume_limit=70)
    app = WebApp(Actions(variant, player), Panel.from_variant(variant, fake=True), tmp_path / "keys.yaml",
                 {"radio": True, "spotify": True, "ytm": True}, ha=False, settings_file=tmp_path / "settings.json")
    monkeypatch.setattr(app.wifi, "current_ssid", lambda: "")
    return app


@pytest.fixture
def served(app):
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield server.port, app
    server.close()


def ask(port, method, path, headers, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


FRONT = {"X-Hasler-Peer": "192.168.1.30", "X-Hasler-Local": "192.168.1.50"}


class TestThroughTheFront:
    def test_probe_path(self, served):
        port, _ = served
        status, body = ask(port, "GET", "/api/front", {"Host": "hasler.local", **FRONT})
        assert status == 200 and json.loads(body) == {"ok": True, "via_front": True}
        status, body = ask(port, "GET", "/api/front", {"Host": "hasler.local"})
        assert json.loads(body)["via_front"] is False

    def test_the_address_the_client_used_counts_as_the_radios(self, served):
        """http://192.168.1.50/ through nginx: Host is that address, and so is X-Hasler-Local."""
        port, _ = served
        body = json.dumps({"volume_limit": 60})
        h = {"Host": "192.168.1.50", "Origin": "http://192.168.1.50", "Content-Type": "application/json"}
        status, _ = ask(port, "PUT", "/api/settings", {**h, **FRONT}, body)
        assert status == 200
        # The same request without the front's word: the box was reached on 127.0.0.1, not that address.
        status, out = ask(port, "PUT", "/api/settings", h, body)
        assert status == 403 and b"not this radio" in out

    def test_origin_still_checked(self, served):
        port, _ = served
        h = {"Host": "hasler.local", "Origin": "http://evil.example", "Content-Type": "application/json", **FRONT}
        status, out = ask(port, "PUT", "/api/settings", h, json.dumps({"volume_limit": 60}))
        assert status == 403 and b"cross-site" in out

    def test_ipv6_from_off_this_network_refused(self, served):
        port, _ = served
        h = {"Host": "hasler.local", "X-Hasler-Peer": "2a02:1234:dead::1", "X-Hasler-Local": "2a02:1234:beef::5"}
        status, _ = ask(port, "GET", "/api/status", h)
        assert status == 403
        h["X-Hasler-Peer"] = "2a02:1234:beef::9"         # the radio's own /64
        status, _ = ask(port, "GET", "/api/status", h)
        assert status == 200

    def test_loopback_only_canary_sees_the_real_client(self, served, monkeypatch):
        port, app = served
        seen = []
        monkeypatch.setattr(app, "ytdlp_canary", lambda body, client: (seen.append(client), (200, {"ok": True}))[1])
        h = {"Host": "hasler.local", "Content-Type": "application/json"}
        ask(port, "POST", "/api/components/yt-dlp/canary", {**h, **FRONT}, "{}")
        ask(port, "POST", "/api/components/yt-dlp/canary", h, "{}")
        assert seen == ["192.168.1.30", "127.0.0.1"]


class TestSettingsDoc:
    def test_without_the_front_moode_keeps_port_80(self, app):
        doc = app.settings("hasler.local")
        assert doc["front"] == {"active": False, "port": 80, "moode_port": 8081}
        assert doc["moode_url"] == "http://hasler.local/"

    def test_with_the_front_moode_is_on_8081(self, app):
        app.front = WebFront(probe=lambda: True, helper="/nonexistent")
        app.front.check()
        doc = app.settings("hasler.local")
        assert doc["front"]["active"] is True
        assert doc["moode_url"] == "http://hasler.local:8081/"
        assert app.settings("192.168.1.50:8080")["moode_url"] == "http://192.168.1.50:8081/"


# -- the check -----------------------------------------------------------------------

class Runner:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        ans = self.answers.pop(0) if self.answers else {"ok": True}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(ans) + "\n", stderr="")


@pytest.fixture
def helper(tmp_path):
    p = tmp_path / "hasler-web-front"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return str(p)


class TestCheck:
    def test_already_there_asks_nobody(self, helper):
        run = Runner([])
        f = WebFront(helper=helper, run=run, probe=lambda: True)
        assert f.check() is True and f.active is True and run.calls == []

    def test_missing_is_put_back(self, helper):
        probes = iter([False, True])
        run = Runner([{"ok": True, "front": True, "changed": True}])
        f = WebFront(helper=helper, run=run, probe=lambda: next(probes))
        assert f.check() is True
        assert run.calls == [["sudo", "-n", helper, "ensure"]]

    def test_failure_keeps_8080(self, helper):
        run = Runner([{"ok": False, "error": "nginx rejected hasler-web.conf"}])
        f = WebFront(helper=helper, run=run, probe=lambda: False)
        assert f.check() is False and f.active is False and "rejected" in f.error
        assert f.doc()["active"] is False

    def test_no_helper_no_sudo(self):
        run = Runner([])
        f = WebFront(helper="/nonexistent/hasler-web-front", run=run, probe=lambda: False)
        assert f.check() is False and run.calls == []

    def test_switched_off_hands_port_80_back(self, helper):
        run = Runner([{"ok": True, "front": False, "changed": True}])
        f = WebFront(enabled=False, helper=helper, run=run, probe=lambda: True)
        assert f.check() is False
        assert run.calls == [["sudo", "-n", helper, "remove"]]

    def test_garbled_helper_answer(self, helper):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="sudo: a password is required")
        f = WebFront(helper=helper, run=run, probe=lambda: False)
        assert f.check() is False and "password" in f.error

    def test_background_check_retries_sooner_while_missing(self, monkeypatch):
        import threading
        monkeypatch.setattr(front_mod, "FIRST_CHECK_S", 0.0)
        monkeypatch.setattr(front_mod, "RETRY_S", 0.01)
        seen = threading.Event()
        calls = []

        def probe():
            calls.append(1)
            if len(calls) >= 3:
                seen.set()
                return True
            return False
        f = WebFront(helper="/nonexistent", probe=probe)
        f.start()
        assert seen.wait(5)
        for _ in range(200):
            if f.active:
                break
            threading.Event().wait(0.01)
        f.stop()
        assert f.active is True

    def test_probe_http_against_a_real_server(self, served):
        port, _ = served
        # Straight to haslerd: no front in between, so not "via_front".
        assert WebFront.probe_http(port=port) is False
        assert WebFront.probe_http(port=1) is False


# -- the root helper -------------------------------------------------------------------

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")


@pytest.fixture
def box(tmp_path):
    root = tmp_path / "root"
    (root / "etc/nginx/sites-enabled").mkdir(parents=True)
    (root / "etc/nginx/sites-enabled/moode-http.conf").write_text("server { listen 80 default_server; }\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"
    # nginx -t passes unless a flag file says otherwise; systemctl: nginx is active.
    sites = root / "etc/nginx/sites-enabled"
    (bin_ / "nginx").write_text(f'#!/bin/sh\necho "nginx $*" >> {log}\n'
                                f'[ -e {tmp_path}/nginx-fails ] && {{ echo "nginx: [emerg] bad" >&2; exit 1; }}\n'
                                f'[ -e {tmp_path}/captive-fails ] && [ -e {sites}/hasler-captive.conf ] && '
                                f'{{ echo "nginx: [emerg] captive" >&2; exit 1; }}\nexit 0\n')
    (bin_ / "systemctl").write_text(f'#!/bin/sh\necho "systemctl $*" >> {log}\nexit 0\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "HASLER_ROOT": str(root), "HASLER_NGINX": str(bin_ / "nginx"),
           "HASLER_SYSTEMCTL": str(bin_ / "systemctl")}

    def run(*args):
        p = subprocess.run(["bash", str(HELPER), *args], env=env, capture_output=True, text=True)
        doc = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip().startswith("{") else None
        return p.returncode, doc, p
    return SimpleNamespace(root=root, sites=root / "etc/nginx/sites-enabled", run=run, log=log, tmp=tmp_path)


@needs_bash
class TestHelper:
    def test_ensure_then_nothing_to_do(self, box):
        rc, doc, _ = box.run("ensure")
        assert rc == 0 and doc == {"ok": True, "front": True, "changed": True, "error": ""}
        site = box.sites / "hasler-web.conf"
        assert site.read_text() == subprocess.run(["bash", str(HELPER), "show"], capture_output=True,
                                                  text=True).stdout
        assert oct(site.stat().st_mode & 0o777) == "0o644"
        assert "systemctl reload nginx" in box.log.read_text()
        assert (box.sites / "moode-http.conf").exists()        # moOde's own file untouched
        box.log.write_text("")
        rc, doc, _ = box.run("ensure")
        assert rc == 0 and doc["changed"] is False and doc["front"] is True
        assert "reload" not in box.log.read_text()
        assert sorted(p.name for p in box.sites.iterdir()) == ["hasler-web.conf", "moode-http.conf"]

    def test_status_and_remove(self, box):
        assert box.run("status")[1]["front"] is False
        box.run("ensure")
        assert box.run("status")[1]["front"] is True
        rc, doc, _ = box.run("remove")
        assert rc == 0 and doc["changed"] is True and not (box.sites / "hasler-web.conf").exists()
        assert box.run("remove")[1]["changed"] is False

    def test_a_changed_site_is_replaced(self, box):
        (box.sites / "hasler-web.conf").write_text("# an older one\n")
        rc, doc, _ = box.run("ensure")
        assert rc == 0 and doc["changed"] is True
        assert "8081" in (box.sites / "hasler-web.conf").read_text()

    def test_rejected_by_nginx_is_taken_back(self, box):
        (box.tmp / "nginx-fails").write_text("")
        rc, doc, _ = box.run("ensure")
        assert rc == 1 and doc["ok"] is False and not (box.sites / "hasler-web.conf").exists()
        assert "broken already" in doc["error"]          # the fake fails with or without ours
        (box.sites / "hasler-web.conf").write_text("# an older one\n")
        rc, doc, _ = box.run("ensure")
        assert rc == 1 and (box.sites / "hasler-web.conf").read_text() == "# an older one\n"
        assert not [p for p in box.sites.iterdir() if p.name.startswith(".")]

    def test_captive_portal_put_back_from_the_release(self, box):
        src = box.root / "opt/hasler/current/app/config/captive/nginx-captive.conf"
        src.parent.mkdir(parents=True)
        src.write_text((ROOT / "config/captive/nginx-captive.conf").read_text())
        box.run("ensure")
        assert (box.sites / "hasler-captive.conf").read_text() == src.read_text()

    def test_a_captive_file_nginx_rejects_does_not_stop_the_key_page(self, box):
        src = box.root / "opt/hasler/current/app/config/captive/nginx-captive.conf"
        src.parent.mkdir(parents=True)
        src.write_text("# captive\n")
        (box.tmp / "captive-fails").write_text("")
        rc, doc, _ = box.run("ensure")
        assert rc == 0 and doc["front"] is True and doc["changed"] is True
        assert (box.sites / "hasler-web.conf").exists() and not (box.sites / "hasler-captive.conf").exists()

    def test_no_nginx_and_bad_arguments(self, box, tmp_path):
        shutil.rmtree(box.sites)
        rc, doc, _ = box.run("ensure")
        assert rc == 1 and doc["ok"] is False and "sites-enabled" in doc["error"]
        assert box.run("start")[0] == 2
        assert box.run("ensure", "/etc/passwd")[0] == 2


class TestSiteFile:
    """The nginx text itself (checked with nginx -t on the bench: docs/build-guide.md)."""

    @pytest.fixture
    def site(self):
        return subprocess.run(["bash", str(HELPER), "show"], capture_output=True, text=True, check=True).stdout

    def test_ports(self, site):
        assert re.search(r"^\tlisten 8081;$", site, re.M) and re.search(r"^\tlisten \[::\]:8081;$", site, re.M)
        assert re.search(r"^\tlisten 80;$", site, re.M) and not re.search(r"^\s*listen .*default_server", site, re.M)
        assert "proxy_pass http://127.0.0.1:8080;" in site
        assert str(front_mod.MOODE_PORT) == "8081" and front_mod.FRONT_PORT == 80

    def test_loopback_names_stay_moodes(self, site):
        m = re.search(r'server_name "~(.+)";', site)
        rx = re.compile(m.group(1))
        for host in ("hasler.local", "hasler", "192.168.1.50", "kitchen.local", "[fe80::1]", "100.64.1.2"):
            assert rx.search(host), host
        for host in ("localhost", "127.0.0.1", "[::1]"):
            assert not rx.search(host), host

    def test_headers_haslerd_relies_on(self, site):
        for line in ("proxy_set_header Host $http_host;", "proxy_set_header X-Hasler-Peer $remote_addr;",
                     "proxy_set_header X-Hasler-Local $server_addr;", "include moode-locations.conf;",
                     "proxy_buffering off;"):
            assert line in site, line
        # The "starting" page never echoes the request's Host into HTML.
        starting = site.split("location @hasler_starting", 1)[1]
        assert "$host" not in starting and "$http_host" not in starting
