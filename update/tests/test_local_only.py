"""scripts/hasler-local-only: MPD and CamillaDSP on 127.0.0.1 only (2026-11-14:
both listened on 0.0.0.0 without a password, past haslerd's volume ceiling).

The fixtures are the bench radio's own files and /proc lines (moOde 10.3.7)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load():
    loader = importlib.machinery.SourceFileLoader("hasler_local_only", str(ROOT / "scripts/hasler-local-only"))
    spec = importlib.util.spec_from_loader("hasler_local_only", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


H = _load()

MOODE_MPD_CONF = """#########################################
# This file is managed by moOde
#########################################

music_directory "/var/lib/mpd/music"
playlist_directory "/var/lib/mpd/playlists"
db_file "/var/lib/mpd/database"
log_file "/var/log/mpd/log"
pid_file "/var/run/mpd/pid"
state_file "/var/lib/mpd/state"
sticker_file "/var/lib/mpd/sticker.sql"
user "mpd"
group "audio"
bind_to_address "any"
port "6600"
log_level "notice"
restore_paused "yes"
zeroconf_enabled "no"
zeroconf_name "Hasler MPD"

audio_output {
type "alsa"
name "ALSA Default"
device "_audioout"
mixer_type "hardware"
}

audio_output {
type "httpd"
name "HTTP Server"
port "8000"
encoder "lame"
bitrate "320"
tags "yes"
always_on "yes"
format "44100:16:2"
}
"""

MOODE_CDSP_CONF = """# For more info about this configuration file see: https://github.com/scripple/alsa_cdsp
pcm.camilladsp {
    type cdsp
    cpath "/usr/local/bin/camilladsp"
    config_out "/usr/share/camilladsp/working_config.yml"

    config_cdsp 1

    min_channels 1
    max_channels 8
    rates = [
        44100
        48000
    ]

###############################################
# Uncomment the following lines,              #
# if you want more detailed logging output in #
# /var/log/mpd/log                            #
###############################################
   cargs [
#        -v
        -p "1234"
        -a "0.0.0.0"
        -s "/var/lib/cdsp/statefile.yml"
   ]

}"""

PROC_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   4: 00000000:19C8 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 7062 1 0 100 0 0 10 16
   5: 00000000:04D2 00000000:0000 0A 00000000:00000000 00:00000000 00000000   107        0 16820137 1 0 100 0 0 10 0
  15: 0100007F:990C 0100007F:19C8 01 00000000:00000000 02:0002CCA8 00000000  1000        0 4882 2 0 20 4 0 10 -1
  46: 0100007F:19C8 0100007F:990C 01 00000000:00000000 02:0002CCAC 00000000   107        0 5853 2 0 21 4 23 10 -1
"""
PROC_TCP6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   2: 00000000000000000000000000000000:1F90 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4876 1 0 100 0 0 10 0
"""
LOCAL_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   4: 0100007F:19C8 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 7062 1 0 100 0 0 10 16
   5: 0100007F:04D2 00000000:0000 0A 00000000:00000000 00:00000000 00000000   107        0 16820137 1 0 100 0 0 10 0
"""
LOCAL_TCP6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   2: 00000000000000000000000001000000:19C8 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 4876 1 0 100 0 0 10 0
"""


# --- mpd.conf -------------------------------------------------------------------------------


def test_moodes_mpd_conf_gets_one_local_bind_and_nothing_else_changes():
    new = H.mpd_text(MOODE_MPD_CONF)
    assert new == MOODE_MPD_CONF.replace('bind_to_address "any"', 'bind_to_address "127.0.0.1"')
    assert H.mpd_text(new) == new


@pytest.mark.parametrize("conf,want", [
    # no bind line is MPD's default, every address: one goes in before port
    ('user "mpd"\nport "6600"\n', 'user "mpd"\nbind_to_address "127.0.0.1"\nport "6600"\n'),
    ('user "mpd"', 'user "mpd"\nbind_to_address "127.0.0.1"\n'),
    # a second TCP bind would fail at start ("address in use"): only one stays
    ('bind_to_address "any"\nbind_to_address "192.168.88.177"\nport "6600"\n',
     'bind_to_address "127.0.0.1"\nport "6600"\n'),
    ('bind_to_address "localhost"   # comment\n', 'bind_to_address "127.0.0.1"\n'),
    ("bind_to_address any\n", 'bind_to_address "127.0.0.1"\n'),
    # a local socket is no network: kept, and a TCP bind still added
    ('bind_to_address "/run/mpd/socket"\nport "6600"\n',
     'bind_to_address "/run/mpd/socket"\nbind_to_address "127.0.0.1"\nport "6600"\n'),
])
def test_mpd_binds(conf, want):
    assert H.mpd_text(conf) == want
    assert H.mpd_text(want) == want


def test_a_bind_inside_an_output_block_is_moodes():
    conf = ('bind_to_address "127.0.0.1"\nport "6600"\naudio_output {\ntype "httpd"\nname "a { b"\n'
            'bind_to_address "any"\nport "8000"\n}\nlog_level "notice"\n')
    assert H.mpd_text(conf) == conf


# --- camilladsp.conf ----------------------------------------------------------------------


def test_moodes_camilladsp_conf_gets_a_local_address_and_nothing_else_changes():
    new = H.cdsp_text(MOODE_CDSP_CONF)
    assert new == MOODE_CDSP_CONF.replace('-a "0.0.0.0"', '-a "127.0.0.1"')
    assert H.cdsp_text(new) == new
    # moOde's log-level switch (sed on the -v line) still finds its line
    verbose = new.replace("#        -v", "         -v")
    assert H.cdsp_text(verbose) == verbose


@pytest.mark.parametrize("cargs,want", [
    ('   cargs [\n        -p "1234"\n   ]\n', '   cargs [\n        -p "1234"\n        -a "127.0.0.1"\n   ]\n'),
    ('   cargs [ -p "1234" -a "0.0.0.0" -s "/x" ]\n', '   cargs [ -p "1234" -a "127.0.0.1" -s "/x" ]\n'),
    ("   cargs [\n        --address=0.0.0.0\n   ]\n", "   cargs [\n        --address=127.0.0.1\n   ]\n"),
    ('#        -a "0.0.0.0"\n', '#        -a "0.0.0.0"\n'),       # a comment stays a comment
    ('    cpath "/usr/local/bin/camilladsp"\n', '    cpath "/usr/local/bin/camilladsp"\n'),
])
def test_camilladsp_addresses(cargs, want):
    assert H.cdsp_text(cargs) == want
    assert H.cdsp_text(want) == want


# --- apply ----------------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "root"
    for rel, text, mode in (("etc/mpd.conf", MOODE_MPD_CONF, 0o666),
                            ("etc/alsa/conf.d/camilladsp.conf", MOODE_CDSP_CONF, 0o644),
                            ("usr/share/alsa/alsa.conf", "# alsa\n", 0o644),
                            ("proc/net/tcp", PROC_TCP, 0o644), ("proc/net/tcp6", PROC_TCP6, 0o644)):
        f = r / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        os.chmod(f, mode)
    os.utime(r / "usr/share/alsa/alsa.conf", (1_000_000, 1_000_000))
    monkeypatch.setattr(H, "ROOT", str(r))
    return r


def test_apply_puts_both_on_localhost_keeping_owner_and_mode(root):
    doc = H.apply(restart=False)
    assert doc["ok"] and doc["changed"] == ["/etc/mpd.conf", "/etc/alsa/conf.d/camilladsp.conf"]
    assert 'bind_to_address "127.0.0.1"' in (root / "etc/mpd.conf").read_text()
    assert '-a "127.0.0.1"' in (root / "etc/alsa/conf.d/camilladsp.conf").read_text()
    assert (root / "etc/mpd.conf").stat().st_mode & 0o777 == 0o666          # moOde's PHP writes it
    assert (root / "etc/alsa/conf.d/camilladsp.conf").stat().st_mode & 0o777 == 0o644
    assert (root / "usr/share/alsa/alsa.conf").stat().st_mtime > 1_000_000, "players reload ALSA's config"
    assert not list((root / "etc").rglob(".*hasler-tmp"))
    assert doc["exposed"] == ["mpd 0.0.0.0:6600", "camilladsp 0.0.0.0:1234"], "running ones change at restart"


def test_apply_again_changes_nothing(root):
    H.apply(restart=False)
    os.utime(root / "usr/share/alsa/alsa.conf", (1_000_000, 1_000_000))
    doc = H.apply(restart=False)
    assert doc["ok"] and doc["changed"] == []
    assert (root / "usr/share/alsa/alsa.conf").stat().st_mtime == 1_000_000


def test_apply_follows_a_linked_file_and_clears_a_stale_temp(root, tmp_path):
    real = tmp_path / "elsewhere.conf"
    real.write_text(MOODE_MPD_CONF)
    (root / "etc/mpd.conf").unlink()
    (root / "etc/mpd.conf").symlink_to(real)
    (root / "etc/.elsewhere.conf.hasler-tmp").symlink_to(tmp_path / "nowhere")
    (tmp_path / ".elsewhere.conf.hasler-tmp").symlink_to(tmp_path / "nowhere")
    assert H.apply(restart=False)["ok"]
    assert 'bind_to_address "127.0.0.1"' in real.read_text() and (root / "etc/mpd.conf").is_symlink()
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.parametrize("rel,cut", [("etc/mpd.conf", MOODE_MPD_CONF.index("format ")),
                                      ("etc/mpd.conf", 120),
                                      ("etc/alsa/conf.d/camilladsp.conf", MOODE_CDSP_CONF.index("-s "))])
def test_a_file_caught_half_written_is_left_alone(root, rel, cut):
    f = root / rel
    f.write_text(f.read_text()[:cut])
    before = f.read_text()
    doc = H.apply(restart=False)
    assert f.read_text() == before, "never cut short"
    assert not doc["ok"] and "looks incomplete" in doc["errors"][0]
    assert len(doc["changed"]) == 1, "the other file is still fixed"


def test_apply_without_moode_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "ROOT", str(tmp_path))
    doc = H.apply(restart=True)
    assert doc == {"ok": True, "changed": [], "exposed": []}


@pytest.mark.parametrize("state,restarted", [("stop", True), ("pause", True), ("play", False), (None, False)])
def test_an_exposed_mpd_is_restarted_only_when_quiet(root, monkeypatch, state, restarted):
    calls = []
    monkeypatch.setattr(H, "mpd_state", lambda: state)
    monkeypatch.setattr(H, "restart_mpd", lambda: calls.append(1) or "restarted")
    doc = H.apply(restart=True)
    assert bool(calls) is restarted
    assert doc["mpd"] == "restarted" if restarted else doc["mpd"].startswith("left alone")


def test_a_local_mpd_is_never_restarted(root, monkeypatch):
    (root / "proc/net/tcp").write_text(LOCAL_TCP)
    (root / "proc/net/tcp6").write_text(LOCAL_TCP6)
    monkeypatch.setattr(H, "mpd_state", lambda: pytest.fail("asked MPD"))
    monkeypatch.setattr(H, "restart_mpd", lambda: pytest.fail("restarted MPD"))
    doc = H.apply(restart=True)
    assert "mpd" not in doc and doc["exposed"] == []


# --- check ----------------------------------------------------------------------------------


def test_check_reads_proc_like_ss(root, capsys):
    assert H.main(["x", "check"]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["listening"] == {"mpd": ["0.0.0.0"], "camilladsp": ["0.0.0.0"]}
    assert not doc["ok"] and doc["exposed"] == ["mpd 0.0.0.0:6600", "camilladsp 0.0.0.0:1234"]
    (root / "proc/net/tcp").write_text(LOCAL_TCP)
    (root / "proc/net/tcp6").write_text(LOCAL_TCP6)
    assert H.main(["x", "check"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] and doc["listening"] == {"mpd": ["127.0.0.1", "::1"], "camilladsp": ["127.0.0.1"]}


def test_an_ipv6_wildcard_is_exposed(root):
    (root / "proc/net/tcp").write_text(LOCAL_TCP)
    (root / "proc/net/tcp6").write_text(PROC_TCP6.replace(":1F90 ", ":19C8 "))
    assert H.exposed(H.listening()) == ["mpd [::]:6600"]


# --- shipped and hooked up --------------------------------------------------------------------


def test_it_runs_before_every_mpd_start_and_after_every_release():
    dropins = ROOT / "config/systemd-dropins/mpd.service.d"
    conf = (dropins / "hasler-bind-local.conf").read_text()
    assert "ExecStartPre=-+/usr/local/sbin/hasler-local-only apply\n" in conf
    # before the amp-card wait of bootstrap.sh --early-mpd (drop-ins are read in name order)
    names = ["hasler-early.conf", "hasler-bind-local.conf"]
    assert min(names) == "hasler-bind-local.conf"
    unit = (ROOT / "config/haslerd.service").read_text()
    assert "ExecStartPre=-+/usr/local/sbin/hasler-local-only apply --restart-mpd\n" in unit
    build = (ROOT / "scripts/release/build_release.py").read_text()
    assert 'Rule("scripts/hasler-local-only", "integration/sbin/", mode=0o755)' in build
    assert os.access(ROOT / "scripts/hasler-local-only", os.X_OK)
