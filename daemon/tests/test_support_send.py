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
        self.tokens, self.enrol_status, self.minted = set(), 200, 0

    def __call__(self, method, url, body, token, timeout=15):
        self.calls.append((method, url, body, token))
        if self.down:
            raise urllib.error.URLError("name or service not known")
        if url.endswith("/v1/enrol"):
            if self.enrol_status != 200:
                return self.enrol_status, {"ok": False}
            self.minted += 1
            tok = f"radio-word-{self.minted}"
            self.tokens.add(tok)
            return 200, {"ok": True, "token": tok}
        if url.endswith("/v1/report"):
            if self.tokens and token != "the-word" and token not in self.tokens:
                return 403, {"ok": False}
            if self.status != 200:
                return self.status, {"ok": False}
            n, self.number = self.number, self.number + 1
            return 200, {"ok": True, "number": n, "deduped": False}
        if "/v1/replies?" in url:
            return 200, {"replies": list(self.replies)}
        if url.endswith("/v1/reply"):
            if self.status != 200:
                return self.status, {"ok": False}
            return 200, {"ok": True, "number": body["issue"], "reopened": False}
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
    # box.yaml may give the root or the full check-in route: the same server either way
    for given in ("https://ci.example", "https://ci.example/", "https://ci.example/v1/checkin"):
        assert [support_api.endpoint(given, r) for r in ("checkin", "report", "replies", "enrol")] == [
            "https://ci.example/v1/checkin", "https://ci.example/v1/report", "https://ci.example/v1/replies", "https://ci.example/v1/enrol"]
    assert support_api.endpoint("", "report") == ""
    assert support_api.DEFAULT_URL == support_api.base_url(support_api.DEFAULT_URL)     # the release default is the root


def test_send_posts_the_stripped_report_with_the_token_and_keeps_the_number(sendable):
    app, api, srv, state = sendable
    assert api.doc() == {"available": True, "requests": [], "pending": [], "unseen_replies": 0}
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
    assert kept["requests"][0]["title"] == "It goes quiet after a few minutes"
    assert kept["requests"][0]["note"] == "It goes quiet after a few minutes"
    assert not any(c[1].endswith("/v1/enrol") for c in srv.calls)     # the shared word: no enrolment
    api.route("POST", "/api/support/send", {"note": "And again"})
    assert [r["number"] for r in api.doc()["requests"]] == [13, 12]
    assert srv.calls[1][2]["json"]["network"]["wifi"] == "Casa"        # wifi on by default, as on the sheet


def test_send_refusals_say_what_to_do_instead(sendable):
    app, api, srv, state = sendable
    srv.status = 429
    status, doc = api.route("POST", "/api/support/send", {"note": "x"})
    assert status == 429 and "Share or Email" in doc["error"]
    srv.status = 403
    assert api.route("POST", "/api/support/send", {})[0] == 502
    srv.status = 500
    with pytest.raises(support_api.Unavailable):
        api.send({"note": "x"})
    assert not (state / "support-requests.json").exists()
    # checkin_url: "" switches sending off: not available, and a send is a 409
    app.box_paths.box_file.write_text('schema: 1\nvariant: hasler-hm\nbox_id: b-1\ncheckin_url: ""\n')
    assert api.doc()["available"] is False
    status, doc = api.route("POST", "/api/support/send", {})
    assert status == 409 and "is off here" in doc["error"]
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
    kept = notes_api.read_notes(state)
    assert kept[0]["by"] == "makers" and kept[0]["text"] == "A new card is in the post.\nLeave it plugged in."
    # the reply shows once, in its request's thread: "From the makers" (GET /api/notes) leaves it out
    assert api.doc()["unseen_replies"] == 1
    assert notes_api.NotesApi(app, state).doc() == {"notes": [], "unseen": 0}
    # the server didn't get the ack and serves it again: still one note
    srv.replies = [{"id": 901, "at": "2026-11-07T10:00:00Z", "text": "A new card is in the post.", "issue": 12}]
    assert api.pull_replies()["added"] == 0
    assert len(notes_api.read_notes(state)) == 1
    srv.down = True
    assert api.pull_replies()["added"] == 0


def test_the_issues_state_reaches_the_request_and_the_notes(sendable):
    app, api, srv, state = sendable
    api.send({"note": "x"})                       # #12
    api.send({"note": "y"})                       # #13
    assert [r["state"] for r in api.doc()["requests"]] == ["sent", "sent"]
    srv.replies = [{"id": "seen:12", "at": "2026-11-08T09:00:00Z", "text": "The makers have seen your request #12.", "issue": 12, "state": "seen"}]
    assert api.pull_replies()["added"] == 1
    assert {r["number"]: r["state"] for r in api.doc()["requests"]} == {12: "seen", 13: "sent"}
    srv.replies = [{"id": "closed:12:t", "at": "t", "text": "Your request #12 is done. The new card is in the post.", "issue": 12, "state": "done"},
                   {"id": "seen:13", "at": "t", "text": "The makers have seen your request #13.", "issue": 13, "state": "seen"}]
    api.pull_replies()
    assert {r["number"]: r["state"] for r in api.doc()["requests"]} == {12: "done", 13: "seen"}
    # a late "seen" never undoes done; reopening does
    srv.replies = [{"id": "seen:12b", "text": "The makers have seen your request #12.", "issue": 12, "state": "seen"}]
    api.pull_replies()
    assert api.doc()["requests"][1]["state"] == "done"
    srv.replies = [{"id": "reopened:12:t2", "text": "The makers reopened your request #12.", "issue": 12, "state": "seen"}]
    api.pull_replies()
    assert api.doc()["requests"][1]["state"] == "seen"
    texts = [n["text"] for n in notes_api.read_notes(state)]
    assert "Your request #12 is done. The new card is in the post." in texts and texts[0] == "The makers reopened your request #12."
    assert all(n["by"] == "makers" for n in notes_api.read_notes(state))
    # sent again (the same issue): the state stays
    srv.number = 13
    api.send({"note": "y"})
    assert api.doc()["requests"][0] == {**api.doc()["requests"][0], "number": 13, "state": "seen"}
    assert [r["number"] for r in api.doc()["requests"]] == [13, 12]


def test_support_over_http(sendable):
    app, api, srv, state = sendable
    app.extra_routes.append(api)
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with urllib.request.urlopen(base + "/api/support", timeout=5) as r:
            assert json.loads(r.read()) == {"available": True, "requests": [], "pending": [], "unseen_replies": 0}
        req = urllib.request.Request(base + "/api/support/send", data=b'{"note": "hello", "wifi": true}', method="POST",
                                     headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{server.port}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            doc = json.loads(r.read())
        assert doc["ok"] and doc["number"] == 12 and doc["requests"][0]["number"] == 12
        srv.status = 502
        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/support/send", data=b"{}", method="POST",
                                                          headers={"Content-Type": "application/json"}), timeout=10)
            raise AssertionError("expected 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503 and "Try again" in json.loads(e.read())["error"]
        srv.status, srv.down = 200, True       # offline: it waits, and says so
        with urllib.request.urlopen(urllib.request.Request(base + "/api/support/send", data=b'{"note": "later"}', method="POST",
                                                           headers={"Content-Type": "application/json"}), timeout=10) as r:
            doc = json.loads(r.read())
        assert doc["ok"] and doc["queued"] and doc["pending"][0]["title"] == "later"
    finally:
        server.close()


# -- the support line out of the box: the default server, the radio's own word, the outbox --------

PLAIN_BOX = "schema: 1\nvariant: hasler-hm\nbox_id: 0f3a7b1e-1111-4222-8333-444455556666\n"


@pytest.fixture
def plain(rig):  # noqa: F811
    """A radio as hasler-install leaves it: no checkin_url, no checkin_token."""
    app, actions, player, panel, fake, state = rig
    app.box_paths.box_file.write_text(PLAIN_BOX)
    srv = Server()
    api = support_api.SupportApi(app, state, transport=srv, clock=lambda: 1794045600.0)
    return app, api, srv, state


def _secret(state):
    for p in (state / "secrets" / "support.json", state / "support.json"):
        if p.exists():
            return p
    return None


def test_every_radio_can_send_to_the_makers_default_server_with_its_own_word(plain):
    app, api, srv, state = plain
    assert api.doc()["available"] is True                     # nothing in box.yaml, and it can send
    base = support_api.base_url(support_api.DEFAULT_URL)
    status, doc = api.route("POST", "/api/support/send", {"note": "No sound\nsince the storm"})
    assert status == 200 and doc["number"] == 12
    assert [c[1] for c in srv.calls] == [base + "/v1/enrol", base + "/v1/report"]
    assert srv.calls[0][2] == {"schema": 1, "box_id": "0f3a7b1e-1111-4222-8333-444455556666"} and srv.calls[0][3] == ""
    assert srv.calls[1][3] == "radio-word-1"
    secret = _secret(state)
    kept = json.loads(secret.read_text())
    assert kept["token"] == "radio-word-1" and kept["server"] == base and oct(secret.stat().st_mode & 0o777) == "0o600"
    assert "radio-word-1" not in json.dumps(srv.calls[1][2])  # a header, never in the report
    assert api.doc()["requests"][0]["title"] == "No sound"
    # the second send: no new enrolment
    api.send({"note": "again"})
    assert [c[1].rsplit("/", 1)[1] for c in srv.calls].count("enrol") == 1 and srv.calls[-1][3] == "radio-word-1"
    # the replies go with the radio's word too
    api.pull_replies()
    assert "/v1/replies?" in srv.calls[-1][1] and srv.calls[-1][3] == "radio-word-1"


def test_a_lost_word_is_asked_for_once_more_and_a_known_radio_says_what_to_do(plain):
    app, api, srv, state = plain
    api.send({"note": "x"})
    srv.tokens = {"something-else"}                           # the makers cleared it on the server
    status, doc = api.route("POST", "/api/support/send", {"note": "y"})
    assert status == 200 and srv.calls[-1][3] == "radio-word-2"
    # a radio the server knows already (reinstalled without its secrets): a plain answer, nothing kept
    _secret(state).unlink()
    srv.enrol_status = 409
    status, doc = api.route("POST", "/api/support/send", {"note": "z"})
    assert status == 502 and "reset" in doc["error"] and "Email" in doc["error"]
    assert api.pending() == []
    # an older server without /v1/enrol: sends without a word, as radios always did
    srv.enrol_status, srv.tokens = 404, set()
    assert api.route("POST", "/api/support/send", {"note": "w"})[0] == 200 and srv.calls[-1][3] == ""


def test_offline_the_request_waits_and_goes_when_the_radio_is_back(plain):
    app, api, srv, state = plain
    srv.down = True
    doc = api.send({"note": "It stopped\nfor good"})
    assert doc["queued"] is True and doc["pending"][0]["title"] == "It stopped" and doc["pending"][0]["error"] is None
    assert api.doc()["pending"] == doc["pending"] and api.requests() == []
    # still down: nothing lost
    assert api.flush() == {"sent": 0, "pending": doc["pending"]}
    srv.down = False
    out = api.route("POST", "/api/support/retry", {})[1]
    assert out["sent"] == 1 and out["pending"] == []
    req = api.doc()["requests"][0]
    assert req["number"] == 12 and req["title"] == "It stopped"
    assert req["note"] == "It stopped\nfor good", "the owner's words in full, line breaks kept, after waiting too"
    # one the owner takes back
    srv.down = True
    pid = api.send({"note": "never mind"})["pending"][0]["id"]
    assert api.route("POST", "/api/support/forget", {"id": pid})[1]["pending"] == []
    # a refusal waits with its words, and isn't tried again by itself
    api.send({"note": "too many"})
    srv.down, srv.status = False, 429
    api.flush()
    assert "several today" in api.pending()[0]["error"]
    srv.status = 200
    assert api.flush()["sent"] == 0
    assert api.flush(force=True)["sent"] == 1


def test_the_main_loop_sends_what_waited_once_online_and_asks_for_replies(plain):
    app, api, srv, state = plain
    srv.down = True
    api.send({"note": "later"})
    calls = len(srv.calls)
    api.tick(1000.0, online=False)
    assert api._thread is None and len(srv.calls) == calls
    srv.down = False
    api.tick(1000.0, online=True)
    api._thread.join(5)
    assert api.pending() == [] and api.requests()[0]["number"] == 12
    # the replies: asked once a request is open, then not again for half an hour
    api.tick(1001.0, online=True)
    api._thread.join(5)
    assert any("/v1/replies?" in c[1] for c in srv.calls)
    n = len(srv.calls)
    api.tick(1010.0, online=True)
    api._thread.join(5)
    assert len(srv.calls) == n
    api.tick(1001.0 + support_api.REPLIES_EVERY_S + 1, online=True)
    api._thread.join(5)
    assert len(srv.calls) > n


def test_your_requests_carry_their_thread_and_what_is_unread(sendable):
    app, api, srv, state = sendable
    api.send({"note": "The keys stopped"})                     # #12
    api.send({"note": "And the lamp"})                         # #13
    srv.replies = [
        {"id": 1, "at": "2026-11-07T10:00:00Z", "text": "Is the cable at the back pushed in?", "issue": 12},
        {"id": "seen:13", "at": "2026-11-07T11:00:00Z", "text": "The makers have seen your request #13.", "issue": 13, "state": "seen"},
        {"id": 7, "at": "2026-11-01T11:00:00Z", "text": "A reply to the radio's last owner", "issue": 3},
    ]
    assert api.pull_replies()["added"] == 2                   # not the last owner's
    doc = api.doc()
    by = {r["number"]: r for r in doc["requests"]}
    assert by[12]["title"] == "The keys stopped" and [x["text"] for x in by[12]["replies"]] == ["Is the cable at the back pushed in?"]
    assert by[12]["replies"][0]["at"] == "2026-11-07T10:00:00Z" and by[12]["replies"][0]["state"] is None
    assert by[13]["state"] == "seen" and by[13]["replies"][0]["state"] == "seen"
    assert doc["unseen_replies"] == 2 and by[12]["unseen"] == 1
    assert srv.calls[-1][2]["ids"] == [1, "seen:13", 7]       # all acked, so the old one stops coming
    # read on the page (Your requests marks the notes seen): nothing unread
    notes_api.NotesApi(app, state).seen({})
    assert api.doc()["unseen_replies"] == 0
    # closed: done, the reply in order; reopened: seen again
    srv.replies = [{"id": "closed:12:t", "at": "2026-11-08T09:00:00Z", "text": "Your request #12 is done. Glad it plays.", "issue": 12, "state": "done"}]
    api.clock = lambda: 1794045700.0
    api.pull_replies()
    r12 = next(r for r in api.doc()["requests"] if r["number"] == 12)
    assert r12["state"] == "done" and [x["state"] for x in r12["replies"]] == [None, "done"] and r12["unseen"] == 1
    srv.replies = [{"id": "reopened:12:t2", "at": "2026-11-09T09:00:00Z", "text": "The makers reopened your request #12.", "issue": 12, "state": "seen"}]
    api.pull_replies()
    assert next(r for r in api.doc()["requests"] if r["number"] == 12)["state"] == "seen"


def test_a_box_yaml_with_the_root_alone_sends_to_the_same_routes(sendable):
    app, api, srv, state = sendable
    app.box_paths.box_file.write_text(BOX_YAML.replace("https://ci.example/v1/checkin", "https://ci.example"))
    api.send({"note": "x"})
    assert srv.calls[-1][1] == "https://ci.example/v1/report" and srv.calls[-1][3] == "the-word"


def test_the_fake_radio_has_no_default_server(plain):
    app, api, srv, state = plain
    api.default_url = ""
    assert api.doc()["available"] is False and api.route("POST", "/api/support/send", {})[0] == 409
    assert srv.calls == []


def test_the_daily_checkin_default_is_the_same_server():
    import re
    from pathlib import Path
    layout = (Path(__file__).resolve().parents[2] / "update" / "hasler_update" / "layout.py").read_text()
    assert re.search(r'DEFAULT_CHECKIN_URL = "([^"]+)"', layout).group(1) == support_api.DEFAULT_URL


def test_switching_the_checkin_off_asks_the_server_to_forget_this_radio_once(sendable):
    app, api, srv, state = sendable
    assert api.forget_checkin(background=False) is False      # the fake server has no /v1/forget: 404
    method, url, body, token = srv.calls[-1]
    assert (method, url, token) == ("POST", "https://ci.example/v1/forget", "the-word")
    assert body == {"schema": 1, "box_id": "0f3a7b1e-1111-4222-8333-444455556666"}
    assert not any(c[1].endswith("/v1/enrol") for c in srv.calls)    # never enrols just to be forgotten
    srv.down = True
    assert api.forget_checkin(background=False) is False      # offline: said in the log, never raised
    app.box_paths.box_file.write_text(BOX_YAML.replace("checkin_url: https://ci.example/v1/checkin\n", 'checkin_url: ""\n'))
    n = len(srv.calls)
    assert api.forget_checkin(background=False) is False and len(srv.calls) == n   # nowhere to send: nothing


def test_an_edited_reply_takes_the_old_ones_place_with_its_line_breaks(sendable):
    app, api, srv, state = sendable
    api.send({"note": "x"})
    srv.replies = [{"id": 901, "at": "2026-11-07T10:00:00Z", "text": "First   go", "issue": 12}]
    assert api.pull_replies() == {"added": 1}
    srv.replies = [{"id": 901, "at": "2026-11-07T10:00:00Z", "text": "Hello!! **how are you**\r\n\n\n### Hope all is good", "issue": 12, "edited": True}]
    assert api.pull_replies() == {"added": 0, "edited": 1}
    thread = api.doc()["requests"][0]["replies"]
    assert [x["text"] for x in thread] == ["Hello!! **how are you**\n\n### Hope all is good"]
    notes = notes_api.read_notes(state)
    assert len(notes) == 1 and notes[0]["text"].startswith("Hello!!")
    # an edit of a reply the radio never had is a reply like any other
    srv.replies = [{"id": 902, "at": "2026-11-07T11:00:00Z", "text": "Late edit", "issue": 12, "edited": True}]
    assert api.pull_replies() == {"added": 1}


def test_the_owner_writes_back_on_a_request(sendable):
    app, api, srv, state = sendable
    api.send({"note": "The keys stopped"})
    srv.replies = [{"id": 901, "at": "2026-11-07T10:00:00Z", "text": "Is the cable in?", "issue": 12},
                   {"id": "closed:12:t", "at": "2026-11-07T10:30:00Z", "text": "Your request #12 is done.", "issue": 12, "state": "done"}]
    api.pull_replies()
    assert api.doc()["requests"][0]["state"] == "done"
    status, doc = api.route("POST", "/api/support/reply", {"number": 12, "note": "  Pushed it in.\n\n\nStill quiet.  "})
    assert status == 200 and doc["ok"]
    method, url, body, _ = srv.calls[-1]
    assert url.endswith("/v1/reply") and body["issue"] == 12 and body["note"] == "Pushed it in.\n\nStill quiet." and body["box_id"]
    req = api.doc()["requests"][0]
    assert req["state"] == "sent", "open again, waiting for the makers"
    assert [(x.get("mine", False), x["text"]) for x in req["replies"]][-1] == (True, "Pushed it in.\n\nStill quiet.")
    assert req["unseen"] == 2, "the makers' two, never the owner's own words"
    # kept across a send that dedups onto the same issue
    api._remember(12, "2026-11-07T12:00:00+01:00", True, "again")
    assert any(x.get("mine") for x in api.doc()["requests"][0]["replies"])
    # refusals: not this radio's, nothing to say, offline, the daily limit
    assert api.route("POST", "/api/support/reply", {"number": 99, "note": "hi"})[0] == 404
    assert api.route("POST", "/api/support/reply", {"number": 12, "note": "  "})[0] == 400
    srv.down = True
    status, doc = api.route("POST", "/api/support/reply", {"number": 12, "note": "hi"})
    assert status == 503 and "still here" in doc["error"]
    srv.down, srv.status = False, 429
    status, doc = api.route("POST", "/api/support/reply", {"number": 12, "note": "hi"})
    assert status == 429 and "tomorrow" in doc["error"]


def test_tidy_text_keeps_the_makers_indentation():
    md = "Try:   \r\n\n\n\n```\nsudo  systemctl restart haslerd\n    --verbose\n```\n- one\n  - one.a\n\n\n"
    assert notes_api.tidy_text(md, keep_indent=True) == \
        "Try:\n\n```\nsudo  systemctl restart haslerd\n    --verbose\n```\n- one\n  - one.a"
    # the owner's own words (and anything else) still squeezed as before
    assert notes_api.tidy_text("  a   b \n\n\n   c  ") == "a b\n\nc"
    assert notes_api.tidy_text("x" * 1999 + "\n   y", cap=2000, keep_indent=True) == "x" * 1999


def test_a_rich_reply_keeps_its_markdown(sendable):
    app, api, srv, state = sendable
    api.send({"note": "x"})                       # #12
    md = "Fixed:\n\n- one\n  - one.a\n\n```\n  indented  code\n```"
    srv.replies = [{"id": "c1", "at": "2026-11-08T09:00:00Z", "text": md + "   \n\n\n", "issue": 12}]
    assert api.pull_replies()["added"] == 1
    assert notes_api.read_notes(state)[0]["text"] == md
    assert api.doc()["requests"][0]["replies"][0]["text"] == md
    # an edit keeps it too
    srv.replies = [{"id": "c1", "text": md + "\n    - deeper", "issue": 12, "edited": True}]
    api.pull_replies()
    assert notes_api.read_notes(state)[0]["text"] == md + "\n    - deeper"


def test_the_owners_whole_note_is_kept_and_shown_first(sendable):
    app, api, srv, state = sendable
    note = "The keys stopped\nafter the storm.\n\n\nKey 3 still lights."
    api.send({"note": note})
    req = api.doc()["requests"][0]
    assert req["title"] == "The keys stopped"
    assert req["note"] == "The keys stopped\nafter the storm.\n\nKey 3 still lights."
    # sent again onto the same issue: the first words stay
    api._remember(12, "2026-11-07T12:00:00+01:00", True, "again", "again")
    assert api.doc()["requests"][0]["note"].startswith("The keys stopped")
    # a request kept by older software has no note: "" (the page falls back to the title)
    (state / "support-requests.json").write_text(json.dumps({"schema": 1, "requests": [
        {"number": 4, "at": "2026-10-01T10:00:00+01:00", "state": "sent", "title": "Old one"}]}))
    old = api.doc()["requests"][0]
    assert old["note"] == "" and old["title"] == "Old one"


def test_the_makers_notes_leave_out_the_replies_to_a_request(sendable):
    app, api, srv, state = sendable
    api.send({"note": "x"})
    srv.replies = [{"id": 901, "at": "2026-11-07T10:00:00Z", "text": "Is the cable in?", "issue": 12},
                   {"id": "reopened:12:t", "at": "2026-11-07T11:00:00Z", "text": "The makers reopened your request #12.",
                    "issue": 12, "state": "seen"}]
    api.pull_replies()
    with (state / "maker-notes.jsonl").open("a") as f:
        f.write(json.dumps({"at": "2026-11-07T09:00:00+01:00", "by": "bill", "text": "Replaced the memory card"}) + "\n")
        f.write(json.dumps({"at": "2026-11-07T09:30:00+01:00", "by": "makers", "text": "An old reply", "issue": 3}) + "\n")
    notes = notes_api.NotesApi(app, state)
    doc = notes.doc()
    assert [n["text"] for n in doc["notes"]] == ["Replaced the memory card"] and doc["unseen"] == 1
    assert len(notes_api.read_notes(state)) == 4, "the file keeps them all"
    # the thread has them, the reopening marked as such
    thread = api.doc()["requests"][0]["replies"]
    assert [x["text"] for x in thread] == ["Is the cable in?", "The makers reopened your request #12."]
    assert thread[1].get("reopened") is True and "reopened" not in thread[0]
    # reading the thread (seen, all) still marks every note read, the replies with them
    notes.seen({})
    assert api.doc()["unseen_replies"] == 0 and notes.doc()["unseen"] == 0
