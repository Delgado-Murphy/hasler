"""MCP23017 pin rules - checked without an I2C bus."""

from pathlib import Path

import pytest

from haslerd.panel.io_mcp23017 import (
    GPIOA,
    GPIOB,
    IPOLA,
    IPOLB,
    Mcp23017Digital,
    check_pins,
    find_addr,
)
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


class RegBus(Bus):
    """One expander at 0x20 with real registers, so a reset can be simulated."""

    def __init__(self):
        super().__init__({0x20})
        self.regs = {}
        self.fail = False

    def read_byte_data(self, addr, reg):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        return self.regs.get(reg, 0x00)

    def write_byte_data(self, addr, reg, value):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        super().write_byte_data(addr, reg, value)
        self.regs[reg] = value

    def power_on_reset(self):
        self.regs = {0x00: 0xFF, 0x01: 0xFF}   # IODIR all inputs, IPOL/GPPU 0


class TestResetRecovery:
    def make(self):
        bus = RegBus()
        mcp = Mcp23017Digital(["d0", "d8"], bus_factory=lambda n: bus)
        return mcp, bus

    def test_reset_chip_is_configured_again(self, caplog):
        mcp, bus = self.make()
        bus.power_on_reset()
        bus.writes.clear()
        mcp.read()
        assert bus.regs[IPOLA] == bus.regs[IPOLB] == 0xFF
        assert len(bus.writes) == 6
        assert "chip reset" in caplog.text

    def test_sample_after_a_reset_is_not_every_key_pressed(self):
        mcp, bus = self.make()
        bus.regs[GPIOA] = 0x01          # d0 closed (after IPOL)
        assert mcp.read() == {"d0": True, "d8": False}
        bus.power_on_reset()
        bus.regs[GPIOA] = bus.regs[GPIOB] = 0xFE   # IPOL 0: open contacts read 1
        assert mcp.read() == {"d0": True, "d8": False}   # last good sample, not all pressed

    def test_healthy_chip_gets_no_writes(self):
        mcp, bus = self.make()
        bus.writes.clear()
        for _ in range(50):
            mcp.read()
        assert bus.writes == []

    def test_i2c_error_during_check_does_not_raise(self, caplog):
        mcp, bus = self.make()
        bus.power_on_reset()
        bus.fail = True
        assert mcp.read() == {"d0": False, "d8": False}   # bus dead: logged, not raised
        assert "retrying" in caplog.text
        bus.fail = False
        mcp.read()                      # recovers: configured again
        bus.regs[GPIOA] = 0x01
        assert mcp.read() == {"d0": True, "d8": False}
        assert bus.regs[IPOLB] == 0xFF

    def test_error_only_in_reinit_writes_does_not_raise(self):
        mcp, bus = self.make()
        bus.power_on_reset()

        def refuse(addr, reg, value):
            raise OSError(121, "Remote I/O error")

        bus.write_byte_data = refuse
        mcp.read()                      # check runs, re-init fails, read still answers
