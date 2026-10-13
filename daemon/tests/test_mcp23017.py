"""MCP23017 pin rules - checked without an I2C bus."""

from pathlib import Path

import pytest

from haslerd.panel.io_mcp23017 import Mcp23017Digital, check_pins, find_addr
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


@pytest.mark.parametrize("variant", ["hasler-hm", "hasler-hm-hat", "excelsior"])
def test_shipped_descriptors_avoid_the_output_only_pins(variant):
    # Both cabinets are meant to run on the expander; a descriptor that drifts
    # back onto d7/d15 would boot fine on a laptop and die on the box.
    check_pins(load_variant(VARIANTS / f"{variant}.yaml").pin_names())


def test_hasler_runs_on_the_expander():
    io = load_variant(VARIANTS / "hasler-hm.yaml").io["digital"]
    assert io["backend"] == "mcp23017"
    assert io["addr"] == "auto"   # CQRobot (0x27) or the green board (0x20)


class Bus:
    """An I2C bus with expanders at ``present``; records register writes."""

    def __init__(self, present):
        self.present = set(present)
        self.writes = []

    def read_byte_data(self, addr, reg):
        if addr not in self.present:
            raise OSError(121, "Remote I/O error")
        return 0xFF

    def write_byte_data(self, addr, reg, value):
        self.writes.append((addr, reg, value))

    def close(self):
        pass


class TestAutoAddress:
    @pytest.mark.parametrize("addr", [0x20, 0x27, 0x23])
    def test_finds_the_one_board(self, addr):
        assert find_addr(Bus({addr, 0x48, 0x4D})) == addr   # ADS1115 and amp chip ignored

    def test_no_board_says_what_to_check(self):
        with pytest.raises(RuntimeError, match="RST"):
            find_addr(Bus({0x4D}))

    def test_two_boards_is_refused(self):
        with pytest.raises(RuntimeError, match="0x20, 0x27"):
            find_addr(Bus({0x20, 0x27}))

    def test_backend_configures_the_found_board(self):
        bus = Bus({0x20})
        mcp = Mcp23017Digital(["d0", "d8"], addr="auto", bus_factory=lambda n: bus)
        assert mcp.addr == 0x20 and {w[0] for w in bus.writes} == {0x20}

    def test_fixed_address_still_works(self):
        bus = Bus({0x27})
        assert Mcp23017Digital(["d0"], addr=0x27, bus_factory=lambda n: bus).addr == 0x27

    def test_nonsense_address_is_refused(self):
        with pytest.raises(ValueError, match="auto"):
            Mcp23017Digital(["d0"], addr="0x20", bus_factory=lambda n: Bus({0x20}))
