"""Box-side ops: integration sources, polkit, yt-dlp upgrade, ota-bootstrap, dev channel.

The end-to-end test builds a real (--no-wheels) release with build_release.py,
publishes it with the dev tools (dev-keys.sh + dev-channel.py, file:// base URL)
and lets the real Checker stage it on a fake box whose trust was installed by
ota-bootstrap.sh - the same path the bench box takes, minus the hardware.
"""

from __future__ import annotations

import importlib.util
import json
import re
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import FACTS

from hasler_update import integrate as integ
from hasler_update.check import Checker
from hasler_update.layout import Layout

ROOT = Path(__file__).resolve().parents[2]
REL = ROOT / "scripts" / "release"
NODE = shutil.which("node")   # resolved now: a later test may leave PATH changed
needs = lambda *tools: pytest.mark.skipif(  # noqa: E731
    not all(shutil.which(t) for t in tools), reason=f"needs {' '.join(tools)}")
COMMIT_TIME = int(time.time()) // 86400 * 86400 - 86400


def _load(name: str, file: str):
    sys.path.insert(0, str(REL))
    spec = importlib.util.spec_from_file_location(name, REL / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def git(repo, *args):
    env = dict(os.environ, GIT_AUTHOR_DATE=f"@{COMMIT_TIME} +0000", GIT_COMMITTER_DATE=f"@{COMMIT_TIME} +0000")
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "commit.gpgsign=false", *args], check=True, capture_output=True, env=env).stdout


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """This checkout's files as git sees them now (tracked + new, minus deleted/ignored), version 0.2.0."""
    dest = tmp_path_factory.mktemp("repo")
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
                           check=True, capture_output=True).stdout.decode().split("\0")
    for rel in filter(None, files):
        src = ROOT / rel
        if src.is_file() and not src.is_symlink():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    pp = dest / "daemon" / "pyproject.toml"
    # whatever version master carries, the fixture builds 0.2.0
    pp.write_text(re.sub(r'(?m)^version = "[^"]+"', 'version = "0.2.0"', pp.read_text(), count=1))
    init = dest / "daemon" / "haslerd" / "__init__.py"
    init.write_text(re.sub(r'(?m)^__version__ = "[^"]+"', '__version__ = "0.2.0"', init.read_text(), count=1))
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "fixture")
    return dest


@pytest.fixture(scope="module")
def built(repo, tmp_path_factory):
    br = _load("ota_ops_build_release", "build_release.py")
    out = tmp_path_factory.mktemp("dist")
    args = br.parse_args(["--version", "0.2.0", "--out", str(out), "--repo", str(repo), "--no-wheels",
                          "--zstd-level", "3", "--channel", "bench", "--strict"])
    tarball = br.build(args)          # --strict: units free of /home/pi, version matches
    mm = sys.modules["make_manifest"]
    top, files = mm.read_tarball(tarball)
    return out, files


# --- integration sources -------------------------------------------------------------

@needs("git", "zstd")
def test_strict_release_integration(built):
    _, files = built
    integ_files = {p for p in files if p.startswith("integration/")}
    for p in ("systemd/haslerd.service", "systemd/go-librespot.service", "systemd/hasler-lamp-boot.service",
              "systemd/yt-dlp-update.service", "systemd/yt-dlp-update.timer",
              "systemd/hasler-update-check.service", "systemd/hasler-update-rollback.service",
              "sudoers/haslerd", "configtxt/hasler.block", "udev/99-hasler-pwm.rules",
              "sbin/hasler-audio-mono", "sbin/hasler-wifi-join", "sbin/hasler-lamp-boot",
              "sbin/hasler-ytdlp-update", "sbin/hasler-update", "sbin/hasler-bootcfg-guard", "sbin/hasler-help",
              "systemd/hasler-bootcfg-guard.service", "systemd/hasler-bootcfg-save.timer", "bin/hasler-tone", "bin/hasler-dsp", "shutdown/hasler-lamp-halt",
              "avahi/hasler.service", "systemd/bluetooth.service.d/hasler-speaker.conf"):
        assert f"integration/{p}" in integ_files, p
    assert files["integration/sbin/hasler-ytdlp-update"][1] == 0o755
    assert files["integration/sbin/hasler-help"][1] == 0o755
    unit = files["integration/systemd/haslerd.service"][0].decode()
    for line in ("ExecStart=/opt/hasler/current/venv/bin/python -m haslerd.main --etc /etc/hasler",
                 "WorkingDirectory=/var/lib/haslerd", "User=pi", "OnFailure=hasler-update-rollback.service",
                 "StartLimitIntervalSec=120", "StartLimitBurst=5"):
        assert line in unit.splitlines(), line
    glr = files["integration/systemd/go-librespot.service"][0].decode()
    assert "ExecStart=/opt/hasler/current/bin/go-librespot --config_dir /var/lib/go-librespot" in glr
    for p, (data, _) in files.items():
        if p.startswith("integration/systemd/"):
            assert b"/home/pi" not in data, p
    assert not any("legacy" in p for p in files)


def test_known_kinds_since_matches_the_shipped_updater():
    """make_manifest.KNOWN_KINDS_SINCE (what a non-major release may carry) is
    what the newest tagged updater knows, and never more than this one knows."""
    mm = _load("make_manifest", "make_manifest.py")
    assert mm.KNOWN_KINDS_SINCE <= integ.known_kinds(), "integrate.py lost a kind boxes rely on"
    try:
        tags = subprocess.run(["git", "-C", str(ROOT), "tag", "--list", "v*"], check=True,
                              capture_output=True, text=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        tags = []
    vs = []
    for t in tags:
        try:
            vs.append((mm.seq_for(t[1:]), t))
        except mm.ReleaseError:
            continue
    if not vs:
        pytest.skip("no v* tags in this checkout")
    tag = max(vs)[1]
    src = subprocess.run(["git", "-C", str(ROOT), "show", f"{tag}:update/hasler_update/integrate.py"],
                         check=True, capture_output=True, text=True).stdout
    shipped = {"systemd" if k == "systemd-dropin" else k for k in re.findall(r'^\s*\("([a-z-]+)", re\.compile', src, re.M)}
    assert shipped == set(mm.KNOWN_KINDS_SINCE), \
        f"KNOWN_KINDS_SINCE should be what {tag}'s updater knows: {sorted(shipped)}"


def test_avahi_service_plan_and_check(tmp_path):
    """integration/avahi/hasler.service -> /etc/avahi/services/hasler.service; nothing else there."""
    layout = Layout(tmp_path / "root")
    rel = tmp_path / "rel"
    (rel / "integration/avahi").mkdir(parents=True)
    src = ROOT / "config/avahi/hasler.service"
    shutil.copy2(src, rel / "integration/avahi/hasler.service")
    items = integ.plan(layout, rel)
    assert [(i.kind, i.dest, i.mode) for i in items] == \
        [("avahi", tmp_path / "root/etc/avahi/services/hasler.service", 0o644)]
    text = src.read_text()
    for needle in ("<type>_hasler._tcp</type>", "<port>8080</port>", "<txt-record>api=1</txt-record>",
                   "<txt-record>path=/api/info</txt-record>"):
        assert needle in text, needle
    from hasler_update.apply import ApplyError, Hooks
    check = Hooks(layout).check_item
    check(items[0])                                           # the shipped file parses
    (rel / "integration/avahi/hasler.service").write_text("<service-group><oops></service-group>")
    with pytest.raises(ApplyError, match="not valid XML"):
        check(integ.plan(layout, rel)[0])
    (rel / "integration/avahi/other.service").write_text("<service-group/>")
    with pytest.raises(integ.IntegrateError, match="allowlist"):
        integ.plan(layout, rel)


def test_avahi_service_put_in_place_and_rolled_back(tmp_path):
    layout = Layout(tmp_path / "root")
    rel = tmp_path / "rel"
    (rel / "integration/avahi").mkdir(parents=True)
    (rel / "integration/avahi/hasler.service").write_text("<service-group/>\n")
    ig = integ.Integrator(layout, "1.1.0")
    ig.apply(integ.plan(layout, rel))
    dest = layout.avahi_services / "hasler.service"
    assert dest.read_text() == "<service-group/>\n" and "avahi" in ig.changed
    assert integ.Integrator(layout, "1.1.0").restore() == {"avahi"}
    assert not dest.exists()


def test_sudoers_source_is_todays_rules():
    text = (ROOT / "config/sudoers/haslerd").read_text()
    rules = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert rules == ["pi ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff, /bin/systemctl poweroff, "
                     "/usr/local/sbin/hasler-wifi-join, /usr/local/sbin/hasler-audio-mono, "
                     "/usr/local/sbin/hasler-multiroom, /usr/local/sbin/hasler-bt-accept, /usr/local/sbin/hasler-help, "
                     "/usr/bin/systemctl try-restart go-librespot.service, "
                     "/bin/systemctl try-restart go-librespot.service"]
    if shutil.which("visudo"):
        assert subprocess.run(["visudo", "-cf", str(ROOT / "config/sudoers/haslerd")]).returncode == 0


def test_configtxt_block_matches_the_legacy_install(tmp_path):
    """The bench box's install.sh block has the same lines, so re-marking it needs no reboot."""
    block = (ROOT / "config/configtxt/hasler.block").read_text()
    assert block.splitlines() == ["[all]", "gpio=17,22,27=op,dh", "dtoverlay=pwm-gpio,gpio=17",
                                  "dtoverlay=pwm-gpio,gpio=22", "dtoverlay=pwm-gpio,gpio=27"]
    b, e = integ.LEGACY_BLOCKS[0]
    legacy = "arm_64bit=1\n" + b + "\n" + block + e + "\n"
    rendered = integ.render_config_txt(legacy, block)
    assert integ.config_block(rendered).strip("\n") == block.strip("\n")
    assert rendered.count("pwm-gpio,gpio=17") == 1


# --- polkit ------------------------------------------------------------------------------

POLKIT_HARNESS = r"""
const fs = require('fs');
let rule;
const polkit = {Result: {YES: 'yes'}, addRule: f => { rule = f; }};
eval(fs.readFileSync(process.argv[1], 'utf8'));
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(([user, verb, unit, id]) => rule(
  {id: id || 'org.freedesktop.systemd1.manage-units', lookup: k => ({verb, unit})[k]}, {user}) || null)));
"""


@needs("node")
@pytest.mark.parametrize("file,cases", [
    ("50-hasler-update.rules", [
        (("pi", "start", "hasler-update-apply@20099.service"), "yes"),
        (("pi", "start", "hasler-update-apply@rollback.service"), "yes"),
        (("pi", "start", "hasler-update-apply@rollback-0.2.0-rc.1.service"), "yes"),
        (("pi", "start", "hasler-update-check.service"), "yes"),
        (("pi", "stop", "hasler-update-check.service"), None),
        (("pi", "restart", "hasler-update-apply@1.service"), None),
        (("pi", "start", "hasler-update-apply@1 x.service"), None),
        (("pi", "start", "hasler-update-apply@../x.service"), None),
        (("pi", "start", "hasler-update-apply.service"), None),
        (("pi", "start", "haslerd.service"), None),
        (("pi", "start", "yt-dlp-update.service"), None),
        (("haslerd", "start", "hasler-update-check.service"), None),
        (("nobody", "start", "hasler-update-apply@1.service"), None),
    ]),
    ("51-hasler-ytdlp.rules", [
        (("pi", "start", "yt-dlp-update.service"), "yes"),
        (("pi", "stop", "yt-dlp-update.service"), None),
        (("pi", "start", "yt-dlp-update.timer"), None),
        (("pi", "start", "hasler-update-check.service"), None),
        (("ytdlp", "start", "yt-dlp-update.service"), None),
    ]),
])
def test_polkit_rules(file, cases):
    args = json.dumps([list(c) for c, _ in cases])
    out = subprocess.run([NODE, "-e", POLKIT_HARNESS, str(ROOT / "config/update" / file), args],
                         capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == [want for _, want in cases]


# --- yt-dlp upgrade ------------------------------------------------------------------------

FAKE_PY = r"""#!/bin/bash
# base python: -m venv DIR; venv python: -m pip (writes a fake yt-dlp), -c (import check)
if [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"; cp "$0" "$3/bin/python"; exit 0; fi
here="$(cd "$(dirname "$0")" && pwd)"
if [ "$1" = -m ] && [ "$2" = pip ]; then
  [ "${FAKE_PIP_FAIL:-0}" = 1 ] && exit 1
  cat > "$here/yt-dlp" <<EOF
#!/bin/bash
[ "\$1" = --version ] && { echo ${FAKE_VERSION:-1.0}; exit 0; }
for a in "\$@"; do case "\$a" in file://*) [ ${FAKE_OFFLINE_RC:-0} = 0 ] && echo "hasler selftest (1)"; exit ${FAKE_OFFLINE_RC:-0};; esac; done
exit ${FAKE_NET_RC:-0}
EOF
  chmod +x "$here/yt-dlp"; exit 0
fi
[ "$1" = -c ] && exit "${FAKE_IMPORT_RC:-0}"
exec python3 "$@"
"""


@pytest.fixture
def ytdlp(tmp_path):
    py = tmp_path / "fakepy"
    py.write_text(FAKE_PY)
    py.chmod(0o755)
    root = tmp_path / "opt-yt-dlp"
    root.mkdir()

    def run(*args, **env):
        e = dict(os.environ, HASLER_YTDLP_ROOT=str(root), HASLER_YTDLP_PYTHON=str(py),
                 HASLER_YTDLP_PROBE_URL="https://example.invalid/v", **{k: str(v) for k, v in env.items()})
        return subprocess.run(["bash", str(ROOT / "scripts/hasler-ytdlp-update"), *args],
                              capture_output=True, text=True, env=e)

    def current():
        link = root / "current"
        return (os.readlink(link), subprocess.run([str(link / "bin/yt-dlp"), "--version"],
                                                  capture_output=True, text=True).stdout.strip())
    return run, current, root


def test_ytdlp_first_install_and_upgrade(ytdlp):
    run, current, root = ytdlp
    r = run(FAKE_VERSION="1.0")
    assert r.returncode == 0, r.stderr
    assert current() == ("venv-a", "1.0")
    assert run(FAKE_VERSION="1.0").returncode == 0           # same version: no switch
    assert current() == ("venv-a", "1.0") and not (root / "venv-b").exists()
    assert run(FAKE_VERSION="2.0").returncode == 0
    assert current() == ("venv-b", "2.0")
    assert json.loads(run("--status").stdout) == {"current": "2.0", "previous": "1.0"}
    assert run("--rollback").returncode == 0
    assert current() == ("venv-a", "1.0")


@pytest.mark.parametrize("env,why", [
    ({"FAKE_PIP_FAIL": 1}, "pip install"),
    ({"FAKE_IMPORT_RC": 1}, "importing"),
    ({"FAKE_OFFLINE_RC": 1}, "offline extraction"),
])
def test_ytdlp_broken_upgrade_keeps_the_old_one(ytdlp, env, why):
    run, current, root = ytdlp
    assert run(FAKE_VERSION="1.0").returncode == 0
    r = run(FAKE_VERSION="2.0", **env)
    assert r.returncode == 1 and why in r.stderr
    assert current() == ("venv-a", "1.0")
    assert not (root / "venv-b").exists()


def test_ytdlp_online_check_is_soft(ytdlp):
    run, current, _ = ytdlp
    assert run(FAKE_VERSION="1.0").returncode == 0                  # old one resolves YouTube
    r = run(FAKE_VERSION="2.0", FAKE_NET_RC=1)                      # new one cannot: refused
    assert r.returncode == 1 and "keeping 1.0" in r.stderr
    assert current() == ("venv-a", "1.0")


def test_ytdlp_offline_box_goes_by_offline_checks(ytdlp):
    run, current, _ = ytdlp
    assert run(FAKE_VERSION="1.0", FAKE_NET_RC=1).returncode == 0   # old one: offline too
    r = run(FAKE_VERSION="2.0", FAKE_NET_RC=1)
    assert r.returncode == 0 and "offline?" in r.stderr
    assert current() == ("venv-b", "2.0")


# --- dev keys, dev channel, ota-bootstrap, Checker ------------------------------------------

def sh(*args, env=None, check=True):
    r = subprocess.run(["bash", *map(str, args)], capture_output=True, text=True,
                       env=dict(os.environ, **(env or {})))
    if check:
        assert r.returncode == 0, r.stdout + r.stderr
    return r


@pytest.fixture(scope="module")
def devkeys(tmp_path_factory):
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    d = tmp_path_factory.mktemp("dev") / "keys"
    sh(REL / "dev-keys.sh", d)
    return d


def test_dev_keys_refuses_overwrite_and_repo(devkeys, tmp_path):
    assert sh(REL / "dev-keys.sh", devkeys, check=False).returncode == 1
    assert sh(REL / "dev-keys.sh", ROOT / "devkeys-here", check=False).returncode == 1
    assert not (ROOT / "devkeys-here").exists()
    assert sorted(p.name for p in (devkeys / "trust").iterdir()) == ["keyring.json", "keyring.json.sig", "root.pub"]


def fake_box(tmp_path: Path) -> Path:
    root = tmp_path / "box"
    cfg = root / "boot/firmware/config.txt"
    cfg.parent.mkdir(parents=True)
    b, e = integ.LEGACY_BLOCKS[0]
    cfg.write_text("arm_64bit=1\n" + b + "\n" + (ROOT / "config/configtxt/hasler.block").read_text() + e + "\n")
    (root / "home/pi/.config/go-librespot").mkdir(parents=True)
    (root / "home/pi/.config/go-librespot/state.json").write_text('{"username":"x"}')
    (root / "home/pi/.config/go-librespot/config.yml").write_text("device_name: hasler\n")
    return root


def ota_bootstrap(root: Path, *args, check=True):
    return sh(ROOT / "scripts/ota-bootstrap.sh", *args, env={"HASLER_ROOT": str(root)}, check=check)


def test_ota_bootstrap_fake_root(devkeys, tmp_path):
    root = fake_box(tmp_path)
    dry = ota_bootstrap(root, "--dry-run", "--trust", devkeys / "trust")
    assert "would" in dry.stdout and not (root / "opt").exists()
    ota_bootstrap(root, "--trust", devkeys / "trust")
    assert (root / "opt/hasler/updater/hasler_update/cli.py").is_file()
    assert not list((root / "opt/hasler/updater").rglob("__pycache__"))
    assert (root / "usr/local/sbin/hasler-update").stat().st_mode & 0o777 == 0o755
    assert (root / "etc/systemd/system/hasler-update-check.service").is_file()
    assert (root / "etc/polkit-1/rules.d/50-hasler-update.rules").is_file()
    assert (root / "etc/systemd/system.conf.d/hasler-watchdog.conf").is_file()
    for n in ("root.pub", "keyring.json", "keyring.json.sig"):
        assert (root / "etc/hasler/trust" / n).stat().st_mode & 0o777 == 0o444
    assert (root / "etc/hasler").stat().st_mode & 0o7777 == 0o1775
    assert (root / "etc/hasler/trust").stat().st_mode & 0o777 == 0o755
    assert (root / "var/lib/go-librespot/state.json").read_text() == '{"username":"x"}'
    cfg = (root / "boot/firmware/config.txt").read_text()
    assert integ.BLOCK_BEGIN in cfg and integ.LEGACY_BLOCKS[0][0] not in cfg
    assert integ.config_block(cfg).strip("\n") == (ROOT / "config/configtxt/hasler.block").read_text().strip("\n")
    # box.yaml comes from migration 001; --update-url only edits it
    r = ota_bootstrap(root, "--update-url", "http://laptop:8000")
    assert "migration 001 first" in r.stdout + r.stderr
    (root / "etc/hasler/box.yaml").write_text("schema: 1\nchannel: bench\nupdate_url: https://old\n")
    ota_bootstrap(root, "--update-url", "http://laptop:8000")
    box = (root / "etc/hasler/box.yaml").read_text()
    assert box.count("update_url:") == 1 and "update_url: http://laptop:8000" in box
    ota_bootstrap(root, "--update-url", "")
    assert "update_url" not in (root / "etc/hasler/box.yaml").read_text()
    # idempotent, and root keys are install-once
    ota_bootstrap(root, "--trust", devkeys / "trust")
    other = tmp_path / "other"
    sh(REL / "dev-keys.sh", other)
    r = ota_bootstrap(root, "--trust", other / "trust", check=False)
    assert r.returncode == 1 and "install-once" in r.stderr
    ota_bootstrap(root, "--trust", other / "trust", "--replace-trust")
    assert (root / "etc/hasler/trust/root.pub").read_bytes() == (other / "trust/root.pub").read_bytes()


def test_ota_bootstrap_refuses_unsigned_keyring(devkeys, tmp_path):
    bad = tmp_path / "bad"
    shutil.copytree(devkeys / "trust", bad)
    os.chmod(bad / "keyring.json", 0o644)
    (bad / "keyring.json").write_text((bad / "keyring.json").read_text().replace('"keyring_seq": 1', '"keyring_seq": 9'))
    r = ota_bootstrap(fake_box(tmp_path), "--trust", bad, check=False)
    assert r.returncode == 1 and "does not verify" in r.stderr


@needs("git", "zstd", "ssh-keygen")
def test_dev_channel_end_to_end(built, devkeys, tmp_path):
    out, _ = built
    site = tmp_path / "site"
    r = subprocess.run([sys.executable, str(REL / "dev-channel.py"), "publish", "--keys", str(devkeys),
                        "--dist", str(out), "--site", str(site), "--base-url", site.as_uri()],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    rel = json.loads((site / "releases/v0.2.0/release.json").read_text())
    assert rel["artifacts"][0]["url"] == f"{site.as_uri()}/releases/v0.2.0/hasler-0.2.0-arm64.tar.zst"

    root = fake_box(tmp_path)
    ota_bootstrap(root, "--trust", devkeys / "trust")
    (root / "etc/hasler/box.yaml").write_text("schema: 1\nvariant: hasler-hm\nchannel: bench\n"
                                              "update_mode: ask\nbox_id: bench-1\n")
    ota_bootstrap(root, "--update-url", site.as_uri())
    layout = Layout(root)
    facts = dict(FACTS, moode="10.3.0")
    status = Checker(layout, facts=facts).stage()
    assert status["result"] == "ok", status
    assert status["available"]["version"] == "0.2.0" and status["available"]["ask"] is True
    staged = layout.staged / str(rel["seq"])
    assert (staged / "STAGED").is_file()
    assert (staged / "tree/integration/systemd/haslerd.service").is_file()

    # halt drill: the box drops the staged release and says so
    r = subprocess.run([sys.executable, str(REL / "dev-channel.py"), "halt", "--keys", str(devkeys),
                        "--site", str(site), "--seq", str(rel["seq"])], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    status = Checker(layout, facts=facts).check()
    assert status["result"] == "ok" and "halted" in status["message"], status
    assert not staged.exists()
