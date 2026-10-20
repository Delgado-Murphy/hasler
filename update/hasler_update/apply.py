"""The root side: install a staged release, switch, health-check, commit or roll back.

docs/ota.md §4 steps 4-9. Nothing staged by the unprivileged user is trusted:
the signatures are re-checked against the root-owned keyring, the tree is
copied into a root-owned place and re-hashed there (against the signed
``files_sha256``, or - without it - by re-extracting the hash-checked
artifact), and only then used.

Phases (state.json): ``idle|committed`` -> ``trial`` (step ``integrate`` ->
``switched`` -> ``restarted``/``rebooting``) -> ``probation`` (24 h) ->
``committed``; or -> ``rolled_back``. A power cut anywhere lands on a working
version: before ``trial`` nothing live changed; in ``trial`` the early boot
service finishes or undoes the switch, and the boot counter rolls back at the
2nd boot; ``rollback`` is idempotent and resumed if interrupted.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import integrate as integ
from . import stage as stage_mod
from .check import installed
from .health import BOOT_APPEAR_S, HealthChecker, idle_now
from .layout import KEEP_RELEASES, Layout, box_settings, read_box_yaml
from .state import PROBATION_S, ApplyState, fsync_dir, iso, parse_iso, read_json, write_atomic
from .verify import (
    VerifyError, accept_keyring, check_compat, check_policy, load_keyring, python_abi, sha256_bytes,
    sha256_file, system_facts, verify_index, verify_release, version_key,
)

COMPLETE = ".complete"
WINDOW = (2, 5)          # local hours: 02:00-05:00


class ApplyError(Exception):
    pass


class Deferred(ApplyError):
    """Not now (radio in use, outside the window, mode) - not a failure."""


class PowerCut(BaseException):
    """Raised by test hooks at a checkpoint to simulate losing power."""


# --- the outside world ---------------------------------------------------------------


class Hooks:
    """Everything that touches the running system. Tests replace it."""

    def __init__(self, layout: Layout) -> None:
        self.layout = layout

    def now(self) -> float:
        return time.time()

    def local_hour(self) -> int:
        return time.localtime(self.now()).tm_hour

    def checkpoint(self, name: str) -> None:
        pass

    def run(self, argv: list[str], **kw) -> subprocess.CompletedProcess:
        kw.setdefault("capture_output", True)
        kw.setdefault("text", True)
        kw.setdefault("timeout", 600)
        return subprocess.run(argv, **kw)

    def systemctl(self, *args: str) -> None:
        self.run(["systemctl", *args])

    def restart(self, unit: str = "haslerd.service") -> None:
        # A release that crash-looped has used up the unit's start limit
        # (StartLimitBurst); without reset-failed systemd refuses to start even
        # the good release we just rolled back to (found by the bench drill).
        try:
            self.systemctl("reset-failed", unit)
        except Exception:   # noqa: BLE001 - nothing to reset is fine
            pass
        self.systemctl("restart", "--no-block", unit)

    def reboot(self) -> None:
        self.systemctl("reboot")

    def daemon_reload(self) -> None:
        self.systemctl("daemon-reload")

    def udev_reload(self) -> None:
        self.run(["udevadm", "control", "--reload"])
        self.run(["udevadm", "trigger", "--action=change"])

    def bootcfg_save(self) -> None:
        """Refresh the boot config guard's known-good config.txt/cmdline.txt
        (``hasler-bootcfg-guard save``, shipped in integration/sbin/) after an
        intended change, so its early-boot repair never puts an old one back.
        Best effort: the guard only saves a sane file, and its own timer saves
        again after the next boot."""
        tool = self.layout.sbin / "hasler-bootcfg-guard"
        if not tool.is_file():
            return
        try:
            self.run(["/usr/bin/python3", "-I", str(tool), "save"], timeout=60,
                     env={**os.environ, "HASLER_ROOT": str(self.layout.root)})
        except Exception:   # noqa: BLE001 - never fail a commit/rollback over it
            pass

    def idle(self) -> tuple[bool, str]:
        return idle_now()

    def checker(self) -> HealthChecker:
        return HealthChecker(cards_file=self.layout.asound_cards)

    def baseline(self) -> dict:
        return self.checker().baseline()

    def health_wait(self, version: str, baseline: dict, after_boot: bool = False) -> tuple[bool, str]:
        checker = self.checker()
        if after_boot:
            # After a cold boot moOde starts MPD late (after its own startup and
            # the time sync) - well past 90 s on the bench. A trial judged after
            # a power cut gets longer to come up; a live install keeps 90 s.
            checker.appear_s = max(checker.appear_s, BOOT_APPEAR_S)
        return checker.wait(version, baseline)

    def facts(self) -> dict:
        return system_facts(self.layout.root)

    def system_python(self) -> str:
        return "/usr/bin/python3"

    def python_abi(self, python: str) -> str | None:
        return python_abi(python) if os.path.exists(python) else None

    def chown_root(self, path: Path) -> None:
        if os.geteuid() == 0:
            os.lchown(path, 0, 0)

    def build_venv(self, release: Path, python: str) -> None:
        """Offline venv at its final path from the release's own wheelhouse."""
        venv = release / "venv"
        lock = release / "requirements.lock"
        reqs = [ln for ln in (lock.read_text().splitlines() if lock.exists() else [])
                if ln.strip() and not ln.lstrip().startswith("#")]
        args = [python, "-m", "venv"] + ([] if reqs else ["--without-pip"]) + [str(venv)]
        self._must(args, "venv")
        vpy = str(venv / "bin" / "python")
        if reqs:
            env = {**os.environ, "PIP_NO_CACHE_DIR": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                   "PIP_NO_INPUT": "1", "PIP_CONFIG_FILE": os.devnull}
            self._must([vpy, "-m", "pip", "install", "--no-index", "--find-links", str(release / "wheelhouse"),
                        "--require-hashes", "--only-binary", ":all:", "-r", str(lock)], "pip install", env=env)
        if (release / "app").is_dir():
            site = self._must([vpy, "-c", "import sysconfig;print(sysconfig.get_path('purelib'))"], "site").stdout.strip()
            (Path(site) / "hasler-app.pth").write_text(str(release / "app") + "\n")
            self.run([vpy, "-m", "compileall", "-q", str(release / "app")])

    def selftest(self, release: Path, etc: Path, state: Path) -> None:
        script = release / "selftest.py"
        if not script.exists():
            return
        vpy = release / "venv" / "bin" / "python"
        self._must([str(vpy), str(script), "--etc", str(etc), "--state", str(state)], "selftest", timeout=180)

    def run_updater(self, args: list[str]) -> subprocess.CompletedProcess | None:
        """``hasler-update <args>`` from the *installed* updater package
        (``/opt/hasler/updater``, replaced at commit) - a new process, so the
        release's own code runs, not the one already loaded here. None when
        no installed package exists (a source checkout)."""
        pkg = self.layout.updater
        if not (pkg / "hasler_update" / "cli.py").is_file():
            return None
        code = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
                "from hasler_update.cli import main; raise SystemExit(main(sys.argv[1:]))")
        return self.run([sys.executable or "/usr/bin/python3", "-I", "-c", code, str(pkg), *args], timeout=600,
                        env={**os.environ, "HASLER_ROOT": str(self.layout.root)})

    def check_item(self, item: integ.Item) -> None:
        if item.kind == "sudoers":
            self._must(["visudo", "-cf", str(item.src)], "visudo")
        elif item.kind == "avahi":
            # avahi-daemon skips a file it can't parse; refuse it here instead, loudly.
            import xml.etree.ElementTree as ET
            try:
                root = ET.fromstring(item.src.read_bytes())
            except ET.ParseError as e:
                raise ApplyError(f"{item.src.name}: not valid XML ({e})") from e
            if root.tag != "service-group":
                raise ApplyError(f"{item.src.name}: not an avahi service-group")
        elif item.kind == "udev" and shutil.which("udevadm"):
            proc = self.run(["udevadm", "verify", str(item.src)])
            if proc.returncode not in (0,) and "Unknown command" not in (proc.stderr or ""):
                raise ApplyError(f"udevadm verify {item.src.name}: {(proc.stderr or '').strip()}")

    def migrate(self, release: Path, etc: Path, state: Path, from_schema: int) -> list[str]:
        """The release's own runner (haslerd.migrations), on the snapshot copies."""
        vpy = release / "venv" / "bin" / "python"
        proc = self._must([str(vpy), "-m", "haslerd.migrations", "--apply", "--etc", str(etc), "--state", str(state)],
                          "migrations", timeout=300, cwd=str(release))
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]

    def _must(self, argv: list[str], what: str, **kw) -> subprocess.CompletedProcess:
        proc = self.run(argv, **kw)
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-3:]
            raise ApplyError(f"{what} failed: {' | '.join(tail)}")
        return proc


def is_major(release_dir: Path) -> bool:
    """The release's signed ``kind`` (release.json is copied from the verified
    bytes into the root-owned release dir at install)."""
    return (read_json(release_dir / "release.json", {}) or {}).get("kind") == "major"


def pending_migrations(mig: Path, from_schema: int) -> list[Path]:
    """Top-level ``migrations/NNN_*.py`` above the box's schema (decides the snapshot;
    the release's ``python -m haslerd.migrations`` decides what actually runs)."""
    if not mig.is_dir():
        return []
    return [f for f in sorted(mig.glob("[0-9][0-9][0-9]_*.py")) if int(f.name[:3]) > from_schema]


# --- helpers -----------------------------------------------------------------------------


def swap_link(link: Path, target: Path | None) -> None:
    """Point ``link`` at ``target`` atomically (or remove it for None)."""
    if target is None:
        if link.is_symlink():
            link.unlink()
            fsync_dir(link.parent)
        return
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.new")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    rel = os.path.relpath(target, link.parent)
    os.symlink(rel, tmp)
    os.replace(tmp, link)
    fsync_dir(link.parent)


def link_target(link: Path) -> Path | None:
    if not link.is_symlink():
        return None
    return (link.parent / os.readlink(link)).resolve()


def current_boot_id() -> str | None:
    """The kernel's id for this boot (changes on every boot, not on restarts).
    ``HASLER_BOOT_ID`` overrides it in tests."""
    env = os.environ.get("HASLER_BOOT_ID")
    if env:
        return env
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def keep_owners(work: Path, before: Path) -> None:
    """Give every file in ``work`` the owner its counterpart in ``before`` had;
    files a migration created get the owner of ``before/box.yaml`` (or of
    ``before`` itself). Root only - elsewhere there is nothing to restore."""
    if os.geteuid() != 0 or not work.is_dir():
        return
    ref = before / "box.yaml"
    default = os.stat(ref if ref.exists() else before) if before.exists() else None
    for dirpath, _dirs, files in os.walk(work):
        for f in files:
            path = Path(dirpath) / f
            old = before / path.relative_to(work)
            st = os.stat(old) if old.exists() else default
            if st is not None and not path.is_symlink():
                os.chown(path, st.st_uid, st.st_gid)


def sync_tree(src: Path, dst: Path, exclude: tuple[str, ...] = ()) -> None:
    """Make ``dst`` hold what ``src`` holds (files atomically, extras removed)."""
    dst.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for dirpath, dirs, files in os.walk(src):
        rel_dir = Path(dirpath).relative_to(src)
        dirs[:] = [d for d in dirs if (rel_dir / d).as_posix() not in exclude]
        for d in dirs:
            seen.add((rel_dir / d).as_posix())
            (dst / rel_dir / d).mkdir(exist_ok=True)
        for f in files:
            rel = (rel_dir / f).as_posix()
            if rel in exclude:
                continue
            seen.add(rel)
            s, d = Path(dirpath) / f, dst / rel
            data = s.read_bytes()
            if d.is_file() and d.read_bytes() == data:
                continue
            st = os.stat(s)
            write_atomic(d, data, st.st_mode & 0o7777)
            if os.geteuid() == 0:
                os.chown(d, st.st_uid, st.st_gid)
    for dirpath, dirs, files in os.walk(dst, topdown=False):
        rel_dir = Path(dirpath).relative_to(dst)
        if any(rel_dir.as_posix() == e or rel_dir.as_posix().startswith(e + "/") for e in exclude):
            continue
        for f in files:
            rel = (rel_dir / f).as_posix()
            if rel not in seen and rel not in exclude:
                (Path(dirpath) / f).unlink()
        for d in dirs:
            rel = (rel_dir / d).as_posix()
            if rel not in seen and rel not in exclude:
                shutil.rmtree(Path(dirpath) / d, ignore_errors=True)


def copy_tree(src: Path, dst: Path, exclude: tuple[str, ...] = ()) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True,
                        ignore=lambda d, names: [n for n in names
                                                 if (Path(d) / n).relative_to(src).as_posix() in exclude])
    else:
        dst.mkdir(parents=True)


# --- the applier -----------------------------------------------------------------------------


class Applier:
    def __init__(self, layout: Layout, hooks: Hooks | None = None) -> None:
        self.layout = layout
        self.hooks = hooks or Hooks(layout)
        self.state = ApplyState(layout.state_file)

    # --- locking ---------------------------------------------------------------------

    @contextmanager
    def locked(self, wait: bool = True) -> Iterator[bool]:
        self.layout.apply_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        fd = os.open(self.layout.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            except BlockingIOError:
                yield False
                return
            self.state = ApplyState(self.layout.state_file)
            yield True
        finally:
            os.close(fd)

    def _cp(self, name: str) -> None:
        self.hooks.checkpoint(name)

    # --- staged -> verified ---------------------------------------------------------------

    def _trust(self):
        now = self.hooks.now()
        ring = load_keyring(self.layout.trust, now)
        pending = self.layout.incoming / "keyring.json"
        if pending.exists():
            data, sig = pending.read_bytes(), (self.layout.incoming / "keyring.json.sig").read_bytes()
            try:
                new = accept_keyring(data, sig, self.layout.trust, max(ring.seq, int(self.state["keyring_seq"] or 0)), now)
            except VerifyError:
                new = None
            if new is not None:
                write_atomic(self.layout.trust / "keyring.json.sig", sig, 0o444)
                write_atomic(self.layout.trust / "keyring.json", data, 0o444)
                self.state.log("keyring", seq=new.seq)
                self.state.update(keyring_seq=new.seq)
                ring = new
        return ring

    def staged_seqs(self) -> list[int]:
        d = self.layout.staged
        if not d.is_dir():
            return []
        return sorted(int(p.name) for p in d.iterdir() if p.name.isdigit() and (p / "STAGED").exists())

    def load_staged(self, seq: int) -> tuple[dict, dict[str, bytes], Path]:
        d = self.layout.staged / str(seq)
        try:
            signed = {n: (d / n).read_bytes() for n in ("release.json", "release.json.sig", "index.json", "index.json.sig")}
        except OSError as e:
            raise ApplyError(f"staged release {seq} incomplete") from e
        ring = self._trust()
        now = self.hooks.now()
        release = verify_release(signed["release.json"], signed["release.json.sig"], ring, now)
        box = box_settings(self.layout)
        index = verify_index(signed["index.json"], signed["index.json.sig"], ring, box["channel"], now)
        if int(release["seq"]) != seq:
            raise VerifyError("staged dir does not match its release.json")
        if sha256_bytes(signed["release.json"]) != index["current"]["release_sha256"]:
            raise VerifyError("staged release.json does not match the signed index")
        check_policy(release, self.state.doc, index, installed(self.layout).get("seq"))
        check_compat(release, self.hooks.facts(), box["variant"] or None)
        return release, signed, d

    def install_release(self, release: dict, signed: dict[str, bytes], staged: Path) -> Path:
        """Root-owned ``releases/<v>/`` with a venv, verified; idempotent."""
        v = str(release["version"])
        final = self.layout.release(v)
        if (final / COMPLETE).exists() and (final / "release.json").read_bytes() == signed["release.json"]:
            return final
        if final in (link_target(self.layout.current), link_target(self.layout.previous)):
            raise ApplyError(f"{final} is in use with different content")
        tmp = self.layout.releases / f"{v}.tmp"
        for p in (tmp, final):
            if p.exists():
                shutil.rmtree(p)
        self.layout.releases.mkdir(parents=True, exist_ok=True)
        if release.get("files_sha256"):
            shutil.copytree(staged / "tree", tmp, symlinks=True)
            stage_mod.verify_tree(tmp, release["files_sha256"])
        else:
            self._reextract(release, tmp)
        for name in ("release.json", "release.json.sig"):
            write_atomic(tmp / name, signed[name], 0o444)
        self._cp("copied")
        os.rename(tmp, final)
        fsync_dir(self.layout.releases)
        bundled = final / "python" / "bin" / "python3"
        python = str(bundled) if bundled.exists() else self.hooks.system_python()
        want = (release.get("requires") or {}).get("python_abi")
        have = self.hooks.python_abi(python)
        if want and have not in (want, None):
            raise ApplyError(f"python {python} is {have}, release needs {want}")
        self.hooks.build_venv(final, python)
        self._cp("venv")
        for dirpath, dirs, files in os.walk(final):
            for n in dirs + files:
                p = Path(dirpath) / n
                if not p.is_symlink():
                    os.chmod(p, os.lstat(p).st_mode & ~0o022)
                self.hooks.chown_root(p)
        stage_mod.fsync_tree(final)
        write_atomic(final / COMPLETE, iso(self.hooks.now()).encode(), 0o444)
        return final

    def _reextract(self, release: dict, tmp: Path) -> None:
        art = release["artifacts"][0]
        src = self.layout.incoming / Path(str(art["name"])).name
        work = Path(tempfile.mkdtemp(prefix="extract-", dir=self.layout.apply_dir))
        try:
            mine = work / src.name
            shutil.copyfile(src, mine)          # root-owned copy, then hash *that*
            if sha256_file(mine) != art["sha256"]:
                raise VerifyError("artifact does not match the signed manifest")
            tar = stage_mod.decompress(mine, work / "plain.tar")
            out = stage_mod.extract(tar, work / "x", f"hasler-{release['version']}")
            stage_mod.verify_tree(out)
            os.rename(out, tmp)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # --- gating -------------------------------------------------------------------------------

    def gate(self, release: dict, auto: bool, now_: bool, force: bool) -> None:
        box = box_settings(self.layout)
        if auto:
            if box["update_mode"] != "auto":
                raise Deferred(f"update_mode is {box['update_mode']}: waiting to be asked")
            if release.get("kind") == "major" or release.get("requires_migration"):
                raise Deferred("major or migration release: waiting to be asked")
        if not now_ and not force:
            h = self.hooks.local_hour()
            if not WINDOW[0] <= h < WINDOW[1]:
                raise Deferred("outside the 02:00-05:00 window")
        if not force:
            ok, why = self.hooks.idle()
            if not ok:
                raise Deferred(why)

    # --- apply -----------------------------------------------------------------------------------

    def apply(self, seq: int | None = None, auto: bool = False, now_: bool = False, force: bool = False,
              allow_reboot: bool = False) -> str:
        with self.locked() as got:
            if not got:
                raise ApplyError("another apply is running")
            self._settle()
            if self.state["phase"] == "trial":
                raise ApplyError("a trial is in progress")
            self._pending_via_updater()
            bad, floor = set(self.state["bad"] or []), int(self.state["max_seq"] or 0)
            seqs = [s for s in self.staged_seqs() if (seq is None or s == seq) and s not in bad and s > floor]
            if not seqs:
                return "nothing staged"
            release, signed, staged = self.load_staged(seqs[-1])
            self.gate(release, auto, now_, force)
            final = self.install_release(release, signed, staged)
            return self._switch(final, release, force=force, allow_reboot=allow_reboot)

    def _preflight(self, final: Path, allow_reboot: bool = False) -> tuple[list[integ.Item], Path | None, list[str]]:
        deferred: list[str] = []
        try:
            items = integ.plan(self.layout, final, major=is_major(final), deferred=deferred)
        except integ.IntegrateError as e:
            raise ApplyError(str(e)) from e
        for it in items:
            self.hooks.check_item(it)
            if it.kind == "configtxt" and not allow_reboot:
                try:
                    current = it.dest.read_text(encoding="utf-8")
                except OSError as e:
                    raise ApplyError(f"{it.dest} missing") from e
                if (integ.config_block(current) or "").strip("\n") != it.src.read_text().strip("\n"):
                    raise ApplyError("config.txt block changes: needs a reboot (apply --allow-reboot, night only)")
        work = None
        schema = int(read_box_yaml(self.layout.box_yaml).get("schema") or 0)
        mig = final / "migrations"
        snap = self.layout.snapshots / final.name
        rel = read_json(final / "release.json", {}) or {}
        needs = bool(pending_migrations(mig, schema)) or int(rel.get("config_schema") or 0) > schema
        if needs:
            shutil.rmtree(snap, ignore_errors=True)
            self.layout.snapshots.mkdir(parents=True, exist_ok=True, mode=0o700)   # holds secrets
            copy_tree(self.layout.etc, snap / "before" / "etc", exclude=("trust",))
            copy_tree(self.layout.haslerd_state, snap / "before" / "state")
            copy_tree(snap / "before" / "etc", snap / "work" / "etc")
            copy_tree(snap / "before" / "state", snap / "work" / "state")
            ran = self.hooks.migrate(final, snap / "work" / "etc", snap / "work" / "state", schema)
            self.state.log("migrations", ran=ran)
            work = snap / "work"
            # Migrations run as root; what they rewrite must stay haslerd's, or it
            # can no longer save box.yaml in the sticky /etc/hasler.
            keep_owners(work / "etc", snap / "before" / "etc")
            keep_owners(work / "state", snap / "before" / "state")
            self.hooks.selftest(final, work / "etc", work / "state")
        else:
            self.hooks.selftest(final, self.layout.etc, self.layout.haslerd_state)
        return items, work, deferred

    def _switch(self, final: Path, release: dict | None, force: bool = False, allow_reboot: bool = False,
                manual: bool = False) -> str:
        to_v = final.name
        to_seq = int(release["seq"]) if release else (read_json(final / "release.json", {}) or {}).get("seq")
        cur = installed(self.layout)
        from_v = cur.get("version")
        items, work, deferred = self._preflight(final, allow_reboot)
        if not force:
            ok, why = self.hooks.idle()      # haslerd's veto, right before the switch
            if not ok:
                raise Deferred(why)
        baseline = self.hooks.baseline()
        integ.Integrator(self.layout, to_v).reset()
        prev_link = link_target(self.layout.previous)
        old_integrated = list(self.state["integrated"] or [])
        self.state.log("switch", **{"from": from_v, "to": to_v, "manual": manual})
        if deferred:
            # A major release's system files of a kind this updater has no rule
            # for: its own updater puts them in place once it commits.
            self.state.log("integration-deferred", to=to_v, items=deferred)
        self.state.update(
            phase="trial", step="integrate", **{"from": from_v}, from_seq=cur.get("seq"), to=to_v,
            to_seq=to_seq, boots=0, boot_id=current_boot_id(), baseline=baseline, reason=None, manual=manual,
            prev_link=str(prev_link) if prev_link else None, integrated_prev=old_integrated,
            integrated=sorted(set(old_integrated) | set(integ.dests(items))), migrated=work is not None,
            deadline=iso(self.hooks.now() + 150),
            pending_integration={"version": to_v, "items": deferred, "error": None, "attempts": 0}
            if deferred else None,
            pending_prev=self.state["pending_integration"],
        )
        self._cp("trial-written")
        ig = integ.Integrator(self.layout, to_v, chown=self.hooks.chown_root)
        try:
            ig.apply(items, [Path(p) for p in old_integrated], allow_configtxt=allow_reboot)
            self._cp("integrated")
            if work is not None:
                sync_tree(work / "etc", self.layout.etc, exclude=("trust",))
                sync_tree(work / "state", self.layout.haslerd_state)
                self._cp("migrated")
            if cur.get("path"):
                swap_link(self.layout.previous, Path(cur["path"]))
            swap_link(self.layout.current, final)
            self.state.update(step="switched", integrated=integ.dests(items))
            self._cp("switched")
            self.hooks.daemon_reload()
            if "udev" in ig.changed:
                self.hooks.udev_reload()
        except Exception as e:      # (PowerCut is a BaseException: the boot path handles that)
            self._rollback(f"switch failed: {e}", restart=True)
            raise ApplyError(f"switch to {to_v} failed and was undone: {e}") from e
        if "configtxt" in ig.changed:
            self.state.update(step="rebooting")
            self._cp("rebooting")
            self.hooks.reboot()
            return "rebooting into the trial"
        self.hooks.restart("haslerd.service")
        self.state.update(step="restarted")
        self._cp("restarted")
        return self._judge()

    def _judge(self, after_boot: bool = False) -> str:
        to_v = self.state["to"]
        ok, why = self.hooks.health_wait(to_v, self.state["baseline"] or {}, after_boot=after_boot)
        self._cp("health")
        if ok:
            self._commit()
            return f"{to_v} committed"
        self._rollback(why, restart=True)
        return f"{to_v} rolled back: {why}"

    # --- commit ------------------------------------------------------------------------------------

    def _commit(self) -> None:
        st = self.state
        manual = bool(st["manual"])
        max_seq = int(st["max_seq"] or 0)
        if not manual and st["to_seq"] is not None:
            max_seq = max(max_seq, int(st["to_seq"]))
        st.log("commit", to=st["to"])
        # A deliberate "go back" has nothing to fall back to: no probation.
        st.update(phase="committed" if manual else "probation", step=None, max_seq=max_seq,
                  deadline=None if manual else iso(self.hooks.now() + PROBATION_S), boots=0)
        self._cp("committed")
        self._install_updater(self.layout.release(st["to"]))
        self.prune()
        stage_mod.clean_staged(self.layout.staged, keep=set())
        if self.layout.incoming.is_dir():
            for p in self.layout.incoming.iterdir():
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        snap = self.layout.snapshots / str(st["to"])
        # The config snapshot stays through probation (a rollback restores it).
        if not st["migrated"]:
            shutil.rmtree(snap, ignore_errors=True)
        # The release's config.txt block (if it changed one) is now the known-good.
        self.hooks.bootcfg_save()
        # Last: what the old updater had no rule for, by the release's own updater.
        self._pending_via_updater()

    def _install_updater(self, release: Path) -> None:
        """A new updater is used only once its release has passed: the package
        (``app/hasler_update`` -> ``/opt/hasler/updater/hasler_update``), its
        launcher (``integration/sbin/hasler-update``) and its units
        (``integration/systemd/hasler-update-*``)."""
        src = release / "app" / "hasler_update"
        if src.is_dir():
            dst, new, old = self.layout.updater, self.layout.opt / "updater.new", self.layout.opt / "updater.old"
            shutil.rmtree(new, ignore_errors=True)
            shutil.copytree(src, new / "hasler_update", symlinks=False,
                            ignore=shutil.ignore_patterns("__pycache__", "tests"))
            shutil.rmtree(old, ignore_errors=True)
            if dst.exists():
                os.rename(dst, old)
            os.rename(new, dst)
            fsync_dir(self.layout.opt)
            shutil.rmtree(old, ignore_errors=True)
        changed = False
        for it in integ.plan(self.layout, release, reserved=True, major=is_major(release)):
            data = it.src.read_bytes()
            if not (it.dest.is_file() and it.dest.read_bytes() == data):
                write_atomic(it.dest, data, it.mode)
                self.hooks.chown_root(it.dest)
                changed = True
        if changed:
            self.hooks.daemon_reload()

    def commit(self) -> str:
        """By hand: accept a trial now, or end probation early."""
        with self.locked() as _:
            if self.state["phase"] == "trial":
                self._commit()
                return f"{self.state['to']} committed (by hand)"
            if self.state["phase"] == "probation":
                self.state.update(phase="committed", deadline=None)
                return f"{self.state['to']} committed"
            return f"nothing to commit (phase {self.state['phase']})"

    def _settle(self) -> None:
        """Probation over -> committed; drop half-built releases."""
        st = self.state
        if st["phase"] == "probation" and st["deadline"]:
            try:
                if self.hooks.now() >= parse_iso(st["deadline"]):
                    st.log("probation-over", to=st["to"])
                    st.update(phase="committed", deadline=None)
                    shutil.rmtree(self.layout.snapshots / str(st["to"]), ignore_errors=True)
            except ValueError:
                pass
        if self.layout.releases.is_dir():
            keep = {link_target(self.layout.current), link_target(self.layout.previous)}
            for p in self.layout.releases.iterdir():
                if p.name.endswith(".tmp") or (p.is_dir() and not (p / COMPLETE).exists() and p.resolve() not in keep):
                    shutil.rmtree(p, ignore_errors=True)

    def prune(self) -> None:
        rels = self.layout.releases
        if not rels.is_dir():
            return
        keep = {link_target(self.layout.current), link_target(self.layout.previous)}
        done = sorted((p for p in rels.iterdir() if (p / COMPLETE).exists()), key=lambda p: version_key(p.name), reverse=True)
        kept = [p for p in done if p.resolve() in keep]
        for p in done:
            if p.resolve() in keep:
                continue
            if len(kept) < KEEP_RELEASES:
                kept.append(p)
            else:
                shutil.rmtree(p, ignore_errors=True)

    # --- rollback ------------------------------------------------------------------------------------

    def _rollback(self, reason: str, restart: bool) -> None:
        st = self.state
        to_v, from_v = st["to"], st["from"]
        if st["phase"] != "trial" or st["step"] != "rollback":
            st.log("rollback", to=to_v, back_to=from_v, reason=reason)
        st.update(phase="trial", step="rollback", reason=reason)
        self._cp("rollback-started")
        touched = integ.Integrator(self.layout, str(to_v)).restore()
        self._cp("rollback-restored")
        snap = self.layout.snapshots / str(to_v) / "before"
        if st["migrated"] and snap.is_dir():
            sync_tree(snap / "etc", self.layout.etc, exclude=("trust",))
            sync_tree(snap / "state", self.layout.haslerd_state)
        back = self.layout.release(from_v) if from_v else None
        swap_link(self.layout.current, back if back is not None and back.is_dir() else None)
        swap_link(self.layout.previous, Path(st["prev_link"]) if st["prev_link"] else None)
        self._cp("rollback-switched")
        if not st["manual"]:
            st.mark_bad(st["to_seq"])
        st.update(phase="rolled_back", step=None, integrated=st["integrated_prev"] or [], deadline=None,
                  failed=to_v, pending_integration=st["pending_prev"])
        self.hooks.daemon_reload()
        if "udev" in touched:
            self.hooks.udev_reload()
        if "configtxt" in touched:
            self.hooks.bootcfg_save()
        if "configtxt" in touched and restart:
            self.hooks.reboot()
        elif restart:
            self.hooks.restart("haslerd.service")

    def rollback(self, to: str | None = None, force: bool = True) -> str:
        """Manual "go back": to ``previous`` or a kept release, through the same switch + health path."""
        with self.locked() as _:
            self._settle()
            if self.state["phase"] == "trial":
                self._rollback("rolled back by hand", restart=True)
                return f"trial rolled back to {self.state['to']}"
            if to is None:
                target = link_target(self.layout.previous)
                if target is None:
                    raise ApplyError("no previous release kept")
            else:
                target = self.layout.release(to)
            if not (target / COMPLETE).exists():
                raise ApplyError(f"release {target.name} is not on this box")
            if target == link_target(self.layout.current):
                return f"{target.name} is already running"
            # The release being left is marked bad so auto mode won't reinstall it tomorrow.
            leaving = installed(self.layout).get("seq")
            out = self._switch(target, None, force=force, manual=True)
            if self.state["phase"] == "committed" and leaving is not None:
                self.state.mark_bad(int(leaving))
                self.state.save()
            return out

    # --- boot, resume, OnFailure -----------------------------------------------------------------------

    def boot(self) -> str:
        """Early in boot, before haslerd (hasler-update-boot.service).

        systemd can also start this unit when haslerd is merely *restarted*
        (it is WantedBy=haslerd.service, and a unit whose condition skipped it
        at boot doesn't stay active). A restart is not a boot: if the kernel's
        boot id is the one the trial began under, return at once - without the
        lock, which a running apply holds while it waits for haslerd's health.
        """
        st = self.state
        if st["phase"] == "trial" and st["boot_id"] and st["boot_id"] == current_boot_id():
            return "same boot: nothing to count"
        with self.locked() as _:
            self._settle()
            st = self.state
            if st["phase"] != "trial":
                return f"phase {st['phase']}"
            if st["step"] == "rollback":
                self._rollback(st["reason"] or "resumed rollback", restart=False)
                return "finished an interrupted rollback"
            if st["step"] == "integrate":
                self._rollback("power cut during the switch", restart=False)
                return "undid an interrupted switch"
            st.update(boots=int(st["boots"] or 0) + 1)
            if st["boots"] >= 2:
                self._rollback(f"boot loop ({st['boots']} boots in trial)", restart=False)
                return "rolled back after a boot loop"
            return f"trial boot {st['boots']}"

    def resume(self) -> str:
        """After haslerd started at boot: finish judging a trial whose apply died."""
        with self.locked(wait=False) as got:
            if not got:
                return "apply running"
            self._settle()
            st = self.state
            if st["phase"] != "trial":
                self._pending_via_updater()
                return f"phase {st['phase']}"
            if st["step"] == "rollback":
                self._rollback(st["reason"] or "resumed rollback", restart=True)
                return "finished an interrupted rollback"
            if st["step"] == "integrate":
                self._rollback("power cut during the switch", restart=True)
                return "undid an interrupted switch"
            if st["step"] == "switched":
                self.hooks.daemon_reload()
                self.hooks.restart("haslerd.service")
            return self._judge(after_boot=True)

    def on_failure(self) -> str:
        """haslerd's OnFailure=: during trial or probation, go back."""
        with self.locked(wait=False) as got:
            if not got:
                return "apply running (it judges the trial)"
            st = self.state
            self._settle()
            if st["phase"] in ("trial", "probation") and st["from"]:
                self._rollback("haslerd failed", restart=True)
                return f"rolled back to {self.state['to']}"
            return f"phase {st['phase']}: nothing to do"

    # --- system files the trial's updater could not place -------------------------------

    def _pending_via_updater(self) -> None:
        """Have the *installed* updater (after a commit: the release's own)
        integrate ``pending_integration``, in a new process. Called with the
        lock held (the child is told so). Never raises: the release stays
        committed; a failure is recorded and retried at the next apply/resume."""
        pend = self.state["pending_integration"]
        if not pend or self.state["phase"] not in ("probation", "committed"):
            return
        before = int(pend.get("attempts") or 0)
        self.state.save()           # the child reads state.json: flush our history first
        try:
            proc = self.hooks.run_updater(["integrate-pending", "--locked"])
        except Exception as e:      # noqa: BLE001 - recorded below
            proc, why = None, f"could not run the installed updater: {e}"
        else:
            if proc is None:        # no installed package (source checkout): this code is the updater
                try:
                    self.integrate_pending(locked=True)
                except ApplyError:
                    pass            # recorded in state.json
                return
            why = " | ".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-3:]) \
                or f"exit {proc.returncode}"
        self.state = ApplyState(self.layout.state_file)      # the child wrote it
        pend = self.state["pending_integration"]
        if pend and int(pend.get("attempts") or 0) == before and (proc is None or proc.returncode != 0):
            self._pending_failed(pend, why)

    def _pending_failed(self, pend: dict, why: str) -> None:
        n = int(pend.get("attempts") or 0) + 1
        self.state.log("integrate-pending-failed", version=pend.get("version"), error=why, attempts=n)
        self.state.update(pending_integration={**pend, "error": why, "attempts": n,
                                               "last_try": iso(self.hooks.now())})

    def integrate_pending(self, locked: bool = False) -> str:
        """``hasler-update integrate-pending``: put in place the current
        release's system files the updater that ran its trial had no rule for.
        Idempotent (unchanged files are left alone); the backups go with the
        release's own (``backup/<version>/``), so a rollback in probation
        removes them too. ``locked``: the caller (a commit) holds the lock."""
        if locked:
            self.state = ApplyState(self.layout.state_file)
            return self._integrate_pending()
        with self.locked() as _:
            return self._integrate_pending()

    def _integrate_pending(self) -> str:
        st = self.state
        pend = st["pending_integration"]
        if not pend:
            return "nothing pending"
        if st["phase"] not in ("probation", "committed"):
            return f"phase {st['phase']}: later"
        version = str(pend.get("version"))
        cur = link_target(self.layout.current)
        if cur is None or cur.name != version:
            st.log("integrate-pending-dropped", version=version, current=cur.name if cur else None)
            st.update(pending_integration=None)
            return f"{version} is not running: dropped"
        try:
            left: list[str] = []
            items = integ.plan(self.layout, cur, major=is_major(cur), deferred=left)
            if left:
                raise ApplyError("no rule in this updater either: " + ", ".join(left))
            for it in items:
                self.hooks.check_item(it)
            ig = integ.Integrator(self.layout, version, chown=self.hooks.chown_root)
            ig.apply(items, allow_configtxt=False)
            if ig.changed:
                self.hooks.daemon_reload()
            if "udev" in ig.changed:
                self.hooks.udev_reload()
        except Exception as e:      # noqa: BLE001 - recorded, retried; the release stays
            self._pending_failed(pend, str(e))
            raise ApplyError(f"pending integration for {version} failed: {e}") from e
        st.log("integrate-pending", version=version, items=pend.get("items") or [], changed=sorted(ig.changed))
        st.update(pending_integration=None,
                  integrated=sorted(set(st["integrated"] or []) | set(integ.dests(items))))
        return f"{version}: pending system files in place"

    def status(self) -> dict:
        kept = []
        if self.layout.releases.is_dir():
            kept = sorted((p.name for p in self.layout.releases.iterdir() if (p / COMPLETE).exists()), key=version_key)
        return {
            "installed": installed(self.layout),
            "previous": (link_target(self.layout.previous) or Path("")).name or None,
            "kept": kept,
            "state": {k: v for k, v in self.state.doc.items() if k not in ("baseline",)},
            "staged": self.staged_seqs(),
            "check": read_json(self.layout.check_status, None),
        }
