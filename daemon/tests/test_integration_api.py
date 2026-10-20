"""What the Home Assistant integration reads: /api/info, /api/art/current, status.art."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import integration
from haslerd.web.integration import ArtProxy
from haslerd.web.server import WebApp, WebServer

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


@pytest.fixture
def library(tmp_path):
    """A moOde station table and logo folder holding the hasler-hm k3 preset's station."""
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    actions = Actions(variant, FakePlayer())
    k3 = variant.actions["k3.tap"]["n"]
    uri = actions.presets[k3].uri
    db = tmp_path / "moode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cfg_radio (id INTEGER PRIMARY KEY, station TEXT, name TEXT, logo TEXT)")
    con.execute("INSERT INTO cfg_radio (station, name, logo) VALUES (?, ?, 'local')", (uri, "Radio Tres"))
    con.execute("INSERT INTO cfg_radio (station, name, logo) VALUES ('http://x/../evil', '../etc/passwd', 'local')")
    con.commit()
    con.close()
    logos = tmp_path / "logos"
    logos.mkdir()
    (logos / "Radio Tres.jpg").write_bytes(JPEG)
    return db, logos, uri


@pytest.fixture
def rig(tmp_path, library):
    db, logos, _ = library
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    actions = Actions(variant, player)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False, station_db=str(db))
    app.art = ArtProxy(str(db), logos)
    app.machine_id_file = tmp_path / "machine-id"
    app.machine_id_file.write_text("0123456789abcdef0123456789abcdef\n")
    return app, actions, player


class TestInfo:
    def test_info(self, rig):
        app, *_ = rig
        doc = app.info()
        assert doc["id"] == hashlib.sha256(b"0123456789abcdef0123456789abcdef").hexdigest()[:12]
        assert doc["variant"] == "hasler-hm" and doc["api"] == 1
        assert isinstance(doc["version"], str) and doc["version"]
        assert doc["name"] and "." not in doc["name"]
        assert set(doc) == {"id", "name", "variant", "version", "api"}

    def test_id_without_machine_id_is_still_stable(self, tmp_path):
        assert integration.radio_id(tmp_path / "nope") == integration.radio_id(tmp_path / "nope")
        assert len(integration.radio_id(tmp_path / "nope")) == 12


class TestArt:
    def test_station_logo_from_moodes_library(self, rig):
        app, actions, _ = rig
        assert app.status()["art"] is None
        actions.dispatch(Event("k3", "tap"))
        art = app.status()["art"]
        assert art.startswith("/api/art/current?k=") and len(art) == len("/api/art/current?k=") + 10
        data, ctype, etag = app.art_current()
        assert data == JPEG and ctype == "image/jpeg" and etag == f'"{art.split("=")[1]}"'

    def test_no_logo_no_art(self, rig):
        app, *_ = rig
        assert app.art.locate("radio", {"uri": "http://not-in-moode/stream"}, None) is None
        assert app.art.locate("radio", {"uri": "http://x/../evil"}, None) is None    # name with a slash: ignored
        assert app.art.locate("spotify", {"uri": "spotify:album:x"}, None) is None

    def test_standby_has_no_art(self, rig):
        app, actions, _ = rig
        actions.dispatch(Event("k3", "tap"))
        actions.standby = True
        assert app.status()["art"] is None and app.art_current() is None

    def test_ytm_thumbnail_fetched_once(self, library):
        db, logos, _ = library
        fetched = []

        def fetch(url, timeout):
            fetched.append(url)
            return JPEG, "image/jpeg; charset=binary"
        art = ArtProxy(str(db), logos, fetch=fetch)
        track = {"title": "T", "thumb": "https://lh3.googleusercontent.com/abc=w544-h544"}
        url = art.url_for("ytm", None, track)
        assert url and url.startswith("/api/art/current?k=")
        assert art.current("ytm", None, track)[:2] == (JPEG, "image/jpeg")
        assert art.current("ytm", None, track)[:2] == (JPEG, "image/jpeg")
        assert fetched == [track["thumb"]]
        assert art.url_for("ytm", None, {"thumb": ""}) is None

    def test_remote_that_isnt_a_picture_is_refused(self, library):
        db, logos, _ = library
        art = ArtProxy(str(db), logos, fetch=lambda url, t: (b"<html>", "text/html"))
        assert art.current("ytm", None, {"thumb": "https://example.com/x"}) is None
        big = ArtProxy(str(db), logos, fetch=lambda url, t: (b"x" * (integration.ART_MAX_BYTES + 1), "image/jpeg"))
        assert big.current("ytm", None, {"thumb": "https://example.com/y"}) is None

        def boom(url, t):
            raise OSError("down")
        assert ArtProxy(str(db), logos, fetch=boom).current("ytm", None, {"thumb": "https://e.com/z"}) is None

    def test_missing_station_db_is_quiet(self, tmp_path):
        art = ArtProxy(str(tmp_path / "none.db"), tmp_path)
        assert art.url_for("radio", {"uri": "http://stream"}, None) is None


class TestHttp:
    def call(self, url, headers=None):
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def test_info_and_art_over_http(self, rig):
        app, actions, _ = rig
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        base = f"http://127.0.0.1:{server.port}"
        try:
            status, _, body = self.call(base + "/api/info")
            assert status == 200 and json.loads(body)["api"] == 1
            assert self.call(base + "/api/art/current")[0] == 404
            actions.dispatch(Event("k3", "tap"))
            status, headers, body = self.call(base + "/api/art/current")
            assert status == 200 and body == JPEG and headers["Content-Type"] == "image/jpeg"
            assert headers["Cache-Control"] == "no-cache"
            status, _, body = self.call(base + "/api/art/current", {"If-None-Match": headers["ETag"]})
            assert status == 304 and body == b""
        finally:
            server.close()
