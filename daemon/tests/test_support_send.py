"""Send to the makers (web/support_api.py): the report goes to the makers' check-in
server (stubbed here) with the check-in's token, nothing private in it; the request's
number is kept on the card; the makers' /owner replies come back as notes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from haslerd.web import notes_api, support_api
from haslerd.web.server import WebServer

from test_support import SECRETS, rig  # noqa: F401  (the fixture)

BOX_YAML = ("schema: 1\nvariant: hasler-hm\nbox_id: 0f3a7b1e-1111-4222-8333-444455556666\n"
            "checkin_url: https://ci.example/v1/checkin\ncheckin_token: the-word\nmakers:\n  contact: hola@example.org\n")


class Server:
    """The makers' Worker, as the radio sees it."""

    def __init__(self):
        self.calls, self.number, self.status, self.replies, self.down = [], 12, 200, [], False

    def __call__(self, method, url, body, token, timeout=15):
        self.calls.append((method, url, body, token))
        if self.down:
            raise urllib.error.URLError("name or service not known")
        if url.endswith("/v1/report"):
            if self.status != 200:
                return self.status, {"ok": False}
            n, self.number = self.number, self.number + 1
            return 200, {"ok": True, "number": n, "deduped": False}
        if "/v1/replies?" in url:
            return 200, {"replies": list(self.replies)}
        if url.endswith("/v1/replies/ack"):
            self.replies = [r for r in self.replies if r["id"] not in body["ids"]]
            return 204, None
        return 404, None


@pytest.fixture
def sendable(rig):  # noqa: F811
    app, actions, player, panel, fake, state = rig
    app.box_paths.box_file.write_text(BOX_YAML)
    srv = Server()
    api = support_api.SupportApi(app, state, transport=srv, clock=lambda: 1794045600.0)
    return app, api, srv, state


def test_base_url():
    assert support_api.base_url("https://ci.example/v1/checkin") == "https://ci.example"
    assert support_api.base_url("https://ci.example/hasler/v1/checkin/ ") == "https://ci.example/hasler"
    assert support_api.base_url("") == ""


def test_send_posts_the_stripped_report_with_the_token_and_keeps_the_number(sendable):
    app, api, srv, state = sendable
    assert api.doc() == {"available": True, "requests": []}
    status, doc = api.route("POST", "/api/support/send", {"note": "It goes quiet after a few minutes", "wifi": False})
    assert status == 200 and doc["ok"] and doc["number"] == 12 and doc["deduped"] is False
    method, url, body, token = srv.calls[0]
    assert (method, url, token) == ("POST", "https://ci.example/v1/report", "the-word")
    assert body["box_id"] == "0f3a7b1e-1111-4222-8333-444455556666" and len(body["short_id"]) == 4
    assert body["note"] == "It goes quiet after a few minutes" and 'Your note: "It goes quiet' in body["text"]
    assert body["json"]["radio"]["short_id"] == body["short_id"]
    blob = json.dumps(body, ensure_ascii=False)
    for secret in SECRETS + ("Casa", "the-word"):     # wifi left out; the token goes as a header only
        assert secret not in blob, secret
    assert len(json.dumps(body).encode()) <= support_api.MAX_BYTES
    kept = json.loads((state / "support-requests.json").read_text())
    assert kept["requests"][0]["number"] == 12 and kept["requests"][0]["at"].startswith("2026-")
    api.route("POST", "/api/support/send", {"note": "And again"})
    assert [r["number"] for r in api.doc()["requests"]] == [13, 12]
    assert srv.calls[1][2]["json"]["network"]["wifi"] == "Casa"        # wifi on by default, as on the sheet


def test_send_refusals_say_what_to_do_instead(sendable):
    app, api, srv, state = sendable
    srv.status = 429
    status, doc = api.route("POST", "/api/support/send", {"note": "x"})
    assert status == 429 and "Share or Copy" in doc["error"]
    srv.status = 403
    assert api.route("POST", "/api/support/send", {})[0] == 502
    srv.status, srv.down = 200, True
    with pytest.raises(support_api.Unavailable):
        api.send({"note": "x"})
    assert not (state / "support-requests.json").exists()
    # no checkin_url: not available, and a send is a 409
    app.box_paths.box_file.write_text("schema: 1\nvariant: hasler-hm\nbox_id: b-1\n")
    assert api.doc()["available"] is False
    assert api.route("POST", "/api/support/send", {})[0] == 409
    assert api.route("GET", "/api/support/send", None)[0] == 405


def test_too_big_a_report_goes_as_text_alone(sendable, monkeypatch):
    app, api, srv, state = sendable
    monkeypatch.setattr(support_api, "MAX_BYTES", 800)
    body = api.payload("x", True)
    assert body["json"] is None and body["text"]


def test_replies_become_notes_from_the_makers_once(sendable):
    app, api, srv, state = sendable
    srv.replies = [{"id": 901, "at": "2026-11-07T10:00:00Z", "text": "A new card is in the post.\nLeave it plugged in.", "issue": 12}]
    # nothing sent yet: nothing asked
    assert api.pull_replies() == {"added": 0} and srv.calls == []
    api.send({"note": "x"})
    assert api.route("POST", "/api/support/replies", {}) == (200, {"ok": True, "added": 1})
    assert srv.calls[-2][1] == "https://ci.example/v1/replies?box_id=0f3a7b1e-1111-4222-8333-444455556666"
    assert srv.calls[-1][1:3] == ("https://ci.example/v1/replies/ack", {"box_id": "0f3a7b1e-1111-4222-8333-444455556666", "ids": [901]})
    notes = notes_api.NotesApi(app, state).doc()
    assert notes["unseen"] == 1
    assert notes["notes"][0]["by"] == "makers" and notes["notes"][0]["text"] == "A new card is in the post. Leave it plugged in."
    # the server didn't get the ack and serves it again: still one note
    srv.replies = [{"id": 901, "at": "2026-11-07T10:00:00Z", "text": "A new card is in the post.", "issue": 12}]
    assert api.pull_replies()["added"] == 0
    assert len(notes_api.read_notes(state)) == 1
    srv.down = True
    assert api.pull_replies()["added"] == 0


def test_support_over_http(sendable):
    app, api, srv, state = sendable
    app.extra_routes.append(api)
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with urllib.request.urlopen(base + "/api/support", timeout=5) as r:
            assert json.loads(r.read()) == {"available": True, "requests": []}
        req = urllib.request.Request(base + "/api/support/send", data=b'{"note": "hello", "wifi": true}', method="POST",
                                     headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{server.port}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            doc = json.loads(r.read())
        assert doc["ok"] and doc["number"] == 12 and doc["requests"][0]["number"] == 12
        srv.down = True
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/support/send", data=b"{}", method="POST",
                                                          headers={"Content-Type": "application/json"}), timeout=10)
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503 and "Share or Copy" in json.loads(e.read())["error"]
    finally:
        server.close()
