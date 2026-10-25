"""The key page on the network: IPv6 and IPv4 on one socket, IPv4-mapped
addresses read as IPv4, IPv6 clients off this network dropped, and one
journal line per change."""

import ipaddress
import json
import logging
import time
import socket
from http.client import HTTPConnection

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import server as server_mod
from haslerd.web.server import WebApp, WebServer, local_peer, onlink_prefixes, plain_addr

BOX6 = "2a02:9130:8a1:4d00:dea6:32ff:fe12:3456"      # the radio's global address
SAME64 = "2a02:9130:8a1:4d00:1c2b:99ff:fe00:1"         # a phone on the same network
OTHER64 = "2a02:9130:8a1:4d01::1"                      # one prefix over
OUTSIDE = "2a03:2880:f12f:83:face:b00c::25de"          # anywhere else (a real global address)


@pytest.fixture(autouse=True)
def no_prefixes(monkeypatch, tmp_path):
    """The machine running the tests lends none of its own IPv6 prefixes."""
    monkeypatch.setattr(server_mod, "IF_INET6", tmp_path / "no-if_inet6")
    monkeypatch.setattr(server_mod, "_prefixes", {"at": None, "nets": {}})


def hexaddr(addr):
    return ipaddress.IPv6Address(addr).exploded.replace(":", "")


@pytest.fixture
def app(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    return WebApp(Actions(variant, FakePlayer()), panel, tmp_path / "keys.yaml",
                  {"radio": True, "spotify": True, "ytm": True}, ha=False)


def ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


def call(host, port, method, path, body=None, headers=None):
    conn = HTTPConnection(host, port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    conn.request(method, path, body=data, headers=hdrs)
    resp = conn.getresponse()
    text = resp.read()
    conn.close()
    return resp.status, text


class TestAddresses:
    @pytest.mark.parametrize("given, plain", [
        ("::ffff:192.168.88.20", "192.168.88.20"),
        ("::FFFF:127.0.0.1", "127.0.0.1"),
        ("192.168.88.20", "192.168.88.20"),
        ("::1", "::1"),
        ("0:0:0:0:0:0:0:1", "::1"),
        ("fe80::1%wlan0", "fe80::1"),
        ("hasler.local", "hasler.local"),
        ("", ""),
        (None, ""),
    ])
    def test_plain_addr(self, given, plain):
        assert plain_addr(given) == plain

    @pytest.mark.parametrize("peer", [
        "::ffff:127.0.0.1", "::1", "127.0.0.1", "::ffff:192.168.88.20", "192.168.88.20",
        "fe80::1c2b:99ff:fe00:1%wlan0", "fd12:3456:789a::5", SAME64,
    ])
    def test_this_network_may_connect(self, peer):
        assert local_peer(peer, BOX6)

    @pytest.mark.parametrize("peer, local", [
        (OTHER64, BOX6), (OUTSIDE, BOX6),
        (SAME64, "::ffff:192.168.88.177"),       # came in on IPv4: no /64 to be on
        (SAME64, "fe80::dea6:32ff:fe12:3456"),   # nor on a link-local one
        ("ff02::1", BOX6), ("::", BOX6), ("junk", BOX6), (None, BOX6),
    ])
    def test_ipv6_from_elsewhere_is_dropped(self, peer, local):
        assert not local_peer(peer, local)

    def test_the_boxs_real_prefixes_are_read(self, tmp_path):
        f = tmp_path / "if_inet6"
        f.write_text(
            f"{hexaddr('::1')} 01 80 10 80       lo\n"
            f"{hexaddr('fe80::dea6:32ff:fe12:3456')} 03 40 20 80    wlan0\n"
            f"{hexaddr(BOX6)} 03 38 00 00    wlan0\n"                         # a /56
            f"{hexaddr('2a01:4f8:1:2::99')} 02 80 00 00     eth0\n"            # DHCPv6 /128
            "garbage line\n")
        nets = onlink_prefixes(f)
        assert {str(n) for n in nets.values()} == {"2a02:9130:8a1:4d00::/56", "2a01:4f8:1:2::/64"}
        assert local_peer(OTHER64, BOX6, nets)                         # inside the /56
        assert local_peer("2a01:4f8:1:2::5", BOX6, nets)               # another on-link prefix
        assert not local_peer("2a02:9130:8a1:4e00::1", BOX6, nets)     # outside the /56
        assert not local_peer(OUTSIDE, BOX6, nets)

    def test_without_the_file_the_connected_address_is_a_64(self):
        assert onlink_prefixes() == {}
        assert local_peer(SAME64, BOX6) and not local_peer(OTHER64, BOX6)

    def test_prefixes_are_read_once_a_minute(self, tmp_path, monkeypatch):
        f = tmp_path / "if_inet6"
        monkeypatch.setattr(server_mod, "IF_INET6", f)
        f.write_text(f"{hexaddr(BOX6)} 03 40 00 00    wlan0\n")
        assert len(onlink_prefixes(now=1000.0)) == 1
        f.write_text("")
        assert len(onlink_prefixes(now=1059.0)) == 1      # still cached
        assert onlink_prefixes(now=1061.0) == {}

    def test_the_server_drops_it_before_reading(self, app):
        class Request:
            def __init__(self, local):
                self.local = local

            def getsockname(self):
                return (self.local, 8080, 0, 0)

        srv = server_mod._Server.__new__(server_mod._Server)
        assert srv.verify_request(Request(BOX6), (SAME64, 50000, 0, 0))
        assert srv.verify_request(Request("::ffff:192.168.88.177"), ("::ffff:192.168.88.20", 50000, 0, 0))
        assert not srv.verify_request(Request(BOX6), (OUTSIDE, 50000, 0, 0))

    @pytest.mark.parametrize("host, local", [
        ("[::1]:8080", "::1"),
        ("127.0.0.1:8080", "::ffff:127.0.0.1"),
        ("192.168.88.177:8080", "::ffff:192.168.88.177"),
        (f"[{BOX6}]:8080", BOX6),
        ("[2a02:9130:08a1:4d00:dea6:32ff:fe12:3456]:8080", BOX6),   # written out long
    ])
    def test_a_host_that_is_the_address_connected_to_is_the_box(self, app, host, local):
        assert app.request_refusal("POST", {"Host": host, "Content-Type": "application/json"}, local) is None

    @pytest.mark.parametrize("host, local", [
        (f"[{SAME64}]:8080", BOX6),
        ("192.168.88.20:8080", "::ffff:192.168.88.177"),
    ])
    def test_a_host_that_is_another_address_is_refused(self, app, host, local):
        why = app.request_refusal("POST", {"Host": host, "Content-Type": "application/json"}, local)
        assert why and "address" in why

    def test_the_hotspot_seen_through_a_dual_stack_socket_is_still_the_hotspot(self, app):
        joined = []
        app.wifi.join = lambda ssid, pwd, sec: joined.append(ssid)
        app.wifi_join({"ssid": "Home", "password": "12345678"}, "::ffff:172.24.1.1")
        assert joined == ["Home"]       # no key press asked for


class TestListening:
    @pytest.mark.skipif(not ipv6_loopback(), reason="no IPv6 here")
    def test_wildcard_listens_on_ipv6_and_ipv4(self, app, caplog):
        caplog.set_level(logging.INFO, logger="haslerd.web.server")
        server = WebServer(app, host="0.0.0.0", port=0)
        server.start()
        try:
            assert server._server.address_family == socket.AF_INET6
            assert f"key page on http://[::]:{server.port}/ (IPv6 and IPv4)" in caplog.text
            for host in ("127.0.0.1", "::1"):
                status, body = call(host, server.port, "GET", "/api/config")
                assert status == 200 and json.loads(body)["variant"] == "hasler-hm"
            assert call("::1", server.port, "POST", "/api/stop", {}, {"Host": f"[::1]:{server.port}"})[0] == 200
            # IPv4 through the IPv6 socket is still loopback: the canary's check passes it
            seen = []
            app.ytdlp_canary = lambda body, client: (seen.append(client), (200, {"ok": True}))[1]
            assert call("127.0.0.1", server.port, "POST", "/api/components/yt-dlp/canary", {},
                        {"Host": f"127.0.0.1:{server.port}"})[0] == 200
            assert seen == ["127.0.0.1"]
        finally:
            server.close()

    def test_falls_back_to_ipv4_when_ipv6_will_not_bind(self, app, monkeypatch, caplog):
        def no_ipv6(self):
            raise OSError(97, "Address family not supported by protocol")
        monkeypatch.setattr(server_mod._DualStackServer, "server_bind", no_ipv6)
        caplog.set_level(logging.INFO, logger="haslerd.web.server")
        server = WebServer(app, host="", port=0)
        server.start()
        try:
            assert server._server.address_family == socket.AF_INET
            assert f"key page on http://0.0.0.0:{server.port}/ (IPv4 only, no IPv6: " in caplog.text
            assert call("127.0.0.1", server.port, "GET", "/api/config")[0] == 200
        finally:
            server.close()

    def test_a_named_host_listens_there_only(self, app):
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        try:
            assert server._server.address_family == socket.AF_INET
            assert server._server.server_address[0] == "127.0.0.1"
        finally:
            server.close()


class TestJournal:
    @pytest.fixture
    def port(self, app):
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        yield server.port
        server.close()

    def lines(self, caplog, wait_s=1.0):
        # The line is written just after the answer goes out: give the server
        # thread a moment (a fast CI box reads the answer first).
        end = time.monotonic() + wait_s
        while True:
            got = [r.getMessage() for r in caplog.records
                   if r.name == "haslerd.web.server" and r.levelno == logging.INFO
                   and r.getMessage().startswith("web: ")]
            if got or time.monotonic() >= end:
                time.sleep(0.05)
                return [r.getMessage() for r in caplog.records
                        if r.name == "haslerd.web.server" and r.levelno == logging.INFO
                        and r.getMessage().startswith("web: ")]
            time.sleep(0.02)

    def test_a_change_is_one_line_without_body_or_query(self, port, caplog):
        caplog.set_level(logging.INFO, logger="haslerd.web.server")
        status, _ = call("127.0.0.1", port, "POST", "/api/stop?token=hunter2", {"password": "s3cret-pass"},
                         {"Host": f"127.0.0.1:{port}"})
        assert status == 200
        lines = self.lines(caplog)
        assert len(lines) == 1
        assert lines[0].startswith("web: POST /api/stop from 127.0.0.1 -> 200 in ")
        assert lines[0].endswith(" ms")
        assert "hunter2" not in caplog.text and "s3cret" not in caplog.text

    def test_a_refused_change_logs_its_status(self, port, caplog):
        caplog.set_level(logging.INFO, logger="haslerd.web.server")
        assert call("127.0.0.1", port, "PUT", "/api/settings", {}, {"Host": "evil.example"})[0] == 403
        assert any(line.startswith("web: PUT /api/settings from 127.0.0.1 -> 403") for line in self.lines(caplog))

    def test_reads_are_not_logged(self, port, caplog):
        caplog.set_level(logging.INFO, logger="haslerd.web.server")
        for _ in range(3):
            assert call("127.0.0.1", port, "GET", "/api/status")[0] == 200
        assert self.lines(caplog) == []

    def test_a_confirm_id_in_the_path_is_not_logged(self, port, caplog):
        caplog.set_level(logging.INFO, logger="haslerd.web.server")
        token = "Ab3dEfGhIjKlMnOpQrStUvWx"
        call("127.0.0.1", port, "DELETE", f"/api/confirm/{token}", None, {"Host": f"127.0.0.1:{port}"})
        assert token not in caplog.text
        assert any(line.startswith("web: DELETE /api/confirm/… from 127.0.0.1") for line in self.lines(caplog))

    def test_a_client_that_stalls_mid_body_is_one_aborted_line(self, app, caplog):
        caplog.set_level(logging.INFO, logger="haslerd.web.server")
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        server._server.RequestHandlerClass.timeout = 0.3
        try:
            with socket.create_connection(("127.0.0.1", server.port), timeout=5) as s:
                s.sendall(b"POST /api/press HTTP/1.0\r\nHost: 127.0.0.1\r\n"
                          b"Content-Type: application/json\r\nContent-Length: 50\r\n\r\n{\"key\":")
                assert s.recv(1024) == b""          # closed, nothing answered
        finally:
            server.close()
        lines = self.lines(caplog)
        assert len(lines) == 1 and lines[0].startswith("web: POST /api/press from 127.0.0.1 aborted after ")
        assert lines[0].endswith("TimeoutError")
        assert "web request failed" not in caplog.text and "Traceback" not in caplog.text
