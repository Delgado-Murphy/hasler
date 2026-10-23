"""haslerd's side of the yt-dlp lane: health, triggers, the canary, the key page's buttons."""

import json
import os
import time
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import VARIANTS

from haslerd import health
from haslerd.actions import Actions
from haslerd.player.base import FakePlayer
from haslerd.player.sources import ytm as ytm_mod
from haslerd.player.sources.ytm import YtmSource, canary_with
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, WebServer
from haslerd.web.software import Software
from haslerd.ytdlp_lane import REQUEST_FILE, UNIT, YtdlpLane

T0 = 1_792_000_000.0
NEW, OLD = "2026.10.22", "2026.8.19"


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def venv(root: Path, slot: str, version: str) -> None:
    b = root / slot / "bin"
    b.mkdir(parents=True, exist_ok=True)
    (b / "yt-dlp").write_text("#!/bin/sh\n")
    (b / "yt-dlp").chmod(0o755)
    (root / slot / "lib/python3.13/site-packages" / f"yt_dlp-{version}.dist-info").mkdir(parents=True)


class Wall:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def lane(tmp_path):
    root = tmp_path / "opt-yt-dlp"
    venv(root, "venv-a", OLD)
    venv(root, "venv-c", NEW)
    os.symlink(root / "venv-c", root / "current")
    os.symlink(root / "venv-a", root / "previous")
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    started = []
    wall = Wall()
    ln = YtdlpLane(root=root, state_dir=state_dir, systemctl=started.append, wall=wall, background=False)
    ln._online = True
    return SimpleNamespace(lane=ln, root=root, state_dir=state_dir, started=started, wall=wall)


def write_state(root: Path, **doc):
    base = {"schema": 1, "current": {"slot": "venv-c", "version": NEW, "channel": "nightly"},
            "previous": {"slot": "venv-a", "version": OLD, "channel": "stable"}}
    base.update(doc)
    (root / "state.json").write_text(json.dumps(base))


def request(r):
    return json.loads((r.state_dir / REQUEST_FILE).read_text())


# --- what /api/health shows ---------------------------------------------------------------

def test_health_document(lane):
    write_state(lane.root, last_check=iso(T0 - 7200), last_ok=iso(T0 - 7200), last_result="ok",
                probation_until=iso(T0 + 3600),
                canary={"result": "pass", "version": NEW, "at": iso(T0 - 7200), "via": "haslerd"})
    (lane.root / "events.jsonl").write_text("".join(json.dumps({"event": f"e{i}"}) + "\n" for i in range(9)))
    doc = lane.lane.doc()
    assert doc["version"] == NEW and doc["previous"] == OLD and doc["channel"] == "nightly"
    assert doc["last_ok"] == iso(T0 - 7200) and doc["probation_until"] == iso(T0 + 3600)
    assert [e["event"] for e in doc["events_recent"]] == ["e4", "e5", "e6", "e7", "e8"]
    assert doc["working"] is True and doc["resolve_failures_recent"] == 0
    app = SimpleNamespace(ytdlp_lane=lane.lane)
    assert health.health_report(app)["components"]["yt-dlp"]["version"] == NEW
    assert health.health_report(SimpleNamespace())["components"] == {}


def test_health_says_failing(lane):
    write_state(lane.root, last_result="error", last_error="2026.10.22 fails its canary",
                canary={"result": "fail", "version": NEW})
    assert lane.lane.doc()["working"] is False
    write_state(lane.root, last_result="ok", probation_until=iso(T0 - 1))
    doc = lane.lane.doc()
    assert doc["working"] is True and doc["probation_until"] is None
    lane.lane.resolve_result("aaaaaaaaaaa", False, "Requested format is not available")
    lane.lane.resolve_result("bbbbbbbbbbb", False, "Requested format is not available")
    assert lane.lane.doc()["working"] is False and lane.lane.doc()["resolve_failures_recent"] == 2


def test_no_lane_on_this_radio(tmp_path):
    assert YtdlpLane(root=tmp_path / "none").doc() is None


def test_events_tail_of_a_big_file(lane):
    (lane.root / "events.jsonl").write_text("".join(json.dumps({"event": "x", "n": i, "pad": "p" * 300}) + "\n"
                                                    for i in range(200)))
    assert [e["n"] for e in lane.lane.events()] == [195, 196, 197, 198, 199]


# --- triggers -------------------------------------------------------------------------------

def test_two_tracks_failing_start_an_urgent_run(lane):
    write_state(lane.root)
    ln = lane.lane
    ln.resolve_result("aaaaaaaaaaa", False, "ERROR: Requested format is not available")
    ln.resolve_result("aaaaaaaaaaa", False, "ERROR: Requested format is not available")
    assert lane.started == []                                   # one track, twice
    ln.resolve_result("bbbbbbbbbbb", False, "ERROR: Requested format is not available")
    assert lane.started == [UNIT] and request(lane)["mode"] == "urgent"
    ln.resolve_result("ccccccccccc", False, "ERROR: x")
    assert lane.started == [UNIT]                               # one per 30 min
    lane.wall.t += 31 * 60
    ln.resolve_result("ddddddddddd", False, "ERROR: x")
    ln.resolve_result("eeeeeeeeeee", False, "ERROR: x")
    assert lane.started == [UNIT, UNIT]


def test_failures_spread_out_or_from_the_network_do_not_count(lane):
    write_state(lane.root)
    ln = lane.lane
    ln.resolve_result("aaaaaaaaaaa", False, "ERROR: x")
    lane.wall.t += 11 * 60
    ln.resolve_result("bbbbbbbbbbb", False, "ERROR: x")
    ln.resolve_result("ccccccccccc", False, "<urlopen error [Errno -3] Temporary failure in name resolution>")
    assert lane.started == []
    ln._online = False
    ln.resolve_result("ddddddddddd", False, "ERROR: x")
    assert lane.started == []


def test_probation_failures_start_the_judge(lane):
    write_state(lane.root, probation_until=iso(T0 + 48 * 3600))
    ln = lane.lane
    for i, vid in enumerate(("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")):
        lane.wall.t += 15 * 60
        ln.resolve_result(vid, False, "ERROR: x")
    assert lane.started == [UNIT] and request(lane)["mode"] == "judge"


def test_network_back_after_a_day_starts_a_check(lane):
    write_state(lane.root, last_ok=iso(T0 - 30 * 3600))
    ln = lane.lane
    ln._online = None                       # haslerd just started
    ln.tick(False)
    assert lane.started == []
    ln.tick(True)
    assert lane.started == [UNIT] and not (lane.state_dir / REQUEST_FILE).exists()   # a plain check
    ln.tick(True)
    ln.tick(False)
    ln.tick(True)
    assert lane.started == [UNIT]           # not again within the hour


def test_network_back_with_a_recent_check_does_nothing(lane):
    write_state(lane.root, last_ok=iso(T0 - 3600))
    lane.lane._online = False
    lane.lane.tick(True)
    assert lane.started == []


def test_a_start_that_fails_shows_in_health(lane):
    write_state(lane.root)

    def refuse(unit):
        raise RuntimeError("Interactive authentication required.")
    lane.lane.systemctl = refuse
    lane.lane.resolve_result("aaaaaaaaaaa", False, "x")
    lane.lane.resolve_result("bbbbbbbbbbb", False, "x")
    assert "Interactive authentication" in lane.lane.doc()["trigger_error"]


# --- the canary ---------------------------------------------------------------------------------

class FakeYtm:
    def __init__(self):
        self.calls = []

    def canary(self, exe, vid, min_bytes):
        self.calls.append((exe, vid, min_bytes))
        return {"result": "pass", "bytes": min_bytes, "ctype": "audio/mp4", "cookies": True}


def test_canary_runs_the_slot_in_haslerd(lane):
    ln = lane.lane
    ln.ytm = FakeYtm()
    st, doc = ln.canary({"slot": "venv-c", "id": "aaaaaaaaaaa", "min_bytes": 65536}, "127.0.0.1")
    assert st == 200 and doc["result"] == "pass" and doc["via"] == "haslerd"
    assert ln.ytm.calls == [(str(lane.root / "venv-c/bin/yt-dlp"), "aaaaaaaaaaa", 65536)]
    ln.resolve_result("zzzzzzzzzzz", True)
    st, doc = ln.canary({"slot": "venv-a"}, "::ffff:127.0.0.1")
    assert st == 200 and ln.ytm.calls[-1][1] == "zzzzzzzzzzz"     # no id: a track it just played


@pytest.mark.parametrize("body,client,code", [
    ({"slot": "venv-a"}, "192.168.1.20", 403),
    ({"slot": "../etc"}, "127.0.0.1", 400),
    ({"slot": "/usr/bin"}, "127.0.0.1", 400),
    ({"slot": "venv-b"}, "127.0.0.1", 400),                   # nothing there
    ({"slot": "venv-a", "id": "x; rm -rf /"}, "127.0.0.1", 400),
])
def test_canary_refusals(lane, body, client, code):
    lane.lane.ytm = FakeYtm()
    assert lane.lane.canary(body, client)[0] == code
    assert lane.lane.ytm.calls == []


def test_canary_refuses_a_binary_outside_its_slot(lane, tmp_path):
    (lane.root / "venv-b/bin").mkdir(parents=True)
    os.symlink("/bin/sh", lane.root / "venv-b/bin/yt-dlp")
    lane.lane.ytm = FakeYtm()
    assert lane.lane.canary({"slot": "venv-b"}, "127.0.0.1")[0] == 400


def test_canary_daily_cap(lane):
    from haslerd import ytdlp_lane
    ln = lane.lane
    ln.ytm = FakeYtm()
    for _ in range(ytdlp_lane.CANARY_PER_DAY):
        assert ln.canary({"slot": "venv-c"}, "127.0.0.1")[0] == 200
    assert ln.canary({"slot": "venv-c"}, "127.0.0.1")[0] == 429
    lane.wall.t += 86401
    assert ln.canary({"slot": "venv-c"}, "127.0.0.1")[0] == 200


class Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_canary_with_production_flags_and_cookies():
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        return Proc(0, "https://rr1.googlevideo.example/videoplayback?expire=1\n")

    audio = b"\0\0\0\x1cftypdash" + b"\0" * 70000
    res = canary_with("/opt/yt-dlp/venv-c/bin/yt-dlp", "aaaaaaaaaaa", 65536, ["--cookies", "/var/lib/haslerd/c.txt"],
                      runner, fetch=lambda url, a, b: (audio[: b + 1], 9_000_000, "audio/mp4"))
    assert res["result"] == "pass" and res["cookies"] is True and res["bytes"] == 65536
    assert seen == [["/opt/yt-dlp/venv-c/bin/yt-dlp", "-f", "bestaudio[ext=m4a]/bestaudio", "-g",
                     "--cookies", "/var/lib/haslerd/c.txt", "https://music.youtube.com/watch?v=aaaaaaaaaaa"]]


def test_canary_with_failures():
    ok_url = lambda cmd, **kw: Proc(0, "https://x.example/v\n")       # noqa: E731

    def refused(url, a, b):
        raise ytm_mod._RangeRefused(403)
    r = canary_with("y", "aaaaaaaaaaa", 65536, [], ok_url, fetch=refused)
    assert (r["result"], r["stage"]) == ("fail", "fetch")
    r = canary_with("y", "aaaaaaaaaaa", 65536, [], ok_url, fetch=lambda u, a, b: (b"<html>" * 20000, None, "text/html"))
    assert (r["result"], r["stage"]) == ("fail", "audio")
    r = canary_with("y", "aaaaaaaaaaa", 65536, [],
                    lambda cmd, **kw: Proc(1, "", "ERROR: [youtube] aaaaaaaaaaa: Requested format is not available"))
    assert (r["result"], r["stage"]) == ("fail", "resolve") and "format" in r["error"]
    r = canary_with("y", "aaaaaaaaaaa", 65536, [],
                    lambda cmd, **kw: Proc(1, "", "ERROR: Temporary failure in name resolution"))
    assert r["result"] == "offline"


def test_ytm_reports_resolves_to_the_lane():
    results = {"aaaaaaaaaaa": Proc(0, "https://x.example/a\n"),
               "bbbbbbbbbbb": Proc(1, "", "ERROR: Requested format is not available"),
               "ccccccccccc": Proc(1, "", "ERROR: [youtube] ccccccccccc: Video unavailable")}

    def runner(cmd, **kw):
        return results[cmd[-1].rsplit("=", 1)[1]]
    src = YtmSource(mpd=lambda: None, runner=runner)
    seen = []
    src.on_resolve = lambda vid, ok, err: seen.append((vid, ok))
    for vid in results:
        src.resolve(vid)
    assert seen == [("aaaaaaaaaaa", True), ("bbbbbbbbbbb", False)]     # a removed track is not yt-dlp's fault


# --- the key page's buttons -------------------------------------------------------------------

@pytest.fixture
def web(tmp_path, lane):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True}, ha=False)
    app.confirm.allow_unconfirmed = False
    started = []
    app.software = Software(root=tmp_path / "nothing", state_dir=lane.state_dir, systemctl=started.append)
    lane.lane.systemctl = app.software.start_unit
    lane.lane.ytm = FakeYtm()
    app.ytdlp_lane = lane.lane
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield SimpleNamespace(app=app, host=f"127.0.0.1:{server.port}", started=started, lane=lane)
    server.close()


def call(hostport, method, path, body=None):
    host, port = hostport.split(":")
    conn = HTTPConnection(host, int(port), timeout=10)
    conn.request(method, path, body=json.dumps(body or {}),
                 headers={"Host": hostport, "Content-Type": "application/json"})
    resp = conn.getresponse()
    doc = json.loads(resp.read() or b"null")
    conn.close()
    return resp.status, doc


def test_update_now_starts_the_lane_without_the_ota_updater(web):
    st, doc = call(web.host, "POST", "/api/software/yt-dlp/update")
    assert st == 200 and web.started == [UNIT]
    assert request(web.lane)["mode"] == "now"


def test_go_back_needs_a_press_on_the_radio(web):
    st, doc = call(web.host, "POST", "/api/software/yt-dlp/rollback", {})
    assert st == 403 and doc.get("confirm_required") and web.started == []
    web.app.confirm.allow_unconfirmed = True
    st, doc = call(web.host, "POST", "/api/confirm/request", {"action": "rollback", "args": {"component": "yt-dlp"}})
    token = doc["token"]
    st, doc = call(web.host, "POST", "/api/software/yt-dlp/rollback", {"confirm": token})
    assert st == 200 and web.started == [UNIT] and request(web.lane)["mode"] == "rollback"


def test_go_back_with_nothing_to_go_back_to(web):
    os.unlink(web.lane.root / "previous")
    web.app.confirm.allow_unconfirmed = True
    st, doc = call(web.host, "POST", "/api/software/yt-dlp/rollback", {})
    assert st == 400 and web.started == []


def test_canary_endpoint_on_loopback(web):
    st, doc = call(web.host, "POST", "/api/components/yt-dlp/canary", {"slot": "venv-c", "id": "aaaaaaaaaaa"})
    assert st == 200 and doc["result"] == "pass"


def test_only_the_lane_unit_is_added_to_the_allowlist(tmp_path):
    sw = Software(root=tmp_path, systemctl=lambda u: None)
    sw.start_unit("yt-dlp-update.service")
    for unit in ("yt-dlp-update.timer", "yt-dlp-update@urgent.service", "yt-dlp-update.service; reboot",
                 "haslerd.service"):
        with pytest.raises(ValueError):
            sw.start_unit(unit)
