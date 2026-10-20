"""The release pre-flight: ok on a sane box, refuses a broken overlay."""

from pathlib import Path

from haslerd import boxconfig, selftest

REPO = Path(__file__).resolve().parents[2]


def _paths(tmp_path):
    etc, state = tmp_path / "etc", tmp_path / "state"
    etc.mkdir()
    state.mkdir()
    (etc / "box.yaml").write_text("schema: 1\nvariant: hasler-hm\n")
    return boxconfig.Paths(release=REPO, etc=etc, state=state)


def test_selftest_passes_on_a_plain_box(tmp_path):
    p = _paths(tmp_path)
    assert selftest.main(["--etc", str(p.etc), "--state", str(p.state)]) == 0


def test_selftest_refuses_an_overlay_for_a_missing_control(tmp_path):
    p = _paths(tmp_path)
    (p.etc / "variant.d").mkdir()
    (p.etc / "variant.d" / "hasler-hm.yaml").write_text("schema: 1\ncontrols:\n  nonexistent: {curve: [[0, 0]]}\n")
    assert selftest.main(["--etc", str(p.etc), "--state", str(p.state)]) == 1
