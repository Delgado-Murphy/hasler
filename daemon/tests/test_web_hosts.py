"""Allowing changes through another address with a press on the radio (web/hosts.py)."""

import json

import pytest
from conftest import VARIANTS
from test_web_confirm import Clock, call

from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web.hosts import AllowedHosts, valid_host
from haslerd.web.server import WebApp, WebServer

DASH = "radio.example.net"


def make_app(tmp_path, allow_unconfirmed=False):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json")
    app.confirm.clock = Clock()
    app.confirm.allow_unconfirmed = allow_unconfirmed
    return app


@pytest.fixture
def served(tmp_path):
    app = make_app(tmp_path)
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"127.0.0.1:{server.port}", app
    server.close()


def via(hp, method, path, body=None, host=DASH, origin=True, **extra):
    """A request as the browser sends it when the page is open at ``host``."""
    headers = {"Host": host, **extra}
    if origin:
        headers["Origin"] = f"https://{host}"
    return call(hp, method, path, body, headers)


def allow(hp, app, host=DASH):
    status, doc = via(hp, "POST", "/api/confirm/request", {"action": "hostname"}, host=host)
    assert status == 200 and doc["state"] == "pending", doc
    app.confirm.intercept([Event("k2", "tap")])
    return via(hp, "POST", "/api/hosts", {"confirm": doc["token"]}, host=host)


class TestValid:
    @pytest.mark.parametrize("host", ["radio.example.net", "hasler", "192.168.1.9", "::1", "a-b.c0"])
    def test_names_and_addresses(self, host):
        assert valid_host(host)

    @pytest.mark.parametrize("host", ["", "-x.net", "x_y.net", "a..b", "x.net/path", "x" * 254, "évil.net"])
    def test_anything_else(self, host):
        assert not valid_host(host)


class TestFlow:
    def test_a_refusal_offers_the_address(self, served):
        hp, _ = served
        status, doc = via(hp, "PUT", "/api/config", {})
        assert status == 403 and "not this radio's address" in doc["error"]
        assert doc["allow_host"] == DASH

    def test_no_offer_cross_site_or_without_origin(self, served):
        hp, _ = served
        status, doc = call(hp, "PUT", "/api/config", {}, {"Host": DASH, "Origin": "https://evil.example"})
        assert status == 403 and "allow_host" not in doc
        status, doc = via(hp, "PUT", "/api/config", {}, origin=False)
        assert status == 403 and "allow_host" not in doc

    def test_a_press_allows_it_and_then_changes_work(self, served):
        hp, app = served
        status, doc = allow(hp, app)
        assert status == 200 and doc["host"] == DASH
        status, doc = via(hp, "POST", "/api/stop", {})
        assert status == 200 and doc["ok"]
        listing = via(hp, "GET", "/api/hosts")[1]
        assert [h["host"] for h in listing["saved"]] == [DASH] and listing["this_allowed"] is True

    def test_without_the_press_nothing_is_allowed(self, served):
        hp, app = served
        status, doc = via(hp, "POST", "/api/confirm/request", {"action": "hostname"})
        status, doc = via(hp, "POST", "/api/hosts", {"confirm": doc["token"]})
        assert status == 403 and doc["confirm_required"]
        assert via(hp, "POST", "/api/stop", {})[0] == 403

    def test_only_the_address_the_page_is_open_at(self, served):
        hp, app = served
        status, doc = via(hp, "POST", "/api/confirm/request", {"action": "hostname"}, host="one.example")
        app.confirm.intercept([Event("k1", "tap")])
        # The token was for one.example: it can't allow two.example.
        status, doc = via(hp, "POST", "/api/hosts", {"confirm": doc["token"]}, host="two.example")
        assert status == 403
        assert via(hp, "GET", "/api/hosts", host=hp)[1]["saved"] == []

    def test_args_in_the_body_are_ignored(self, served):
        hp, app = served
        status, doc = via(hp, "POST", "/api/confirm/request",
                          {"action": "hostname", "args": {"host": "evil.example"}})
        assert app.confirm._requests[doc["token"]].args == {"host": DASH}

    def test_other_confirmations_stay_refused_from_there(self, served):
        hp, _ = served
        status, doc = via(hp, "POST", "/api/confirm/request", {"action": "update", "args": {"version": "9"}})
        assert status == 403 and "only be asked to allow" in doc["error"]

    def test_cancel_works_from_there(self, served):
        hp, _ = served
        token = via(hp, "POST", "/api/confirm/request", {"action": "hostname"})[1]["token"]
        assert via(hp, "DELETE", f"/api/confirm/{token}", {})[1]["ok"] is True

    def test_own_address_has_nothing_to_allow(self, served):
        hp, _ = served
        status, doc = call(hp, "POST", "/api/confirm/request", {"action": "hostname"},
                           {"Host": hp, "Origin": f"http://{hp}"})
        assert status == 400 and "already" in doc["error"]

    def test_remove_from_an_own_address(self, served):
        hp, app = served
        allow(hp, app)
        status, doc = call(hp, "DELETE", f"/api/hosts/{DASH}", {})
        assert status == 200 and doc["removed"] == DASH
        assert via(hp, "POST", "/api/stop", {})[0] == 403
        assert call(hp, "DELETE", f"/api/hosts/{DASH}", {})[0] == 400

    def test_box_yaml_names_are_listed_not_removable(self, served):
        hp, app = served
        app.allowed_hosts.add("box.example")
        listing = call(hp, "GET", "/api/hosts")[1]
        assert listing["box"] == ["box.example"] and listing["this_allowed"] is True
        status, doc = call(hp, "DELETE", "/api/hosts/box.example", {})
        assert status == 400 and "box.yaml" in doc["error"]

    def test_bench_flag_still_needs_a_press(self, tmp_path):
        app = make_app(tmp_path, allow_unconfirmed=True)
        req = app.confirm.request("hostname", {"host": DASH})
        assert req["state"] == "pending"
        assert app.confirm.request("reset", {})["state"] == "confirmed"     # the rest stays as it was


class TestStore:
    def test_survives_a_restart(self, tmp_path):
        AllowedHosts(tmp_path).add("Radio.Example.NET")
        again = AllowedHosts(tmp_path)
        assert again.names() == {"radio.example.net"}
        assert json.loads((tmp_path / "allowed-hosts.json").read_text())["hosts"][0]["host"] == "radio.example.net"

    def test_bad_file_starts_empty(self, tmp_path):
        (tmp_path / "allowed-hosts.json").write_text("{nope")
        assert AllowedHosts(tmp_path).names() == set()

    def test_at_most_ten(self, tmp_path):
        hosts = AllowedHosts(tmp_path)
        for n in range(10):
            hosts.add(f"h{n}.example")
        with pytest.raises(ValueError):
            hosts.add("eleven.example")
        hosts.add("h3.example")             # already there: fine
