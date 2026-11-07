"""The opt-in daily check-in (hasler_update/checkin.py): what the payload holds
and never holds, the page's preview over the CLI, the POST after the check
(stubbed HTTP), the metered skip, once a day - and a release from a USB stick."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error

import pytest
from conftest import ROOT, World

from hasler_update import checkin
from hasler_update.check import Checker
from hasler_update.layout import DEFAULT_CHECKIN_URL, box_settings


@pytest.fixture(autouse=True)
def pokes(monkeypatch):
    """Never knock on a real haslerd: the replies poke after a send, recorded."""
    calls = []
    monkeypatch.setattr(checkin, "poke_replies", lambda *a, **k: calls.append(a))
    return calls

DIAG = {
    "wifi": {"ssid": "Casa", "signal": 82, "hotspot": False, "words": "On Casa, good signal"},
    "online": True,
    "temperature": {"c": 61.2, "hot": False, "throttled": {"raw": "0x80000", "any_now": False, "any_seen": True}, "words": "warm"},
    "storage": {"free_bytes": 12 * 1024 ** 2, "total_bytes": 29 * 1024 ** 3, "read_only": False, "bad": True, "words": "nearly full"},
    "last_shutdown": {"kind": "power-cut", "words": "the power was cut"},
    "power_cuts": {"total": 4, "month": 3, "last_at": "2026-11-07T06:12:03+01:00", "hint": True},
    "running": {"uptime_s": 270000, "daemon_uptime_s": 1000, "since": "2026-11-04T06:00:00+01:00", "words": "3 d 3 h"},
    "player": {"mpd": True, "audio_card": True, "recovering": False, "restarts_last_hour": 1, "dsp": {}, "words": "player answering"},
    "lamp": {"present": True, "words": "..."},
    "panel": "ok", "faults": ["storage"], "fault_cleared_at": None,
    "software": {"version": "1.4.0"}, "variant": "hasler-hm-hat", "version": "1.4.0", "at": "2026-11-07T18:00:00+01:00",
}
HEALTH = {
    "version": "1.4.0", "ok": True, "uptime_s": 1000,
    "checks": {"mpd": True, "audio_card": True, "panel": "ok", "lamp": True, "web": True},
    "idle": False, "standby": False, "playing": True, "recovering": False, "recovering_reason": None,
    "mpd": {"unresponsive_s": None, "restarts_last_hour": 1, "recovering": False}, "stalled_s": None,
    "stream_events_recent": [
        {"t": "2026-11-07T08:00:00", "action": "stalled", "preset": {"n": 2, "name": "Radio Clásica", "source": "radio"}},
        {"t": "2026-11-07T08:01:00", "action": "recovered", "preset": {"n": 2, "name": "Radio Clásica", "source": "radio"}},
        {"t": "2026-11-07T09:00:00", "action": "stalled", "preset": None},
        {"t": "2026-10-01T09:00:00", "action": "stalled", "preset": None},
    ],
    "last_play_s": 12,
    "components": {"yt-dlp": {"version": "2026.10.22", "channel": "nightly", "working": True}},
    "os_updates": {"enabled": True, "last_result": "ok", "paused": False, "last_upgrade": {"count": 3, "at": "2026-10-21T03:00:00Z"}},
}
NOW = 1_794_050_000.0    # 2026-11-07, late morning UTC (the events above are "today" in any European zone)
PRIVATE = ("Casa", "Kitchen", "Radio Clásica", "192.168", "hasler-kitchen", "Pixel")


def _box(**over):
    return {"channel": "stable", "update_mode": "auto", "box_id": "box-1", "metered": False, "variant": "hasler-hm-hat",
            "update_url": "file:///x", "checkin": True, "checkin_url": "https://ci.example/v1/checkin", "checkin_token": "", **over}


def test_payload_holds_what_a_maker_asks_first_and_nothing_private():
    check = {"checked": "2026-11-07T04:00:00Z", "result": "ok", "message": "up to date", "installed": {"version": "1.4.0", "seq": 3}}
    state = {"phase": "rolled_back", "failed": "1.4.1", "to": "1.4.1", "from": "1.4.0", "reason": "check panel failed (absent)"}
    p = checkin.build_payload(_box(), check, state, DIAG, HEALTH, "ab12", NOW)
    assert p["box_id"] == "box-1" and p["short_id"] == "ab12" and p["variant"] == "hasler-hm-hat"
    assert p["version"] == "1.4.0" and p["channel"] == "stable" and p["update_mode"] == "auto"
    assert p["check"] == "ok"
    assert p["last_result"] == {"ok": False, "version": "1.4.1", "back_to": "1.4.0", "reason": "check panel failed (absent)"}
    assert p["health"] == {"ok": True, "failed": []} and p["faults"] == ["storage"]
    assert p["player"] == {"restarts_last_hour": 1, "stalls_today": 2}
    assert p["ytdlp"] is True and p["os_updates"] == "ok"
    assert p["uptime_days"] == 3.1 and p["last_shutdown"] == "power-cut" and p["power_cuts_month"] == 3
    assert p["free_bytes"] == 12 * 1024 ** 2 and p["storage_bad"] is True and p["temp_c"] == 61 and p["throttled"] == "0x80000"
    assert checkin.forbidden_in(p) == []
    body = json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(body) < 640, len(body)                       # about half a kilobyte with every field filled
    for word in PRIVATE:
        assert word not in body.decode(), word


def test_forbidden_keys_are_caught_at_any_depth():
    assert checkin.forbidden_in({"a": {"ssid": 1}, "b": [{"hostname": "x"}], "ok": 1}) == ["/a/ssid", "/b/hostname"]
    for key in ("ip", "ssid", "hostname", "room", "name", "playing", "volume", "standby", "token", "update_url", "id"):
        assert key in checkin.FORBIDDEN_KEYS


def test_failed_checks_and_an_absent_panel_are_named():
    health = {**HEALTH, "ok": False, "checks": {"mpd": False, "audio_card": True, "panel": "absent", "lamp": True, "web": True}}
    p = checkin.build_payload(_box(), {}, {"phase": "committed", "to": "1.4.0"}, DIAG, health, "ab12", NOW)
    assert p["health"] == {"ok": False, "failed": ["mpd", "panel"]} and p["last_result"] == {"ok": True, "version": "1.4.0", "probation": False}
    health = {**HEALTH, "os_updates": {**HEALTH["os_updates"], "paused": True}}
    assert checkin.build_payload(_box(), {}, {}, DIAG, health, "ab12", NOW)["os_updates"] == "paused"
    p = checkin.build_payload(_box(), {}, {}, {}, {}, "ab12", NOW)
    assert p["last_result"] is None and p["ytdlp"] is None and p["uptime_days"] is None and checkin.forbidden_in(p) == []


def test_preview_over_the_cli_with_haslerds_documents_on_stdin(world: World):
    (world.root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    env = {**os.environ, "HASLER_ROOT": str(world.root)}
    exe = [sys.executable, "-I", str(ROOT / "update/bin/hasler-update"), "checkin", "--preview", "--stdin"]
    out = subprocess.run(exe, env=env, input=json.dumps({"diagnostics": DIAG, "health": HEALTH}), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    p = json.loads(out.stdout.strip().splitlines()[-1])
    assert p["box_id"] == "box-1" and p["variant"] == "hasler-hm" and p["faults"] == ["storage"] and len(p["short_id"]) == 4
    assert p["short_id"] == checkin.short_id(world.root / "etc/machine-id")
    assert checkin.forbidden_in(p) == []
    assert not (world.layout.upd / "checkin.json").exists()          # a preview sends and records nothing


def test_after_check_posts_once_a_day_skips_metered_and_records_failures(world: World):
    (world.root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    world.layout.upd.mkdir(parents=True, exist_ok=True)
    posted = []

    def post(url, payload, token="", timeout=15):
        posted.append((url, payload, token))
        return 204

    fetch = lambda url: DIAG if url.endswith("/diagnostics") else HEALTH   # noqa: E731
    # off: nothing sent, nothing written
    assert checkin.after_check(world.layout, _box(checkin=False), NOW, fetch, post) is None
    assert posted == [] and not checkin.status_path(world.layout).exists()
    # on: one POST, recorded
    rec = checkin.after_check(world.layout, _box(checkin_token="shh"), NOW, fetch, post)
    assert rec["ok"] is True and rec["error"] is None and rec["bytes"] < 2048 and len(rec["short_id"]) == 4
    assert len(posted) == 1 and posted[0][0] == "https://ci.example/v1/checkin" and posted[0][2] == "shh"
    assert posted[0][1]["box_id"] == "box-1" and checkin.forbidden_in(posted[0][1]) == []
    assert json.loads(checkin.status_path(world.layout).read_text())["ok"] is True
    # the check runs again a few hours later ("Check now"): sent today already
    assert checkin.after_check(world.layout, _box(), NOW + 3 * 3600, fetch, post)["at"] == rec["at"] and len(posted) == 1
    # the next day: sent again
    assert checkin.after_check(world.layout, _box(), NOW + 25 * 3600, fetch, post)["ok"] is True and len(posted) == 2
    # metered: skipped, said so
    rec = checkin.after_check(world.layout, _box(metered=True), NOW + 50 * 3600, fetch, post)
    assert rec["ok"] is False and rec["skipped"] == "metered" and len(posted) == 2
    # switched off in box.yaml (checkin_url: ""): nothing can go
    rec = checkin.after_check(world.layout, _box(checkin_url=""), NOW + 75 * 3600, fetch, post)
    assert rec["error"] == "checkin_url is switched off in box.yaml" and len(posted) == 2
    # the makers' server gone: recorded, never raised

    def down(url, payload, token="", timeout=15):
        raise urllib.error.URLError("name or service not known")
    rec = checkin.after_check(world.layout, _box(), NOW + 100 * 3600, fetch, down)
    assert rec["ok"] is False and "name or service" in rec["error"]
    rec = checkin.after_check(world.layout, _box(), NOW + 125 * 3600, fetch, lambda *a, **k: 500)
    assert rec["error"] == "the makers' server answered 500"
    # haslerd not answering: recorded too

    def no_page(url):
        raise OSError("connection refused")
    rec = checkin.after_check(world.layout, _box(), NOW + 150 * 3600, no_page, post)
    assert rec["ok"] is False and "refused" in rec["error"]
    with pytest.raises(ValueError):
        checkin.post_json("http://plain.example/x", {"box_id": "b"})


def test_box_settings_read_the_checkin_keys(tmp_path):
    from hasler_update.layout import Layout
    lay = Layout(tmp_path)
    lay.etc.mkdir(parents=True)
    lay.box_yaml.write_text("schema: 1\nvariant: hasler-hm\ncheckin: true\ncheckin_url: https://ci.example/v1/checkin\ncheckin_token: w\n")
    b = box_settings(lay)
    assert b["checkin"] is True and b["checkin_url"] == "https://ci.example/v1/checkin" and b["checkin_token"] == "w"
    # nothing said: the makers' own server (the switch is still the owner's, off)
    lay.box_yaml.write_text("schema: 1\nvariant: hasler-hm\n")
    b = box_settings(lay)
    assert b["checkin"] is False and b["checkin_url"] == DEFAULT_CHECKIN_URL
    # an empty value, quoted or not, means nowhere
    for line in ('checkin_url: ""', "checkin_url:", "checkin_url: ''"):
        lay.box_yaml.write_text("schema: 1\nvariant: hasler-hm\n" + line + "\n")
        assert box_settings(lay)["checkin_url"] == "", line


def test_the_checkin_goes_with_the_radios_own_word_when_there_is_no_shared_one(world: World):
    from hasler_update.layout import DEFAULT_CHECKIN_URL as url
    secrets = world.layout.haslerd_state / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "support.json").write_text(json.dumps({"server": url, "box_id": "box-1", "token": "own"}))
    assert checkin.own_token(world.layout, url) == "own" and checkin.own_token(world.layout, url + "/v1/checkin") == "own"
    assert checkin.own_token(world.layout, "https://other.example/v1/checkin") == ""
    posted = []

    def post(u, payload, token="", timeout=15):
        posted.append((u, token))
        return 204
    checkin.after_check(world.layout, _box(checkin_url=url), NOW, lambda u: DIAG if "diagnostics" in u else HEALTH, post)
    checkin.after_check(world.layout, _box(checkin_url=url, checkin_token="shared"), NOW + 25 * 3600,
                        lambda u: DIAG if "diagnostics" in u else HEALTH, post)
    assert posted == [(url + "/v1/checkin", "own"), (url + "/v1/checkin", "shared")]


@pytest.mark.parametrize("given", ["https://ci.example", "https://ci.example/", "https://ci.example/v1/checkin", "https://ci.example/v1/checkin/"])
def test_the_checkin_url_may_be_the_root_or_the_full_route(world: World, given):
    assert checkin.checkin_endpoint(given) == "https://ci.example/v1/checkin"
    posted = []

    def post(u, payload, token="", timeout=15):
        posted.append(u)
        return 204
    checkin.after_check(world.layout, _box(checkin_url=given), NOW, lambda u: DIAG if "diagnostics" in u else HEALTH, post)
    assert posted == ["https://ci.example/v1/checkin"]


def test_the_daily_check_never_fails_over_the_checkin(world: World, capsys):
    """``hasler-update check`` with the switch on and no haslerd to ask: the check
    is what it is, and checkin.json says the check-in couldn't be built."""
    world.publish("1.0.0", 1)
    (world.root / "etc/machine-id").write_text("abcdef0123456789abcdef0123456789\n")
    world.layout.box_yaml.write_text(world.layout.box_yaml.read_text() + "checkin: true\ncheckin_url: https://ci.example/v1/checkin\n")
    env = {**os.environ, "HASLER_ROOT": str(world.root), "HASLER_MOODE_VERSION": "10.0.2"}
    exe = [sys.executable, "-I", str(ROOT / "update/bin/hasler-update"), "check"]
    out = subprocess.run(exe, env=env, capture_output=True, text=True, timeout=120)
    doc = json.loads(out.stdout)
    assert doc["checked"]
    rec = json.loads(checkin.status_path(world.layout).read_text())
    assert rec["ok"] is False and rec["error"]


def test_a_release_from_a_usb_stick_installs_against_the_trust_on_the_radio(world: World):
    """The post-makers path (docs/ota.md §5): a signed release on a stick,
    ``update_url: file:///media/stick``, checked and staged like any other."""
    world.publish("1.0.0", 1)
    stick = world.server                      # the fake server is already a directory: a stick is the same thing
    text = world.layout.box_yaml.read_text().replace(world.server.as_uri(), stick.as_uri())
    world.layout.box_yaml.write_text(text)
    assert box_settings(world.layout)["update_url"].startswith("file://")
    st = Checker(world.layout, now=world.now, facts={"arch": "aarch64", "os": "trixie", "moode": "10.0.2", "python_abi": "cp313"}).stage()
    assert st["result"] == "ok" and st["available"]["version"] == "1.0.0" and st["staged"]


def test_a_sent_check_in_asks_haslerd_for_the_makers_replies_and_a_failed_one_does_not(world: World, pokes):
    (world.root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    world.layout.upd.mkdir(parents=True, exist_ok=True)
    fetch = lambda url: DIAG if url.endswith("/diagnostics") else HEALTH   # noqa: E731
    checkin.after_check(world.layout, _box(), NOW, fetch, lambda *a, **k: 500)
    assert pokes == []
    checkin.after_check(world.layout, _box(), NOW, fetch, lambda *a, **k: 204)
    assert len(pokes) == 1
    assert checkin.REPLIES_URL == "http://127.0.0.1:8080/api/support/replies"
    checkin.after_check(world.layout, _box(), NOW, fetch, lambda *a, **k: 204, poke=lambda: pokes.append("own"))
    assert len(pokes) == 1          # sent today already: nothing new to ask
