"""Multiroom: one Hasler leads, others play what it plays, in step.

Between Haslers only, off by default (settings.json ``multiroom.enabled``),
on a trusted LAN (no tokens between radios, as the key page has no login).
The audio underneath is Snapcast (multiroom/audio.py, ``SnapAudio``): the
leader routes its own sources into snapserver and plays them through its own
snapclient; a follower's snapclient plays the leader's stream.

**Roles.** Each radio is ``alone``, ``lead`` or ``follow``. Nothing about a
group is kept across a restart: every radio boots alone (see "Power cuts").

**Forming a group.**

* On the leader's key page, "Also play on" ticks other Haslers:
  ``PUT /api/group {"members": [ids]}``. The leader turns into a leader
  (``audio.lead()``) and asks each new member ``POST /api/peer/follow``;
  members ticked off get ``POST /api/peer/release``.
* On a follower, a key bound to ``join_group`` (or ``POST /api/group/join``)
  asks a leader ``POST /api/peer/join``. Which leader, when none is named
  (``pick_leader``): a Hasler that leads a group, else one that plays alone;
  among several, the one whose music started most recently (``since``, wall
  clock, from each radio's /api/info). A follower of a group counts as its
  leader. mDNS has no idea of distance, so "nearest" is "most recent".
  ``join_group`` on a follower leaves its group (one key, both ways); on a
  leader it fails (red pulses).

**On a follower.** Before ``audio.follow()`` the local players are stopped
(``player.stop()``: MPD stop, go-librespot pause). The lamp breathes while it
joins and then shows the playing amber in the tint of the leader's source.
Preset keys follow ``follower_keys``: ``"group"`` (default) forwards the
preset to the leader (``POST /api/peer/key``), so the whole group changes and
"same key again" is judged at the leader (a radio preset again stops the
leader, which ends the group); ``"leave"`` takes this radio out of the group
and plays the preset here alone. The PU source key, the Test button and a
phone casting to this radio also take it out first. Volume and tone stay
local. Standby (knob, key, page) on a follower takes only that radio out:
the rest play on.

**On the leader.** Standby or shutdown ends the group: each follower is
released and goes back to what it was before it joined - asleep if it was
asleep, otherwise ready (silent amber). The last member leaving ends the
lead (``audio.alone()``).

**Keeping in touch.** Every FOLLOWER_CHECK_S a follower reads its leader's
/api/info: the leader's source and playing flag drive the follower's lamp,
and a leader that no longer counts it as a member releases it quietly.
LEADER_MISSES unanswered reads in a row = the leader is gone: red pulses,
then alone. Every LEADER_CHECK_S the leader reads each member's /api/info;
a member that follows someone else (or nobody: it lost power) is dropped,
one unanswered MEMBER_MISSES times in a row likewise.

**Power cuts.** A follower comes back alone and silent (ready), never
playing its own old preset and never rejoining by itself: state.json says it
was following (``"follower": true``) and the boot resume plays nothing. The
leader comes back alone; its old followers notice (above). To group again,
tick them again on the leader's page, or hold their join key.

**Threads.** Keys and web requests never wait on the network or on audio
changes: state flips at once (so the lamp can answer), and the radio-to-radio
calls and ``SnapAudio`` calls run in order on one worker thread per radio.
Each change of role bumps a generation, so a slow call that finishes after
the owner changed their mind changes nothing.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..player.base import PRESET_SOURCES, Preset
from .peers import (API_PORT, Fetch, Peer, PeerUnreachable, Peers, address_towards, http_json,
                    url_for)

log = logging.getLogger(__name__)

ROLES = ("alone", "lead", "follow")
FOLLOWER_KEYS = ("group", "leave")
DEFAULT_FOLLOWER_KEYS = "group"
SNAP_PORT = 1704
CALL_TIMEOUT_S = 2.0        # radio-to-radio, every call
FOLLOWER_CHECK_S = 5.0      # a follower reads its leader this often
LEADER_CHECK_S = 15.0       # a leader reads its members this often
LEADER_MISSES = 3           # unanswered reads before a follower gives up (~15 s)
MEMBER_MISSES = 8           # unanswered reads before a leader drops a member (~2 min)
AFTER_KEY_CHECK_S = 1.5     # a forwarded key: read the leader again this soon
SOURCE_LABELS = {"radio": "Radio", "spotify": "Spotify", "ytm": "YouTube Music"}


class GroupError(Exception):
    """The request is understood but can't be done now (HTTP 409)."""


def settings_from(raw: Any) -> dict:
    """A submitted ``multiroom`` doc -> {"enabled"?, "follower_keys"?}, or ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("multiroom must be {\"enabled\": true|false, \"follower_keys\": \"group\"|\"leave\"}")
    out: dict = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ValueError("enabled must be true or false")
        out["enabled"] = raw["enabled"]
    if "follower_keys" in raw:
        if raw["follower_keys"] not in FOLLOWER_KEYS:
            raise ValueError("follower_keys must be \"group\" (the key changes the whole group) "
                             "or \"leave\" (this radio leaves and plays it alone)")
        out["follower_keys"] = raw["follower_keys"]
    return out


def pick_leader(peers: list[Peer]) -> Peer | None:
    """Who a ``join_group`` key joins: see the module docstring."""
    by_id = {p.id: p for p in peers}
    leaders: dict[str, Peer] = {}
    for p in peers:
        if not (p.online and p.enabled):
            continue
        if p.role == "lead":
            leaders[p.id] = p
        elif p.role == "follow":
            lead = by_id.get(str(p.multiroom.get("leader") or ""))
            if lead is not None and lead.online and lead.enabled:
                leaders.setdefault(lead.id, lead)
    pool = list(leaders.values()) or [
        p for p in peers if p.online and p.enabled and p.role == "alone" and p.multiroom.get("playing")]
    if not pool:
        return None

    def since(p: Peer) -> float:
        value = p.multiroom.get("since")
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    return sorted(pool, key=lambda p: (-since(p), p.name.lower(), p.id))[0]


@dataclass
class Member:
    """The other end of a group: a follower (on the leader), the leader (on a follower)."""

    id: str
    name: str
    host: str
    port: int = API_PORT
    online: bool = True
    misses: int = 0
    added_at: float = 0.0
    pending: bool = False       # asked to follow, no answer yet
    error: str | None = None

    def doc(self) -> dict:
        return {"id": self.id, "name": self.name, "host": self.host, "port": self.port,
                "online": self.online, "joining": self.pending, "error": self.error}


class Worker:
    """One daemon thread running jobs in the order they were given."""

    def __init__(self, name: str = "multiroom") -> None:
        self.name = name
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def __call__(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
                self._thread.start()
        self._q.put(fn)

    def _run(self) -> None:
        while True:
            fn = self._q.get()
            try:
                fn()
            except Exception:
                log.exception("multiroom job failed")


def _text(raw: Any, name: str, limit: int = 120) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return raw.strip()[:limit]


def _port(raw: Any, name: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 65535:
        raise ValueError(f"{name} must be a port number")
    return raw


class Group:
    """This radio's part in a group. Thread-safe; key-side calls never block."""

    def __init__(
        self,
        actions: Any,
        audio: Any,
        identity: Callable[[], tuple[str, str]],
        peers: Peers | None = None,
        fetch: Fetch = http_json,
        spawn: Callable[[Callable[[], None]], None] | None = None,
        towards: Callable[[str], str] = address_towards,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        api_port: int = API_PORT,
    ) -> None:
        self.actions = actions
        self.audio = audio
        self.identity = identity
        self.fetch = fetch
        self.peers = peers if peers is not None else Peers(lambda: self.self_id, fetch)
        self._spawn = spawn or Worker()
        self.towards = towards
        self.clock = clock
        self.wall = wall
        self.api_port = api_port
        # Held while a forwarded key plays (web/server.py's layout lock, set by main).
        self.actions_lock: Any = threading.Lock()
        self.enabled = False
        self.follower_keys = DEFAULT_FOLLOWER_KEYS
        self.role = "alone"
        self.joining = False
        self.leader: Member | None = None            # following: whose music
        self.members: dict[str, Member] = {}         # leading: who plays along
        self.error: str | None = None                # the last thing that went wrong, for the page
        self.lead_since: float | None = None         # wall clock
        self._leader_state: dict = {}                # following: the leader's playing/source/members
        self._before_standby = False                 # following: asleep when it joined?
        self._misses = 0
        self._gen = 0
        self._next_check = 0.0
        self._checking = False
        self._lock = threading.RLock()
        self._ops = threading.RLock()

    # -- who am I ------------------------------------------------------------

    @property
    def self_id(self) -> str:
        return self.identity()[0]

    @property
    def self_name(self) -> str:
        return self.identity()[1]

    @property
    def available(self) -> bool:
        """Is the audio side installed on this radio (SnapAudio.available)?"""
        if self.audio is None:
            return False
        try:
            return bool(getattr(self.audio, "available", True))
        except Exception:
            return False

    @property
    def following(self) -> bool:
        return self.role == "follow"

    @property
    def leading(self) -> bool:
        return self.role == "lead"

    # -- settings --------------------------------------------------------------

    def apply_saved(self, settings: Any) -> None:
        """settings.json's ``multiroom`` at startup; a bad value is logged and ignored."""
        doc = settings.get("multiroom") if isinstance(settings, dict) else None
        if not isinstance(doc, dict):
            return
        for key in ("enabled", "follower_keys"):
            if key not in doc:
                continue
            try:
                value = settings_from({key: doc[key]})[key]
            except ValueError as exc:
                log.warning("ignoring saved multiroom %s %r: %s", key, doc[key], exc)
                continue
            if key == "enabled" and value and not self.available:
                log.warning("multiroom is switched on but its audio isn't installed; leaving it off")
                continue
            setattr(self, key, value)

    def configure(self, enabled: bool | None = None, follower_keys: str | None = None) -> None:
        """Apply checked settings live. Switching off leaves (or ends) the group."""
        if enabled and not self.available:
            raise GroupError("Multiroom isn't installed on this radio (Snapcast is missing)")
        if follower_keys is not None:
            self.follower_keys = follower_keys
        if enabled is not None and enabled != self.enabled:
            self.enabled = enabled
            if not enabled:
                self.leave("multiroom switched off")
            else:
                self.peers.kick(force=True)

    # -- what the lamp, /api/info and the page see ---------------------------

    def lamp(self) -> tuple[str, str, bool]:
        """("" | "joining" | "following", the leader's source, the leader plays?)."""
        if self.joining:
            return "joining", "radio", False
        if self.role == "follow":
            state = self._leader_state
            return "following", str(state.get("source") or "radio"), bool(state.get("playing"))
        return "", "radio", False

    def _own_playing(self) -> tuple[bool, str | None, float | None]:
        actions = self.actions
        try:
            current = actions.player.current()
        except Exception:
            current = None
        playing = current is not None and not actions.standby
        since = getattr(actions, "started_wall", None) if playing else None
        return playing, (current.source if playing else None), since

    def info_doc(self) -> dict:
        """The ``multiroom`` block of /api/info (what other radios read)."""
        with self._lock:
            role, leader, members = self.role, self.leader, list(self.members)
            state = dict(self._leader_state)
        if role == "follow":
            playing, source, since = bool(state.get("playing")), state.get("source"), None
        else:
            playing, source, since = self._own_playing()
        try:
            me = self.self_id
        except Exception:
            me = None
        return {
            "enabled": bool(self.enabled),
            "role": role,
            "leader": me if role == "lead" else (leader.id if leader is not None else None),
            "members": members if role == "lead" else [],
            "playing": playing,
            "source": source,
            "since": since,
        }

    def status_doc(self) -> dict | None:
        """For /api/status: None when alone, else the role and the leader's name."""
        if self.role == "alone" and not self.joining:
            return None
        leader = self.leader
        return {"role": self.role, "joining": self.joining,
                "leader": {"id": leader.id, "name": leader.name} if leader is not None else None,
                "members": len(self.members)}

    def doc(self, refresh: bool = False) -> dict:
        """``GET /api/group``."""
        if self.enabled:
            self.peers.kick(force=refresh)
        with self._lock:
            role, leader = self.role, self.leader
            members = [m.doc() for m in self.members.values()]
            state = dict(self._leader_state)
        me_id, me_name = self.identity()
        if role == "lead":
            leader_doc = {"id": me_id, "name": me_name, "host": None, "port": self.api_port, "online": True}
        elif leader is not None:
            leader_doc = {k: v for k, v in leader.doc().items() if k in ("id", "name", "host", "port", "online")}
        else:
            leader_doc = None
        if role == "follow":
            # The leader's other followers, as the leader last said.
            members = []
            for pid in state.get("members") or []:
                if pid == me_id:
                    continue
                p = self.peers.get(pid)
                members.append({"id": pid, "name": p.name if p else pid, "host": p.host if p else None,
                                "port": p.port if p else API_PORT, "online": bool(p and p.online),
                                "joining": False, "error": None})
        grouped = {m["id"] for m in members} | ({leader_doc["id"]} if leader_doc else set()) | {me_id}
        others = [p.doc() for p in self.peers.list() if p.id not in grouped]
        return {
            "enabled": bool(self.enabled),
            "available": self.available,
            "role": role,
            "joining": self.joining,
            "self": {"id": me_id, "name": me_name},
            "leader": leader_doc,
            "members": members,
            "others": others,
            "follower_keys": self.follower_keys,
            "follower_keys_choices": list(FOLLOWER_KEYS),
            "leader_playing": bool(state.get("playing")) if role == "follow" else None,
            "leader_source": state.get("source") if role == "follow" else None,
            "searching": self.peers.searching,
            "note": self.peers.note,
            "error": self.error,
        }

    # -- small helpers ------------------------------------------------------------

    def _changed(self) -> None:
        hook = getattr(self.actions, "_changed", None)
        if callable(hook):
            hook()

    def _reply(self, name: str) -> None:
        reply = getattr(self.actions, "reply", None)
        if callable(reply):
            reply(name)

    def _persist(self) -> None:
        persist = getattr(self.actions, "_persist", None)
        if callable(persist):
            try:
                persist()
            except Exception:
                log.debug("could not save state", exc_info=True)

    def _call(self, member: Member, method: str, path: str, body: Any = None,
              timeout: float = CALL_TIMEOUT_S) -> tuple[int, Any]:
        """Raises PeerUnreachable; any other failure is turned into one."""
        try:
            return self.fetch(method, url_for(member.host, member.port, path), body, timeout)
        except PeerUnreachable:
            raise
        except Exception as exc:
            raise PeerUnreachable(str(exc)) from exc

    def _audio(self, what: str, *args: Any) -> None:
        """A SnapAudio call; raises what it raises."""
        if self.audio is None:
            raise RuntimeError("multiroom audio isn't installed")
        getattr(self.audio, what)(*args)

    def _audio_quiet(self, what: str, *args: Any) -> None:
        try:
            self._audio(what, *args)
        except Exception:
            log.exception("multiroom audio %s failed", what)

    @staticmethod
    def _why(status: int, doc: Any) -> str:
        if isinstance(doc, dict) and doc.get("error"):
            return str(doc["error"])
        return f"answered {status}"

    def _require_enabled(self) -> None:
        if not self.available:
            raise GroupError(f"Multiroom isn't installed on {self.self_name}")
        if not self.enabled:
            raise GroupError(f"Multiroom is off on {self.self_name} - switch it on in Settings")

    # -- leading ------------------------------------------------------------------

    def _become_leader(self) -> None:
        """Under self._lock: alone -> lead; the audio follows on the worker."""
        self.role = "lead"
        self.lead_since = self.wall()
        self.error = None
        self._gen += 1
        self._next_check = self.clock() + LEADER_CHECK_S
        log.info("multiroom: leading a group")
        self._spawn(lambda: self._with_ops(lambda: self._audio_quiet("lead")))

    def _end_lead(self, reason: str, release: bool = True) -> None:
        with self._lock:
            if self.role != "lead":
                return
            members = list(self.members.values())
            self.role = "alone"
            self.members = {}
            self.lead_since = None
            self._gen += 1
        log.info("multiroom: group ended (%s)", reason)
        me = self.self_id

        def job() -> None:
            if release:
                for m in members:
                    try:
                        self._call(m, "POST", "/api/peer/release", {"leader_id": me})
                    except PeerUnreachable as exc:
                        log.info("multiroom: could not release %s: %s", m.name, exc)
            self._audio_quiet("alone")
        self._spawn(lambda: self._with_ops(job))

    def set_members(self, ids: Any) -> dict:
        """``PUT /api/group {"members": [ids]}`` on the leader: who plays along."""
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ValueError("members must be a list of radio ids")
        self._require_enabled()
        me = self.self_id
        wanted = list(dict.fromkeys(i for i in ids if i != me))
        with self._lock:
            if self.role == "follow":
                name = self.leader.name if self.leader else "another radio"
                raise GroupError(f"{self.self_name} plays {name}'s music - leave that group first")
            add: list[Member] = []
            for pid in wanted:
                if pid in self.members:
                    continue
                peer = self.peers.get(pid)
                if peer is None:
                    raise ValueError(f"no Hasler called {pid!r} has been found on this network")
                add.append(Member(peer.id, peer.name, peer.host, peer.port, added_at=self.clock(),
                                  pending=True))
            drop = [m for pid, m in self.members.items() if pid not in wanted]
            if add and self.role == "alone":
                self._become_leader()
            for m in drop:
                self.members.pop(m.id, None)
            for m in add:
                self.members[m.id] = m
            ending = self.role == "lead" and not self.members
            gen = self._gen
        if drop:
            log.info("multiroom: %s no longer in the group", ", ".join(m.name for m in drop))
        for m in drop:
            self._spawn(lambda m=m: self._with_ops(lambda: self._release(m)))
        for m in add:
            self._spawn(lambda m=m: self._with_ops(lambda: self._invite(m, gen)))
        if ending:
            self._end_lead("no members left", release=False)
        return self.doc()

    def _release(self, m: Member) -> None:
        try:
            self._call(m, "POST", "/api/peer/release", {"leader_id": self.self_id})
        except PeerUnreachable as exc:
            log.info("multiroom: could not release %s: %s", m.name, exc)

    def _invite(self, m: Member, gen: int) -> None:
        with self._lock:
            if self.role != "lead" or self.members.get(m.id) is not m:
                return
        body = {"leader_id": self.self_id, "name": self.self_name, "host": self.towards(m.host),
                "port": SNAP_PORT, "api_port": self.api_port}
        try:
            status, doc = self._call(m, "POST", "/api/peer/follow", body)
            why = None if status == 200 else self._why(status, doc)
        except PeerUnreachable:
            why = "isn't answering"
        with self._lock:
            if self.members.get(m.id) is not m:
                return
            m.pending = False
            if why is None:
                m.online, m.misses = True, 0
                log.info("multiroom: %s follows", m.name)
                return
            self.members.pop(m.id, None)
            self.error = f"{m.name}: {why}"
            ending = self.role == "lead" and not self.members
        log.warning("multiroom: %s could not join: %s", m.name, why)
        if ending:
            self._end_lead("nobody could join", release=False)

    # -- following ----------------------------------------------------------------

    def _become_follower(self, leader: Member, snap_host: str, snap_port: int) -> None:
        actions = self.actions
        with self._lock:
            old = self.leader if self.role == "follow" else None
            if self.role != "follow":
                self._before_standby = bool(actions.standby)
            woke = bool(actions.standby)
            self.role = "follow"
            self.leader = leader
            self.members = {}
            self.joining = True
            self.error = None
            self._leader_state = {"playing": False, "source": None, "members": []}
            self._misses = 0
            self._next_check = self.clock() + FOLLOWER_CHECK_S
            self._gen += 1
            gen = self._gen
        log.info("multiroom: following %s (%s:%s)", leader.name, snap_host, snap_port)
        if old is not None and old.id != leader.id:
            self._spawn(lambda: self._with_ops(lambda: self._tell_left(old)))
        join = getattr(actions, "join_group_playback", None)
        if callable(join):
            join()                      # awake, persisted as a follower, lamp told
        else:
            self._changed()
        self._spawn(lambda: self._with_ops(lambda: self._follow_audio(gen, leader, snap_host, snap_port, woke)))

    def _follow_audio(self, gen: int, leader: Member, host: str, port: int, woke: bool) -> None:
        if gen != self._gen:
            return
        player = self.actions.player
        try:
            player.stop()               # MPD stop, go-librespot pause: the leader's music only
            if woke:
                player.set_mute(False)
            self._audio("follow", host, port)
        except Exception as exc:
            log.exception("multiroom: could not play %s's stream", leader.name)
            self._go_alone(f"could not play {leader.name}'s music ({exc})", notify=True,
                           restore=True, fail=True, gen=gen)
            return
        with self._lock:
            if gen != self._gen:
                return
            self.joining = False
        self._changed()

    def _go_alone(self, reason: str, notify: bool, restore: bool, fail: bool = False,
                  gen: int | None = None) -> None:
        """Following -> alone. ``restore``: back to asleep if it was asleep before."""
        with self._lock:
            if self.role != "follow" or (gen is not None and gen != self._gen):
                return
            leader, before = self.leader, self._before_standby
            self.role = "alone"
            self.leader = None
            self.joining = False
            self._leader_state = {}
            self._gen += 1
            if fail:
                self.error = reason
        log.info("multiroom: left %s's group (%s)", leader.name if leader else "the", reason)
        self._persist()
        if fail:
            self._reply("fail")
        self._changed()
        me = self.self_id

        def job() -> None:
            self._audio_quiet("alone")
            if notify and leader is not None:
                try:
                    self._call(leader, "POST", "/api/peer/left", {"id": me})
                except PeerUnreachable as exc:
                    log.info("multiroom: could not tell %s we left: %s", leader.name, exc)
            if restore and before and not self.actions.standby and self.role == "alone":
                try:
                    self.actions.enter_standby()
                except Exception:
                    log.exception("could not go back to standby after the group")
        self._spawn(lambda: self._with_ops(job))

    def join(self, leader_id: str | None = None) -> dict:
        """``POST /api/group/join`` (and the join_group key): follow a leader."""
        if leader_id is not None and not isinstance(leader_id, str):
            raise ValueError("leader must be a radio id")
        self._require_enabled()
        with self._lock:
            if self.role == "lead":
                raise GroupError(f"{self.self_name} leads its own group")
            if self.role == "follow" and self.leader is not None and self.leader.id == leader_id:
                return self.doc()
            self.joining = True
            self.error = None
            self._gen += 1
            gen = self._gen
        self._changed()
        self._spawn(lambda: self._with_ops(lambda: self._join(leader_id, gen)))
        return self.doc()

    def _join_failed(self, gen: int, why: str) -> None:
        with self._lock:
            if gen != self._gen:
                return
            self.joining = False
            self.error = why
        log.warning("multiroom: could not join a group: %s", why)
        self._reply("fail")
        self._changed()

    def _join(self, leader_id: str | None, gen: int) -> None:
        if gen != self._gen:
            return
        peer = self.peers.get(leader_id) if leader_id else None
        if peer is None or not leader_id:
            self.peers.refresh()
            peer = self.peers.get(leader_id) if leader_id else pick_leader(self.peers.list())
        if peer is None:
            self._join_failed(gen, "no Hasler is playing to join" if not leader_id
                              else "that Hasler isn't on this network")
            return
        if peer.role == "follow" and peer.multiroom.get("leader"):
            lead = self.peers.get(str(peer.multiroom["leader"]))
            if lead is not None:
                peer = lead
        if peer.id == self.self_id:
            self._join_failed(gen, "a radio can't join itself")
            return
        target = Member(peer.id, peer.name, peer.host, peer.port)
        body = {"id": self.self_id, "name": self.self_name, "host": self.towards(peer.host),
                "port": self.api_port}
        try:
            status, doc = self._call(target, "POST", "/api/peer/join", body)
        except PeerUnreachable:
            self._join_failed(gen, f"{peer.name} isn't answering")
            return
        if status != 200 or not isinstance(doc, dict):
            self._join_failed(gen, f"{peer.name}: {self._why(status, doc)}")
            return
        with self._lock:
            cancelled = gen != self._gen
        if cancelled:
            try:
                self._call(target, "POST", "/api/peer/left", {"id": self.self_id})
            except PeerUnreachable:
                pass
            return
        snap_host = doc.get("host") if isinstance(doc.get("host"), str) and doc.get("host") else peer.host
        snap_port = doc.get("port") if isinstance(doc.get("port"), int) else SNAP_PORT
        self._become_follower(target, snap_host, snap_port)

    def join_key(self) -> None:
        """The ``join_group`` verb: join (or, following already, leave). Never blocks."""
        if not (self.available and self.enabled) or self.role == "lead":
            self._reply("fail")
            return
        if self.role == "follow" or self.joining:
            self._reply("ack")
            self.leave("the join key again")
            return
        try:
            self.join(None)
        except GroupError:
            self._reply("fail")

    def leave(self, reason: str = "asked to") -> None:
        """Out of whatever group this radio is in. Never blocks."""
        role = self.role
        if role == "follow":
            self._go_alone(reason, notify=True, restore=False)
        elif role == "lead":
            self._end_lead(reason, release=True)
        if self.joining:
            with self._lock:
                self.joining = False
                self._gen += 1
            self._changed()

    def leave_doc(self) -> dict:
        self.leave("left from the key page")
        return self.doc()

    def standby_entered(self) -> None:
        """Standby or shutdown on this radio: a follower leaves, a leader ends the group."""
        if self.role != "alone" or self.joining:
            self.leave("standby")

    def forward_preset(self, preset: Preset) -> None:
        """A follower's preset key, in "group" mode: the leader plays it. Never blocks."""
        leader = self.leader
        if leader is None:
            return
        body = {"from": self.self_id, "preset": {"n": preset.n, "name": preset.name, "source": preset.source,
                                                 "uri": preset.uri, "shuffle": preset.shuffle}}

        def job() -> None:
            try:
                status, doc = self._call(leader, "POST", "/api/peer/key", body)
            except PeerUnreachable:
                log.warning("multiroom: %s didn't take the key (not answering)", leader.name)
                self._reply("fail")
                return
            if status != 200:
                log.warning("multiroom: %s didn't take the key: %s", leader.name, self._why(status, doc))
                self._reply("fail")
                return
            self._next_check = min(self._next_check, self.clock() + AFTER_KEY_CHECK_S)
        log.info("multiroom: preset %s (%s) forwarded to %s", preset.n, preset.name, leader.name)
        self._spawn(lambda: self._with_ops(job))

    # -- peer endpoints (radio to radio) ----------------------------------------

    def peer_follow(self, body: Any) -> dict:
        """``POST /api/peer/follow`` from a leader: play its stream here."""
        body = body if isinstance(body, dict) else {}
        leader_id = _text(body.get("leader_id"), "leader_id", 64)
        host = _text(body.get("host"), "host", 255)
        port = _port(body.get("port"), "port", SNAP_PORT)
        api_port = _port(body.get("api_port"), "api_port", API_PORT)
        name = str(body.get("name") or leader_id)[:60]
        self._require_enabled()
        with self._lock:
            if self.role == "lead":
                raise GroupError(f"{self.self_name} leads its own group")
            old = self.leader if self.role == "follow" else None
            if old is not None and old.id == leader_id and old.host == host:
                return {"ok": True, "id": self.self_id, "name": self.self_name, "role": self.role}
        self._become_follower(Member(leader_id, name, host, api_port), host, port)
        return {"ok": True, "id": self.self_id, "name": self.self_name, "role": "follow"}

    def _tell_left(self, leader: Member) -> None:
        try:
            self._call(leader, "POST", "/api/peer/left", {"id": self.self_id})
        except PeerUnreachable:
            pass

    def peer_release(self, body: Any) -> dict:
        """``POST /api/peer/release`` from the leader: the group is over for this radio."""
        body = body if isinstance(body, dict) else {}
        leader_id = body.get("leader_id")
        leader = self.leader
        if self.role == "follow" and (leader_id is None or (leader is not None and leader.id == leader_id)):
            self._go_alone("released by the leader", notify=False, restore=True)
        return {"ok": True, "role": self.role}

    def peer_join(self, body: Any) -> dict:
        """``POST /api/peer/join`` from a radio that wants to follow this one."""
        body = body if isinstance(body, dict) else {}
        pid = _text(body.get("id"), "id", 64)
        host = _text(body.get("host"), "host", 255)
        port = _port(body.get("port"), "port", API_PORT)
        name = str(body.get("name") or pid)[:60]
        self._require_enabled()
        if pid == self.self_id:
            raise ValueError("a radio can't join itself")
        with self._lock:
            if self.role == "follow":
                leader = self.leader
                raise GroupError(f"{self.self_name} plays {leader.name if leader else 'another'}'s music")
            if self.role == "alone":
                self._become_leader()
            self.members[pid] = Member(pid, name, host, port, added_at=self.clock())
        log.info("multiroom: %s joined", name)
        return {"ok": True, "leader": {"id": self.self_id, "name": self.self_name},
                "host": self.towards(host), "port": SNAP_PORT, "api_port": self.api_port}

    def peer_left(self, body: Any) -> dict:
        """``POST /api/peer/left`` from a follower that left by itself."""
        body = body if isinstance(body, dict) else {}
        pid = _text(body.get("id"), "id", 64)
        with self._lock:
            gone = self.members.pop(pid, None)
            ending = self.role == "lead" and gone is not None and not self.members
        if gone is not None:
            log.info("multiroom: %s left the group", gone.name)
        if ending:
            self._end_lead("the last member left", release=False)
        return {"ok": True, "role": self.role}

    def peer_key(self, body: Any) -> dict:
        """``POST /api/peer/key`` from a follower: its preset key, for the whole group."""
        body = body if isinstance(body, dict) else {}
        sender = body.get("from")
        spec = body.get("preset")
        if not isinstance(spec, dict):
            raise ValueError("expected {\"from\": id, \"preset\": {\"source\", \"uri\", \"name\", \"n\"}}")
        source = spec.get("source", "radio")
        if source not in PRESET_SOURCES:
            raise ValueError(f"unknown source {source!r}")
        uri = _text(spec.get("uri"), "preset.uri", 2048)
        try:
            n = int(spec.get("n") or 0)
        except (TypeError, ValueError):
            raise ValueError("preset.n must be a number") from None
        preset = Preset(n=n, name=str(spec.get("name") or f"Preset {n}")[:60], uri=uri, source=source,
                        shuffle=bool(spec.get("shuffle")))
        with self._lock:
            if self.role != "lead" or sender not in self.members:
                raise GroupError(f"{self.self_name} doesn't lead a group with that radio in it")
        sources = getattr(self.actions.player, "sources", None)
        if sources is not None and source not in sources:
            raise GroupError(f"{SOURCE_LABELS.get(source, source)} isn't set up on {self.self_name}")

        def job() -> None:
            with self.actions_lock:
                try:
                    self.actions.play_forwarded(preset)
                except Exception:
                    log.exception("multiroom: a forwarded preset failed")
                    self._reply("fail")
        self._spawn(job)
        return {"ok": True}

    # -- keeping in touch -------------------------------------------------------

    def tick(self, now: float, source: str | None = None) -> None:
        """From the main loop. Cheap; the reads run on the worker. Never raises."""
        try:
            role = self.role
            if role == "follow" and source is not None and not self.joining \
                    and self.actions.phone_has_it(source):
                self.leave(f"a phone is casting ({source})")
                return
            if role == "alone" or self._checking or now < self._next_check:
                return
            self._checking = True
            self._spawn(self._check_job)
        except Exception:
            log.exception("multiroom tick failed")

    def _check_job(self) -> None:
        try:
            self._with_ops(self.check)
        finally:
            self._checking = False

    def check(self) -> None:
        """One round of keeping in touch (the worker runs it; tests call it)."""
        if self.role == "follow":
            self._check_leader()
        elif self.role == "lead":
            self._check_members()

    def _check_leader(self) -> None:
        with self._lock:
            leader, gen = self.leader, self._gen
            self._next_check = self.clock() + FOLLOWER_CHECK_S
        if leader is None:
            return
        peer = self.peers.info(leader.host, leader.port, CALL_TIMEOUT_S)
        if peer is None:
            with self._lock:
                if gen != self._gen:
                    return
                self._misses += 1
                leader.online = False
                misses = self._misses
            log.info("multiroom: %s didn't answer (%d/%d)", leader.name, misses, LEADER_MISSES)
            if misses >= LEADER_MISSES:
                self._go_alone(f"{leader.name} can't be reached", notify=False, restore=True,
                               fail=True, gen=gen)
            return
        self.peers.remember(peer)
        mr = peer.multiroom
        if peer.id != leader.id or mr.get("role") != "lead" or self.self_id not in (mr.get("members") or []):
            self._go_alone(f"{leader.name} no longer leads this radio", notify=False, restore=True, gen=gen)
            return
        with self._lock:
            if gen != self._gen:
                return
            self._misses = 0
            leader.online = True
            leader.name = peer.name
            before = dict(self._leader_state)
            self._leader_state = {"playing": bool(mr.get("playing")), "source": mr.get("source"),
                                  "members": list(mr.get("members") or [])}
            changed = before.get("playing") != self._leader_state["playing"] or \
                before.get("source") != self._leader_state["source"]
        if changed:
            self._changed()

    def _check_members(self) -> None:
        with self._lock:
            members = list(self.members.values())
            self._next_check = self.clock() + LEADER_CHECK_S
        me = self.self_id
        drop: list[Member] = []
        for m in members:
            peer = self.peers.info(m.host, m.port, CALL_TIMEOUT_S)
            with self._lock:
                if peer is None:
                    m.misses += 1
                    m.online = False
                    if m.misses >= MEMBER_MISSES:
                        drop.append(m)
                    continue
                self.peers.remember(peer)
                m.misses, m.online, m.name = 0, True, peer.name
                young = self.clock() - m.added_at < LEADER_CHECK_S
                if peer.multiroom.get("leader") != me and not m.pending and not young:
                    drop.append(m)
        if not drop:
            return
        with self._lock:
            for m in drop:
                if self.members.get(m.id) is m:
                    self.members.pop(m.id)
            ending = self.role == "lead" and not self.members
        log.info("multiroom: %s dropped from the group", ", ".join(m.name for m in drop))
        if ending:
            self._end_lead("no members left", release=False)

    # -- plumbing -------------------------------------------------------------------

    def _with_ops(self, fn: Callable[[], None]) -> None:
        with self._ops:
            fn()
