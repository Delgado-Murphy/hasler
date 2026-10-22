"""Multiroom audio (multiroom/audio.py): SnapAudio around its root helper."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from haslerd.multiroom import audio as mra
from haslerd.multiroom.audio import MultiroomError, SnapAudio, buffer_ms, connected_to_port

REPO = Path(__file__).resolve().parents[2]

# /proc/net/tcp lines: local, remote (hex ip:port), state 01 = ESTABLISHED.
TCP_HEAD = "  sl  local_address rem_address   st tx_queue rx_queue\n"
TCP_TO_1704 = TCP_HEAD + "   0: 0100007F:D2F0 0100007F:06A8 01 00000000:00000000\n"
TCP_LISTEN = TCP_HEAD + "   0: 00000000:06A8 00000000:0000 0A 00000000:00000000\n"


class Box:
    """The helper, systemd and mpc, faked: the helper writes the /run files."""

    def __init__(self, tmp_path, playing=False, answer=None, times=""):
        self.run_dir = tmp_path / "run"
        self.playing = playing
        self.times = times
        self.answer = answer
        self.calls = []
        self.units = set()
        self.tcp = tmp_path / "tcp"
        self.tcp.write_text(TCP_HEAD)
        self.conf = tmp_path / "snapserver.conf"
        self.conf.write_text((REPO / "config/snapcast/snapserver.conf").read_text())
        self.dsp = tmp_path / ".hasler-dsp"

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[0] == "sudo":
            assert argv[:2] == ["sudo", "-n"] and kw.get("timeout")
            if self.answer is not None:
                return SimpleNamespace(stdout=self.answer, stderr="boom", returncode=1)
            return self._helper(argv[3:])
        if argv[:2] == ["mpc", "status"]:
            return SimpleNamespace(stdout=f"x\n[playing] #1/3 {self.times}\n" if self.playing else "x\n",
                                   stderr="")
        return SimpleNamespace(stdout="", stderr="")

    def _helper(self, args):
        self.run_dir.mkdir(exist_ok=True)
        mode = args[0]
        if mode == "alone":
            for f in ("mode", "client.env"):
                (self.run_dir / f).unlink(missing_ok=True)
            self.units.clear()
            leader = None
        else:
            host, port = ("127.0.0.1", "1704") if mode == "lead" else (args[1], args[2])
            (self.run_dir / "mode").write_text(mode + "\n")
            (self.run_dir / "client.env").write_text(f"SNAP_HOST={host}\nSNAP_PORT={port}\n")
            self.units = {mra.CLIENT_UNIT} | ({mra.SERVER_UNIT} if mode == "lead" else set())
            if self.dsp.exists():
                self.units.add(mra.DSP_UNIT)
            leader = f"{host}:{port}" if mode == "follow" else None
        doc = {"ok": True, "mode": mode, "server": mra.SERVER_UNIT in self.units,
               "client": mra.CLIENT_UNIT in self.units, "leader": leader}
        return SimpleNamespace(stdout=json.dumps(doc) + "\n", stderr="", returncode=0)

    def audio(self, **kw):
        return SnapAudio(helper="/usr/local/sbin/hasler-multiroom", run=self, run_dir=self.run_dir,
                         unit_active=lambda u: u in self.units, server_conf=self.conf,
                         tcp_tables=(self.tcp,), available=lambda p: True,
                         dsp_mark=self.dsp, **kw)

    def helper_calls(self):
        return [c[3:] for c in self.calls if c[0] == "sudo"]


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_starts_alone(box):
    assert box.audio().status() == {"mode": "alone", "server": False, "client": False,
                                    "connected": False, "leader": None, "latency_ms": 0,
                                    "error": None}


def test_lead_runs_the_helper_and_reports_the_buffer(box):
    a = box.audio()
    a.lead()
    assert box.helper_calls() == [["lead"]]
    box.tcp.write_text(TCP_TO_1704)
    st = a.status()
    assert st["mode"] == "lead" and st["server"] and st["client"] and st["connected"]
    assert st["leader"] is None and st["error"] is None
    assert st["latency_ms"] == 1000          # the shipped snapserver.conf


def test_follow_names_the_leader(box):
    a = box.audio()
    a.follow("192.168.1.20", 1704)
    assert box.helper_calls() == [["follow", "192.168.1.20", "1704"]]
    box.tcp.write_text(TCP_TO_1704)
    st = a.status()
    assert (st["mode"], st["server"], st["client"], st["leader"]) == ("follow", False, True, "192.168.1.20:1704")
    assert st["connected"] and st["error"] is None


def test_follow_not_connected_is_an_error_but_not_a_failure(box):
    a = box.audio()
    a.follow("hasler-kitchen.local")
    box.tcp.write_text(TCP_LISTEN)            # only a listening socket: no leader
    st = a.status()
    assert st["leader"] == "hasler-kitchen.local:1704"
    assert not st["connected"] and st["error"] == "not connected to the leader"


@pytest.mark.parametrize("host,port", [
    ("", 1704), ("-oProxy", 1704), ("a b", 1704), ("x;reboot", 1704), (None, 1704),
    ("h", 0), ("h", 70000), ("h", "1704"), ("h", True),
])
def test_follow_refuses_bad_targets_before_any_helper(box, host, port):
    with pytest.raises(ValueError):
        box.audio().follow(host, port)
    assert box.helper_calls() == []


def test_alone_after_lead_back_to_alone(box):
    a = box.audio()
    a.lead()
    a.alone()
    assert a.status()["mode"] == "alone" and a.status()["latency_ms"] == 0
    assert box.helper_calls() == [["lead"], ["alone"]]


def test_switch_reopens_a_playing_mpd_but_never_stops_it_for_good(tmp_path):
    box = Box(tmp_path, playing=True)
    a = box.audio()
    a.lead()
    assert box.calls[-2:] == [["mpc", "-q", "stop"], ["mpc", "-q", "play"]]
    n = len(box.calls)
    a.lead()                                   # idempotent: nothing moves, nothing reopens
    assert not any(c[0] == "mpc" for c in box.calls[n:])
    a.alone()                                  # a preset that started meanwhile moves to the card
    assert box.calls[-1] == ["mpc", "-q", "play"]


def test_reopen_keeps_a_seekable_items_position(tmp_path):
    box = Box(tmp_path, playing=True, times="  1:23:05/2:00:00 (69%)")
    box.audio().lead()
    assert box.calls[-3:] == [["mpc", "-q", "stop"], ["mpc", "-q", "play"],
                              ["mpc", "-q", "seek", "1:23:05"]]


def test_reopen_of_a_radio_stream_does_not_seek(tmp_path):
    box = Box(tmp_path, playing=True, times="  12:34/0:00 (0%)")
    box.audio().lead()
    assert box.calls[-1] == ["mpc", "-q", "play"]


@pytest.mark.parametrize("text,at", [
    ("Song\n[playing] #3/12   1:23/4:56 (28%)\nvolume: 40%", "0:01:23"),
    ("[playing] #1/1   0:00/4:56 (0%)", None),          # at the start anyway
    ("[playing] #1/1   7:02/0:00 (0%)", None),          # a stream: no total
    ("[paused]  #1/1   1:00/4:56 (20%)", None),
    ("volume: 11%   repeat: off", None), (None, None),
])
def test_resume_at(text, at):
    assert mra.resume_at(text) == at


def test_follow_does_not_touch_the_local_players(tmp_path):
    box = Box(tmp_path, playing=True)
    box.audio().follow("10.0.0.2")
    assert not any(c[0] == "mpc" for c in box.calls)


def test_idle_mpd_is_left_alone(box):
    box.audio().lead()
    assert ["mpc", "-q", "play"] not in box.calls


def test_custom_reopen_hook(box):
    seen = []
    a = box.audio(reopen=lambda: seen.append("reopen"))
    a.lead()
    a.alone()
    assert seen == ["reopen", "reopen"]
    assert not any(c[0] == "mpc" for c in box.calls)


def test_a_failing_reopen_never_fails_the_switch(box):
    def boom():
        raise RuntimeError("mpd gone")
    box.audio(reopen=boom).lead()


def test_helper_failure_raises_and_shows_in_status(tmp_path):
    box = Box(tmp_path, answer=json.dumps({"ok": False, "error": "snapserver did not start",
                                           "mode": "alone"}))
    a = box.audio()
    with pytest.raises(MultiroomError, match="snapserver did not start"):
        a.lead()
    assert a.status()["error"] == "snapserver did not start"


def test_helper_garbage_is_a_failure(tmp_path):
    box = Box(tmp_path, answer="not json")
    with pytest.raises(MultiroomError, match="boom"):
        box.audio().lead()


def test_helper_timeout_is_a_failure(box):
    def run(argv, **kw):
        if argv[0] == "sudo":
            raise subprocess.TimeoutExpired(argv, 20)
        return SimpleNamespace(stdout="", stderr="")
    a = SnapAudio(run=run, run_dir=box.run_dir, available=lambda p: True,
                  unit_active=lambda u: False, tcp_tables=(box.tcp,))
    with pytest.raises(MultiroomError, match="did not answer"):
        a.lead()


def test_not_installed(box):
    a = SnapAudio(run=box, run_dir=box.run_dir, available=lambda p: False,
                  unit_active=lambda u: False, tcp_tables=(box.tcp,))
    assert a.available is False
    a.alone()                                  # nothing to undo, no error
    with pytest.raises(MultiroomError, match="isn't installed"):
        a.lead()
    with pytest.raises(MultiroomError):
        a.follow("10.0.0.2")
    assert box.helper_calls() == []


def test_available_needs_helper_and_snapcast(box):
    have = {"/usr/local/sbin/hasler-multiroom", "/usr/bin/snapserver"}
    a = SnapAudio(available=lambda p: p in have)
    assert a.available is False
    have.add("/usr/bin/snapclient")
    assert a.available is True


def test_a_unit_that_died_shows_as_an_error(box):
    a = box.audio()
    a.lead()
    box.units.discard(mra.SERVER_UNIT)
    assert a.status()["error"] == "snapserver isn't running"
    box.units = {mra.SERVER_UNIT}
    assert a.status()["error"] == "snapclient isn't running"


def test_tone_dsp_is_checked_only_when_the_dsp_is_on(box):
    box.dsp.write_text("")
    a = box.audio()
    a.lead()
    box.tcp.write_text(TCP_TO_1704)
    assert a.status()["error"] is None
    box.units.discard(mra.DSP_UNIT)
    assert "CamillaDSP" in a.status()["error"]


def test_status_without_run_dir_or_proc(tmp_path):
    a = SnapAudio(run_dir=tmp_path / "nope", unit_active=lambda u: False,
                  tcp_tables=(tmp_path / "none",), server_conf=tmp_path / "none.conf",
                  available=lambda p: True)
    assert a.status()["mode"] == "alone"


# -- pure helpers --------------------------------------------------------------

def test_buffer_ms_reads_the_stream_section_only():
    assert buffer_ms("[stream]\nbuffer = 800 # ms\n") == 800
    assert buffer_ms("[other]\nbuffer = 5\n[stream]\nport = 1704\n") == mra.BUFFER_MS
    assert buffer_ms(None) == mra.BUFFER_MS
    assert buffer_ms("#buffer = 3\n") == mra.BUFFER_MS


def test_connected_to_port_wants_established_remote_port():
    assert connected_to_port([TCP_TO_1704], 1704)
    assert not connected_to_port([TCP_LISTEN], 1704)
    assert not connected_to_port([TCP_TO_1704], 1705)
    six = TCP_HEAD + "   0: 0000000000000000FFFF00000100007F:D2F0 0000000000000000FFFF00000100007F:06A8 01 0\n"
    assert connected_to_port([None, six], 1704)


# -- the shipped files ----------------------------------------------------------

def test_helper_scripts_parse_and_refuse_non_root():
    for name in ("hasler-multiroom", "hasler-audio-mono"):
        script = REPO / "scripts" / name
        assert os.access(script, os.X_OK)
        subprocess.run(["sh", "-n", str(script)], check=True)
    if os.geteuid() != 0:
        out = subprocess.run([str(REPO / "scripts/hasler-multiroom"), "lead"],
                             capture_output=True, text=True)
        assert json.loads(out.stdout) == {"ok": False, "error": "must run as root"}


def test_units_are_never_enabled_and_use_the_release_paths():
    for name in ("snapserver-hasler", "snapclient-hasler", "camilladsp-hasler"):
        text = (REPO / "config" / f"{name}.service").read_text()
        assert "[Install]" not in text            # started by hasler-multiroom only
        assert "/opt/hasler/releases" not in text
    server = (REPO / "config/snapserver-hasler.service").read_text()
    assert "/opt/hasler/current/app/config/snapcast/snapserver.conf" in server
    client = (REPO / "config/snapclient-hasler.service").read_text()
    assert "--mixer none" in client and "--soundcard hasler_local" in client
    dsp = (REPO / "config/camilladsp-hasler.service").read_text()
    assert " -l error " in dsp and "-l warn" not in dsp     # its warnings fill the journal


def test_snapserver_conf_matches_the_loopback_route():
    conf = (REPO / "config/snapcast/snapserver.conf").read_text()
    helper = (REPO / "scripts/hasler-multiroom").read_text()
    assert "sampleformat = 44100:16:2" in conf
    assert 'rate 44100; format S16_LE; channels 2' in helper     # what players write
    assert "device=hw:Loopback,1,0" in conf and '"hw:Loopback,0,0"' in helper
    assert "bind_to_address = 127.0.0.1" in conf                  # control stays local
    assert "enabled = false" in conf.split("[http]")[1].split("[")[0]


def _rate_line(libs: str) -> str:
    import re
    helper = (REPO / "scripts/hasler-multiroom").read_text()
    fn = re.search(r"^rate_line\(\) \{.*?^\}$", helper, re.M | re.S).group(0)
    return subprocess.run(["sh", "-c", f"{fn}\nrate_line"], env={"SPEEX_LIBS": libs, "PATH": os.environ["PATH"]},
                          capture_output=True, text=True, check=True).stdout.strip()


def test_loopback_route_uses_speex_only_when_installed(tmp_path):
    helper = (REPO / "scripts/hasler-multiroom").read_text()
    assert helper.count("type plug\n$RATE\n") == 2           # both plugs, nothing global
    assert "defaults.pcm.rate_converter" not in helper
    lib = tmp_path / "x" / "alsa-lib" / "libasound_module_rate_speexrate_medium.so"
    pattern = str(tmp_path / "*" / "alsa-lib" / lib.name)
    assert _rate_line(pattern).startswith("#")               # absent: a comment, ALSA's default
    lib.parent.mkdir(parents=True)
    lib.write_text("")
    assert _rate_line(pattern) == 'rate_converter "speexrate_medium"'
    assert "libasound2-plugins" in (REPO / "scripts/bootstrap.sh").read_text().split("PKGS=(")[1].split(")")[0]


def test_sudoers_allows_the_helper():
    line = [l for l in (REPO / "config/sudoers/haslerd").read_text().splitlines()
            if l.startswith("pi ")][0]
    assert "/usr/local/sbin/hasler-multiroom" in line


def test_legacy_install_says_multiroom_needs_a_release():
    script = (REPO / "scripts/install.sh").read_text()
    note = script[script.index('say "done"'):]
    assert "multiroom and the room name for Spotify Connect need a release install" in note
    assert "/opt/hasler/current" in note
    assert "hasler-multiroom" not in script.replace("# hasler-multiroom", "")   # not installed there
