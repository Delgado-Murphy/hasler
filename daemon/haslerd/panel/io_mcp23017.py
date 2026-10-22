"""Digital backend: MCP23017 I2C expander.

Collapses the panel loom to four wires (SDA/SCL/3V3/GND), so every cabinet gets
one identical connector regardless of how many keys it has. Logical pins map
d0-d7 to port A and d8-d15 to port B.

Inputs with pull-ups enabled and IPOL inversion set, so a closed contact reads
as 1 in hardware and nothing has to be inverted in software.

**GPA7 and GPB7 are not inputs.** Microchip's current datasheet (rev. D on)
documents both as output-only, and newer silicon does not reliably read them.
So ``d7`` and ``d15`` are refused at startup rather than becoming a switch that
works on one chip and silently never fires on the next. Wire around them: 14
usable inputs is still more than either cabinet needs.

**Address.** Boards differ only in how their A0-A2 pads are strapped: the
CQRobot board defaults to 0x27, the common green breakout to 0x20. ``addr:
auto`` probes the chip's whole range (0x20-0x27) at start and takes the one
board that answers, so swapping boards needs no config edit. Two answering is
an error - better a clear refusal than reading the wrong one.

**Reset recovery.** A supply dip or ESD on the cabinet ribbon can reset the
chip, and its registers go back to power-on defaults: IPOL = 0, so every open
contact reads as pressed (and the pull-ups are off). Every ``read`` reads
IPOLA/IPOLB back before the ports; if they are not what was written it re-runs
the setup and returns the last good sample instead, so a reset never shows up
as every key pressed - not even for one poll, which would be enough to fire
taps or arm the end switch. An I2C error in the check or the setup is logged
and retried on the next read, never raised into the poll loop.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

IODIRA, IODIRB = 0x00, 0x01
IPOLA, IPOLB = 0x02, 0x03
GPPUA, GPPUB = 0x0C, 0x0D
GPIOA, GPIOB = 0x12, 0x13

# Logical pin index -> port bit. d0-d7 = GPA0-7, d8-d15 = GPB0-7.
OUTPUT_ONLY = {7: "GPA7", 15: "GPB7"}
ADDR_RANGE = range(0x20, 0x28)     # A0-A2 select one of these eight


def find_addr(bus) -> int:
    """The one MCP23017 address that answers on ``bus``, for ``addr: auto``."""
    found = []
    for addr in ADDR_RANGE:
        try:
            bus.read_byte_data(addr, IODIRA)
        except OSError:
            continue
        found.append(addr)
    if not found:
        raise RuntimeError(
            "mcp23017: no expander answers at 0x20-0x27 - check VCC (3.3 V), "
            "GND, SDA/SCL, and that RST is held at VCC (i2cdetect -y 1)"
        )
    if len(found) > 1:
        raise RuntimeError(
            "mcp23017: several expanders answer (" + ", ".join(f"0x{a:02x}" for a in found)
            + ") - set addr: to the one wired to the keys"
        )
    return found[0]


def check_pins(names: list[str]) -> None:
    """Refuse names the chip cannot read. Pure, so it is tested without a bus."""
    for name in names:
        if not (name.startswith("d") and name[1:].isdigit()):
            raise ValueError(f"mcp23017: pin {name!r} is not a dN name")
        index = int(name[1:])
        if index > 15:
            raise ValueError(f"mcp23017: {name} is past GPB7 - the chip has d0-d15")
        if index in OUTPUT_ONLY:
            raise ValueError(
                f"mcp23017: {name} is {OUTPUT_ONLY[index]}, which is output-only on "
                f"current MCP23017 silicon - move that contact to another pin"
            )


class Mcp23017Digital:
    def __init__(
        self,
        names: list[str],
        addr: int | str = 0x20,
        bus: int = 1,
        int_gpio: int | None = None,
        bus_factory=None,
    ) -> None:
        check_pins(names)
        if bus_factory is None:
            from smbus2 import SMBus as bus_factory

        self._bus = bus_factory(bus)
        if addr == "auto":
            addr = find_addr(self._bus)
            log.info("mcp23017: keys found at 0x%02x", addr)
        elif not isinstance(addr, int):
            raise ValueError(f"mcp23017: addr must be a number or 'auto', not {addr!r}")
        self.addr = addr
        self.names = list(names)
        self.int_gpio = int_gpio
        self._configure()
        self._last = {name: False for name in self.names}

    def _configure(self) -> None:
        for direction, polarity, pullup in ((IODIRA, IPOLA, GPPUA), (IODIRB, IPOLB, GPPUB)):
            self._bus.write_byte_data(self.addr, direction, 0xFF)  # all inputs
            self._bus.write_byte_data(self.addr, pullup, 0xFF)     # pull-ups on
            self._bus.write_byte_data(self.addr, polarity, 0xFF)   # closed contact reads 1

    def _reset_seen(self) -> bool:
        """True if the chip has reset since it was set up (and set it up again)."""
        try:
            ipol = tuple(self._bus.read_i2c_block_data(self.addr, IPOLA, 2))
            if ipol == (0xFF, 0xFF):
                return False
            log.warning(
                "mcp23017: IPOL reads 0x%02x/0x%02x, not 0xff/0xff - chip reset? "
                "configuring it again", *ipol
            )
            self._configure()
        except OSError as exc:
            log.warning("mcp23017: reset check failed, retrying on the next read: %s", exc)
        return True

    def read(self) -> dict[str, bool]:
        if self._reset_seen():
            return dict(self._last)
        # GPIOA and GPIOB in one transaction: the register pointer steps from
        # 0x12 to 0x13 by itself (IOCON.BANK=0, SEQOP=0 - the power-on
        # defaults, which nothing here changes). With the IPOL check, two I2C
        # transfers per poll at 50 Hz.
        a, b = self._bus.read_i2c_block_data(self.addr, GPIOA, 2)
        word = a | (b << 8)
        self._last = {name: bool(word & (1 << int(name[1:]))) for name in self.names}
        return dict(self._last)

    def close(self) -> None:
        self._bus.close()
