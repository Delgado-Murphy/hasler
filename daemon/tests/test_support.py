"""Support: "Tell the makers" (web/report.py, nothing private in it), the
rollback reasons in plain words, consent with a dead key board (the owner's
message, a knob movement for help), the makers' notes on the radio
(scripts/hasler-note, web/notes_api.py), the check-in preview
(web/checkin_api.py over the real updater), remote help's edges (last words,
resumed; the helper's consent file) and the boot lamp's SOS."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import stat
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import VARIANTS

from haslerd import boxconfig
from haslerd.actions import Actions
from haslerd.bootresume import note_power_cut
from haslerd.panel.controls import Event, Pot
from haslerd.panel.io_base import AbsentDigital, FakeAnalog, NullAnalog
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import checkin_api, help_api, integration, notes_api, report, software
from haslerd.web.confirm import ABSENT_MESSAGE, ConfirmBroker, ConfirmUnavailable
from haslerd.web.help_api import HelpApi
from haslerd.web.server import WebApp, WebServer

from test_help import URL, FakeHelper, load_helper
from test_web_confirm import Clock

ROOT = Path(__file__).resolve().parents[2]
SOURCES = {"radio": True, "spotify": True, "ytm": True}


def load_script(name: str, modname: str):
    loader = importlib.machinery.SourceFileLoader(modname, str(ROOT / "scripts" / name))
    spec = importlib.util.spec_from_loader(modname, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A radio with a fault, a power cut, a Wi-Fi name, secrets in its settings
    and a phone's name in its status: everything a report must leave out."""
    monkeypatch.setenv("HASLER_ROOT", str(tmp_path / "root"))
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    actions = Actions(variant, player)
    state = tmp_path / "state"
    state.mkdir()
    app = WebApp(actions, panel, state / "keys.yaml", SOURCES, ha=False, settings_file=state / "settings.json")
    app.load_saved()
    app.confirm.clock = Clock()
    (tmp_path / "temp").write_text("61234\n")
    (tmp_path / "uptime").write_text("270000 1\n")
    app.cpu_temp_file = tmp_path / "temp"
    app.throttled_file = tmp_path / "none"
    app.uptime_file = tmp_path / "uptime"
    app.shutdown_kind = "power-cut"
    app.connectivity = SimpleNamespace(online=True, hotspot=False)
    app.lamp_keeper = SimpleNamespace(fault_reasons=["storage"], fault_cleared_at=None)
    app.wifi = SimpleNamespace(current_ssid=lambda: "Casa", signal_of=lambda s: 82, _hotspot=lambda: False,
                               status=lambda: {"setup": {"password": "hotspot-secret-pw"}})
    app.machine_id_file = tmp_path / "machine-id"
    app.machine_id_file.write_text("0123456789abcdef0123456789abcdef\n")
    note_power_cut(state)
    doc = json.loads((state / "settings.json").read_text()) if (state / "settings.json").exists() else {"schema": 1}
    doc.update({"room": {"name": "Kitchen"}, "ha_token": "ha-secret-token", "hotspot": {"psk": "psk-secret"},
                "ytm": {"cookie": "ytm-cookie-secret"}})
    (state / "settings.json").write_text(json.dumps(doc))
    app.room.load(doc)
    events = state / "stream-events.jsonl"
    today = __import__("time").strftime("%Y-%m-%d")
    events.write_text("\n".join(json.dumps(e) for e in [
        {"t": f"{today}T08:00:00", "action": "stalled", "preset": {"n": 2, "name": "Radio Clásica", "source": "radio",
                                                                     "uri": "http://stream.example/secret?key=abc"}, "reason": "no bytes"},
        {"t": f"{today}T08:01:00", "action": "recovered", "preset": {"n": 2, "name": "Radio Clásica", "source": "radio"}},
        {"t": "2020-01-01T00:00:00", "action": "stalled", "preset": None},
    ]) + "\n")
    app.stream_watch = SimpleNamespace(events=SimpleNamespace(path=events), silent=False, recovering=False, reason=None,
                                       status_doc=lambda: {"recovering": False, "stalled_s": None},
                                       recent_events=lambda: [json.loads(ln) for ln in events.read_text().splitlines()][:2])
    paths = boxconfig.Paths(release=ROOT, etc=tmp_path / "etc", state=state)
    paths.etc.mkdir()
    paths.box_file.write_text("schema: 1\nvariant: hasler-hm\ncheckin: false\nmakers:\n  contact: hola@example.org\n")
    app.box_paths = paths
    # remote help, off, with a last session and a one-time link that must never show
    fake = FakeHelper()
    fake.last = {"at": int(__import__("time").time()) - 7200, "why": "stopped", "maker_seconds": 2400,
                 "started_at": int(__import__("time").time()) - 7200 - 2400}
    app.extra_routes.append(HelpApi(app, runner=fake, helper_present=lambda: True))
    app.extra_routes.append(report.ReportApi(app))
    app.extra_routes.append(notes_api.NotesApi(app, state))
    return app, actions, player, panel, fake, state


# -- rollback reasons in plain words ---------------------------------------------------------


@pytest.mark.parametrize("raw, words", [
    ("check panel failed (absent)", "the keys weren’t answering after the switch"),
    ("haslerd failed", "the new version kept crashing"),
    ("boot loop (2 boots in trial)", "the radio restarted twice in a row"),
    ("power cut during the switch", "the power went in the middle"),
    ("rolled back by hand", "you went back from the page"),
    ("sound card missing: sndrpihifiberry", "the sound card vanished"),
    ("MPD not back within 90s", "the player didn’t come back in time"),
    ("check mpd failed (False); sound card missing: x", "the player wasn’t answering after the switch; the sound card vanished"),
    ("something new", "something new"),
    ("", ""),
])
def test_rollback_words(raw, words):
    assert software.rollback_words(raw) == words


def test_last_result_carries_both_the_raw_reason_and_the_words():
    res = software.Software._last_result({"phase": "rolled_back", "failed": "1.4.1", "from": "1.4.0",
                                          "reason": "check panel failed (absent)", "history": []})
    assert res["reason"] == "check panel failed (absent)" and res["why"] == "the keys weren’t answering after the switch"


# -- the report --------------------------------------------------------------------------


SECRETS = ("ha-secret-token", "psk-secret", "ytm-cookie-secret", "hotspot-secret-pw", "Pixel 11", URL, "/a/", "key=abc")


def test_report_header_is_the_six_phone_lines_in_order(rig):
    app, actions, player, panel, fake, state = rig
    actions.dispatch(Event("k2", "tap"))
    out = report.build(app, note="Since yesterday it goes quiet", wifi=True)
    lines = out["text"].splitlines()
    assert lines[0].startswith("Hasler · Kitchen · radio " + integration.short_id(app.machine_id_file) + " · ")
    assert "(hasler-hm)" in lines[0]
    assert lines[1].startswith("Version ") and "updates not set up" in lines[1]     # no updater in this rig
    assert lines[2] == "Lamp now: red, tapping SOS — something needs a maker: the memory card is full, or won’t write" \
        or lines[2].startswith("Lamp now: ")
    assert lines[3].startswith("Playing: key 2, ") and "not in standby" in lines[3] and "volume" in lines[3]
    assert lines[4].startswith("Running for 3 d 3 h · last switched off: the power was cut")
    assert lines[5] == "Wi-Fi: on Casa · internet: yes"
    assert lines[6] == "" and lines[7] == 'Your note: "Since yesterday it goes quiet"'
    assert lines[9] == "--- details for the makers ---"
    assert lines[10].startswith("checks: player ") and "keys OK" in lines[10]
    assert "faults: storage (" in out["text"]
    assert "stalls today: 1" in out["text"]
    assert "stream events (last 2):" in out["text"] and "stalled · 2 (Radio Clásica) · no bytes" in out["text"]
    assert "remote help: last ended" in out["text"] and "(stopped from the key page)" in out["text"]
    assert out["json"]["radio"]["room"] == "Kitchen" and out["json"]["network"]["wifi"] == "Casa"
    assert report.header_lines(out["json"]) == lines[:6]


def test_report_never_carries_a_secret_a_phone_name_or_a_withheld_wifi_name(rig):
    app, actions, player, panel, fake, state = rig
    actions.dispatch(Event("k2", "tap"))
    player.phone_casts("bluetooth", "Pixel 11", "Song", "Band")
    fake.active, fake.backend = True, "NeedsLogin"          # a link is waiting: it must not be in the report
    out = report.build(app, note='a "note" with "quotes"', wifi=False)
    blob = out["text"] + json.dumps(out["json"])
    for secret in SECRETS:
        assert secret not in blob, secret
    assert "Casa" not in blob and "Wi-Fi: on (name left out)" in out["text"]
    assert "Playing: a phone over bluetooth" in out["text"]
    assert out["json"]["network"]["wifi"] is None and out["json"]["network"]["wifi_withheld"] is True
    assert out["json"]["remote_help"]["state"] == "waiting"
    assert "login_url" not in json.dumps(out["json"])


def test_report_over_http(rig):
    app, *_ = rig
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with urllib.request.urlopen(base + "/api/report?note=hello%20there&wifi=0", timeout=5) as r:
            doc = json.loads(r.read())
        assert doc["ok"] and 'Your note: "hello there"' in doc["text"] and "Casa" not in doc["text"]
        assert doc["json"]["note"] == "hello there"
        with urllib.request.urlopen(base + "/api/info", timeout=5) as r:
            info = json.loads(r.read())
        assert info["short_id"] == integration.radio_id(app.machine_id_file)[:4] and len(info["short_id"]) == 4
        assert info["makers_contact"] == "hola@example.org"
    finally:
        server.close()


def test_report_copes_with_a_bare_app():
    out = report.build(SimpleNamespace())
    assert out["text"].startswith("Hasler · no room name · radio ") and "--- details for the makers ---" in out["text"]


# -- consent with a dead key board --------------------------------------------------------


def _knob_panel(analog):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    panel.digital = AbsentDigital("no expander answers")
    panel.analog = analog
    return panel


def test_absent_message_is_for_the_owner_and_the_hint_for_the_log(caplog):
    broker = ConfirmBroker(_knob_panel(NullAnalog()), clock=Clock())
    with pytest.raises(ConfirmUnavailable) as exc:
        broker.request("update", {"version": "1.5.0"})
    assert str(exc.value) == ABSENT_MESSAGE
    assert "box.yaml" not in ABSENT_MESSAGE and "panel board" in ABSENT_MESSAGE
    assert "allow_unconfirmed" in caplog.text


def test_a_knob_movement_is_the_hand_on_the_radio_for_help_only():
    panel = _knob_panel(FakeAnalog({0: 1000, 1: 1000}))
    assert panel.absent and panel.knobs_answer
    broker = ConfirmBroker(panel, clock=Clock())
    with pytest.raises(ConfirmUnavailable):
        broker.request("update", {"version": "1.5.0"})            # a key press is still the rule
    doc = broker.request("help")
    assert doc["by_knob"] is True and doc["state"] == "pending"
    # a key press cannot arrive (no board); a knob's change confirms, and still reaches the player
    assert broker.intercept([Event("k1", "tap")]) == [Event("k1", "tap")]
    assert broker.poll(doc["token"])["state"] == "pending"
    turned = [Event("volume", "change", 42.0)]
    assert broker.intercept(turned) == turned
    assert broker.poll(doc["token"])["state"] == "confirmed"
    broker.consume(doc["token"], "help", {})


def test_the_panel_reads_its_pots_alone_when_the_board_is_dead():
    analog = FakeAnalog({0: 100, 1: 100})
    panel = _knob_panel(analog)
    pots = [c for c in panel.controls if isinstance(c, Pot)]
    assert pots and all(c.channel in (0, 1) for c in pots)
    for t in range(10):
        panel.poll(t * 0.02)
    analog.set(pots[0].channel, 20000)
    events = []
    for t in range(10, 30):
        events += panel.poll(t * 0.02)
    assert any(e.control == pots[0].id and e.kind == "change" for e in events)
    assert all(e.kind == "change" for e in events)          # never a key, never the end switch
    assert _knob_panel(NullAnalog()).poll(0.0) == [] and not _knob_panel(NullAnalog()).knobs_answer


def test_a_dead_board_still_opens_the_adc(monkeypatch):
    import haslerd.panel.io_ads1115 as ads
    import haslerd.panel.io_mcp23017 as mcp
    from haslerd import variant as variant_mod

    def no_board(*a, **k):
        raise RuntimeError("mcp23017: no expander answers")
    monkeypatch.setattr(mcp, "Mcp23017Digital", no_board)
    monkeypatch.setattr(ads, "Ads1115Analog", lambda channels, **k: FakeAnalog(dict.fromkeys(channels, 0)))
    v = variant_mod.load_variant(VARIANTS / "hasler-hm.yaml")
    digital, analog = variant_mod.open_backends(v)
    assert isinstance(digital, AbsentDigital) and isinstance(analog, FakeAnalog)


# -- the makers' notes ----------------------------------------------------------------------------


def test_hasler_note_appends_caps_and_the_page_reads_newest_first(rig, monkeypatch, capsys):
    app, actions, player, panel, fake, state = rig
    monkeypatch.setenv("HASLER_STATE", str(state))
    monkeypatch.setenv("HASLER_PAGE", "http://127.0.0.1:1")      # --ok: nobody answers, and that is fine
    mod = load_script("hasler-note", "hasler_note")
    mod.STATE = str(state)
    mod.PAGE = "http://127.0.0.1:1"
    assert mod.main(["Replaced the memory card and put your keys back."]) == 0
    assert mod.main(["--ok", "--by", "wmurphy", "Calibrated the volume knob"]) == 0
    out = capsys.readouterr().out
    assert "noted at" in out and "lamp:" in out
    lines = (state / "maker-notes.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["by"] == "wmurphy"
    assert stat.S_IMODE((state / "maker-notes.jsonl").stat().st_mode) == 0o644
    with pytest.raises(SystemExit):
        mod.main(["   "])
    # the cap: like the stream events, the file is trimmed to 3/4 of NOTES_KEEP
    for i in range(mod.NOTES_KEEP + 10):
        mod.add(f"note {i}", "m")
    kept = (state / "maker-notes.jsonl").read_text().splitlines()
    assert len(kept) <= mod.NOTES_KEEP and kept[-1].endswith(f'note {mod.NOTES_KEEP + 9}"}}')
    api = notes_api.NotesApi(app, state)
    doc = api.doc()
    assert doc["notes"][0]["text"] == f"note {mod.NOTES_KEEP + 9}" and doc["unseen"] == len(doc["notes"])
    assert len(doc["notes"]) == notes_api.NOTES_SHOWN
    # Seen: that note and everything older (by time, so a later day's note stays new)
    (state / "maker-notes.jsonl").write_text("".join(json.dumps({"at": f"2026-11-0{d}T10:00:00+01:00", "by": "m", "text": f"day {d}"}) + "\n"
                                                     for d in (3, 1, 5)))
    doc = api.doc()
    assert [n["text"] for n in doc["notes"]] == ["day 5", "day 3", "day 1"] and doc["unseen"] == 3
    assert api.seen({"at": "2026-11-03T10:00:00+01:00"})["unseen"] == 1
    assert [n["seen"] for n in api.doc()["notes"]] == [False, True, True]
    assert api.seen({})["unseen"] == 0
    assert api.doc()["notes"][0]["seen"] is True and api.doc()["notes"][0]["at"] == "2026-11-05T10:00:00+01:00"


def test_notes_over_http(rig):
    app, actions, player, panel, fake, state = rig
    (state / "maker-notes.jsonl").write_text(json.dumps({"at": "2026-11-07T18:02:00+01:00", "by": "m", "text": "The SOS is gone."}) + "\n"
                                             + "garbage\n")
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with urllib.request.urlopen(base + "/api/notes", timeout=5) as r:
            doc = json.loads(r.read())
        assert doc == {"notes": [{"at": "2026-11-07T18:02:00+01:00", "by": "m", "text": "The SOS is gone.", "seen": False}], "unseen": 1}
        req = urllib.request.Request(base + "/api/notes/seen", data=b'{"at": "2026-11-07T18:02:00+01:00"}', method="POST",
                                     headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{server.port}"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read()) == {"ok": True, "unseen": 0}
        assert (state / "maker-notes.seen").read_text().strip() == "2026-11-07T18:02:00+01:00"
    finally:
        server.close()


# -- the check-in preview ------------------------------------------------------------------------------


def test_checkin_preview_runs_the_updaters_builder_on_haslerds_documents(rig, tmp_path, monkeypatch):
    app, *_ = rig
    root = tmp_path / "root"
    (root / "etc/hasler").mkdir(parents=True)
    (root / "etc/hasler/box.yaml").write_text("schema: 1\nvariant: hasler-hm\nchannel: stable\nbox_id: box-77\ncheckin: true\n")
    (root / "etc/machine-id").write_text("abcdef0123456789abcdef0123456789\n")
    api = checkin_api.CheckinApi(app, launcher=ROOT / "update/bin/hasler-update")
    status, doc = api.route("GET", "/api/checkin/preview", None)
    assert status == 200 and doc["ok"]
    p = doc["payload"]
    assert p["box_id"] == "box-77" and p["variant"] == "hasler-hm" and p["faults"] == ["storage"]
    assert p["last_shutdown"] == "power-cut" and p["power_cuts_month"] == 1 and p["temp_c"] == 61
    assert p["player"]["stalls_today"] == 1 and len(p["short_id"]) == 4
    assert doc["bytes"] < 2048 and '"box_id"' in doc["text"]
    blob = json.dumps(p)
    for word in ("Casa", "Kitchen", "Radio Clásica", "ssid", "hostname", "room"):
        assert word not in blob, word
    monkeypatch.setattr(checkin_api, "find_launcher", lambda release=None: None)
    status, doc = checkin_api.CheckinApi(app).route("GET", "/api/checkin/preview", None)
    assert status == 404 and doc["installed"] is False


def test_checkin_switch_in_box_yaml_and_its_status(tmp_path):
    root = tmp_path / "root"
    (root / "var/lib/hasler-update").mkdir(parents=True)
    paths = boxconfig.Paths(release=ROOT, etc=tmp_path / "etc", state=tmp_path / "state")
    paths.etc.mkdir()
    paths.box_file.write_text("schema: 1\nvariant: hasler-hm\ncheckin_url: https://x.example/v1/checkin\n")
    sw = software.Software(root=root, box_paths=paths)
    assert sw.checkin_doc() == {"enabled": False, "url_set": True, "last": None}
    sw.put_settings({"checkin": True})
    assert "checkin: true" in paths.box_file.read_text()
    (root / "var/lib/hasler-update/checkin.json").write_text(json.dumps({"at": "2026-11-07T04:12:00Z", "ok": True, "short_id": "ab12", "bytes": 410}))
    assert sw.checkin_doc()["enabled"] is True and sw.checkin_doc()["last"]["short_id"] == "ab12"
    sw.put_settings({"checkin": False})
    assert "checkin: true" not in paths.box_file.read_text() and "checkin_url:" in paths.box_file.read_text()
    with pytest.raises(ValueError):
        software.Software.check_settings({"checkin": "yes"})


# -- remote help's edges ---------------------------------------------------------------------------------


def test_last_words():
    at = lambda t: "18:02"   # noqa: E731
    assert help_api.last_words({"at": 1.0, "why": "stopped", "maker_seconds": 2400}, at) == \
        "A maker was on for 40 minutes; help ended at 18:02 when you pressed Stop."
    assert help_api.last_words({"at": 1.0, "why": "expired", "maker_seconds": 5400}, at) == \
        "A maker was on for 1 h 30 min; help ended at 18:02 when the 24 hours were up."
    assert help_api.last_words({"at": 1.0, "why": "unapproved", "maker_seconds": 0}, at) == \
        "No maker came on; help ended at 18:02 because nobody approved the link within an hour."
    assert help_api.last_words({"why": "failed"}) == "No maker came on; help ended because it couldn't start."


def test_help_doc_carries_resumed_and_the_last_sessions_words(rig):
    app, actions, player, panel, fake, state = rig
    api = next(r for r in app.extra_routes if isinstance(r, HelpApi))
    doc = api.doc()
    assert doc["state"] == "off" and doc["last"]["maker_seconds"] == 2400
    assert doc["last"]["words"].startswith("A maker was on for 40 minutes; help ended at ") and "when you pressed Stop" in doc["last"]["words"]
    fake.active, fake.backend = True, "NeedsLogin"
    t = int(__import__("time").time())
    fake.session = {"hostname": "hasler-kitchen-ab12", "started_at": t - 60, "expires_at": t + 80000, "approve_by": t + 3000}
    original = fake.status

    def status(ok=True, error=None):
        return {**original(ok, error), "resumed": True}
    fake.status = status
    api._cache = None
    doc = api.doc()
    assert doc["state"] == "waiting" and doc["resumed"] is True and doc["login_url"] == URL


def test_helper_keeps_the_owners_yes_across_a_restart_and_forgets_it_at_a_stop(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    assert mod.main(["start", "hasler-kitchen-ab12"]) == 0
    consent = Path(mod.CONSENT)
    assert consent.exists() and stat.S_IMODE(consent.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(mod.CONSENT_DIR).stat().st_mode) == 0o700
    doc = json.loads(consent.read_text())
    assert doc == {"started_at": 5000, "expires_at": 5000 + 86400, "hostname": "hasler-kitchen-ab12"}
    assert "hasler-help-watch.timer" in sysd.units
    # the radio restarts: the transient units and /run are gone, the yes is not
    sysd.units.clear()
    sysd.backend = None
    import shutil
    shutil.rmtree(mod.RUN, ignore_errors=True)
    monkeypatch.setattr(mod, "now", lambda: 5000.0 + 3600 * 5)
    assert mod.main(["resume"]) == 0
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["active"] and doc["resumed"] is True and doc["auth_url"] == URL
    assert doc["expires_at"] == 5000 + 86400 and doc["approve_by"] == 5000 + 3600 * 5 + 3600
    end = [c for c in sysd.cmds if c[0].endswith("systemd-run") and "--unit=hasler-help-end" in c][-1]
    assert f"--on-active={86400 - 3600 * 5}s" in end                  # the time left, not a new day
    assert json.loads(consent.read_text())["started_at"] == 5000       # the original yes stands
    # a maker is on for a while: the minutes are counted by the watch timer
    sysd.backend = "Running"
    sysd.peer = {"p": {"HostName": "bills-macbook", "Active": True, "UserID": "1"}}
    sysd.user = {"1": {"LoginName": "wmurphy@github"}}
    for minute in range(1, 11):
        monkeypatch.setattr(mod, "now", lambda m=minute: 5000.0 + 3600 * 5 + 60 * m)
        assert mod.main(["watch"]) == 0
    assert capsys.readouterr().out == ""                              # watch prints nothing (no URL in the journal)
    assert mod.main(["stop", "stopped"]) == 0
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["last"]["why"] == "stopped" and doc["last"]["maker_seconds"] == 540
    assert not consent.exists()
    assert mod.main(["resume"]) == 0                                   # nothing to resume
    assert not json.loads(capsys.readouterr().out.strip().splitlines()[-1])["active"]


def test_helper_expired_yes_is_forgotten_and_rescue_makes_its_own_hostname(tmp_path, monkeypatch, capsys):
    mod, sysd = load_helper(tmp_path, monkeypatch)
    Path(mod.CONSENT_DIR).mkdir(parents=True)
    Path(mod.CONSENT).write_text(json.dumps({"started_at": 1, "expires_at": 4000, "hostname": "hasler-kitchen-ab12"}))
    assert mod.main(["resume"]) == 0
    assert not Path(mod.CONSENT).exists() and not any(u.startswith("hasler-help") for u in sysd.units)
    capsys.readouterr()
    Path(mod.MACHINE_ID).write_text("0123456789abcdef0123456789abcdef\n")
    Path(mod.SETTINGS).write_text(json.dumps({"room": {"name": "Kitchen Radio"}}))
    expected = "hasler-kitchen-radio-" + integration.radio_id(Path(mod.MACHINE_ID))[:4]
    assert mod.own_hostname() == expected
    assert mod.main(["rescue"]) == 0
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["active"] and doc["hostname"] == expected and doc["resumed"] is False
    assert Path(mod.CONSENT).exists()


def test_the_rescue_file_and_resume_units_ship():
    for unit in ("hasler-help-rescue.service", "hasler-help-resume.service"):
        text = (ROOT / "config" / unit).read_text()
        assert "ConditionPathExists=" in text and "network-online.target" in text
        assert unit in (ROOT / "config/haslerd.service").read_text()
        assert f'("config/{unit}", "integration/systemd/{unit}")' in (ROOT / "scripts/hasler-install").read_text()
    rules = (ROOT / "scripts/release/build_release.py").read_text()
    assert 'Rule("scripts/hasler-help-rescue", "integration/sbin/", mode=0o755)' in rules
    assert 'Rule("scripts/hasler-note", "integration/bin/", mode=0o755)' in rules
    for name in ("hasler-help-rescue", "hasler-note"):
        assert (ROOT / "scripts" / name).stat().st_mode & 0o111
    assert "hasler-help.txt" in (ROOT / "scripts/hasler-help-rescue").read_text()


# -- the boot lamp's SOS ----------------------------------------------------------------------------------


def test_boot_lamp_says_sos_after_ten_minutes_without_a_takeover():
    mod = load_script("hasler-lamp-boot", "hasler_lamp_boot")
    assert mod.SOS_AFTER_S == 600.0
    assert mod.sos_level(0.0) == mod.SOS_MARK                      # the first dot
    assert mod.sos_level(mod.SOS_UNIT_S + 0.01) == mod.SOS_GLOW    # the gap after it
    dash_at = 8 * mod.SOS_UNIT_S + 0.01                            # ... is 5 units, then a 3-unit gap: the O's first dash
    assert mod.sos_level(dash_at) == mod.SOS_MARK and mod.sos_level(dash_at + 2 * mod.SOS_UNIT_S) == mod.SOS_MARK
    assert mod.sos_level(dash_at + 3 * mod.SOS_UNIT_S) == mod.SOS_GLOW
    assert mod.sos_level(mod.SOS_CYCLE_S - 1.0) == mod.SOS_GLOW     # the 7 s rest
    assert mod.sos_level(mod.SOS_CYCLE_S + 0.01) == mod.SOS_MARK    # and again
    assert "SOS" in (ROOT / "docs/lamp.md").read_text()
