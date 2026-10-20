"""Remote help: /api/help (web/help_api.py) and the root helper
scripts/hasler-help - every outside touch (sudo, systemd, tailscale) faked."""

import importlib.machinery
import importlib.util
import json
import logging
from pathlib import Path

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import help_api
from haslerd.web.confirm import ConfirmError
from haslerd.web.help_api import HelpApi, tailnet_hostname
from haslerd.web.server import Unavailable, WebApp, WebServer

from test_web_confirm import Clock, call

ROOT = Path(__file__).resolve().parents[2]
URL = "https://login.tailscale.com/a/1a2b3c4d5e6f"


# -- a fake hasler-help ------------------------------------------------------------


class FakeHelper:
    """What the helper prints, as a little state machine."""

    def __init__(self, installed=True):
        self.installed = installed
        self.active = False
        self.backend = None
        self.tailnet = "delgado-murphy.org.github"
        self.session = {}
        self.last = None
        self.calls = []
        self.start_ok = True

    def status(self, ok=True, error=None):
        running = self.backend == "Running"
        doc = {"ok": ok, "installed": self.installed, "active": self.active, "backend": self.backend,
               "auth_url": URL if self.active and self.backend == "NeedsLogin" else None,
               "address": "100.101.102.103" if running else None,
               "dns_name": "hasler-kitchen-ab12.tail1234.ts.net" if running else None,
               "tailnet": self.tailnet if running else None,
               "hostname": self.session.get("hostname"), "started_at": self.session.get("started_at"),
               "expires_at": self.session.get("expires_at"), "approve_by": self.session.get("approve_by"),
               "stale": bool(self.session) and not self.active, "last": self.last}
        if error:
            doc["error"] = error
        return doc

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[0] == "start":
            if not self.start_ok:
                return 1, json.dumps(self.status(False, "couldn't start tailscaled")) + "\n"
            self.active, self.backend = True, "NeedsLogin"
            self.session = {"hostname": args[1], "started_at": 1000, "expires_at": 1000 + 86400,
                            "approve_by": 1000 + 3600}
            self.last = None
        elif args[0] == "stop":
            self.active, self.backend, self.session = False, None, {}
            self.last = {"at": 2000, "why": args[1]}
        return 0, "some noise\n" + json.dumps(self.status()) + "\n"


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    app.confirm.clock = Clock()
    mid = tmp_path / "machine-id"
    mid.write_text("0123456789abcdef\n")
    app.machine_id_file = mid
    app.room.name = lambda: "Kitchen"
    fake = FakeHelper()
    now = [1500.0]
    api = HelpApi(app, runner=fake, helper_present=lambda: True, clock=lambda: now[0])
    app.extra_routes.append(api)
    return app, api, fake, now, panel


def confirmed(app, panel):
    token = app.confirm.request("help")["token"]
    from haslerd.panel.controls import Event
    key = next(c.id for c in panel.controls if c.kind in ("momentary", "interlocked_bank"))
    app.confirm.intercept([Event(key, "tap")])
    return token


def test_hostname():
    assert tailnet_hostname("Kitchen", "ab12cd34ef56") == "hasler-kitchen-ab12"
    assert tailnet_hostname("Küche & Bad!", "ab12") == "hasler-k-che-bad-ab12"
    assert tailnet_hostname("", "ab12") == "hasler-ab12"
    assert tailnet_hostname("hasler-12", "ab12") == "hasler-12-ab12"
    long = tailnet_hostname("x" * 200, "ab12cd")
    assert len(long) <= 63 and long.endswith("-ab12")


def test_off_by_default(rig):
    _, api, fake, _, _ = rig
    doc = api.doc()
    assert doc["available"] is True and doc["state"] == "off" and doc["login_url"] is None
    assert fake.calls == [["status"]]           # nothing started by just looking


def test_not_installed(rig):
    _, api, fake, _, _ = rig
    fake.installed = False
    assert api.doc()["available"] is False
    api.helper_present = lambda: False
    n = len(fake.calls)
    doc = api.doc()
    assert doc["available"] is False and doc["state"] == "off" and len(fake.calls) == n
    with pytest.raises(Unavailable):
        api.start({})


def test_start_needs_a_press_on_the_radio(rig):
    app, api, fake, _, panel = rig
    with pytest.raises(ConfirmError):
        api.start({})
    token = app.confirm.request("help")["token"]         # asked, not pressed
    with pytest.raises(ConfirmError):
        api.start({"confirm": token})
    assert not any(c[0] == "start" for c in fake.calls)


def test_start_waiting_connected_stop(rig, caplog):
    app, api, fake, now, panel = rig
    caplog.set_level(logging.DEBUG)
    doc = api.start({"confirm": confirmed(app, panel)})
    assert ["start", "hasler-kitchen-" + help_api.integration.radio_id(app.machine_id_file)[:4]] in fake.calls
    assert doc["state"] == "waiting" and doc["login_url"] == URL
    assert doc["expires_at"] == 1000 + 86400 and doc["approve_by"] == 1000 + 3600
    assert doc["address"] is None and doc["ssh"] is None
    # a maker approves
    fake.backend = "Running"
    now[0] += 5
    doc = api.doc()
    assert doc["state"] == "connected" and doc["login_url"] is None
    assert doc["status_text"] == "Connected to Delgado & Murphy's help network."
    assert doc["address"] == "100.101.102.103" and doc["ssh"] == "pi@100.101.102.103"
    assert doc["page"] == "http://100.101.102.103:8080/" and doc["tailnet"] == "delgado-murphy.org.github"
    assert doc["approve_by"] is None
    doc = api.stop()
    assert doc["state"] == "off" and doc["last"]["why"] == "stopped" and doc["last"]["text"]
    assert ["stop", "stopped"] in fake.calls
    # the one-time join link is never in the log
    assert URL not in caplog.text and "/a/" not in caplog.text


def test_status_is_cached_briefly(rig):
    _, api, fake, now, _ = rig
    api.doc(); api.doc()
    assert fake.calls.count(["status"]) == 1
    now[0] += help_api.CACHE_S + 0.1
    api.doc()
    assert fake.calls.count(["status"]) == 2


def test_start_failure_is_503_with_the_reason(rig):
    app, api, fake, _, panel = rig
    fake.start_ok = False
    with pytest.raises(Unavailable, match="tailscaled"):
        api.start({"confirm": confirmed(app, panel)})


@pytest.mark.parametrize("case", ["expired", "stale", "wrong-tailnet"])
def test_enforced_from_the_page_too(rig, case):
    app, api, fake, now, panel = rig
    api.tailnet = "delgado-murphy.org.github"
    api.start({"confirm": confirmed(app, panel)})
    fake.backend = "Running"
    if case == "expired":
        now[0] = 1000 + 86400 + 1
    elif case == "stale":
        fake.active = False                      # tailscaled died, session on record
    else:
        fake.tailnet = "someone-else@gmail.com"
    now[0] += help_api.CACHE_S + 1
    doc = api.doc()
    why = {"stale": "failed"}.get(case, case)
    assert ["stop", why] in fake.calls
    assert doc["state"] == "off" and doc["last"]["why"] == why


def test_right_tailnet_is_left_alone(rig):
    app, api, fake, now, panel = rig
    api.tailnet = "Delgado-Murphy.org.github"
    api.start({"confirm": confirmed(app, panel)})
    fake.backend = "Running"
    now[0] += 10
    assert api.doc()["state"] == "connected"
    assert not any(c[0] == "stop" for c in fake.calls)


def test_helper_garbage_is_503(rig):
    _, api, _, _, _ = rig
    api.runner = lambda args, timeout: (1, "sudo: a password is required\n")
    doc = api.doc()
    assert doc["state"] == "off" and "helper failed" in doc["error"]
    with pytest.raises(Unavailable):
        api.stop()


def test_over_http(rig):
    app, api, fake, _, panel = rig
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    try:
        hp = f"127.0.0.1:{server.port}"
        status, doc = call(hp, "GET", "/api/help")
        assert status == 200 and doc["state"] == "off"
        status, doc = call(hp, "POST", "/api/help/start", {})
        assert status == 403 and doc["confirm_required"] is True
        status, doc = call(hp, "POST", "/api/help/start", {"confirm": confirmed(app, panel)})
        assert status == 200 and doc["ok"] and doc["state"] == "waiting" and doc["login_url"] == URL
        status, doc = call(hp, "POST", "/api/help/start", {}, {"Host": "evil.example"})
        assert status == 403
        status, doc = call(hp, "POST", "/api/help/stop", {})
        assert status == 200 and doc["state"] == "off"
        assert call(hp, "DELETE", "/api/help", {})[0] == 405
    finally:
        server.close()


# -- the root helper, scripts/hasler-help -----------------------------------------


def load_helper(tmp_path, monkeypatch):
    path = ROOT / "scripts" / "hasler-help"
    loader = importlib.machinery.SourceFileLoader("hasler_help", str(path))
    spec = importlib.util.spec_from_loader("hasler_help", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    mod.RUN = str(tmp_path / "run" / "hasler-help")
    mod.LAST = str(tmp_path / "run" / "hasler-help.last.json")
    (tmp_path / "run").mkdir()
    sysd = FakeSystem()
    monkeypatch.setattr(mod, "sh", sysd.sh)
    monkeypatch.setattr(mod, "sleep", lambda s: None)
    monkeypatch.setattr(mod, "now", lambda: 5000.0)
    monkeypatch.setattr(mod, "installed", lambda: sysd.installed)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 0)
    return mod, sysd


class FakeSystem:
    """systemctl, systemd-run and tailscale, as far as the helper uses them."""

    def __init__(self):
        self.installed = True
        self.units = set()          # active units
        self.backend = None
        self.cmds = []
        self.packaged = "disabled"
        self.filter_ok = True

    def sh(self, cmd, timeout=20):
        self.cmds.append(cmd)
        exe = cmd[0].rsplit("/", 1)[-1]
        if exe == "systemctl":
            verb = cmd[1]
            if verb == "is-active":
                return (0 if cmd[-1] in self.units else 3), ""
            if verb == "is-enabled":
                return 0, self.packaged + "\n"
            if verb == "stop":
                for u in cmd[2:]:
                    self.units.discard(u)
                    if u == "hasler-help-tailscaled.service":
                        self.backend = None
                return 0, ""
            if verb == "disable":
                self.packaged = "disabled"
                self.units.discard("tailscaled.service")
            return 0, ""
        if exe == "systemd-run":
            unit = next(a.split("=", 1)[1] for a in cmd if a.startswith("--unit="))
            if not self.filter_ok and any(a.startswith("--property=LogFilterPatterns") for a in cmd):
                return 1, ""
            if any(a.startswith("--on-active") for a in cmd):
                self.units.add(unit + ".timer")
            else:
                self.units.add(unit + ".service")
                if unit == "hasler-help-tailscaled":
                    self.backend = "NoState"
                if unit == "hasler-help-up":
                    self.backend = "NeedsLogin"
            return 0, ""
        if exe == "tailscale":
            if "hasler-help-tailscaled.service" not in self.units:
                return 1, ""
            if "status" in cmd:
                doc = {"BackendState": self.backend, "AuthURL": URL if self.backend == "NeedsLogin" else "",
                       "Self": {"DNSName": "hasler-kitchen-ab12.tail1234.ts.net.",
                                "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.64.0.7"]},
                       "CurrentTailnet": {"Name": "delgado-murphy.org.github"} if self.backend == "Running" else None}
                return 0, json.dumps(doc)
            if "logout" in cmd:
                self.backend = "NeedsLogin"
                return 0, ""
        raise AssertionError(f"unexpected command {cmd}")

    def run_index(self, unit_prefix):
        return next(i for i, c in enumerate(self.cmds)
                    if c[0].endswith("systemd-run") and f"--unit={unit_prefix}" in c)


def last_json(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


@pytest.mark.parametrize("argv", [[], ["start"], ["start", "Hasler-Kitchen"], ["start", "kitchen"],
                                  ["start", "hasler-kitchen;reboot"], ["start", "hasler-" + "a" * 60],
                                  ["start", "hasler-a", "extra"], ["stop", "because"], ["status", "x"],
                                  ["logout"]])
def test_helper_refuses_bad_arguments(tmp_path, monkeypatch, capsys, argv):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    assert mod.main(argv) == 2
    assert last_json(capsys)["ok"] is False and sysd.cmds == []


def test_helper_needs_root(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.os, "geteuid", lambda: 1000)
    assert mod.main(["status"]) == 1 and sysd.cmds == []


def test_helper_not_installed(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    sysd.installed = False
    assert mod.main(["start", "hasler-kitchen-ab12"]) == 1
    doc = last_json(capsys)
    assert doc["ok"] is False and doc["installed"] is False and "install" in doc["error"]


def test_helper_start_status_stop(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    assert mod.main(["start", "hasler-kitchen-ab12"]) == 0
    doc = last_json(capsys)
    assert doc["ok"] and doc["active"] and doc["backend"] == "NeedsLogin" and doc["auth_url"] == URL
    assert doc["expires_at"] == 5000 + 86400 and doc["approve_by"] == 5000 + 3600
    assert doc["hostname"] == "hasler-kitchen-ab12" and doc["address"] is None
    # the end is set before anything starts
    assert sysd.run_index("hasler-help-end") < sysd.run_index("hasler-help-tailscaled")
    assert sysd.run_index("hasler-help-unapproved") < sysd.run_index("hasler-help-tailscaled")
    end = sysd.cmds[sysd.run_index("hasler-help-end")]
    assert "--on-active=86400s" in end and end[-3:] == ["/usr/local/sbin/hasler-help", "stop", "expired"]
    una = sysd.cmds[sysd.run_index("hasler-help-unapproved")]
    assert "--on-active=3600s" in una and una[-2:] == ["stop", "unapproved"]
    daemon = sysd.cmds[sysd.run_index("hasler-help-tailscaled")]
    assert "--state=mem:" in daemon and "--no-logs-no-support" in daemon
    assert any(a.startswith("--property=LogFilterPatterns=~") for a in daemon)
    up = sysd.cmds[sysd.run_index("hasler-help-up")]
    for flag in ("--hostname=hasler-kitchen-ab12", "--ssh=false", "--accept-dns=false", "--accept-routes=false",
                 "--property=StandardOutput=null", "--property=StandardError=null"):
        assert flag in up, flag
    assert not any("authkey" in a or "advertise-tags" in a for a in up)
    assert (Path(mod.RUN) / "session.json").stat().st_mode & 0o777 == 0o600

    # again while on: same session, nothing restarted
    n = len([c for c in sysd.cmds if c[0].endswith("systemd-run")])
    assert mod.main(["start", "hasler-kitchen-ab12"]) == 0
    assert len([c for c in sysd.cmds if c[0].endswith("systemd-run")]) == n
    capsys.readouterr()

    sysd.backend = "Running"
    assert mod.main(["status"]) == 0
    doc = last_json(capsys)
    assert doc["auth_url"] is None and doc["address"] == "100.64.0.7"
    assert doc["dns_name"] == "hasler-kitchen-ab12.tail1234.ts.net" and doc["tailnet"] == "delgado-murphy.org.github"

    # the hour for approval is up, but a maker approved: nothing happens
    assert mod.main(["stop", "unapproved"]) == 0
    assert last_json(capsys)["active"] is True
    assert not any("logout" in c for c in sysd.cmds)

    assert mod.main(["stop", "expired"]) == 0
    doc = last_json(capsys)
    assert doc["active"] is False and doc["last"] == {"at": 5000, "why": "expired"}
    assert any("logout" in c for c in sysd.cmds)
    assert not Path(mod.RUN).exists()
    assert not any(u.startswith("hasler-help") for u in sysd.units)


def test_helper_unapproved_ends_a_waiting_session(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    mod.main(["start", "hasler-kitchen-ab12"])
    assert mod.main(["stop", "unapproved"]) == 0
    doc = last_json(capsys)
    assert doc["active"] is False and doc["last"]["why"] == "unapproved"


def test_helper_without_log_filter_silences_tailscaled(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    sysd.filter_ok = False
    assert mod.main(["start", "hasler-kitchen-ab12"]) == 0
    runs = [c for c in sysd.cmds if c[0].endswith("systemd-run") and "--unit=hasler-help-tailscaled" in c]
    assert len(runs) == 2 and "--property=StandardOutput=null" in runs[1]


def test_helper_refuses_beside_a_packaged_tailscaled(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    sysd.units.add("tailscaled.service")
    assert mod.main(["start", "hasler-kitchen-ab12"]) == 1
    assert "tailscaled.service" in last_json(capsys)["error"]
    assert not any(c[0].endswith("systemd-run") for c in sysd.cmds)


def test_helper_stop_disables_the_packaged_daemon(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    sysd.packaged = "enabled"
    assert mod.main(["stop"]) == 0
    assert any(c[1:3] == ["disable", "--now"] for c in sysd.cmds if c[0].endswith("systemctl"))
    assert last_json(capsys)["last"]["why"] == "stopped"


def test_helper_reports_a_dead_session_as_stale(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    mod.main(["start", "hasler-kitchen-ab12"])
    sysd.units.discard("hasler-help-tailscaled.service")
    capsys.readouterr()
    mod.main(["status"])
    assert last_json(capsys)["stale"] is True


def test_shipped_as_a_root_helper():
    sudoers = (ROOT / "config/sudoers/haslerd").read_text()
    assert "/usr/local/sbin/hasler-help," in sudoers
    rules = (ROOT / "scripts/release/build_release.py").read_text()
    assert 'Rule("scripts/hasler-help", "integration/sbin/", mode=0o755)' in rules
    assert (ROOT / "scripts/hasler-help").stat().st_mode & 0o111
    boot = (ROOT / "scripts/bootstrap.sh").read_text()
    assert "pkgs.tailscale.com/stable/debian trixie main" in boot and "signed-by=" in boot
    assert "disable --quiet --now tailscaled.service" in boot
