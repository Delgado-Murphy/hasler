"""MCP23017 pin rules - checked without an I2C bus."""

from pathlib import Path

import pytest

from haslerd.panel.io_mcp23017 import check_pins
from haslerd.variant import load_variant

VARIANTS = Path(__file__).resolve().parents[2] / "config" / "variants"


class TestCheckPins:
    def test_usable_pins_pass(self):
        check_pins([f"d{i}" for i in range(16) if i not in (7, 15)])

    @pytest.mark.parametrize("name, port", [("d7", "GPA7"), ("d15", "GPB7")])
    def test_output_only_pins_are_refused_by_name(self, name, port):
        with pytest.raises(ValueError, match=port):
            check_pins(["d0", name])

    @pytest.mark.parametrize("name", ["d16", "x3", "d", "7"])
    def test_names_the_chip_cannot_have_are_refused(self, name):
        with pytest.raises(ValueError):
            check_pins([name])


@pytest.mark.parametrize("variant", ["hasler-hm", "excelsior"])
def test_shipped_descriptors_avoid_the_output_only_pins(variant):
    # Both cabinets are meant to run on the expander; a descriptor that drifts
    # back onto d7/d15 would boot fine on a laptop and die on the box.
    check_pins(load_variant(VARIANTS / f"{variant}.yaml").pin_names())


def test_hasler_runs_on_the_expander():
    io = load_variant(VARIANTS / "hasler-hm.yaml").io["digital"]
    assert io["backend"] == "mcp23017"
    assert io["addr"] == 0x27   # CQRobot default, A0-A2 open
