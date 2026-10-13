"""A missing panel board must not crash haslerd - nor shut the Pi down."""

from pathlib import Path

from haslerd import variant as variant_mod
from haslerd.panel.io_base import AbsentDigital, NullAnalog
from haslerd.variant import Panel, open_backends


def _no_board(*args, **kwargs):
    raise RuntimeError("mcp23017: no expander answers at 0x20-0x27")


def test_missing_mcp_gives_an_absent_panel(monkeypatch):
    import haslerd.panel.io_mcp23017 as mcp

    monkeypatch.setattr(mcp, "Mcp23017Digital", _no_board)
    v = variant_mod.load_variant(Path(__file__).parents[2] / "config/variants/hasler-hm.yaml")
    digital, analog = open_backends(v)
    assert isinstance(digital, AbsentDigital)
    assert isinstance(analog, NullAnalog)


def test_absent_panel_emits_no_events_even_with_an_end_switch():
    from haslerd.panel.controls import EndSwitch

    panel = Panel([EndSwitch("power", "d8", off_hold_s=5)], AbsentDigital(), NullAnalog())
    events = panel.poll(0.0) + panel.poll(10.0) + panel.poll(60.0)
    assert events == []
    assert panel.absent
