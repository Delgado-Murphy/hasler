"""Analog backend: ADS1115 16-bit ADC.

The Pi has no analog input, so the pots need this. Four single-ended channels at
+/-4.096 V full scale, which puts a 3.3 V rail at ~26400 counts - the default
range in the variant descriptors.

Conversions round-robin one channel per read() and never block: a read collects
whatever conversion was started last time and kicks off the next. At a 50 Hz
poll with two pots each wheel updates at 25 Hz - far quicker than a hand can
turn a knob, and the poll loop never sleeps on I2C.
"""

from __future__ import annotations

import time

CONVERSION, CONFIG = 0x00, 0x01
# One conversion at 128 SPS is 7.8 ms, +10% for the internal oscillator's
# tolerance, plus wake-up: by 10 ms it is certainly done, so a poll that
# comes later (every poll, at 50 Hz) collects it without first reading
# CONFIG to ask. One I2C transfer fewer per poll; the ready bit is still
# asked when a read comes early.
CONVERSION_S = 0.010

_OS_SINGLE = 0x8000        # start a single conversion / 1 = idle when read back
_PGA_4_096V = 0x0200       # +/-4.096 V - headroom over a 3.3 V rail
_MODE_SINGLE = 0x0100
_DR_128SPS = 0x0080
_COMP_DISABLE = 0x0003
_MUX_SINGLE = {0: 0x4000, 1: 0x5000, 2: 0x6000, 3: 0x7000}


class Ads1115Analog:
    def __init__(
        self,
        channels: list[int],
        addr: int = 0x48,
        bus: int = 1,
        bus_factory=None,
        clock=time.monotonic,
    ) -> None:
        if not channels:
            raise ValueError("ADS1115 backend built with no channels")
        if bus_factory is None:
            from smbus2 import SMBus as bus_factory

        self.addr = addr
        self.channels = list(channels)
        self._bus = bus_factory(bus)
        self._clock = clock
        self._values = dict.fromkeys(self.channels, 0)
        self._pending: int | None = None
        self._started_at = 0.0
        self._start(self.channels[0])

    def _start(self, channel: int) -> None:
        config = (
            _OS_SINGLE
            | _MUX_SINGLE[channel]
            | _PGA_4_096V
            | _MODE_SINGLE
            | _DR_128SPS
            | _COMP_DISABLE
        )
        self._bus.write_i2c_block_data(self.addr, CONFIG, [(config >> 8) & 0xFF, config & 0xFF])
        self._pending = channel
        self._started_at = self._clock()

    def _ready(self) -> bool:
        hi, lo = self._bus.read_i2c_block_data(self.addr, CONFIG, 2)
        return bool(((hi << 8) | lo) & _OS_SINGLE)

    def read(self) -> dict[int, int]:
        if self._pending is not None and (
            self._clock() - self._started_at >= CONVERSION_S or self._ready()
        ):
            hi, lo = self._bus.read_i2c_block_data(self.addr, CONVERSION, 2)
            raw = (hi << 8) | lo
            if raw > 0x7FFF:
                raw -= 0x10000
            self._values[self._pending] = max(0, raw)
            nxt = self.channels[(self.channels.index(self._pending) + 1) % len(self.channels)]
            self._start(nxt)
        return dict(self._values)

    def close(self) -> None:
        self._bus.close()
