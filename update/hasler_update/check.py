"""``check`` and ``stage``: the unprivileged half (user ``hasler-update``).

Fetch the signed channel index, decide whether there is something for this box,
fetch and verify its release.json, then (``stage``) download the artifact,
hash-check it and extract it safely into ``staged/<seq>/``. Writes a status
file the key page can show. Never touches /opt, /etc or a running service.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import fetch, stage as stage_mod
from .layout import Layout, box_settings
from .state import iso, read_json, write_atomic, write_json
from .verify import (
    Keyring, VerifyError, accept_keyring, check_compat, check_policy, load_keyring, ring_open,
    sha256_bytes, system_facts, verify_index, verify_release,
)


def installed(layout: Layout) -> dict:
    """Version and seq of what ``current`` points at ({} if nothing yet)."""
    try:
        target = layout.current.resolve(strict=True)
    except OSError:
        return {}
    doc = read_json(target / "release.json", {}) or {}
    return {"version": doc.get("version", target.name), "seq": doc.get("seq"), "path": str(target)}


class Checker:
    def __init__(self, layout: Layout, now: float | None = None, facts: dict | None = None) -> None:
        self.layout = layout
        self._now = now
        self.facts = facts
        self.box = box_settings(layout)

    @property
    def now(self) -> float:
        return time.time() if self._now is None else self._now

    def _ring(self) -> Keyring:
        return load_keyring(self.layout.trust, self.now)

    def _maybe_new_keyring(self, ring: Keyring, needed: int) -> Keyring:
        """The release wants a newer keyring: fetch, verify with the root key,
        leave it in incoming/ for apply (root) to install."""
        base = self.box["update_url"]
        data = fetch.get_small(f"{base}/keyring.json")
        sig = fetch.get_small(f"{base}/keyring.json.sig")
        new = accept_keyring(data, sig, self.layout.trust, ring.seq, self.now)
        if new.seq < needed:
            raise VerifyError(f"published keyring {new.seq} is older than the release needs ({needed})")
        write_atomic(self.layout.incoming / "keyring.json", data, 0o644)
        write_atomic(self.layout.incoming / "keyring.json.sig", sig, 0o644)
        return new

    def check(self) -> dict:
        """Look for an update. Returns (and saves) the status document."""
        status: dict = {"checked": iso(self.now), "channel": self.box["channel"],
                        "mode": self.box["update_mode"], "installed": installed(self.layout),
                        "available": None, "result": "ok", "message": ""}
        try:
            status.update(self._check())
        except (VerifyError, fetch.FetchError, OSError, ValueError, KeyError) as e:
            status.update(result="error", message=str(e))
        write_json(self.layout.check_status, status, 0o644)
        return status

    def _check(self) -> dict:
        ring = self._ring()
        base, channel = self.box["update_url"], self.box["channel"]
        url = f"{base}/channels/{channel}.json"
        idx_bytes, idx_sig = fetch.get_small(url), fetch.get_small(url + ".sig")
        index = verify_index(idx_bytes, idx_sig, ring, channel, self.now)
        cur = index["current"]
        apply_state = read_json(self.layout.state_file, {}) or {}
        have = installed(self.layout)
        out = {"index_expires": index["expires"]}
        seq = int(cur["seq"])
        # A halt published after staging: drop it so apply never sees it.
        for s in (index.get("halted") or []):
            if (self.layout.staged / str(s)).exists():
                stage_mod.clean_staged(self.layout.staged, keep=set())
        if seq <= int(apply_state.get("max_seq") or 0) or (have.get("seq") is not None and seq <= int(have["seq"])):
            return {**out, "message": "up to date"}
        if seq in (index.get("halted") or []):
            return {**out, "message": f"{cur['version']} is halted"}
        if seq in (apply_state.get("bad") or []):
            return {**out, "message": f"{cur['version']} failed here before"}
        if not ring_open(index, self.box["box_id"], self.now):
            return {**out, "message": f"{cur['version']} not yet open for this box"}
        rel_bytes = fetch.get_small(cur["release_url"])
        if sha256_bytes(rel_bytes) != cur["release_sha256"]:
            raise VerifyError("release.json does not match the channel index")
        rel_sig = fetch.get_small(cur["release_url"] + ".sig")
        needed = int(json.loads(rel_bytes).get("keyring_seq", 0))
        if needed > ring.seq:
            ring = self._maybe_new_keyring(ring, needed)
        release = verify_release(rel_bytes, rel_sig, ring, self.now)
        check_policy(release, apply_state, index, have.get("seq"))
        check_compat(release, self.facts if self.facts is not None else system_facts(), self.box["variant"] or None)
        inc = self.layout.incoming / str(seq)
        for name, data in (("index.json", idx_bytes), ("index.json.sig", idx_sig),
                           ("release.json", rel_bytes), ("release.json.sig", rel_sig)):
            write_atomic(inc / name, data, 0o644)
        ask = release.get("kind") == "major" or bool(release.get("requires_migration"))
        out["available"] = {"version": release["version"], "seq": seq, "kind": release.get("kind"),
                            "requires_migration": bool(release.get("requires_migration")),
                            "notes": release.get("notes", ""), "ask": ask or self.box["update_mode"] != "auto"}
        return out

    def stage(self, force: bool = False) -> dict:
        """Check, then download + extract what is available. Status says what happened."""
        status = self.check()
        avail = status.get("available")
        if not avail:
            return status
        if self.box["update_mode"] == "off" and not force:
            status["message"] = "updates are off"
        elif self.box["metered"] and not force:
            status["message"] = "metered connection: not downloading"
        else:
            try:
                path = self._stage(int(avail["seq"]))
                status.update(staged=str(path), message=f"{avail['version']} staged")
            except (VerifyError, fetch.FetchError, OSError, ValueError, KeyError) as e:
                status.update(result="error", message=f"staging failed: {e}")
        write_json(self.layout.check_status, status, 0o644)
        return status

    def _stage(self, seq: int) -> Path:
        inc = self.layout.incoming / str(seq)
        signed = {n: (inc / n).read_bytes() for n in ("release.json", "release.json.sig", "index.json", "index.json.sig")}
        ring = self._ring()
        pending = self.layout.incoming / "keyring.json"
        if pending.exists():
            ring = accept_keyring(pending.read_bytes(), (self.layout.incoming / "keyring.json.sig").read_bytes(),
                                  self.layout.trust, ring.seq, self.now)
        # Re-verify what is on disk: it is what apply will be handed.
        release = verify_release(signed["release.json"], signed["release.json.sig"], ring, self.now)
        art = release["artifacts"][0]
        size = int(art.get("size") or 0) or None
        fetch.ensure_space(self.layout.upd, 2 * (size or 0) + (64 << 20))
        dest = fetch.download(str(art["url"]), self.layout.incoming / Path(str(art["name"])).name, art["sha256"], size)
        stage_mod.clean_staged(self.layout.staged, keep={seq})
        out = stage_mod.stage(self.layout.staged, release, signed, dest, self.layout.incoming)
        return out
