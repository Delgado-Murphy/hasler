"""sync_tree: the state snapshot put back on a rollback, while haslerd is still running."""

from __future__ import annotations

from pathlib import Path

from hasler_update import apply as ap


def test_makes_dst_hold_src(tmp_path: Path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "a.json").write_text("A")
    (src / "sub" / "b.json").write_text("B")
    dst.mkdir()
    (dst / "a.json").write_text("old")
    (dst / "extra.json").write_text("x")
    ap.sync_tree(src, dst)
    assert (dst / "a.json").read_text() == "A"
    assert (dst / "sub" / "b.json").read_text() == "B"
    assert not (dst / "extra.json").exists()


def test_haslerds_half_written_files_are_left_alone(tmp_path: Path):
    # 2026-11-21: a rollback died on ENOENT for /var/lib/haslerd/.hasler-e_lls0aj.json,
    # haslerd's temp file renamed away mid-sync. Temps are never copied nor removed.
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / ".hasler-snapped.json").write_text("half")
    (dst / ".hasler-live.json").write_text("writing")
    ap.sync_tree(src, dst)
    assert not (dst / ".hasler-snapped.json").exists()
    assert (dst / ".hasler-live.json").read_text() == "writing"


def test_a_file_vanishing_under_the_sync_is_not_an_error(tmp_path: Path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (dst / "gone.json").write_text("x")
    real_walk = ap.os.walk

    def walk(top, *a, **kw):
        for dirpath, dirs, files in real_walk(top, *a, **kw):
            if Path(dirpath) == dst:
                (dst / "gone.json").unlink(missing_ok=True)   # haslerd removed it meanwhile
            yield dirpath, dirs, files

    monkeypatch.setattr(ap.os, "walk", walk)
    ap.sync_tree(src, dst)
    assert not (dst / "gone.json").exists()
