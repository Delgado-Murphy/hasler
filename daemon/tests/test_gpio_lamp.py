"""The GPIO pilot lamp - gpiozero replaced by a recorder, no pins touched."""

import pytest

from haslerd import main
from haslerd.hw.gpio_lamp import GpioRgbController
from haslerd.hw.powerctl import Lamp


class FakeRGBLED:
    def __init__(self, red, green, blue, active_high, pwm):
        self.pins = (red, green, blue)
        self.active_high = active_high
        self.pwm = pwm
        self.value = (0, 0, 0)
        self.closed = False

    def off(self):
        self.value = (0, 0, 0)

    def close(self):
        self.closed = True


def make(**kwargs):
    made = []

    def factory(**kw):
        made.append(FakeRGBLED(**kw))
        return made[-1]

    return GpioRgbController(factory=factory, **kwargs), made


def test_default_pins_and_common_cathode_drive_high():
    ctl, (led,) = make()
    assert led.pins == (27, 22, 23)
    assert led.active_high is True
    assert led.pwm is True


def test_common_anode_inverts():
    _, (led,) = make(common="anode")
    assert led.active_high is False


def test_a_typo_in_common_is_refused():
    with pytest.raises(ValueError):
        make(common="annode")


def test_rgb_is_scaled_to_0_1_and_balanced():
    ctl, (led,) = make(balance={"green": 0.5})
    ctl.set_rgb(255, 150, 51)
    assert led.value == pytest.approx((1.0, 150 / 255 * 0.5, 0.2))


def test_values_are_clamped():
    ctl, (led,) = make()
    ctl.set_rgb(300, -5, 0)
    assert led.value == (1.0, 0.0, 0.0)


def test_close_turns_it_off():
    ctl, (led,) = make()
    ctl.set_rgb(255, 255, 255)
    ctl.close()
    assert led.value == (0, 0, 0) and led.closed
    ctl.set_rgb(255, 0, 0)  # after close: a no-op, not a crash


def test_missing_gpio_degrades_to_no_lamp():
    def broken(**kw):
        raise RuntimeError("no gpiochip")

    ctl = GpioRgbController(factory=broken)
    assert not ctl.available
    ctl.set_rgb(1, 2, 3)


def test_lamp_drives_the_gpio_controller():
    ctl, (led,) = make()
    lamp = Lamp(ctl)
    lamp.set_state("spotify")
    lamp.tick(0.0)
    assert led.value[1] > 0.5 and led.value[0] == 0.0


class TestOpenLamp:
    def test_gpio_backend(self, monkeypatch):
        seen = {}

        class Ctl:
            available = True

            def __init__(self, pins, common, balance):
                seen.update(pins=pins, common=common)

        monkeypatch.setattr(main, "GpioRgbController", Ctl)
        lamp = main.open_lamp({"backend": "gpio", "pins": {"red": 5}, "common": "anode"})
        assert isinstance(lamp, Lamp)
        assert seen == {"pins": {"red": 5}, "common": "anode"}

    def test_none_and_unknown_give_no_lamp(self):
        assert main.open_lamp({"backend": "none"}) is None
        assert main.open_lamp({"backend": "neon"}) is None

    def test_default_is_the_power_controller(self, monkeypatch):
        class Pc:
            available = False

        monkeypatch.setattr(main, "PowerController", Pc)
        assert main.open_lamp({}) is None   # absent controller -> no lamp


def test_hasler_descriptor_uses_the_gpio_lamp():
    from conftest import VARIANTS
    from haslerd.variant import load_variant

    lamp = load_variant(VARIANTS / "hasler-hm.yaml").io["lamp"]
    assert lamp["backend"] == "gpio"
    assert lamp["pins"] == {"red": 17, "green": 27, "blue": 22}   # odd-row pins 11, 13, 15
