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
    "temperature": {"c": 61.2, "hot": False, "throttled": {"raw": "0x80000", "under_voltage_now": False, "any_now": False,
                                                           "any_seen": True}, "words": "warm"},
    "storage": {"free_bytes": 12 * 1024 ** 2, "total_bytes": 29 * 1024 ** 3, "read_only": False, "bad": True, "words": "nearly full"},
    "last_shutdown": {"kind": "power-cut", "words": "the power was cut"},
    "power_cuts": {"total": 4, "month": 3, "last_at": "2026-11-07T06:12:03+01:00", "hint": True, "last_30d": 4},
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
    ],
    "last_play_s": 12,
    "components": {"yt-dlp": {"version": "2026.10.22", "channel": "nightly", "working": True, "resolve_failures_recent": 0}},
    "os_updates": {"enabled": True, "last_run": "2026-11-05T03:00:00Z", "last_result": "ok", "paused": False, "deferred": None,
                   "last_upgrade": {"count": 3, "at": "2026-10-21T03:00:00Z"}},
    "day": {"stalls": 7, "mpd_restarts": 2, "daemon_starts": 1, "ytdlp_failures": 3, "faults": ["heat"], "temp_max": 81,
            "undervolt": True, "throttled": False, "hours": 24},
}
FACTS = {"hw": {"model": "Raspberry Pi 4 Model B Rev 1.5", "code": "d03115", "ram_gb": 8},
         "os": {"debian": "13.7", "kernel": "6.18.50+rpt-rpi-v8", "moode": "10.3.7"}}
NOW = 1_794_050_000.0    # 2026-11-07, late morning UTC
PRIVATE = ("Casa", "Kitchen", "Radio Clásica", "192.168", "hasler-kitchen", "Pixel", "Europe/")


def _box(**over):
    return {"channel": "stable", "update_mode": "auto", "box_id": "box-1", "metered": False, "variant": "hasler-hm-hat",
            "update_url": "file:///x", "checkin": True, "checkin_url": "https://ci.example/v1/checkin", "checkin_token": "", **over}


def _size(p):
    return len(json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def test_payload_holds_what_a_maker_asks_first_and_nothing_private():
    check = {"checked": "2026-11-07T04:00:00Z", "result": "ok", "message": "up to date", "installed": {"version": "1.4.0", "seq": 3}}
    state = {"phase": "rolled_back", "failed": "1.4.1", "to": "1.4.1", "from": "1.4.0", "reason": "check panel failed (absent)"}
    p = checkin.build_payload(_box(), check, state, DIAG, HEALTH, "ab12", NOW, facts=FACTS)
    assert p["schema"] == 2 and p["box_id"] == "box-1" and p["short_id"] == "ab12" and p["variant"] == "hasler-hm-hat"
    assert p["version"] == "1.4.0" and p["channel"] == "stable" and p["update_mode"] == "auto" and p["check"] == "ok"
    assert p["release"] == {"state": "rolled_back", "version": "1.4.1", "from": "1.4.0", "reason": "check panel failed (absent)"}
    assert p["health"] == {"ok": True, "failed": []}
    assert p["faults"] == {"now": ["storage"], "day": ["heat", "storage"]}
    assert p["player"] == {"stalls_24h": 7, "restarts_24h": 2, "daemon_restarts_24h": 1}
    assert p["ytdlp"] == {"working": True, "version": "2026.10.22", "failures_24h": 3}
    assert p["os_updates"] == {"state": "ok", "last_ok_d": 2}
    assert p["power"] == {"last_off": "power-cut", "cuts_30d": 4, "undervolt_now": False, "undervolt_24h": True, "throttled_24h": False}
    assert p["temp_c"] == {"now": 61, "max_24h": 81}
    assert p["storage"] == {"free_mb": 12, "total_gb": 29, "state": "full"} and p["uptime_d"] == 3
    assert p["hw"] == FACTS["hw"] and p["os"] == FACTS["os"]
    assert "at" not in p and "last_result" not in p and "throttled" not in p
    assert checkin.forbidden_in(p) == []
    body = json.dumps(p, separators=(",", ":"), ensure_ascii=False)
    assert _size(p) < 1000, _size(p)
    for word in PRIVATE:
        assert word not in body, word


def test_the_worst_case_payload_stays_near_a_kilobyte():
    state = {"phase": "rolled_back", "failed": "10.10.10", "from": "10.10.9", "reason": "x" * 400}
    health = {**HEALTH, "ok": False, "checks": {"mpd": False, "audio_card": False, "panel": "absent", "lamp": False, "web": True},
              "day": {**HEALTH["day"], "faults": ["update", "storage", "sound", "panel", "heat"], "stalls": 99999}}
    diag = {**DIAG, "faults": ["update", "storage", "sound", "panel", "heat"]}
    facts = {"hw": {"model": "R" * 48, "code": "a02082ff", "ram_gb": 16}, "os": {"debian": "13.10", "kernel": "k" * 40, "moode": "10.10.10"}}
    p = checkin.build_payload(_box(box_id="0f3a7b1e-1111-4222-8333-444455556666", variant="hasler-hm-hat"),
                              {"result": "error"}, state, diag, health, "ab12", NOW, facts=facts)
    assert len(p["release"]["reason"]) == 120
    assert _size(p) <= 1200 < checkin.MAX_BYTES, _size(p)     # every field at its longest; typical is ~850


def test_forbidden_keys_are_caught_at_any_depth():
    assert checkin.forbidden_in({"a": {"ssid": 1}, "b": [{"hostname": "x"}], "ok": 1}) == ["/a/ssid", "/b/hostname"]
    for key in ("ip", "ssid", "hostname", "room", "name", "playing", "volume", "standby", "token", "update_url", "id",
                "serial", "cid", "board_serial", "boot_id", "at", "timezone"):
        assert key in checkin.FORBIDDEN_KEYS


def test_failed_checks_release_states_and_the_os_lane_in_one_word():
    health = {**HEALTH, "ok": False, "checks": {"mpd": False, "audio_card": True, "panel": "absent", "lamp": True, "web": True}}
    p = checkin.build_payload(_box(), {}, {"phase": "probation", "to": "1.4.0", "from": "1.3.9"}, DIAG, health, "ab12", NOW)
    assert p["health"] == {"ok": False, "failed": ["mpd", "panel"]}
    assert p["release"] == {"state": "probation", "version": "1.4.0", "from": "1.3.9", "reason": None}
    os_up = HEALTH["os_updates"]
    for over, word in (({"paused": True}, "paused"), ({"enabled": False}, "off"),
                       ({"deferred": "a Hasler release is in probation", "last_result": None, "last_run": None}, "deferred"),
                       ({"last_result": None, "last_run": None}, "never"), ({"last_result": "failed"}, "failed")):
        got = checkin.build_payload(_box(), {}, {}, DIAG, {**HEALTH, "os_updates": {**os_up, **over}}, "ab12", NOW)["os_updates"]
        assert got["state"] == word, (over, got)
    p = checkin.build_payload(_box(), {}, {}, {}, {}, "ab12", NOW)
    assert p["release"] is None and p["ytdlp"] is None and p["os_updates"] is None and p["uptime_d"] is None
    assert p["player"] == {"stalls_24h": 0, "restarts_24h": 0, "daemon_restarts_24h": 0} and checkin.forbidden_in(p) == []
    assert p["hw"] == {"model": None, "code": None, "ram_gb": None}
    # a stale index, and a broken yt-dlp lane that says so (B1)
    check = {"result": "ok", "index_expires": "2026-11-01T00:00:00Z"}
    lane = {**HEALTH, "components": {"yt-dlp": {"version": "2026.10.22", "working": False}}}
    p = checkin.build_payload(_box(), check, {}, DIAG, lane, "ab12", NOW)
    assert p["check"] == "stale" and p["ytdlp"]["working"] is False


def test_storage_words():
    gb = 1024 ** 3
    s = lambda **kw: checkin._storage({"free_bytes": 20 * gb, "total_bytes": 59 * gb, **kw})["state"]   # noqa: E731
    assert s() == "ok" and s(free_bytes=int(0.9 * gb)) == "low" and s(free_bytes=10 * 1024 ** 2) == "full"
    assert s(read_only=True) == "read-only" and s(bad=True) == "full"


def test_the_version_is_the_one_running_not_the_checks_snapshot(world: World):
    """B2: status.json says 1.6.1 (checked before apply switched); /api/health and the link say 1.6.2."""
    check = {"result": "ok", "installed": {"version": "1.6.1"}}
    assert checkin.running_version(world.layout, {"version": "1.6.2"}, check) == "1.6.2"
    rel = world.layout.releases / "1.6.2"
    rel.mkdir(parents=True, exist_ok=True)
    if world.layout.current.is_symlink() or world.layout.current.exists():
        world.layout.current.unlink()
    world.layout.current.symlink_to(rel)
    assert checkin.running_version(world.layout, {}, check) == "1.6.2"
    assert checkin.running_version(world.layout, {"version": "unknown"}, check) == "1.6.2"
    world.layout.current.unlink()
    assert checkin.running_version(world.layout, {}, check) == "1.6.1"


def test_machine_facts_are_generic_and_never_the_serial(world: World):
    (world.root / "proc/device-tree").mkdir(parents=True, exist_ok=True)
    (world.root / "proc/device-tree/model").write_bytes(b"Raspberry Pi 4 Model B Rev 1.5\x00")
    (world.root / "proc/cpuinfo").write_text("processor\t: 0\nHardware\t: BCM2835\nRevision\t: d03115\n"
                                             "Serial\t\t: 10000000deadbeef\nModel\t\t: Raspberry Pi 4 Model B Rev 1.5\n")
    (world.root / "proc/meminfo").write_text("MemTotal:        7999616 kB\nMemFree: 1 kB\n")
    (world.root / "etc").mkdir(parents=True, exist_ok=True)
    (world.root / "etc/debian_version").write_text("13.7\n")
    os.environ["HASLER_MOODE_VERSION"], os.environ["HASLER_KERNEL"] = "10.3.7", "6.18.50+rpt-rpi-v8"
    try:
        f = checkin.machine_facts(world.layout)
    finally:
        del os.environ["HASLER_MOODE_VERSION"], os.environ["HASLER_KERNEL"]
    assert f == FACTS
    assert "deadbeef" not in json.dumps(f)


def test_a_radio_just_switched_on_holds_its_check_in_back(world: World):
    (world.root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    world.layout.upd.mkdir(parents=True, exist_ok=True)
    slept, posted = [], []
    fetch = lambda url: DIAG if url.endswith("/diagnostics") else HEALTH   # noqa: E731
    rec = checkin.after_check(world.layout, _box(), NOW, fetch, lambda *a, **k: posted.append(a) or 204,
                              uptime=lambda: 120.0, sleep=slept.append)
    assert rec["ok"] is True and len(slept) == 1 and 1800 <= slept[0] <= 7200 and len(posted) == 1
    assert checkin.parse_iso(rec["at"]) >= NOW + 1800
    slept.clear()
    checkin.after_check(world.layout, _box(), NOW + 30 * 3600, fetch, lambda *a, **k: 204, uptime=lambda: 4000.0, sleep=slept.append)
    assert slept == []


def test_preview_over_the_cli_with_haslerds_documents_on_stdin(world: World):
    (world.root / "etc/machine-id").write_text("0123456789abcdef0123456789abcdef\n")
    env = {**os.environ, "HASLER_ROOT": str(world.root)}
    exe = [sys.executable, "-I", str(ROOT / "update/bin/hasler-update"), "checkin", "--preview", "--stdin"]
    out = subprocess.run(exe, env=env, input=json.dumps({"diagnostics": DIAG, "health": HEALTH}), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    p = json.loads(out.stdout.strip().splitlines()[-1])
    assert p["box_id"] == "box-1" and p["variant"] == "hasler-hm" and p["faults"]["now"] == ["storage"] and len(p["short_id"]) == 4
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
