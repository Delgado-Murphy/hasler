"""scripts/hasler-own-state: the radio's files back to haslerd's user (haslerd.service ExecStartPre=+)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hasler-own-state"


def test_the_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_it_touches_only_the_radios_own_files():
    text = SCRIPT.read_text()
    fixed = re.findall(r"^\[\[ -[ed] (\S+) \]\] && fix (\S+)", text, re.M)
    assert [p for p, _ in fixed] == ["/etc/hasler/box.yaml", "/etc/hasler/variant.d", "/var/lib/haslerd"]
    assert "/etc/hasler/trust" not in [p for p, _ in fixed]
    assert "! -type l" in text and "-xdev" in text          # links never followed, one filesystem


def test_haslerd_runs_it_as_root_before_starting_and_the_release_ships_it():
    unit = (ROOT / "config" / "haslerd.service").read_text()
    assert "ExecStartPre=-+/usr/local/sbin/hasler-own-state\n" in unit and "%u" not in unit.split("hasler-own-state")[1].splitlines()[0]
    rules = (ROOT / "scripts" / "release" / "build_release.py").read_text()
    assert 'Rule("scripts/hasler-own-state", "integration/sbin/"' in rules


def test_an_unknown_user_is_a_no_op():
    run = subprocess.run(["bash", str(SCRIPT), "no-such-user-xyz"], capture_output=True)
    assert run.returncode == 0


def test_root_is_never_the_target():
    for arg in ("root", ""):
        run = subprocess.run(["bash", str(SCRIPT), arg] if arg else ["bash", "-c", f"systemctl() {{ echo root; }}; export -f systemctl; bash {SCRIPT}"],
                             capture_output=True)
        assert run.returncode == 0
    assert '"$user" != root' in SCRIPT.read_text()
