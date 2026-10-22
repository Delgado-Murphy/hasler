"""Captive-portal Wi-Fi setup: scan parsing, the root helper hand-off."""

import json
import re
import sqlite3
import subprocess

import pytest

from haslerd.web.wifi import Wifi, parse_iw_scan, parse_scan, security_for, split_terse

SCAN = (
    "Erinaceus:80:WPA2\n"
    "Livebox7-5483-WiFi7:75:WPA3\n"
    "Livebox7-5483:74:WPA2\n"
    "Livebox7-5483:60:WPA2\n"          # same SSID, weaker: dropped
    ":70:WPA1 WPA2\n"                  # hidden network: no name to show
    "Hasler:99:WPA2\n"                 # our own hotspot
    "Caf\\:e Libre:40:\n"              # escaped colon, open network
)


def test_terse_fields_unescape_colons():
    assert split_terse("Caf\\:e Libre:40:") == ["Caf:e Libre", "40", ""]


def test_scan_is_deduped_sorted_and_skips_hidden_and_our_hotspot():
    nets = parse_scan(SCAN)
    assert [n["ssid"] for n in nets] == ["Erinaceus", "Livebox7-5483-WiFi7", "Livebox7-5483", "Caf:e Libre"]
    assert nets[2]["signal"] == 74


@pytest.mark.parametrize("nm, moode", [("WPA2", "wpa-psk"), ("WPA1 WPA2", "wpa-psk"), ("WPA2 WPA3", "wpa-psk"),
                                      ("WPA3", "sae"), ("", "none"), ("--", "none")])
def test_security_maps_to_moodes_names(nm, moode):
    assert security_for(nm) == moode


class FakeRun:
    def __init__(self, reply='{"ok": true}', rc=0):
        self.calls, self.reply, self.rc = [], reply, rc

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[0] == "nmcli":
            return subprocess.CompletedProcess(cmd, 0, SCAN, "")
        return subprocess.CompletedProcess(cmd, self.rc, self.reply + "\n", "")


def test_join_passes_secrets_on_stdin_never_argv():
    run = FakeRun()
    Wifi(runner=run).join("Home", "s3cret-pass", "wpa-psk")
    cmd, kw = run.calls[-1]
    assert cmd == ["sudo", "-n", "/usr/local/sbin/hasler-wifi-join"]
    assert "s3cret-pass" not in " ".join(cmd)
    assert json.loads(kw["input"]) == {"ssid": "Home", "password": "s3cret-pass", "security": "wpa-psk"}


def test_join_reports_the_helpers_reason():
    run = FakeRun('{"ok": false, "error": "a WPA password is 8-63 characters"}', rc=1)
    with pytest.raises(ValueError, match="8-63"):
        Wifi(runner=run).join("Home", "x" * 9, "auto")


@pytest.mark.parametrize("ssid, pwd, sec", [("", "longenough", "auto"), ("x" * 33, "longenough", "auto"),
                                            ("Home", "short", "wpa-psk"), ("Home", "longenough", "wep")])
def test_join_refuses_obviously_bad_input_before_sudo(ssid, pwd, sec):
    run = FakeRun()
    with pytest.raises(ValueError):
        Wifi(runner=run).join(ssid, pwd, sec)
    assert run.calls == []


def test_status_lists_networks_and_the_ssid_moode_was_looking_for(tmp_path):
    db = tmp_path / "moode.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cfg_network (iface TEXT, wlanssid TEXT)")
    con.execute("INSERT INTO cfg_network VALUES ('wlan0', 'Erinaceus+')")
    con.commit()
    con.close()
    st = Wifi(runner=FakeRun(), hotspot=lambda: True, db=str(db)).status()
    assert st["hotspot"] is True and st["current"] == "Erinaceus+"
    assert st["networks"][0]["ssid"] == "Erinaceus"


# --- setup mode: scanning as a hotspot, the hotspot's own details, retry --------------

IW = """BSS 11:22:33:44:55:66(on wlan0)
\tcapability: ESS Privacy ShortSlotTime (0x0411)
\tsignal: -48.00 dBm
\tSSID: Erinaceus+
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK
BSS 11:22:33:44:55:67(on wlan0)
\tcapability: ESS Privacy (0x0011)
\tsignal: -70.00 dBm
\tSSID: Erinaceus+
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK
BSS aa:bb:cc:dd:ee:ff(on wlan0)
\tcapability: ESS Privacy (0x0011)
\tsignal: -60.00 dBm
\tSSID: Livebox\\xc3\\xa9 6
\tRSN:\t * Version: 1
\t\t * Authentication suites: SAE
BSS 00:00:00:00:00:01(on wlan0)
\tcapability: ESS (0x0001)
\tsignal: -80.00 dBm
\tSSID: Cafe
BSS 00:00:00:00:00:02(on wlan0)
\tsignal: -30.00 dBm
\tSSID: Hasler
"""


def test_iw_scan_parses_like_nmcli():
    nets = parse_iw_scan(IW)
    assert nets == [
        {"ssid": "Erinaceus+", "signal": 100, "security": "wpa-psk"},
        {"ssid": "Liveboxé 6", "signal": 80, "security": "sae"},
        {"ssid": "Cafe", "signal": 40, "security": "none"},
    ]


def make_db(tmp_path, wlan="Erinaceus+", apd=("Hasler", "ab" * 32), saved=("Erinaceus+", "Old"), name="moode.db"):
    db = tmp_path / name
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cfg_network (iface TEXT, wlanssid TEXT, wlanpsk TEXT)")
    con.execute("INSERT INTO cfg_network VALUES ('wlan0', ?, '')", (wlan,))
    con.execute("INSERT INTO cfg_network VALUES ('apd0', ?, ?)", apd)
    con.execute("CREATE TABLE cfg_ssid (ssid TEXT)")
    con.executemany("INSERT INTO cfg_ssid VALUES (?)", [(s,) for s in saved])
    con.commit()
    con.close()
    return str(db)


class HelperRun(FakeRun):
    """nmcli lists nothing (hotspot mode); the helper answers per action."""

    def __init__(self, replies):
        super().__init__()
        self.replies = replies

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        if cmd[0] == "nmcli":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        action = json.loads(kw["input"]).get("action", "join")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(self.replies[action]) + "\n", "")


def test_in_hotspot_mode_the_helper_scans_and_the_answer_is_kept(tmp_path):
    t = [0.0]
    run = HelperRun({"scan": {"ok": True, "iw": IW}})
    w = Wifi(runner=run, hotspot=lambda: True, db=make_db(tmp_path), clock=lambda: t[0])
    assert [n["ssid"] for n in w.networks()] == ["Erinaceus+", "Liveboxé 6", "Cafe"]
    run.replies["scan"] = {"ok": True, "iw": ""}
    t[0] = 5.0                       # within the throttle: no new scan, the last list
    assert len(w.networks()) == 3 and len(run.calls) == 1
    t[0] = 60.0                      # a scan that finds nothing keeps the last list too
    assert len(w.networks()) == 3 and len(run.calls) == 2


def test_the_list_from_before_the_hotspot_is_offered_when_it_cannot_scan(tmp_path):
    hot = [False]
    run = FakeRun()
    w = Wifi(runner=run, hotspot=lambda: hot[0], db=make_db(tmp_path))
    w.refresh()                      # the watchdog, while the radio is still looking
    hot[0] = True
    run.reply = '{"ok": false, "error": "command failed: Operation not supported (-95)"}'
    assert [n["ssid"] for n in w.networks()][0] == "Erinaceus"


def test_status_tells_the_setup_hotspot_and_saved_networks(tmp_path):
    pw = tmp_path / "hotspot.txt"
    pw.write_text("# bootstrap.sh\nssid=Hasler\npassword=k7mq-ra2x-pd9h\n")
    w = Wifi(runner=HelperRun({"scan": {"ok": True, "iw": IW}}), hotspot=lambda: True,
             db=make_db(tmp_path), hotspot_file=pw)
    st = w.status()
    assert st["saved"] == ["Erinaceus+", "Old"]
    assert st["setup"] == {"ssid": "Hasler", "password_set": True, "password": "k7mq-ra2x-pd9h",
                           "address": "172.24.1.1", "page": "http://172.24.1.1:8080/wifi"}
    pw.write_text("ssid=Other\npassword=x\n")       # for another hotspot name: not ours to show
    assert "password" not in w.hotspot_info()
    assert Wifi(db=make_db(tmp_path, apd=("Hasler", ""), name="m2.db"), hotspot_file=tmp_path / "none").hotspot_info()[
        "password_set"] is False


def test_retry_goes_to_the_helper_on_stdin(tmp_path):
    run = HelperRun({"retry": {"ok": True, "result": "not-visible"}})
    w = Wifi(runner=run, hotspot=lambda: True, db=make_db(tmp_path))
    assert w.retry()["result"] == "not-visible"
    cmd, kw = run.calls[-1]
    assert cmd == ["sudo", "-n", "/usr/local/sbin/hasler-wifi-join"]
    assert json.loads(kw["input"]) == {"action": "retry", "force": False}
    run.replies["retry"] = {"ok": False, "error": "must run as root"}
    assert w.retry(force=True) == {"ok": False, "error": "must run as root", "result": "error"}


def test_ensure_hotspot_keeps_a_password_it_chose_and_only_that(tmp_path):
    keep = tmp_path / "state" / "hotspot.txt"
    run = HelperRun({"hotspot": {"ok": True, "changed": True, "password_used": True, "ssid": "Hasler"}})
    w = Wifi(runner=run, db=make_db(tmp_path, apd=("Hasler", "")), hotspot_file=keep)
    w.ensure_hotspot()
    sent = json.loads(run.calls[-1][1]["input"])
    assert sent["action"] == "hotspot" and sent["ensure"] is True
    assert re.fullmatch(r"[a-km-np-z2-9]{4}-[a-km-np-z2-9]{4}-[a-km-np-z2-9]{4}", sent["password"])
    assert oct(keep.stat().st_mode & 0o777) == "0o600"
    assert f"password={sent['password']}" in keep.read_text()
    assert "s3cret" not in " ".join(run.calls[-1][0])          # on stdin, not argv
    keep.unlink()
    run.replies["hotspot"] = {"ok": True, "changed": False, "password_used": False}
    w.ensure_hotspot()
    assert not keep.exists()                                  # it had one: nothing to keep
