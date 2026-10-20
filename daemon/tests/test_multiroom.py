"""Multiroom between Haslers: discovery, the group, the keys, the lamp, the API.

Three radios (Living, Kitchen, Study) are wired together in-process: each
has its own WebApp, Actions and FakePlayer, a fake Snapcast (``FakeAudio``)
and a ``Lan`` whose fetch hands each radio-to-radio request straight to the
other radio's routes. Every "background" job runs inline, so each test reads
top to bottom with no waiting. One test at the end runs two radios over real
sockets to prove the CSRF rules let radio-to-radio calls through.
"""

from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

import pytest
from conftest import VARIANTS

from haslerd import main as haslerd_main
from haslerd.actions import Actions
from haslerd.lamp import policy
from haslerd.lamp.contract import BoxState
from haslerd.multiroom import group as group_mod
from haslerd.multiroom.group import Group, pick_leader, settings_from
from haslerd.multiroom.peers import Peer, PeerUnreachable, Peers, http_json, parse_avahi
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.state import StateStore
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.group_api import GroupApi
from haslerd.web.server import WebApp, WebServer, load_settings


def inline(fn):
    fn()


class FakeAudio:
    """SnapAudio's interface; records calls into the radio's shared log."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.mode = "alone"
        self.fail_follow: Exception | None = None

    def lead(self) -> None:
        self.log.append(("audio", "lead"))
        self.mode = "lead"

    def follow(self, host: str, port: int = 1704) -> None:
        if self.fail_follow is not None:
            raise self.fail_follow
        self.log.append(("audio", "follow", host, port))
        self.mode = "follow"

    def alone(self) -> None:
        self.log.append(("audio", "alone"))
        self.mode = "alone"

    def status(self) -> dict:
        return {"mode": self.mode, "server": None, "client": None, "leader": None,
                "latency_ms": 0, "error": None}


class LoggingPlayer(FakePlayer):
    """FakePlayer whose stop lands in the shared log too (stop before follow)."""

    def __init__(self, log: list) -> None:
        super().__init__()
        self.log = log

    def stop(self) -> None:
        self.log.append(("player", "stop"))
        super().stop()


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Radio:
    def __init__(self, lan: "Lan", rid: str, name: str, host: str, tmp_path) -> None:
        self.id, self.name, self.host = rid, name, host
        self.log: list = []
        self.replies: list[str] = []
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        variant.actions["k1.hold"] = {"do": "join_group"}
        self.player = LoggingPlayer(self.log)
        d = tmp_path / rid
        d.mkdir()
        self.state = StateStore(d / "state.json")
        self.actions = Actions(variant, self.player, state=self.state, on_reply=self.replies.append)
        self.app = WebApp(self.actions, Panel.from_variant(variant, fake=True), d / "keys.yaml",
                          {"radio": True}, ha=False, settings_file=d / "settings.json")
        self.audio = FakeAudio(self.log)
        self.peers = Peers(lambda: rid, fetch=lan.fetch, runner=lan.runner, clock=lan.clock, spawn=inline)
        self.group = Group(self.actions, self.audio, identity=lambda: (rid, name), peers=self.peers,
                           fetch=lan.fetch, spawn=inline, towards=lambda _h: host, clock=lan.clock,
                           wall=lambda: lan.wall)
        self.group.enabled = True
        self.actions.group = self.group
        self.app.group = self.group
        self.api = GroupApi(self.app, self.group)
        self.app.extra_routes.append(self.api)

    def route(self, method: str, path: str, body=None):
        try:
            answer = self.app.route_extra(method, path, body)
        except ValueError as exc:
            return 400, {"ok": False, "error": str(exc)}
        return answer if answer is not None else (404, {})

    def press(self, key: str, kind: str = "tap") -> None:
        self.actions.dispatch(Event(key, kind))

    def play(self, key: str = "k2") -> None:
        """A local preset, and the wall clock moves on (``since``)."""
        self.press(key)


class Lan:
    """The network: who answers at which address. ``down`` hosts don't."""

    def __init__(self) -> None:
        self.radios: dict[str, Radio] = {}
        self.down: set[str] = set()
        self.clock = Clock()
        self.wall = 1_700_000_000.0
        self.calls: list[tuple[str, str, str]] = []

    def add(self, radio: Radio) -> Radio:
        self.radios[radio.host] = radio
        return radio

    def fetch(self, method, url, body, timeout):
        u = urlparse(url)
        radio = self.radios.get(u.hostname)
        self.calls.append((method, u.hostname, u.path))
        if radio is None or u.hostname in self.down:
            raise PeerUnreachable(f"{url}: no route")
        assert u.port == 8080 and timeout <= 5
        body = json.loads(json.dumps(body)) if body is not None else None
        if method == "GET" and u.path == "/api/info":
            return 200, {**radio.app.info(), "id": radio.id, "name": radio.name}
        return radio.route(method, u.path + (f"?{u.query}" if u.query else ""), body)

    def runner(self, cmd, **kw):
        assert cmd == ["avahi-browse", "-rtp", "_hasler._tcp"]
        lines = []
        for r in self.radios.values():
            lines.append(f"+;eth0;IPv4;Hasler\\032radio\\032{r.name};_hasler._tcp;local")
            lines.append(f"=;eth0;IPv6;Hasler\\032radio\\032{r.name};_hasler._tcp;local;{r.name}.local;"
                         f"fe80::1;8080;\"api=1\" \"path=/api/info\"")
            lines.append(f"=;eth0;IPv4;Hasler\\032radio\\032{r.name};_hasler._tcp;local;{r.name}.local;"
                         f"{r.host};8080;\"api=1\" \"path=/api/info\"")
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(lines) + "\n", stderr="")


@pytest.fixture
def lan(tmp_path):
    lan = Lan()
    lan.living = lan.add(Radio(lan, "aaaaaaaaaaaa", "Living", "10.0.0.1", tmp_path))
    lan.kitchen = lan.add(Radio(lan, "bbbbbbbbbbbb", "Kitchen", "10.0.0.2", tmp_path))
    lan.study = lan.add(Radio(lan, "cccccccccccc", "Study", "10.0.0.3", tmp_path))
    for r in lan.radios.values():
        r.peers.refresh()
    return lan


def grouped(lan):
    """Living leads Kitchen and Study, Living playing preset 2."""
    living = lan.living
    living.play("k2")
    status, doc = living.route("PUT", "/api/group", {"members": [lan.kitchen.id, lan.study.id]})
    assert status == 200, doc
    return living, lan.kitchen, lan.study


# -- discovery -------------------------------------------------------------------


class TestDiscovery:
    def test_parse_avahi_one_address_per_radio_ipv4_first(self):
        out = "\n".join([
            "+;eth0;IPv4;Hasler radio a;_hasler._tcp;local",
            "=;eth0;IPv6;Hasler\\032radio\\032a;_hasler._tcp;local;a.local;fe80::2;8080;\"api=1\"",
            "=;eth0;IPv4;Hasler\\032radio\\032a;_hasler._tcp;local;a.local;10.0.0.9;8080;\"api=1\"",
            "=;wlan0;IPv4;Hasler\\032radio\\032a;_hasler._tcp;local;a.local;10.0.0.10;8080;\"api=1\"",
            "=;eth0;IPv4;Hasler\\032radio\\032b;_hasler._tcp;local;b.local;10.0.0.11;8081;\"api=1\"",
            "=;eth0;IPv4;Future;_hasler._tcp;local;c.local;10.0.0.12;8080;\"api=2\"",
            "=;eth0;IPv4;Broken;_hasler._tcp;local;d.local;10.0.0.13;port;\"api=1\"",
            "garbage",
        ])
        assert parse_avahi(out) == [("10.0.0.9", 8080), ("10.0.0.11", 8081)]

    def test_refresh_finds_the_others_but_not_itself(self, lan):
        ids = [p.id for p in lan.living.peers.list()]
        assert ids == [lan.kitchen.id, lan.study.id]          # sorted by name; Living left out
        kitchen = lan.living.peers.get(lan.kitchen.id)
        assert (kitchen.name, kitchen.host, kitchen.port, kitchen.online, kitchen.enabled) == \
            ("Kitchen", "10.0.0.2", 8080, True, True)

    def test_a_radio_gone_from_the_network_shows_offline(self, lan):
        lan.down.add(lan.study.host)
        lan.living.peers.refresh()
        assert lan.living.peers.get(lan.study.id).online is False
        assert lan.living.peers.get(lan.kitchen.id).online is True

    def test_no_avahi_says_why(self, lan):
        def missing(*a, **k):
            raise FileNotFoundError("avahi-browse")
        peers = Peers(lambda: "x", fetch=lan.fetch, runner=missing, spawn=inline)
        assert peers.refresh() == [] and "avahi-browse isn't installed" in peers.note

    def test_a_slow_browse_keeps_what_resolved(self, lan):
        out = lan.runner(["avahi-browse", "-rtp", "_hasler._tcp"]).stdout

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 4, output=out.encode())
        peers = Peers(lambda: lan.living.id, fetch=lan.fetch, runner=slow, spawn=inline)
        assert [p.name for p in peers.refresh()] == ["Kitchen", "Study"]

    def test_kick_only_when_stale(self, lan):
        runs = []
        peers = Peers(lambda: "x", fetch=lan.fetch, runner=lambda *a, **k: runs.append(1) or lan.runner(*a, **k),
                      clock=lan.clock, spawn=inline)
        peers.kick()
        peers.kick()
        assert len(runs) == 1
        lan.clock.t += 31
        peers.kick()
        peers.kick(force=True)
        assert len(runs) == 3

    def test_info_carries_the_multiroom_block(self, lan):
        living, kitchen, _ = grouped(lan)
        mr = living.app.info()["multiroom"]
        assert mr["enabled"] and mr["role"] == "lead" and mr["leader"] == living.id
        assert sorted(mr["members"]) == [kitchen.id, lan.study.id]
        assert mr["playing"] is True and mr["source"] == "radio" and isinstance(mr["since"], float)
        mr = kitchen.app.info()["multiroom"]
        assert mr["role"] == "follow" and mr["leader"] == living.id and mr["members"] == []


# -- forming a group from the leader's page ------------------------------------------


class TestLeaderAddsMembers:
    def test_put_members_makes_a_group(self, lan):
        living, kitchen, study = grouped(lan)
        assert living.group.role == "lead" and ("audio", "lead") in living.log
        for r in (kitchen, study):
            assert r.group.role == "follow" and r.group.leader.id == living.id
            # local players stopped before the leader's stream plays
            assert r.log[-2:] == [("player", "stop"), ("audio", "follow", "10.0.0.1", 1704)]
            assert r.group.lamp() == ("following", "radio", False)     # until the first read
        # nothing else changed on the leader: it still plays its preset
        assert living.player.current().n == 2

    def test_get_group_shapes(self, lan):
        living, kitchen, study = grouped(lan)
        status, doc = living.route("GET", "/api/group")
        assert status == 200
        assert doc["enabled"] is True and doc["role"] == "lead" and doc["follower_keys"] == "group"
        assert doc["leader"] == {"id": living.id, "name": "Living", "host": None, "port": 8080, "online": True}
        assert [(m["id"], m["name"], m["host"], m["online"], m["joining"], m["error"]) for m in doc["members"]] == \
            [(kitchen.id, "Kitchen", "10.0.0.2", True, False, None), (study.id, "Study", "10.0.0.3", True, False, None)]
        assert doc["others"] == []
        kitchen.group.check()                            # learns the leader's state and members
        status, doc = kitchen.route("GET", "/api/group")
        assert doc["role"] == "follow" and doc["leader"]["id"] == living.id and doc["leader"]["name"] == "Living"
        assert [m["id"] for m in doc["members"]] == [study.id]
        assert doc["leader_playing"] is True and doc["leader_source"] == "radio"
        assert doc["others"] == []
        _, alone = lan.study.route("POST", "/api/group/leave", {})
        assert alone["role"] == "alone"
        _, doc = living.route("GET", "/api/group")
        assert [m["id"] for m in doc["members"]] == [kitchen.id]
        assert [o["id"] for o in doc["others"]] == [study.id]
        assert set(doc["others"][0]) == {"id", "name", "host", "port", "online", "enabled", "role",
                                         "leader", "playing", "source"}

    def test_status_says_who_leads(self, lan):
        living, kitchen, _ = grouped(lan)
        assert living.app.status()["group"] == {"role": "lead", "joining": False, "leader": None, "members": 2}
        assert kitchen.app.status()["group"]["leader"] == {"id": living.id, "name": "Living"}
        assert lan.study.app.status()["group"]["role"] == "follow"

    def test_ticking_off_releases(self, lan):
        living, kitchen, study = grouped(lan)
        living.route("PUT", "/api/group", {"members": [study.id]})
        assert kitchen.group.role == "alone" and kitchen.log[-1] == ("audio", "alone")
        assert study.group.role == "follow" and living.group.role == "lead"
        living.route("PUT", "/api/group", {"members": []})
        assert study.group.role == "alone" and living.group.role == "alone"
        assert living.log[-1] == ("audio", "alone")

    def test_a_radio_with_multiroom_off_says_so(self, lan):
        lan.kitchen.group.enabled = False
        lan.living.play()
        status, doc = lan.living.route("PUT", "/api/group", {"members": [lan.kitchen.id]})
        assert status == 200
        assert doc["role"] == "alone" and doc["members"] == []
        assert doc["error"] == "Kitchen: Multiroom is off on Kitchen - switch it on in Settings"
        assert ("audio", "alone") in lan.living.log and lan.kitchen.group.role == "alone"

    def test_an_unreachable_radio_does_not_join(self, lan):
        lan.down.add(lan.study.host)
        _, doc = lan.living.route("PUT", "/api/group", {"members": [lan.kitchen.id, lan.study.id]})
        assert [m["id"] for m in doc["members"]] == [lan.kitchen.id]
        assert doc["error"] == "Study: isn't answering"

    def test_bad_requests(self, lan):
        living = lan.living
        assert living.route("PUT", "/api/group", {"members": "everyone"})[0] == 400
        assert living.route("PUT", "/api/group", {"members": ["nobody"]})[0] == 400
        assert living.route("PUT", "/api/group", {})[0] == 400
        assert living.route("DELETE", "/api/group")[0] == 405
        living.group.enabled = False
        status, doc = living.route("PUT", "/api/group", {"members": [lan.kitchen.id]})
        assert status == 409 and "Multiroom is off on Living" in doc["error"]

    def test_a_follower_cannot_lead(self, lan):
        living, kitchen, study = grouped(lan)
        status, doc = kitchen.route("PUT", "/api/group", {"members": [study.id]})
        assert status == 409 and "plays Living's music" in doc["error"]

    def test_a_leader_cannot_be_made_to_follow(self, lan):
        living, kitchen, _ = grouped(lan)
        status, doc = kitchen.route("POST", "/api/peer/follow",
                                    {"leader_id": lan.study.id, "host": "10.0.0.3"})
        assert status == 200       # kitchen is a follower: it moves to the new leader
        status, doc = living.route("POST", "/api/peer/follow", {"leader_id": lan.study.id, "host": "10.0.0.3"})
        assert status == 409 and "leads its own group" in doc["error"]

    def test_switching_leader_tells_the_old_one(self, lan):
        living, kitchen, study = grouped(lan)
        study.route("PUT", "/api/group", {"members": []})          # study is a follower; no-op
        study.group.leave()
        study.play()
        study.route("PUT", "/api/group", {"members": [kitchen.id]})
        assert kitchen.group.leader.id == study.id
        assert kitchen.id not in living.group.members
        assert living.group.role == "alone"                         # its last member went


# -- the join key ---------------------------------------------------------------------


class TestJoinKey:
    def test_joins_the_one_playing(self, lan):
        lan.living.play()
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.group.role == "follow" and lan.kitchen.group.leader.id == lan.living.id
        assert lan.living.group.role == "lead" and list(lan.living.group.members) == [lan.kitchen.id]
        assert lan.kitchen.log[-1] == ("audio", "follow", "10.0.0.1", 1704)
        assert lan.kitchen.replies == []                     # no fail; the breath was the reply

    def test_prefers_a_leader_then_the_most_recent(self, lan, tmp_path):
        lan.living.play()
        lan.wall += 60
        lan.study.play()                                     # study started later
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.group.leader.id == lan.study.id
        lan.kitchen.group.leave()
        # Living leads a (quiet) group now: a leader beats a radio that plays.
        den = lan.add(Radio(lan, "dddddddddddd", "Den", "10.0.0.4", tmp_path))
        lan.living.peers.refresh()
        lan.living.press("power", "off")
        lan.living.route("PUT", "/api/group", {"members": [den.id]})
        assert lan.living.group.role == "lead"
        lan.wall += 60
        lan.study.play("k3")
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.group.leader.id == lan.living.id

    def test_a_follower_counts_as_its_leader(self):
        a = Peer("a", "A", "1", multiroom={"enabled": True, "role": "lead", "since": 1.0})
        b = Peer("b", "B", "2", multiroom={"enabled": True, "role": "follow", "leader": "a"})
        c = Peer("c", "C", "3", multiroom={"enabled": True, "role": "alone", "playing": True, "since": 9.0})
        off = Peer("d", "D", "4", multiroom={"enabled": False, "role": "lead", "since": 99.0})
        assert pick_leader([b, c, off, a]).id == "a"
        assert pick_leader([c, off]).id == "c"
        assert pick_leader([off]) is None
        gone = Peer("e", "E", "5", multiroom={"enabled": True, "role": "lead"}, online=False)
        assert pick_leader([gone, Peer("f", "F", "6", multiroom={"enabled": True})]) is None

    def test_nothing_to_join_fails(self, lan):
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.group.role == "alone" and lan.kitchen.replies == ["fail"]
        assert lan.kitchen.group.error == "no Hasler is playing to join"
        assert lan.kitchen.group.lamp() == ("", "radio", False)

    def test_the_key_again_leaves(self, lan):
        lan.living.play()
        lan.kitchen.press("k1", "hold")
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.group.role == "alone" and lan.kitchen.replies == ["ack"]
        assert lan.living.group.role == "alone"             # told; its only member left

    def test_on_a_leader_it_fails(self, lan):
        grouped(lan)
        lan.living.press("k1", "hold")
        assert lan.living.replies[-1] == "fail" and lan.living.group.role == "lead"

    def test_multiroom_off_fails(self, lan):
        lan.living.play()
        lan.kitchen.group.enabled = False
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.replies == ["fail"] and lan.kitchen.group.role == "alone"

    def test_no_multiroom_at_all_fails(self, lan):
        lan.kitchen.actions.group = None
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.replies == ["fail"]

    def test_joining_breathes_until_the_stream_plays(self, lan):
        """With a real worker the follow happens later: the lamp breathes meanwhile."""
        jobs = []
        k = lan.kitchen
        k.group._spawn = jobs.append
        lan.living.play()
        k.press("k1", "hold")
        assert k.group.lamp()[0] == "joining"
        look, _ = policy.look_for(BoxState(group="joining"))
        assert look == policy.LOADING
        while jobs:
            jobs.pop(0)()
        assert k.group.lamp()[0] == "following" and k.group.role == "follow"

    def test_leaving_while_joining_cancels(self, lan):
        jobs = []
        k = lan.kitchen
        k.group._spawn = jobs.append
        lan.living.play()
        k.press("k1", "hold")
        k.press("power", "off")                              # standby before the join ran
        while jobs:
            jobs.pop(0)()
        assert k.group.role == "alone" and not k.group.joining
        assert ("audio", "follow", "10.0.0.1", 1704) not in k.log

    def test_joining_another_leader_tells_the_first(self, lan):
        living, kitchen, study = grouped(lan)
        study.group.leave()
        study.play("k3")
        status, doc = kitchen.route("POST", "/api/group/join", {"leader": study.id})
        assert status == 200 and kitchen.group.leader.id == study.id
        assert living.group.role == "alone" and list(study.group.members) == [kitchen.id]

    def test_api_join_names_the_leader(self, lan):
        lan.living.play()
        status, doc = lan.kitchen.route("POST", "/api/group/join", {"leader": lan.living.id})
        assert status == 200 and doc["role"] == "follow" and doc["leader"]["id"] == lan.living.id
        status, doc = lan.kitchen.route("POST", "/api/group/join", {"leader": 7})
        assert status == 400

    def test_follow_failing_pulses_red_and_tells_the_leader(self, lan):
        lan.living.play()
        lan.kitchen.audio.fail_follow = RuntimeError("snapclient won't start")
        lan.kitchen.press("k1", "hold")
        assert lan.kitchen.group.role == "alone" and lan.kitchen.replies == ["fail"]
        assert "snapclient won't start" in lan.kitchen.group.error
        assert lan.living.group.role == "alone"


# -- keys on a follower -------------------------------------------------------------


class TestFollowerKeys:
    def test_group_mode_forwards_to_the_leader(self, lan):
        living, kitchen, study = grouped(lan)
        kitchen.press("k3")
        assert kitchen.replies == ["ack"]
        assert living.player.current().n == 3 and living.player.current().uri == kitchen.actions.presets[3].uri
        assert kitchen.player.current() is None and kitchen.group.role == "follow"
        assert study.group.role == "follow"
        # living's own preset 3 is the same station: remembered as if pressed there
        assert living.state.load()["last_preset"] == 3

    def test_follower_lamp_wears_the_leaders_tint(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.press("k5")                                   # a YouTube Music preset
        kitchen.group.check()
        assert kitchen.group.lamp() == ("following", "ytm", True)
        look, _ = policy.look_for(BoxState(group="following", group_source="ytm", group_playing=True))
        assert look == policy.playing_look("ytm") and look.colour == "amber_pale"
        # a follower whose leader is silent is ready, and offline red still waits for playing
        look, _ = policy.look_for(BoxState(group="following", group_playing=False))
        assert look == policy.READY
        look, _ = policy.look_for(BoxState(group="following", group_playing=True, offline_s=999))
        assert look == policy.playing_look("radio")

    def test_same_key_again_is_judged_at_the_leader(self, lan):
        living, kitchen, study = grouped(lan)
        kitchen.press("k3")
        kitchen.press("k3")                                   # radio again: the leader stops
        assert living.actions.standby is True and living.replies[-1] == "stop"
        assert living.group.role == "alone"
        for r in (kitchen, study):
            assert r.group.role == "alone" and r.actions.standby is False      # ready, as they were
            assert r.log[-1] == ("audio", "alone")

    def test_playlist_again_is_next_track(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.press("k4")
        kitchen.press("k4")
        assert living.player.calls[-1] == ("next_track",) and living.group.role == "lead"

    def test_a_foreign_preset_is_not_remembered_by_the_leader(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.actions.presets[3] = kitchen.actions.presets[3].__class__(
            n=3, name="Kitchen FM", uri="http://kitchen.example/fm", source="radio")
        kitchen.press("k3")
        assert living.player.current().uri == "http://kitchen.example/fm"
        assert living.state.load()["last_preset"] == 2

    def test_leave_mode_plays_alone(self, lan):
        living, kitchen, study = grouped(lan)
        status, _ = kitchen.route("PUT", "/api/group", {"follower_keys": "leave"})
        assert status == 200
        kitchen.press("k3")
        assert kitchen.group.role == "alone" and kitchen.player.current().n == 3
        assert living.player.current().n == 2 and list(living.group.members) == [study.id]

    def test_leader_unreachable_for_a_key_fails(self, lan):
        living, kitchen, _ = grouped(lan)
        lan.down.add(living.host)
        kitchen.press("k3")
        assert kitchen.replies == ["ack", "fail"]

    def test_a_source_the_leader_lacks_is_refused(self, lan):
        living, kitchen, _ = grouped(lan)
        living.player.sources = {"radio": object()}
        kitchen.press("k4")                                   # Spotify
        assert kitchen.replies == ["ack", "fail"] and living.player.current().n == 2
        status, doc = living.route("POST", "/api/peer/key", {"from": kitchen.id, "preset": {
            "source": "spotify", "uri": "spotify:playlist:x"}})
        assert status == 409 and doc["error"] == "Spotify isn't set up on Living"

    def test_peer_key_from_a_stranger_is_refused(self, lan):
        grouped(lan)
        status, _ = lan.living.route("POST", "/api/peer/key", {"from": "zzz", "preset": {
            "source": "radio", "uri": "http://x/"}})
        assert status == 409
        assert lan.living.route("POST", "/api/peer/key", {"from": lan.kitchen.id})[0] == 400
        assert lan.living.route("POST", "/api/peer/key", {"from": lan.kitchen.id, "preset": {
            "source": "tape", "uri": "x"}})[0] == 400

    def test_source_key_leaves_first(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.press("pu")
        assert kitchen.group.role == "alone" and kitchen.player.calls[-1][0] == "select_source"

    def test_volume_stays_local(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.actions.dispatch(Event("volume", "change", 30.0))
        kitchen.press("k6", "hold")
        assert kitchen.group.role == "follow"
        assert living.player.volume is None and kitchen.player.volume == 32.0

    def test_the_test_button_leaves(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.app.play({"source": "radio", "uri": "http://x.example/s", "name": "x"})
        assert kitchen.group.role == "alone" and kitchen.player.current().uri == "http://x.example/s"

    def test_a_phone_casting_to_a_follower_takes_it_out(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.group.tick(lan.clock(), "airplay")
        assert kitchen.group.role == "alone" and living.group.role == "lead"


# -- standby ---------------------------------------------------------------------------


class TestStandby:
    def test_follower_standby_leaves_alone(self, lan):
        living, kitchen, study = grouped(lan)
        kitchen.press("power", "off")
        assert kitchen.actions.standby and kitchen.group.role == "alone"
        assert living.group.role == "lead" and list(living.group.members) == [study.id]
        assert study.group.role == "follow"

    def test_leader_standby_ends_the_group_and_restores_the_followers(self, lan):
        lan.study.press("power", "off")                       # Study asleep before it joins
        living, kitchen, study = grouped(lan)
        assert study.actions.standby is False                 # joining woke it
        living.press("power", "off")
        assert living.group.role == "alone" and ("audio", "alone") in living.log
        assert kitchen.group.role == "alone" and kitchen.actions.standby is False
        assert study.group.role == "alone" and study.actions.standby is True

    def test_leader_shutdown_ends_the_group(self, lan):
        living, kitchen, _ = grouped(lan)
        living.press("power", "off_held")
        assert kitchen.group.role == "alone" and living.group.role == "alone"

    def test_standby_from_the_page_on_a_follower(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.app.set_standby({"on": True})
        assert kitchen.group.role == "alone" and living.group.role == "lead"


# -- keeping in touch ----------------------------------------------------------------


class TestHeartbeat:
    def test_follower_learns_the_leaders_source(self, lan):
        living, kitchen, _ = grouped(lan)
        changes = []
        kitchen.actions.on_change = lambda: changes.append(kitchen.group.lamp())
        kitchen.group.check()
        assert changes == [("following", "radio", True)]
        kitchen.group.check()
        assert len(changes) == 1                              # no news, no lamp poke

    def test_leader_gone_pulses_red_then_alone(self, lan):
        lan.kitchen.press("power", "off")
        living, kitchen, _ = grouped(lan)
        lan.down.add(living.host)
        for _ in range(group_mod.LEADER_MISSES - 1):
            kitchen.group.check()
            assert kitchen.group.role == "follow"
        kitchen.group.check()
        assert kitchen.group.role == "alone" and kitchen.replies[-1] == "fail"
        assert kitchen.actions.standby is True                # asleep before: asleep again
        assert "can't be reached" in kitchen.group.error

    def test_leader_that_restarted_alone_releases_quietly(self, lan):
        living, kitchen, _ = grouped(lan)
        living.group.role, living.group.members = "alone", {}     # it lost power and came back
        kitchen.group.check()
        assert kitchen.group.role == "alone" and kitchen.replies == []

    def test_leader_drops_a_member_that_came_back_alone(self, lan):
        living, kitchen, study = grouped(lan)
        kitchen.group.role, kitchen.group.leader = "alone", None    # it lost power
        lan.clock.t += group_mod.LEADER_CHECK_S
        living.group.check()
        assert list(living.group.members) == [study.id]

    def test_leader_keeps_an_offline_member_a_while(self, lan):
        living, kitchen, study = grouped(lan)
        lan.down.add(kitchen.host)
        for _ in range(group_mod.MEMBER_MISSES - 1):
            living.group.check()
        assert living.group.members[kitchen.id].online is False
        living.group.check()
        assert list(living.group.members) == [study.id]
        lan.down.add(study.host)
        for _ in range(group_mod.MEMBER_MISSES):
            living.group.check()
        assert living.group.role == "alone"

    def test_tick_runs_the_check_when_due(self, lan):
        living, kitchen, _ = grouped(lan)
        before = len(lan.calls)
        kitchen.group.tick(lan.clock())
        assert len(lan.calls) == before                       # not due yet
        lan.clock.t += group_mod.FOLLOWER_CHECK_S
        kitchen.group.tick(lan.clock(), "radio")
        assert lan.calls[-1] == ("GET", living.host, "/api/info")
        assert kitchen.group.lamp() == ("following", "radio", True)
        lan.living.group.tick(lan.clock())                    # leader: not due yet either
        lan.clock.t += group_mod.LEADER_CHECK_S
        n = len(lan.calls)
        living.group.tick(lan.clock())
        assert [c[1] for c in lan.calls[n:]] == [kitchen.host, lan.study.host]


# -- power cuts --------------------------------------------------------------------------


class TestPowerCut:
    def test_a_follower_comes_back_alone_and_silent(self, lan):
        living, kitchen = lan.living, lan.kitchen
        living.play("k2")
        kitchen.play("k2")                                    # its own preset, before the group
        living.route("PUT", "/api/group", {"members": [kitchen.id]})
        kitchen.play("k3")                                    # forwarded: the leader plays it
        assert kitchen.state.load() == {"last_preset": 2, "standby": False, "follower": True}
        # power cut: a new daemon on the same state file, nothing about the group kept
        actions = Actions(kitchen.actions.variant, FakePlayer(), state=StateStore(kitchen.state.path))
        actions.restore()
        assert actions.resume_preset() is None and actions.resume_last() is True
        assert actions.player.calls == []
        actions.dispatch(Event("k2", "tap"))                  # a hand: plays, flag gone
        assert actions.player.current().n == 2 and "follower" not in StateStore(kitchen.state.path).load()

    def test_after_leaving_it_resumes_normally(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.group.leave()
        assert "follower" not in kitchen.state.load()

    def test_knob_off_and_on_after_a_cut_plays_again(self, lan):
        kitchen = lan.kitchen
        kitchen.play("k2")
        lan.living.play()
        kitchen.press("k1", "hold")
        actions = Actions(kitchen.actions.variant, FakePlayer(), state=StateStore(kitchen.state.path))
        actions.restore()
        assert actions.resume_preset() is None
        actions.dispatch(Event("power", "off"))
        actions.dispatch(Event("power", "on"))
        assert actions.player.current().n == 2


# -- lamp wiring -------------------------------------------------------------------------


class FakeDriver:
    def __init__(self):
        self.shown = []

    def show(self, look, fade_s=0.0):
        self.shown.append(look)

    def reply(self, name):
        pass

    def countdown(self, progress):
        pass

    def set_night(self, night):
        pass

    def close(self, halt_dark=True):
        pass


def test_lamp_keeper_reads_the_group(lan):
    living, kitchen, _ = grouped(lan)
    keeper = haslerd_main.LampKeeper(FakeDriver(), kitchen.actions, night=lambda *_: False)
    keeper._resume_done = True
    keeper._net_seen = True
    kitchen.group.check()
    state = keeper.gather(lan.clock())
    assert (state.group, state.group_source, state.group_playing) == ("following", "radio", True)
    assert keeper.refresh() == policy.playing_look("radio")


# -- settings ----------------------------------------------------------------------------


class TestSettings:
    def test_off_by_default(self, lan, tmp_path):
        g = Group(lan.living.actions, FakeAudio([]), identity=lambda: ("x", "X"), spawn=inline)
        assert g.enabled is False and g.follower_keys == "group"
        g.apply_saved({})
        assert g.enabled is False

    def test_put_persists_and_applies(self, lan):
        living = lan.living
        living.group.enabled = False
        status, doc = living.route("PUT", "/api/group", {"enabled": True, "follower_keys": "leave"})
        assert status == 200 and doc["ok"] and doc["enabled"] and doc["follower_keys"] == "leave"
        saved = load_settings(living.app.settings_file)
        assert saved["multiroom"] == {"enabled": True, "follower_keys": "leave"}
        g = Group(living.actions, FakeAudio([]), identity=lambda: ("x", "X"), spawn=inline)
        g.apply_saved(saved)
        assert g.enabled is True and g.follower_keys == "leave"

    def test_bad_values(self, lan):
        assert lan.living.route("PUT", "/api/group", {"enabled": "yes"})[0] == 400
        assert lan.living.route("PUT", "/api/group", {"follower_keys": "sometimes"})[0] == 400
        with pytest.raises(ValueError):
            settings_from([])
        g = Group(lan.living.actions, FakeAudio([]), identity=lambda: ("x", "X"), spawn=inline)
        g.apply_saved({"multiroom": {"enabled": "on", "follower_keys": "leave"}})
        assert g.enabled is False and g.follower_keys == "leave"

    def test_switching_off_leaves_the_group(self, lan):
        living, kitchen, _ = grouped(lan)
        kitchen.route("PUT", "/api/group", {"enabled": False})
        assert kitchen.group.role == "alone" and kitchen.id not in living.group.members
        living.route("PUT", "/api/group", {"enabled": False})
        assert living.group.role == "alone" and lan.study.group.role == "alone"

    def test_without_snapcast_it_cannot_be_switched_on(self, lan):
        g = Group(lan.living.actions, None, identity=lambda: ("x", "X"), spawn=inline)
        lan.living.api.group = g
        status, doc = lan.living.route("PUT", "/api/group", {"enabled": True})
        assert status == 409 and "isn't installed" in doc["error"]
        g.apply_saved({"multiroom": {"enabled": True}})
        assert g.enabled is False
        assert g.doc()["available"] is False

    def test_peer_endpoints_refuse_when_off(self, lan):
        lan.kitchen.group.enabled = False
        status, doc = lan.kitchen.route("POST", "/api/peer/follow", {"leader_id": "a", "host": "10.0.0.1"})
        assert status == 409
        status, doc = lan.kitchen.route("POST", "/api/peer/join", {"id": "a", "host": "10.0.0.1"})
        assert status == 409
        assert lan.kitchen.route("POST", "/api/peer/follow", {"host": "10.0.0.1"})[0] == 400
        assert lan.kitchen.route("POST", "/api/peer/release", {})[0] == 200    # always harmless


def test_keymap_offers_join_group(lan):
    assert any(v["do"] == "join_group" for v in keymap.VERBS)
    assert keymap.to_page({"do": "join_group"}) == {"do": "join_group"}
    assert keymap.action_from_page({"do": "join_group"}, "k1.hold", set(), None, []) == {"do": "join_group"}
    keys = {k["id"]: k for k in keymap.config_doc(lan.living.actions, {}, False)["keys"]}
    assert keys["k1"]["hold"] == {"do": "join_group"}


# -- over real sockets: the CSRF rules let radios through ----------------------------------


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_radio_to_radio_over_http(tmp_path):
    servers, radios = [], []
    try:
        for rid, name in (("111111111111", "Living"), ("222222222222", "Kitchen")):
            variant = load_variant(VARIANTS / "hasler-hm.yaml")
            player = FakePlayer()
            actions = Actions(variant, player)
            app = WebApp(actions, Panel.from_variant(variant, fake=True), None, {"radio": True}, ha=False)
            server = WebServer(app, host="127.0.0.1", port=0)
            server.start()
            servers.append(server)
            audio = FakeAudio([])
            g = Group(actions, audio, identity=lambda rid=rid, name=name: (rid, name), fetch=http_json,
                      spawn=inline, towards=lambda _h: "127.0.0.1", api_port=server.port)
            g.enabled = True
            actions.group = app.group = g
            app.extra_routes.append(GroupApi(app, g))
            radios.append((app, g, audio, server))
        (living, lg, _, ls), (kitchen, kg, kaudio, ks) = radios
        lg.peers.remember(Peer(kg.self_id, "Kitchen", "127.0.0.1", ks.port, {"enabled": True}))
        living.actions.dispatch(Event("k2", "tap"))
        status, doc = _post(f"http://127.0.0.1:{ls.port}/api/group/", {"members": [kg.self_id]})
        assert status == 200 and doc["role"] == "lead"   # POST /api/group is PUT, for POST-only clients
        assert kg.role == "follow" and kaudio.mode == "follow"
        assert kg.leader.port == ls.port and lg.members[kg.self_id].online
        kg.check()                                       # the follower reads the leader's /api/info
        assert kg.lamp() == ("following", "radio", True)
        # A browser page on another site can't make a radio follow anyone.
        status, doc = _post(f"http://127.0.0.1:{ks.port}/api/peer/follow",
                            {"leader_id": "evil", "host": "6.6.6.6"}, {"Origin": "http://evil.example"})
        assert status == 403 and "cross-site" in doc["error"]
        req = urllib.request.Request(f"http://127.0.0.1:{ks.port}/api/peer/release", data=b"{}", method="POST",
                                     headers={"Content-Type": "text/plain"})
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(req, timeout=5)
        assert err.value.code == 403
        assert kg.role == "follow"
        # The real thing, follower -> leader: a forwarded key.
        kitchen.actions.dispatch(Event("k3", "tap"))
        assert living.actions.player.current().n == 3
        lg.leave()
        assert kg.role == "alone" and kaudio.mode == "alone"
    finally:
        for s in servers:
            s.close()


def test_worker_runs_jobs_in_order_off_the_caller():
    worker = group_mod.Worker("test")
    done, seen = threading.Event(), []
    for i in range(5):
        worker(lambda i=i: seen.append(i))
    worker(lambda: 1 / 0)                                # a failing job doesn't stop the next
    worker(done.set)
    assert done.wait(5)
    assert seen == [0, 1, 2, 3, 4]
