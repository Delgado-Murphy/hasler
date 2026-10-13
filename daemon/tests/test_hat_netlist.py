"""The HAT variant's pins against the HAT's own netlist (hardware/hat/gen/design.py)."""

import importlib.util

from conftest import ROOT, VARIANTS

from haslerd.variant import load_variant

_spec = importlib.util.spec_from_file_location("hat_design", ROOT / "hardware/hat/gen/design.py")
design = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(design)


def _d(mcp_name):  # GPA0..7 -> d0..d7, GPB0..7 -> d8..d15
    return "d%d" % ((8 if mcp_name.startswith("GPB") else 0) + int(mcp_name[-1]))


def test_hm_hat_pins_match_the_netlist():
    sig_to_pin = {sig: _d(name) for _, sig, _, name in design.DIG}
    want = {"k1": "K1", "k2": "K2", "k3": "K3", "k4": "K4", "k5": "K5", "k6": "K6", "pu": "PU"}
    controls = {c["id"]: c for c in load_variant(VARIANTS / "hasler-hm-hat.yaml").controls}
    for cid, sig in want.items():
        assert controls[cid]["pin"] == sig_to_pin[sig], cid
    assert sig_to_pin["TSW"] == "d0"


def test_netlist_has_no_dangling_nets():
    assert [n for n, pins in design.nets().items() if len(pins) < 2] == []
