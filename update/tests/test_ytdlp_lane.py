"""The yt-dlp lane (scripts/hasler-ytdlp-update): candidates, slots, canary, holds, judge.

Most tests drive ``Lane`` in-process with fakes for pip, the self-check, the
canary, haslerd and the clock (``FakeLane``). ``fetch_components`` is tested
with real OpenSSH signatures from throwaway keys, and one end-to-end run goes
through the CLI with a fake python/pip/yt-dlp and a fake haslerd on loopback.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hasler-ytdlp-update"
REL = ROOT / "scripts" / "release"


def _load():
    loader = importlib.machinery.SourceFileLoader("hasler_ytdlp_lane", str(SCRIPT))
    spec = importlib.util.spec_from_loader("hasler_ytdlp_lane", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


M = _load()
T0 = 1_792_000_000.0          # 2026-10-14
H = 3600.0
OLD, NEW, NIGHTLY = "2026.8.19", "2026.10.22", "2026.10.23.235959.dev0"
IDS = ["aaaaaaaaaaa", "bbbbbbbbbbb"]


def install(root: Path, slot: str, version: str) -> None:
    """What a venv with yt-dlp ``version`` looks like to the lane."""
    b = root / slot / "bin"
    b.mkdir(parents=True, exist_ok=True)
    (b / "yt-dlp").write_text(f"#!/bin/sh\necho {version}\n")
    (b / "yt-dlp").chmod(0o755)
    d = root / slot / "lib" / "python3.13" / "site-packages" / f"yt_dlp-{version}.dist-info"
    d.mkdir(parents=True, exist_ok=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: yt-dlp\nVersion: {version}\n\nbody\n")


def lock_text(version: str) -> str:
    return (f"yt-dlp[default]=={version} \\\n    --hash=sha256:{'a' * 64}\n"
            f"websockets==15.0.1 \\\n    --hash=sha256:{'b' * 64} \\\n    --hash=sha256:{'c' * 64}\n")


def cand(version, channel="stable", online=True):
    return {"version": version, "channel": channel, "requirements": lock_text(version),
            "tested": {"install": True, "offline": True, "online": online}}


def block(*cands, blocked=(), ids=IDS):
    return {"candidates": list(cands), "blocked": list(blocked), "canary": {"ytm_ids": list(ids), "min_bytes": 65536}}


class FakeLane(M.Lane):
    def __init__(self, root: Path):
        super().__init__()
        self.root = str(root)
        self.api = ""
        self.request_file = str(root / "no-request.json")
        self.t = T0
        self.block, self.cstat = None, "missing"
        self.pypi = OLD
        self.pip_fail: set[str] = set()
        self.offline_bad: set[str] = set()
        self.canary_by_version: dict[str, str] = {}
        self.canaried: list[str] = []
        self.builds: list[tuple[str, str]] = []
        self.play: list[bool] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def fetch_components(self):
        if self.block is None:
            return None, self.cstat, "test"
        return self.block, "ok", "verified"

    def build(self, slot, c):
        if slot in (self.current_slot(), self.previous_slot()):
            return f"refusing to build over {slot}"
        v = c["version"] or self.pypi
        self.builds.append((slot, v))
        shutil.rmtree(self.p(slot), ignore_errors=True)
        if v in self.pip_fail:
            return "pip: no matching distribution"
        install(Path(self.root), slot, v)
        return None

    def offline_check(self, slot):
        return "offline extraction failed" if self.version_of(slot) in self.offline_bad else None

    def _canary_one(self, slot, vid, min_bytes):
        v = self.version_of(slot)
        self.canaried.append(v)
        return {"result": self.canary_by_version.get(v, "pass"), "via": "test", "id": vid}

    def playing(self):
        return self.play.pop(0) if self.play else False

    def go(self, *args):
        return M.main(list(args), lane=self)

    # views
    def cur(self):
        return self.version_of(self.current_slot())

    def prev(self):
        return self.version_of(self.previous_slot())

    def events(self):
        p = Path(self.root) / "events.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def names(self):
        return [e["event"] for e in self.events()]

    def saved(self):
        return json.loads((Path(self.root) / "state.json").read_text())


@pytest.fixture
def lane(tmp_path):
    root = tmp_path / "opt-yt-dlp"
    root.mkdir()
    return FakeLane(root)


def legacy(lane: FakeLane, cur=OLD, other=None):
    """A radio from before the lane: venv-a (current) [+ venv-b], no state."""
    install(Path(lane.root), "venv-a", cur)
    os.symlink(lane.p("venv-a"), lane.p("current"))
    if other:
        install(Path(lane.root), "venv-b", other)


# --- versions and locks ----------------------------------------------------------------

def test_version_order():
    k = M.vkey
    assert k("2026.10.22") < k("2026.10.23.235959.dev0") < k("2026.10.23.235959") < k("2026.10.24")
    assert k("2026.10.02") == k("2026.10.2") and M.same_version("2026.08.19", "2026.8.19")
    assert k("2026.8.19") < k("2026.10.1")
    # a nightly wheel's METADATA says "...233146dev", its file name "...233146.dev0"
    assert M.same_version("2026.10.21.233146dev", "2026.10.21.233146.dev0")
    assert not M.same_version("2026.10.21.233146dev", "2026.10.21.233146")


def test_requirements_must_be_a_plain_hashed_lock():
    assert M.validate_requirements(lock_text(NEW), NEW)
    bad = [
        "--index-url https://evil.example/simple\n" + lock_text(NEW),
        lock_text(NEW) + "-r other.txt\n",
        lock_text(NEW) + "https://evil.example/x.whl\n",
        lock_text(NEW) + "./local.whl\n",
        f"yt-dlp=={NEW}\n",                                   # no hash
        lock_text(NEW) + "brotli==1.1.0\n",                   # last one unhashed
        lock_text("2026.10.21"),                              # not the candidate's version
        f"websockets==15.0.1 \\\n    --hash=sha256:{'b' * 64}\n",   # no yt-dlp at all
        "",
    ]
    for text in bad:
        with pytest.raises(ValueError):
            M.validate_requirements(text, NEW)


def test_candidates_ranked_and_filtered(lane):
    broken = cand("2026.10.21", "stable")
    broken["requirements"] = "--extra-index-url http://x\n" + broken["requirements"]
    b = block(cand(NIGHTLY, "nightly"), cand(OLD, "stable", online=False), cand(NEW, "stable"),
              cand("2026.10.1"), broken, {"version": "x; rm -rf /", "channel": "stable"},
              blocked=["2026.10.01"])
    lane.mark_bad("2026.10.20", "test")
    b["candidates"].append(cand("2026.10.20"))
    order = [c["version"] for c in lane.candidates(b)]
    assert order == [NEW, NIGHTLY, OLD]       # stable first, untested-online last; blocked, bad, unsafe out


# --- install, upgrade, the slot bug ------------------------------------------------------

def test_first_install_from_signed_candidates(lane):
    lane.block = block(cand(NEW), cand(NIGHTLY, "nightly"))
    assert lane.go() == 0
    assert lane.cur() == NEW and lane.current_slot() == "venv-a"
    st = lane.saved()
    assert st["current"]["version"] == NEW and st["current"]["channel"] == "stable"
    assert st["last_result"] == "ok" and st["last_ok"] and M.parse_iso(st["probation_until"]) == T0 + M.PROBATION_S
    assert "switched" in lane.names()


def test_upgrade_builds_a_third_slot_and_keeps_the_previous(lane):
    legacy(lane, cur="2026.8.1", other="2026.7.1")
    lane.block = block(cand(NEW))
    assert lane.go() == 0
    assert lane.builds == [("venv-c", NEW)]
    assert (lane.cur(), lane.prev()) == (NEW, "2026.8.1")
    assert lane.go("--rollback") == 0
    assert (lane.cur(), lane.prev()) == ("2026.8.1", NEW)


def test_a_failed_build_never_touches_the_previous_slot(lane):
    legacy(lane, cur=OLD, other="2026.7.1")
    lane.block = block(cand(NEW))
    lane.pip_fail = {NEW}
    assert lane.go() == 1
    assert lane.cur() == OLD and lane.prev() == "2026.7.1"          # the old bug: venv-b rm -rf'd first
    assert lane.saved()["last_result"] == "error" and "pip" in lane.saved()["last_error"]
    assert "install_failed" in lane.names() and "failed" in lane.names()
    assert lane.go("--rollback") == 0 and lane.cur() == "2026.7.1"


def test_self_check_failure_keeps_current(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    lane.offline_bad = {NEW}
    assert lane.go() == 1 and lane.cur() == OLD
    assert not os.path.exists(lane.p("venv-b"))


def test_same_version_is_no_change_and_no_canary(lane):
    legacy(lane, cur=NEW)
    lane.block = block(cand(NEW), cand(NIGHTLY, "nightly"))
    assert lane.go() == 0
    assert lane.builds == [] and lane.canaried == []
    assert lane.names()[-1] == "up_to_date"


def test_broken_stable_loses_to_a_tested_nightly(lane):
    legacy(lane, cur=OLD)
    lane.block = block(cand(OLD, "stable", online=False), cand(NIGHTLY, "nightly"))
    assert lane.go() == 0
    assert lane.cur() == NIGHTLY and lane.saved()["current"]["channel"] == "nightly"


def test_back_to_stable_from_nightly_when_stable_is_tested(lane):
    lane.block = block(cand(NIGHTLY, "nightly"))
    assert lane.go() == 0 and lane.cur() == NIGHTLY
    lane.t += 2 * M.DAY
    lane.block = block(cand("2026.10.23", "stable"), cand(NIGHTLY, "nightly"))
    assert lane.go() == 0 and lane.cur() == "2026.10.23"


# --- the canary decides ----------------------------------------------------------------------

def test_new_version_failing_its_canary_is_refused(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    lane.canary_by_version = {NEW: "fail"}
    assert lane.go() == 0                             # nothing broke: current still works
    assert lane.cur() == OLD and "kept" in lane.names()


def test_urgent_repairs_a_failing_current_while_music_plays(lane):
    legacy(lane, cur=OLD)
    lane.block = block(cand(OLD), cand(NIGHTLY, "nightly"))
    lane.canary_by_version = {OLD: "fail"}
    lane.play = [True] * 100
    assert lane.go("--urgent") == 0
    assert lane.cur() == NIGHTLY and lane.canaried == [OLD, OLD, NIGHTLY]   # two tracks before "fails"


def test_urgent_with_a_working_current_changes_nothing(lane):
    legacy(lane, cur=NEW)
    lane.block = block(cand(NEW))
    assert lane.go("--urgent") == 0
    assert lane.cur() == NEW and lane.builds == []
    assert any(e["event"] == "urgent" and "not yt-dlp" in e["verdict"] for e in lane.events())


def test_repair_tries_the_next_candidate(lane):
    legacy(lane, cur=OLD)
    lane.block = block(cand(NEW), cand(NIGHTLY, "nightly"))
    lane.canary_by_version = {OLD: "fail", NEW: "fail"}
    assert lane.go("--urgent") == 0
    assert lane.cur() == NIGHTLY
    assert [v for _, v in lane.builds] == [NEW, NIGHTLY]


def test_repair_that_finds_nothing_is_a_visible_failure(lane):
    legacy(lane, cur=OLD)
    lane.block = block(cand(NEW))
    lane.canary_by_version = {OLD: "fail", NEW: "fail"}
    assert lane.go("--urgent") == 1
    assert lane.cur() == OLD
    assert "fails its canary" in lane.saved()["last_error"]


def test_offline_urgent_does_nothing(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    lane.canary_by_version = {OLD: "offline"}
    assert lane.go("--urgent") == 0
    assert lane.builds == [] and lane.saved()["last_result"] == "offline"


# --- holds and budgets --------------------------------------------------------------------------

def test_one_upgrade_a_day(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    assert lane.go() == 0 and lane.cur() == NEW
    lane.t += 6 * H
    lane.block = block(cand("2026.10.25"))
    assert lane.go() == 0 and lane.cur() == NEW
    assert lane.saved()["last_result"] == "deferred"
    lane.t += 19 * H
    assert lane.go() == 0 and lane.cur() == "2026.10.25"


def test_update_now_ignores_the_daily_limit(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    assert lane.go() == 0
    lane.t += H
    lane.block = block(cand("2026.10.25"))
    assert lane.go("--now") == 0 and lane.cur() == "2026.10.25"


def test_canary_budget(lane):
    legacy(lane, cur=NEW)
    lane.block = block(cand(NEW))
    for _ in range(M.MAX_CANARIES_PER_DAY):
        lane.go("--urgent")
    assert len(lane.canaried) == M.MAX_CANARIES_PER_DAY
    lane.go("--urgent")
    assert len(lane.canaried) == M.MAX_CANARIES_PER_DAY
    assert lane.saved()["canary"]["result"] == "skipped"
    lane.t += M.DAY
    lane.go("--urgent")
    assert len(lane.canaried) == M.MAX_CANARIES_PER_DAY + 1


def test_no_upgrade_while_music_plays_and_the_build_is_kept(lane):
    lane.play_wait_s = 600
    legacy(lane)
    lane.block = block(cand(NEW))
    lane.play = [True] * 50
    assert lane.go() == 0
    assert lane.cur() == OLD and lane.saved()["last_result"] == "deferred"
    assert lane.saved()["staged"]["version"] == NEW
    lane.t += 6 * H
    lane.play = []
    assert lane.go() == 0 and lane.cur() == NEW
    assert lane.builds == [("venv-b", NEW)]          # built once, reused
    assert "reusing" in lane.names()


# --- blocked, judge, bad ---------------------------------------------------------------------------

def test_blocked_current_goes_back_at_once(lane):
    legacy(lane, cur="2026.10.1", other=OLD)
    lane.block = block(cand("2026.10.1"), blocked=["2026.10.01"])
    lane.play = [True] * 100
    assert lane.go() == 0
    assert lane.cur() == OLD
    assert "2026.10.1" in lane.saved()["bad"]
    lane.t += 2 * M.DAY
    assert lane.go() == 0 and lane.cur() == OLD and lane.builds == []   # never put back


def test_blocked_current_without_a_previous_is_replaced(lane):
    legacy(lane, cur="2026.10.1")
    lane.block = block(cand(NEW), blocked=["2026.10.1"])
    lane.canary_by_version = {NEW: "skipped"}       # even without a canary: blocked is worse
    assert lane.go() == 0 and lane.cur() == NEW


def test_judge_goes_back_when_only_the_old_one_works(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    assert lane.go() == 0 and lane.cur() == NEW
    lane.t += 5 * H
    lane.canary_by_version = {NEW: "fail"}
    assert lane.go("--judge") == 0
    assert lane.cur() == OLD and "failed in probation" in lane.saved()["bad"][NEW]["why"]
    assert lane.saved()["probation_until"] is None
    lane.t += 2 * M.DAY
    assert lane.go() == 0 and lane.cur() == OLD and len(lane.builds) == 1   # NEW is bad here now


def test_judge_keeps_a_working_current(lane):
    legacy(lane, cur=NEW, other=OLD)
    lane.block = block(cand(NEW))
    assert lane.go("--judge") == 0
    assert lane.cur() == NEW and lane.canaried == [NEW]


def test_judge_both_failing_repairs(lane):
    legacy(lane, cur=NEW, other=OLD)
    lane.block = block(cand(NEW), cand(NIGHTLY, "nightly"))
    lane.canary_by_version = {NEW: "fail", OLD: "fail"}
    assert lane.go("--judge") == 0
    assert lane.cur() == NIGHTLY


def test_manual_rollback_holds_the_version_off(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    assert lane.go() == 0
    assert lane.go("--rollback") == 0 and lane.cur() == OLD
    lane.t += 2 * M.DAY
    assert lane.go() == 0 and lane.cur() == OLD


def test_rollback_with_nothing_to_go_back_to(lane):
    legacy(lane)
    os.symlink(lane.p("venv-a"), lane.p("previous"))   # previous == current: nothing
    assert lane.go("--rollback") == 1
    assert "no previous" in lane.saved()["last_error"]


# --- without components.json --------------------------------------------------------------------

def test_missing_components_and_a_working_current_change_nothing(lane):
    legacy(lane)
    assert lane.go() == 0
    assert lane.builds == [] and lane.canaried == []
    assert lane.saved()["last_result"] == "ok" and not lane.saved().get("last_ok")


def test_fallback_to_pypi_only_when_current_fails(lane):
    legacy(lane)
    lane.cstat = "expired"
    lane.canary_by_version = {OLD: "fail"}
    lane.pypi = NEW
    assert lane.go("--urgent") == 0
    assert lane.cur() == NEW and lane.saved()["current"]["source"] == "pypi-fallback"
    fb = [e for e in lane.events() if e["event"] == "fallback"]
    assert fb and fb[0]["level"] == "warn" and "hash-unchecked" in fb[0]["note"]


def test_fallback_with_nothing_newer_on_pypi(lane):
    legacy(lane)
    lane.cstat = "missing"
    lane.canary_by_version = {OLD: "fail"}
    assert lane.go("--urgent") == 1
    assert lane.cur() == OLD and "nothing newer" in lane.saved()["last_error"]


def test_no_fallback_past_a_refused_signature(lane):
    legacy(lane)
    lane.cstat = "bad"
    lane.canary_by_version = {OLD: "fail"}
    assert lane.go("--urgent") == 1
    assert lane.builds == []


def test_first_install_without_components_uses_pypi(lane):
    lane.cstat = "unverified"
    lane.pypi = NEW
    lane.canary_by_version = {NEW: "offline"}
    assert lane.go() == 0 and lane.cur() == NEW


# --- requests from haslerd, events, dry run ---------------------------------------------------------

def test_request_file_is_honoured_once_and_only_fresh(lane, tmp_path):
    legacy(lane, cur=NEW)
    lane.block = block(cand(NEW))
    req = tmp_path / "yt-dlp-request.json"
    lane.request_file = str(req)
    req.write_text(json.dumps({"mode": "urgent", "at": M.iso(T0 - 60), "why": "2 tracks failed"}))
    assert lane.go() == 0 and lane.canaried == [NEW]
    assert lane.go() == 0 and lane.canaried == [NEW]           # used once
    req.write_text(json.dumps({"mode": "judge", "at": M.iso(T0 - 3 * H)}))
    assert lane.go() == 0 and lane.canaried == [NEW]           # stale
    req.write_text(json.dumps({"mode": "rm -rf", "at": M.iso(T0)}))
    assert lane.go() == 0 and lane.canaried == [NEW]


def test_request_made_during_a_run_is_taken_at_its_end(lane, tmp_path):
    legacy(lane, cur=NEW)
    lane.block = block(cand(NEW))
    req = tmp_path / "yt-dlp-request.json"
    lane.request_file = str(req)
    plain = FakeLane.fetch_components

    def fetch(self=lane):
        if not req.exists():                          # haslerd asks while the timer's run is busy
            req.write_text(json.dumps({"mode": "urgent", "at": M.iso(T0)}))
        return plain(self)
    lane.fetch_components = fetch
    assert lane.go() == 0
    assert lane.canaried == [NEW] and lane.names().count("check") == 2


def test_events_are_capped(lane):
    for i in range(500):
        lane.event("x", n=i, pad="p" * 200)
    lines = (Path(lane.root) / "events.jsonl").read_text().splitlines()
    assert len(lines) <= M.EVENTS_MAX_LINES
    assert sum(len(x) + 1 for x in lines) <= M.EVENTS_MAX_BYTES
    assert json.loads(lines[-1])["n"] == 499


def test_dry_run_changes_nothing(lane):
    legacy(lane)
    lane.block = block(cand(NEW))
    assert lane.go("--check") == 0
    assert lane.builds == [] and lane.cur() == OLD
    assert not (Path(lane.root) / "state.json").exists()


def test_unexpected_exception_is_recorded(lane, monkeypatch):
    legacy(lane)
    monkeypatch.setattr(lane, "fetch_components", lambda: 1 / 0)
    assert lane.go() == 1
    assert "ZeroDivisionError" in lane.saved()["last_error"]


def test_usage(lane):
    assert lane.go("--bogus") == 2


# --- the canary itself (haslerd first, local without cookies) ------------------------------------------

def test_canary_asks_haslerd_then_falls_back_to_local(tmp_path):
    root = tmp_path / "y"
    root.mkdir()
    install(root, "venv-a", NEW)
    lane = M.Lane()
    lane.root, lane.api = str(root), "http://127.0.0.1:9"
    calls = []

    def post(url, doc, timeout=150):
        calls.append((url, doc))
        return 200, {"result": "pass", "id": doc["id"] or "ccccccccccc"}
    lane.http_post_json = post
    r = lane.canary("venv-a", {"ytm_ids": IDS}, "test")
    assert r["result"] == "pass" and r["via"] == "haslerd"
    assert calls == [("http://127.0.0.1:9/api/components/yt-dlp/canary",
                      {"slot": "venv-a", "id": IDS[0], "min_bytes": 65536})]

    def gone(url, doc, timeout=150):
        raise OSError("connection refused")
    lane.http_post_json = gone
    ran = []

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    lane.run = lambda cmd, timeout=120: (ran.append(cmd), R(0, "https://rr1.googlevideo.example/videoplayback?x\n"))[1]
    lane.http_get = lambda url, headers=None, timeout=60, limit=0: (206, b"\0\0\0\x18ftypdash" + b"\0" * 70000, {})
    r = lane.canary("venv-a", {"ytm_ids": IDS}, "test")
    assert r["result"] == "pass" and r["via"] == "local"
    assert ran[0][1:] == ["-f", M.AUDIO_FORMAT, "-g", "--no-playlist", f"https://music.youtube.com/watch?v={IDS[0]}"]
    lane.http_get = lambda url, headers=None, timeout=60, limit=0: (200, b"x" * 70000, {})
    r = lane.canary("venv-a", {"ytm_ids": IDS}, "test")
    assert r["result"] == "fail" and r["stage"] == "fetch"
    lane.run = lambda cmd, timeout=120: R(1, "", "ERROR: [youtube] x: Requested format is not available")
    r = lane.canary("venv-a", {"ytm_ids": IDS}, "test")
    assert r["result"] == "fail" and r["stage"] == "resolve" and "format" in r["error"]
    lane.run = lambda cmd, timeout=120: R(1, "", "ERROR: <urlopen error [Errno -3] Temporary failure in name resolution>")
    assert lane.canary("venv-a", {"ytm_ids": IDS}, "test")["result"] == "skipped"   # budget: 4 a day


def test_haslerd_busy_is_skipped_not_failed(tmp_path):
    lane = M.Lane()
    lane.root, lane.api = str(tmp_path), "http://127.0.0.1:9"
    install(tmp_path, "venv-a", NEW)
    lane.http_post_json = lambda url, doc, timeout=150: (429, {"error": "a canary is running"})
    assert lane.canary("venv-a", {}, "t")["result"] == "skipped"


# --- components.json, really signed ----------------------------------------------------------------

@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    d = tmp_path_factory.mktemp("lane") / "keys"
    subprocess.run(["bash", str(REL / "dev-keys.sh"), str(d)], check=True, capture_output=True,
                   env=dict(os.environ, PYTHON=sys.executable))
    return d


def publish(site: Path, keys: Path, doc: dict, key="release-dev", ns=M.NAMESPACE) -> str:
    site.mkdir(parents=True, exist_ok=True)
    f = site / "components.json"
    f.write_text(json.dumps(doc))
    sig = site / "components.json.sig"
    sig.unlink(missing_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(keys / key), "-n", ns, str(f)],
                   check=True, capture_output=True)
    return f.as_uri()


def components_doc(now: float, **over) -> dict:
    doc = {"schema": 1, "issued": M.iso(now - 60), "expires": M.iso(now + 14 * M.DAY),
           "components": {"yt-dlp": block(cand(NEW))}}
    doc.update(over)
    return doc


def real_lane(tmp_path, keys, url):
    lane = M.Lane()
    lane.root = str(tmp_path)
    lane.trust = str(keys / "trust")
    lane.url = url
    return lane


def test_components_verified(tmp_path, keys):
    import time
    now = time.time()
    url = publish(tmp_path / "site", keys, components_doc(now))
    blk, st, _ = real_lane(tmp_path, keys, url).fetch_components()
    assert st == "ok" and blk["candidates"][0]["version"] == NEW


@pytest.mark.parametrize("case,want", [
    ("expired", "expired"), ("future", "bad"), ("schema", "bad"), ("wrong_key", "bad"), ("wrong_ns", "bad"),
    ("missing", "missing"), ("no_keyring", "unverified"),
])
def test_components_refused(tmp_path, keys, case, want):
    import time
    now = time.time()
    doc = components_doc(now)
    key, ns = "release-dev", M.NAMESPACE
    if case == "expired":
        doc["expires"] = M.iso(now - 60)
    elif case == "future":
        doc["issued"] = M.iso(now + 3 * H)
    elif case == "schema":
        doc["schema"] = 2
    elif case == "wrong_key":
        key = "timestamp-dev"        # in the keyring, but not with role release
    elif case == "wrong_ns":
        ns = "hasler-release"
    url = publish(tmp_path / "site", keys, doc, key, ns)
    lane = real_lane(tmp_path, keys, url)
    if case == "missing":
        lane.url = (tmp_path / "nowhere" / "components.json").as_uri()
    if case == "no_keyring":
        lane.trust = str(tmp_path / "no-trust")
    blk, st, _ = lane.fetch_components()
    assert (blk, st) == (None, want)


def test_components_url_from_box_yaml(tmp_path, monkeypatch):
    (tmp_path / "box.yaml").write_text("schema: 1\nchannel: bench\nupdate_url: http://laptop:8000/\n")
    monkeypatch.setenv("HASLER_BOX_YAML", str(tmp_path / "box.yaml"))
    monkeypatch.delenv("HASLER_COMPONENTS_URL", raising=False)
    assert M.Lane().url == "http://laptop:8000/components.json"
    monkeypatch.setenv("HASLER_BOX_YAML", str(tmp_path / "none.yaml"))
    assert M.Lane().url == M.DEFAULT_BASE + "/components.json"


# --- end to end: the CLI, a fake python/pip/yt-dlp, a fake haslerd ---------------------------------------

FAKE_PY = r"""#!/bin/bash
# base python: -m venv DIR; venv python: -m pip (installs a fake yt-dlp of the lock's version), -c (imports)
if [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; exit 0; fi
here="$(cd "$(dirname "$0")" && pwd)"; venv="$(dirname "$here")"
if [ "$1" = -m ] && [ "$2" = pip ]; then
  echo "$*" >> "$FAKE_LOG"
  [ "${FAKE_PIP_FAIL:-0}" = 1 ] && exit 1
  v="${FAKE_VERSION:-1.0}"; prev=""
  for a in "$@"; do [ "$prev" = -r ] && v="$(sed -n 's/^yt-dlp\[default\]==\([^ ]*\).*/\1/p' "$a")"; prev="$a"; done
  mkdir -p "$venv/lib/python3.13/site-packages/yt_dlp-$v.dist-info"
  printf 'Name: yt-dlp\nVersion: %s\n\n' "$v" > "$venv/lib/python3.13/site-packages/yt_dlp-$v.dist-info/METADATA"
  cat > "$here/yt-dlp" <<EOF
#!/bin/bash
[ "\$1" = --version ] && { echo $v; exit 0; }
for a in "\$@"; do case "\$a" in file://*) echo "hasler selftest (1)"; exit 0;; esac; done
exit 1
EOF
  chmod +x "$here/yt-dlp"; exit 0
fi
[ "$1" = -c ] && exit 0
exec python3 "$@"
"""


class FakeHaslerd:
    """/api/health and the canary endpoint; the canary answer per version."""

    def __init__(self, root: Path):
        self.root = root
        self.fail: set[str] = set()
        self.asked: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self._send(200, {"playing": False, "idle": True})

            def do_POST(self):
                doc = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.asked.append(doc)
                v = next((p.name.split("-", 1)[1][:-len(".dist-info")] for p in
                          (outer.root / doc["slot"]).rglob("yt_dlp-*.dist-info")), None)
                self._send(200, {"result": "fail" if v in outer.fail else "pass", "id": doc["id"]})

            def _send(self, code, doc):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture
def e2e(tmp_path, keys):
    import time
    py = tmp_path / "fakepy"
    py.write_text(FAKE_PY)
    py.chmod(0o755)
    root = tmp_path / "opt-yt-dlp"
    root.mkdir()
    site = tmp_path / "site"
    haslerd = FakeHaslerd(root)
    log = tmp_path / "pip.log"

    def run(*args, doc=None, **env):
        url = publish(site, keys, doc or components_doc(time.time()))
        e = dict(os.environ, HASLER_YTDLP_ROOT=str(root), HASLER_YTDLP_PYTHON=str(py),
                 HASLER_YTDLP_TRUST=str(keys / "trust"), HASLER_COMPONENTS_URL=url,
                 HASLER_YTDLP_API=haslerd.url, HASLER_YTDLP_REQUEST=str(tmp_path / "req.json"),
                 HASLER_UPDATER_PATH=str(ROOT / "update"), FAKE_LOG=str(log),
                 **{k: str(v) for k, v in env.items()})
        return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True, env=e)
    yield run, root, haslerd, log
    haslerd.close()


def test_end_to_end(e2e):
    import time
    run, root, haslerd, log = e2e
    r = run()
    assert r.returncode == 0, r.stderr
    assert os.readlink(root / "current").endswith("venv-a")
    assert "--require-hashes -r" in log.read_text()
    assert haslerd.asked and haslerd.asked[0]["slot"] == "venv-a" and haslerd.asked[0]["id"] == IDS[0]
    st = json.loads(run("--status").stdout)
    assert st["current"] == NEW and st["channel"] == "stable" and st["last_result"] == "ok"
    # a nightly that fails its canary in haslerd: refused, NEW stays
    doc = components_doc(time.time())
    doc["components"]["yt-dlp"] = block(cand(NIGHTLY, "nightly"), cand(NEW, "stable", online=False))
    haslerd.fail = {NIGHTLY}
    r = run("--now", doc=doc)
    assert r.returncode == 0, r.stderr
    assert json.loads(run("--status").stdout)["current"] == NEW
    assert "fails its canary" in r.stderr
    events = [json.loads(x) for x in (root / "events.jsonl").read_text().splitlines()]
    assert {"check", "canary", "switched", "kept"} <= {e["event"] for e in events}
    # busy: another run holds the lock
    import fcntl
    with open(root / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        assert run().returncode == 75
