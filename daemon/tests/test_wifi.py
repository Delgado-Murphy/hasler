"""Captive-portal Wi-Fi setup: scan parsing, the root helper hand-off."""

import json
import sqlite3
import subprocess

import pytest

from haslerd.web.wifi import Wifi, parse_scan, security_for, split_terse

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
