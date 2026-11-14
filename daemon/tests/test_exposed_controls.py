"""/api/health ``exposed_controls``: MPD or CamillaDSP listening beyond the radio
(no password, past the volume ceiling; scripts/hasler-local-only fixes it).
Informational only: never in ``checks`` or ``ok``, so no release rolls back over it."""

from __future__ import annotations

from types import SimpleNamespace

from haslerd import health
from haslerd.health import health_report

HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
ROW = "   {n}: {addr}:{port} 00000000:0000 {st} 00000000:00000000 00:00000000 00000000     0        0 7062 1 0\n"
ANY6 = "0" * 32
LOOP6 = "0" * 24 + "01000000"


def proc(tmp_path, tcp: list[tuple[str, str, str]], tcp6: list[tuple[str, str, str]] = ()):
    for name, rows in (("tcp", tcp), ("tcp6", tcp6)):
        (tmp_path / name).write_text(HEAD + "".join(ROW.format(n=i, addr=a, port=p, st=s)
                                                    for i, (a, p, s) in enumerate(rows)))
    return tmp_path


def test_moode_as_shipped_is_reported(tmp_path, monkeypatch):
    # the bench radio on 2026-11-14: MPD and CamillaDSP on 0.0.0.0, haslerd's page on [::]:8080
    monkeypatch.setattr(health, "PROC_NET", proc(tmp_path, [("00000000", "19C8", "0A"), ("00000000", "04D2", "0A"),
                                                             ("0100007F", "19C8", "01")],
                                                 [(ANY6, "1F90", "0A")]))
    doc = health_report(SimpleNamespace())
    assert doc["exposed_controls"] == ["mpd 0.0.0.0:6600", "camilladsp 0.0.0.0:1234"]
    assert "exposed_controls" not in doc["checks"]


def test_local_only_is_an_empty_list(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "PROC_NET", proc(tmp_path, [("0100007F", "19C8", "0A"), ("0100007F", "04D2", "0A")],
                                                 [(LOOP6, "19C8", "0A")]))
    assert health_report(SimpleNamespace())["exposed_controls"] == []


def test_an_ipv6_wildcard_counts_and_ok_never_depends_on_it(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "PROC_NET", proc(tmp_path, [], [(ANY6, "19C8", "0A")]))
    exposed = health_report(SimpleNamespace())
    monkeypatch.setattr(health, "PROC_NET", proc(tmp_path, []))
    local = health_report(SimpleNamespace())
    assert exposed["exposed_controls"] == ["mpd [::]:6600"] and local["exposed_controls"] == []
    assert exposed["ok"] == local["ok"] and exposed["checks"] == local["checks"]


def test_no_proc_no_answer_no_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "PROC_NET", tmp_path / "missing")
    assert health_report(SimpleNamespace())["exposed_controls"] == []
