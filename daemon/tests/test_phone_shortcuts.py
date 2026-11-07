"""Phone shortcuts (web/phone_api.py): the stable API, the request guard as an
iPhone's Shortcuts app meets it, the Android /do/* pages, and the hosted files."""

import http.client
import json
import os
import plistlib
from pathlib import Path

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import phone_api
from haslerd.web.server import WebApp, WebServer

ROOT = Path(__file__).resolve().parents[2]
UNSIGNED = ROOT / "tools" / "shortcuts" / "unsigned"
SITE = Path(os.environ.get("HASLER_SITE") or ROOT.parent / "delgado-murphy.github.io") / "shortcuts"
CONFIRM_GATED = ("/api/software", "/api/wifi", "/api/reset", "/api/restart", "/api/handover", "/api/hosts",
                 "/api/confirm", "/api/settings", "/api/config")


@pytest.fixture
def radio(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    actions = Actions(variant, player)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False)
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield server.port, app, actions, player
    server.close()


def send(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    out = resp.status, resp.read()
    conn.close()
    return out


# What "Get Contents of URL" (POST, Request Body: JSON) sends: no Origin, no Referer.
IOS = {"Host": "hasler.local", "Content-Type": "application/json", "Accept": "*/*",
       "User-Agent": "BackgroundShortcutRunner/2607.0.4 CFNetwork/3826 Darwin/25.0.0"}


class TestApi:
    def test_the_table_only_calls_everyday_endpoints(self):
        assert phone_api.PATHS == {"/api/volume", "/api/press", "/api/standby", "/api/playpause", "/api/next"}
        for a in phone_api.ACTIONS.values():
            assert a["path"] in phone_api.PATHS
            assert a["body"], "iOS sends a JSON body (and its Content-Type) only with a field"
        assert not any(p.startswith(CONFIRM_GATED) for p in phone_api.PATHS)
        assert [a for a in phone_api.ACTIONS if a.startswith("key/")] == [f"key/{n}" for n in range(1, 7)]

    def test_an_iphone_shortcut_turns_it_up_and_down(self, radio):
        port, _, _, player = radio
        player.set_volume(40)
        status, body = send(port, "POST", "/api/volume", {"delta": 5}, IOS)
        assert status == 200 and json.loads(body)["volume"] == 45
        status, body = send(port, "POST", "/api/volume", {"delta": -5}, IOS)
        assert status == 200 and json.loads(body)["volume"] == 40

    def test_every_action_in_the_table_is_accepted_as_ios_sends_it(self, radio):
        port, *_ = radio
        for aid, a in phone_api.ACTIONS.items():
            status, body = send(port, "POST", a["path"], a["body"], IOS)
            assert status == 200, (aid, body)

    def test_the_radio_s_ip_works_too(self, radio):
        port, *_ = radio
        status, _ = send(port, "POST", "/api/next", {"via": "shortcut"}, {**IOS, "Host": "127.0.0.1"})
        assert status == 200

    def test_another_site_is_refused(self, radio):
        port, _, _, player = radio
        player.set_volume(40)
        status, body = send(port, "POST", "/api/volume", {"delta": 5}, {**IOS, "Origin": "https://evil.example"})
        assert status == 403 and "cross-site" in json.loads(body)["error"]
        status, _ = send(port, "POST", "/api/volume", {"delta": 5}, {**IOS, "Host": "evil.example"})
        assert status == 403
        status, _ = send(port, "POST", "/api/volume", {"delta": 5}, {**IOS, "Content-Type": "text/plain"})
        assert status == 403
        assert player.get_volume() == 40

    def test_play_pause_stops_resumes_and_wakes(self, radio):
        port, app, actions, player = radio
        status, body = send(port, "POST", "/api/press", {"key": "k1", "event": "tap"}, IOS)
        assert status == 200
        actions.player.current = lambda: object()          # something plays
        status, body = send(port, "POST", "/api/playpause", {"via": "shortcut"}, IOS)
        assert status == 200 and json.loads(body)["playing"] is False and ("stop",) in player.calls
        actions.standby = True
        woke = []
        actions._standby_toggle = lambda ev, spec: (woke.append(1), setattr(actions, "standby", False))
        status, body = send(port, "POST", "/api/playpause", {"via": "shortcut"}, IOS)
        assert status == 200 and json.loads(body)["playing"] is True and woke == [1]

    def test_the_card_s_listing_points_at_this_radio(self, radio):
        port, *_ = radio
        status, body = send(port, "GET", "/api/phone-shortcuts", headers={"Host": "kitchen.local:8080"})
        doc = json.loads(body)
        assert status == 200 and doc["step"] == phone_api.STEP
        louder = next(a for a in doc["actions"] if a["id"] == "louder")
        assert louder["link"] == "http://kitchen.local:8080/do/louder" and louder["file"] == "louder.shortcut"
        status, body = send(port, "GET", "/api/phone-shortcuts", headers={"Host": "a b"})
        assert json.loads(body)["actions"][0]["link"].startswith("http://hasler.local/")


class TestActionPages:
    def test_get_only_sends_the_page(self, radio):
        port, _, _, player = radio
        player.set_volume(40)
        before = list(player.calls)
        for aid in phone_api.ACTIONS:
            status, page = send(port, "GET", "/do/" + aid, headers={"Host": "hasler.local"})
            assert status == 200, aid
            text = page.decode()
            assert f"<title>{phone_api.ACTIONS[aid]['name']}</title>" in text
            assert f"/icons/do-{phone_api.slug(aid)}.png" in text
            assert 'rel="manifest"' not in text      # its own name and icon, not the key page's
        assert player.calls == before and player.get_volume() == 40

    def test_the_page_posts_the_same_call(self, radio):
        port, *_ = radio
        _, page = send(port, "GET", "/do/key/3")
        call = json.loads(page.decode().split('id="call">')[1].split("</script>")[0])
        assert call["path"] == "/api/press" and call["body"] == {"key": "k3", "event": "tap"}

    def test_unknown_actions_and_icons(self, radio):
        port, *_ = radio
        assert send(port, "GET", "/do/reset")[0] == 404
        assert send(port, "GET", "/do/")[0] == 404
        assert send(port, "POST", "/do/louder", {}, IOS)[0] == 404
        for aid in phone_api.ACTIONS:
            status, png = send(port, "GET", f"/icons/do-{phone_api.slug(aid)}.png")
            assert status == 200 and png[:4] == b"\x89PNG", aid


def _calls(path: Path) -> tuple[str, str, dict]:
    doc = plistlib.loads(path.read_bytes())
    acts = doc["WFWorkflowActions"]
    assert len(acts) == 1 and acts[0]["WFWorkflowActionIdentifier"] == "is.workflow.actions.downloadurl"
    p = acts[0]["WFWorkflowActionParameters"]
    body = {}
    for item in p["WFJSONValues"]["Value"]["WFDictionaryFieldValueItems"]:
        key = item["WFKey"]["Value"]["string"]
        raw = item["WFValue"]["Value"]
        val = raw["string"] if isinstance(raw, dict) else raw
        body[key] = int(val) if item["WFItemType"] == 3 else val
    return p["WFURL"], p["WFHTTPMethod"], body


class TestHostedShortcuts:
    def test_every_hosted_shortcut_calls_a_whitelisted_endpoint(self):
        files = sorted(UNSIGNED.glob("*.shortcut"))
        assert len(files) == len(phone_api.ACTIONS)
        by_name = {a["name"].replace("/", "-"): a for a in phone_api.ACTIONS.values()}
        for f in files:
            url, method, body = _calls(f)
            assert url.startswith("http://hasler.local/") and method == "POST" and body, f.name
            path = url[len("http://hasler.local"):]
            assert path in phone_api.PATHS, f.name
            a = by_name[f.stem]
            assert (path, body) == (a["path"], a["body"]), f.name

    def test_the_committed_files_are_what_the_table_makes(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("make_shortcuts", ROOT / "tools" / "shortcuts" / "make_shortcuts.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for aid in phone_api.ACTIONS:
            want = plistlib.dumps(mod.workflow(aid), fmt=plistlib.FMT_BINARY, sort_keys=True)
            assert (UNSIGNED / mod.file_name(aid)).read_bytes() == want, "run tools/shortcuts/make_shortcuts.py"

    @pytest.mark.skipif(not SITE.is_dir(), reason="the org site repo isn't checked out beside this one")
    def test_the_site_hosts_one_signed_file_per_shortcut(self):
        signed = sorted(p.name for p in SITE.glob("*.shortcut"))
        assert signed == sorted(p.name for p in UNSIGNED.glob("*.shortcut"))
        for name in signed:
            assert (SITE / name).read_bytes()[:4] == b"AEA1", name     # signed (Apple Encrypted Archive)
