"""ADS1115 round-robin, against a fake bus: no conversion is waited on."""

from haslerd.panel.io_ads1115 import CONFIG, CONVERSION, CONVERSION_S, Ads1115Analog


class AdsBus:
    def __init__(self):
        self.log = []
        self.ready = True
        self.counts = {0: 1000, 1: 2000}
        self.mux = None

    def write_i2c_block_data(self, addr, reg, data):
        self.log.append(("write", reg))
        self.mux = (((data[0] << 8) | data[1]) >> 12) & 0x3

    def read_i2c_block_data(self, addr, reg, n):
        self.log.append(("read", reg))
        if reg == CONFIG:
            return [0x80 if self.ready else 0x00, 0x00]
        value = self.counts[self.mux]
        return [value >> 8, value & 0xFF]

    def close(self):
        pass


def make():
    bus, t = AdsBus(), [0.0]
    ads = Ads1115Analog([0, 1], bus_factory=lambda n: bus, clock=lambda: t[0])
    bus.log.clear()
    return ads, bus, t


def test_a_poll_after_the_conversion_time_skips_the_ready_check():
    ads, bus, t = make()
    t[0] = 0.020                                   # the next 50 Hz poll
    assert ads.read() == {0: 1000, 1: 0}
    assert bus.log == [("read", CONVERSION), ("write", CONFIG)]
    t[0] = 0.040
    assert ads.read() == {0: 1000, 1: 2000}


def test_an_early_poll_asks_the_ready_bit_first():
    ads, bus, t = make()
    t[0] = CONVERSION_S / 2
    bus.ready = False
    assert ads.read() == {0: 0, 1: 0}
    assert bus.log == [("read", CONFIG)]
    bus.ready = True
    bus.log.clear()
    assert ads.read() == {0: 1000, 1: 0}
    assert bus.log == [("read", CONFIG), ("read", CONVERSION), ("write", CONFIG)]
