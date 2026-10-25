"""A hung MPD (2026-10-25): alive, port 6600 open, answering nobody.

MPD 0.24's ffmpeg decoder stuck in libavformat's HLS retry loop, the main
thread waiting on it. haslerd queued every caller behind the shared
connection's lock for a full timeout each - the panel loop among them, so
systemd's watchdog killed haslerd, and the updater rolled back an innocent
release. Here against real sockets (a server that accepts and never says a
word) and, for the recovery, a fake MPD that hangs on demand.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from conftest import VARIANTS
from test_stream_watchdog import SJ, FakeMpd

from haslerd.actions import Actions
from haslerd.health import health_report
from haslerd.main import LampKeeper, Watchdog
from haslerd.panel.controls import Event
from haslerd.player.moode import MoodeConfig, MoodePlayer
from haslerd.player.sources.radio import MpdConnection, MpdUnresponsive, RadioSource, mpd_probe
from haslerd.player.watchdog import MPD_HANG_S, MPD_RESTART, StreamEvents, StreamWatchdog
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp, WebServer


# -- servers ------------------------------------------------------------------


class HungServer:
    """Accepts connections and never sends a byte: MPD on 2026-10-25."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.held: list[socket.socket] = []
        self._run = True
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while self._run:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)

    def close(self) -> None:
        self._run = False
        for conn in self.held:
            conn.close()
        self.sock.close()


class TalkingServer:
    """Greets like MPD and answers every line with OK."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _serve(conn: socket.socket) -> None:
        with conn:
            conn.sendall(b"OK MPD 0.24.0\n")
            for _line in conn.makefile("rb"):
                conn.sendall(b"OK\n")

    def close(self) -> None:
        self.sock.close()


class SocketMpd:
    """Just enough of python-mpd2's MPDClient over a real socket: timeouts as MPDClient has them."""

    def __init__(self) -> None:
        self._timeout = None
        self.sock: socket.socket | None = None
        self.file = None

    @property
    def timeout(self):
        return self._timeout

    @timeout.setter
    def timeout(self, value) -> None:
        self._timeout = value
        if self.sock is not None:
            self.sock.settimeout(value)

    def connect(self, host, port) -> None:
        self.sock = socket.create_connection((host, port), timeout=self._timeout)
        self.sock.settimeout(self._timeout)
        self.file = self.sock.makefile("rb")
        if not self.file.readline().startswith(b"OK MPD"):
            raise ConnectionError("not MPD")

    def disconnect(self) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def __getattr__(self, name):
        def command(*args):
            self.sock.sendall((" ".join([name, *map(str, args)]) + "\n").encode())
            doc = {}
            while True:
                line = self.file.readline().decode()
                if not line:
                    raise ConnectionError("connection lost")
                if line.startswith("OK"):
                    return doc
                key, _, value = line.rstrip("\n").partition(": ")
                doc[key] = value

        return command


@pytest.fixture
def hung():
    server = HungServer()
    yield server
    server.close()


def timed(fn):
    t0 = time.monotonic()
    try:
        return fn(), time.monotonic() - t0
    except Exception as exc:
        return exc, time.monotonic() - t0


# -- the probe and the connection -----------------------------------------------


class TestProbe:
    def test_hung_talking_and_closed(self, hung):
        result, took = timed(lambda: mpd_probe("127.0.0.1", hung.port, 0.4))
        assert result == "timeout" and took < 1.0
        talking = TalkingServer()
        try:
            assert mpd_probe("127.0.0.1", talking.port, 1.0) == "ok"
        finally:
            talking.close()
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        assert mpd_probe("127.0.0.1", port, 0.5) == "refused"


class TestHungConnection:
    def test_one_timeout_then_everyone_fails_fast(self, hung):
        conn = MpdConnection("127.0.0.1", hung.port, timeout=0.8, client_factory=SocketMpd)
        results = {}

        def ask(who):
            results[who] = timed(lambda: conn().status())

        first = threading.Thread(target=ask, args=("first",))
        first.start()
        time.sleep(0.1)
        waiter = threading.Thread(target=ask, args=("waiter",))   # queued behind the lock
        waiter.start()
        first.join(5)
        waiter.join(5)
        assert isinstance(results["first"][0], (TimeoutError, MpdUnresponsive))
        assert results["first"][1] < 1.5
        # The waiter gave up when the first found out - not a timeout of its own.
        assert isinstance(results["waiter"][0], MpdUnresponsive)
        assert results["waiter"][1] < 1.5
        assert conn.unresponsive_s() is not None
        later, took = timed(lambda: conn().status())
        assert isinstance(later, MpdUnresponsive) and took < 0.1

    def test_a_probe_that_hears_mpd_again_clears_it(self, hung):
        state = {"answer": "timeout"}
        conn = MpdConnection("127.0.0.1", hung.port, timeout=0.3, client_factory=SocketMpd,
                             prober=lambda h, p, t: state["answer"])
        with pytest.raises(Exception):
            conn().status()
        assert conn.probe() == "timeout" and conn.unresponsive_s() is not None
        state["answer"] = "ok"
        assert conn.probe() == "ok" and conn.unresponsive_s() is None


# -- the web page, a key press and the panel loop, against a hung MPD ---------------


class AskingYtm:
    """The ytm source's questions (is_active, now_playing) on the shared connection, as ytm.py asks them."""

    name = "ytm"
    backend = "mpd"

    def __init__(self, mpd):
        self.mpd = mpd

    def play(self, preset):
        self.mpd().clear()

    def stop(self):
        self.mpd().stop()

    def next_track(self):
        pass

    def is_active(self):
        try:
            client = self.mpd()
            return client.status().get("state") == "play" and "ytm" in client.currentsong().get("file", "")
        except Exception:
            return False

    def now_playing(self):
        try:
            self.mpd().currentsong()
        except Exception:
            pass
        return None

    def close(self):
        pass


@pytest.fixture
def box(hung, tmp_path):
    # A full timeout well above what the page and the panel loop may wait (1 s).
    conn = MpdConnection("127.0.0.1", hung.port, timeout=3.0, client_factory=SocketMpd)
    radio = RadioSource(mpd=conn)
    player = MoodePlayer(MoodeConfig(source_flags={}, amixer="/nonexistent/amixer"),
                         sources={"radio": radio, "ytm": AskingYtm(conn)})
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    variant.presets = [{"n": n, "name": f"Station {n}", "radio": f"{SJ}?{n}"} for n in range(1, 7)]
    actions = Actions(variant, player)
    replies = []
    actions.on_reply = replies.append
    panel = Panel.from_variant(variant, fake=True)
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "ytm": True}, ha=False)
    app.ytm = player.sources["ytm"]
    watch = StreamWatchdog(actions, lock=app.lock, run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="active", stderr=""))
    app.stream_watch = watch
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.port}", app=app, actions=actions, player=player,
                          conn=conn, replies=replies, watch=watch)
    server.close()


def call(url, method="GET", body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read()), time.monotonic() - t0
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read()), time.monotonic() - t0


class TestWebWhileMpdHangs:
    def test_status_and_health_answer_quickly(self, box):
        status, doc, took = call(box.url + "/api/status")
        assert status == 200 and took < 2.0
        status, doc, took = call(box.url + "/api/health")
        assert status == 200 and took < 2.0
        assert doc["checks"]["mpd"] is False and doc["ok"] is False

    def test_a_key_press_fails_fast_and_blocks_nobody(self, box):
        status, _, took = call(box.url + "/api/press", "POST", {"key": "k1"})
        assert status == 200 and took < 2.0         # the start is the worker's
        # Meanwhile the worker is stuck on MPD for its full timeout; the page isn't.
        for path in ("/api/status", "/api/health"):
            status, _, took = call(box.url + path)
            assert status == 200 and took < 2.0, path
        assert box.player.wait_idle(6)
        assert "fail" in box.replies               # the lamp says so
        assert box.conn.unresponsive_s() is not None
        # The next press: MPD is known to be out, so it fails at once.
        box.replies.clear()
        call(box.url + "/api/press", "POST", {"key": "k2"})
        t0 = time.monotonic()
        assert box.player.wait_idle(2) and time.monotonic() - t0 < 1.0
        assert "fail" in box.replies
        status, doc, took = call(box.url + "/api/health")
        assert took < 2.0 and doc["checks"]["mpd"] is False and doc["mpd"]["unresponsive_s"] is not None

    def test_volume_and_save_dont_wait_on_mpd(self, box):
        box.player._amixer = lambda *args: None                # no ALSA here
        box.player.start_preset(box.actions.presets[1])      # the worker now hangs on MPD
        time.sleep(0.2)
        status, _, took = call(box.url + "/api/volume", "POST", {"value": 30})
        assert status == 200 and took < 4.0, took   # well under the 8 s MPD timeout; a busy CI box is slow
        status, cfg, _ = call(box.url + "/api/config")
        status, _, took = call(box.url + "/api/config", "PUT",
                               {"keys": [{k: v for k, v in key.items() if k != "label"} for key in cfg["keys"]],
                                "presets": cfg["presets"]})
        assert status == 200 and took < 4.0, took   # well under the 8 s MPD timeout; a busy CI box is slow


class FakeDriver:
    def __init__(self):
        self.shows, self.replies = [], []

    def show(self, look, fade_s=0.0):
        self.shows.append(look)

    def reply(self, name):
        self.replies.append(name)

    def countdown(self, progress):
        pass

    def set_night(self, night):
        pass

    def close(self, halt_dark=True):
        pass


class TestHeartbeat:
    def test_the_panel_loop_keeps_pinging_systemd(self, box, monkeypatch):
        monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent")
        monkeypatch.setenv("WATCHDOG_USEC", str(200_000))   # ping every 0.1 s
        dog = Watchdog()
        pings = []
        dog._send = lambda message: pings.append(time.monotonic())
        dog.ping(time.monotonic())                           # from before the keeper asks anything
        keeper = LampKeeper(FakeDriver(), box.actions)
        keeper.loading_started(box.actions.presets[1])     # the loading lamp asks MPD too
        end = time.monotonic() + 5.0                         # > the connection's 3 s timeout
        while time.monotonic() < end:
            now = time.monotonic()
            keeper.tick(now)
            dog.ping(now)
            time.sleep(0.02)
        gaps = [b - a for a, b in zip(pings, pings[1:])]
        assert len(pings) > 10 and max(gaps) < 1.6, max(gaps)


# -- recovery: restart MPD (once, rate-limited), then the preset again ----------------


class HangingMpd(FakeMpd):
    """FakeMpd that can hang: every command times out, as on a socket."""

    def __init__(self, world):
        super().__init__()
        self.world = world

    def connect(self, host, port):
        if self.world["hung"]:
            raise TimeoutError("timed out")

    def disconnect(self):
        pass

    def __getattribute__(self, name):
        if name in ("ping", "status", "clear", "add", "play", "stop", "currentsong", "random", "next") \
                and object.__getattribute__(self, "world")["hung"]:
            def hang(*a, **k):
                raise TimeoutError("timed out")
            return hang
        return object.__getattribute__(self, name)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def rec(tmp_path):
    world = {"hung": False, "mpd": None}
    clock = Clock()

    def factory():
        world["mpd"] = HangingMpd(world)
        return world["mpd"]

    conn = MpdConnection(client_factory=factory, clock=clock,
                         prober=lambda h, p, t: "timeout" if world["hung"] else "ok")
    radio = RadioSource(mpd=conn)
    player = MoodePlayer(MoodeConfig(source_flags={}), sources={"radio": radio})
    player.set_mute = lambda muted: None
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    variant.presets = [{"n": 1, "name": "Radio Swiss Classic", "radio": "http://stream.srg-ssr.ch/m/rsc_de/mp3_128"},
                       {"n": 2, "name": "Radio Swiss Jazz", "radio": SJ}]
    actions = Actions(variant, player)
    replies = []
    actions.on_reply = replies.append
    ran = []

    def run(cmd, **kw):
        ran.append(list(cmd))
        if cmd[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if tuple(cmd) == MPD_RESTART and world.get("restart_fixes", True):
            world["hung"] = False                   # a fresh MPD: stopped, empty queue
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    events = StreamEvents(tmp_path / "stream-events.jsonl")
    watch = StreamWatchdog(actions, clock=clock, events=events, run=run)
    actions.stalled = watch.stalled_on
    app = SimpleNamespace(actions=actions, player=player, stream_watch=watch, panel=None, lamp=True)
    return SimpleNamespace(world=world, clock=clock, conn=conn, player=player, actions=actions, watch=watch,
                           ran=ran, events=events, replies=replies, app=app, path=tmp_path / "stream-events.jsonl")


def advance(r, seconds, step=2.0):
    end = r.clock.t + seconds
    while r.clock.t < end:
        r.clock.t += step
        r.watch.check()
        r.player.wait_idle(3)


def restarts(r):
    return [c for c in r.ran if tuple(c) == MPD_RESTART]


class TestMpdRecovery:
    def test_hang_restart_once_then_the_preset_again(self, rec):
        rec.actions.dispatch(Event("k2", "tap"))
        rec.player.wait_idle(3)
        advance(rec, 6)                                 # heard: watching
        rec.world["hung"] = True
        advance(rec, 6)
        assert rec.watch.recovering and rec.watch.breathing and rec.watch.silent
        assert rec.watch.reason.startswith("MPD")
        doc = health_report(rec.app)
        assert doc["checks"]["mpd"] is False and doc["ok"] is False
        assert doc["recovering"] is True and doc["mpd"]["recovering"] is True
        assert doc["recovering_reason"].startswith("MPD")
        assert restarts(rec) == []                      # not before MPD_HANG_S
        advance(rec, MPD_HANG_S + 4)
        assert len(restarts(rec)) == 1
        advance(rec, 4)
        assert not rec.watch.recovering
        mpd = rec.world["mpd"]
        assert mpd.state == "play" and mpd.queue == [SJ], "preset 2 started again"
        actions = [e["action"] for e in rec.events.recent()]
        assert "mpd unresponsive" in [json.loads(l)["action"] for l in rec.path.read_text().splitlines()]
        assert "mpd unresponsive -> restarted" in actions and "mpd back" in actions
        back = [e for e in rec.events.recent() if e["action"] == "mpd back"][-1]
        assert back["replayed"] == "2" and back["restarted_by_haslerd"] is True
        assert health_report(rec.app)["mpd"]["restarts_last_hour"] == 1

    def test_the_failed_press_is_what_comes_back(self, rec):
        """The incident: a press MPD never took (current() None after it failed)."""
        rec.world["hung"] = True
        rec.actions.dispatch(Event("k2", "tap"))
        rec.player.wait_idle(3)
        assert rec.player.current() is None and rec.player.wanted.n == 2
        rec.world["hung"] = False                       # systemd's watchdog restarted it
        rec.world["mpd"] = None
        advance(rec, 4)
        assert rec.world["mpd"].queue == [SJ] and rec.world["mpd"].state == "play"
        assert restarts(rec) == []

    def test_nothing_comes_back_after_standby(self, rec):
        rec.actions.dispatch(Event("k2", "tap"))
        rec.player.wait_idle(3)
        rec.world["hung"] = True
        advance(rec, 4)
        rec.actions.standby = True
        rec.world["hung"] = False
        rec.world["mpd"].state = "stop"                 # a fresh MPD
        advance(rec, 4)
        assert rec.world["mpd"].state != "play"

    def test_rate_limited(self, rec):
        rec.world["restart_fixes"] = False              # restarting doesn't help
        rec.actions.dispatch(Event("k2", "tap"))
        rec.player.wait_idle(3)
        rec.world["hung"] = True
        advance(rec, 3600, step=10.0)
        assert len(restarts(rec)) == 3                  # MPD_RESTARTS_PER_H
        at = [e for e in json.loads("[" + ",".join(rec.path.read_text().splitlines()) + "]")]
        assert any(e["action"] == "mpd restart withheld" for e in at)
        assert rec.watch.reason == "MPD not answering; restart limit reached"

    def test_no_restart_while_systemd_is_already_at_it(self, rec):
        rec.actions.dispatch(Event("k2", "tap"))
        rec.player.wait_idle(3)
        rec.world["hung"] = True
        real = rec.watch.mpd_recovery.run

        def busy(cmd, **kw):
            if cmd[:2] == ["systemctl", "is-active"]:
                rec.ran.append(list(cmd))
                return SimpleNamespace(returncode=3, stdout="deactivating\n", stderr="")
            return real(cmd, **kw)

        rec.watch.mpd_recovery.run = busy
        advance(rec, MPD_HANG_S + 10)
        assert restarts(rec) == []


def test_sudoers_allows_exactly_the_mpd_restart():
    from conftest import ROOT
    line = [l for l in (ROOT / "config/sudoers/haslerd").read_text().splitlines() if l.startswith("pi ")][0]
    assert "/usr/bin/systemctl restart mpd.service," in line
    assert MPD_RESTART[:2] == ("sudo", "-n") and " ".join(MPD_RESTART[2:]) == "/usr/bin/systemctl restart mpd.service"


def test_mpd_dropin_watchdog():
    from conftest import ROOT
    text = (ROOT / "config/systemd-dropins/mpd.service.d/hasler-watchdog.conf").read_text()
    lines = [l for l in text.splitlines() if l and not l.startswith("#")]
    assert lines == ["[Service]", "WatchdogSec=20", "TimeoutStopSec=10", "Restart=on-failure", "RestartSec=1"]
