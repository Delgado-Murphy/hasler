"""Accepting Bluetooth pairing requests only while the window is open."""

import io
import threading
import time

from haslerd.player.btaccept import PairAcceptor, parse_request


def test_parse_agent_lines():
    assert parse_request("confirm(/org/bluez/hci0/dev_B0_9C) code=921218 -> req b5039031") == (
        "confirm", "/org/bluez/hci0/dev_B0_9C", "b5039031")
    assert parse_request("authorize(/org/bluez/hci0/dev_X) code= -> req 0123abcd")[2] == "0123abcd"
    assert parse_request("Cancel") is None
    assert parse_request("confirm(/x) code=1 -> req ZZZZZZZZ") is None
    assert parse_request("confirm(/x) code=1 -> req 1234; rm -rf /") is None


class FakeProc:
    def __init__(self, lines):
        self.stdout = io.StringIO("".join(line + "\n" for line in lines))
        self.killed = False

    def kill(self):
        self.killed = True

    def poll(self):
        return 0 if self.killed else None


def run(acceptor):
    acceptor._thread.join(2)
    assert not acceptor._thread.is_alive()


def test_accepts_requests_while_open():
    proc = FakeProc(["registered", "confirm(/d/dev_A) code=111111 -> req aaaaaaaa", "Cancel",
                     "confirm(/d/dev_A) code=222222 -> req bbbbbbbb"])
    got = []
    a = PairAcceptor(follow=lambda: proc, accept=lambda r: got.append(r) or True)
    a.open(60)
    run(a)
    assert got == ["aaaaaaaa", "bbbbbbbb"] and a.accepted == got and proc.killed


def test_nothing_accepted_once_closed():
    now = [0.0]
    proc = FakeProc(["confirm(/d/dev_A) code=111111 -> req aaaaaaaa"])
    got = []
    a = PairAcceptor(follow=lambda: proc, accept=lambda r: got.append(r) or True, clock=lambda: now[0])
    gate = threading.Event()
    real = a._watch

    def late():
        gate.wait(2)
        real()
    a._watch = late
    a.open(10)
    now[0] = 11.0        # window over before the request arrives
    gate.set()
    run(a)
    assert got == []


def test_failed_accept_not_recorded():
    proc = FakeProc(["confirm(/d/dev_A) code=111111 -> req aaaaaaaa"])
    a = PairAcceptor(follow=lambda: proc, accept=lambda r: False)
    a.open(60)
    run(a)
    assert a.accepted == []


def test_reopening_extends_one_watcher():
    started = []

    def follow():
        started.append(1)
        time.sleep(0.2)
        return FakeProc([])
    a = PairAcceptor(follow=follow, accept=lambda r: True)
    a.open(60)
    a.open(120)
    a._thread.join(2)
    assert len(started) == 1
