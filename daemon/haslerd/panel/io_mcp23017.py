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
"""

from __future__ import annotations

IODIRA, IODIRB = 0x00, 0x01
IPOLA, IPOLB = 0x02, 0x03
GPPUA, GPPUB = 0x0C, 0x0D
GPIOA, GPIOB = 0x12, 0x13

# Logical pin index -> port bit. d0-d7 = GPA0-7, d8-d15 = GPB0-7.
OUTPUT_ONLY = {7: "GPA7", 15: "GPB7"}


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
        addr: int = 0x20,
        bus: int = 1,
        int_gpio: int | None = None,
    ) -> None:
        check_pins(names)
        from smbus2 import SMBus

        self.addr = addr
        self.names = list(names)
        self.int_gpio = int_gpio
        self._bus = SMBus(bus)
        for direction, polarity, pullup in ((IODIRA, IPOLA, GPPUA), (IODIRB, IPOLB, GPPUB)):
            self._bus.write_byte_data(addr, direction, 0xFF)  # all inputs
            self._bus.write_byte_data(addr, pullup, 0xFF)     # pull-ups on
            self._bus.write_byte_data(addr, polarity, 0xFF)   # closed contact reads 1

    def read(self) -> dict[str, bool]:
        a = self._bus.read_byte_data(self.addr, GPIOA)
        b = self._bus.read_byte_data(self.addr, GPIOB)
        word = a | (b << 8)
        return {name: bool(word & (1 << int(name[1:]))) for name in self.names}

    def close(self) -> None:
        self._bus.close()
