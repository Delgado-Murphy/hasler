"""Pilot lamp behaviour - pure logic, no I2C bus involved."""

import pytest
from conftest import VARIANTS  # noqa: F401

from haslerd.hw.powerctl import LAMP_STATES, Lamp, brightness


class FakeController:
    def __init__(self):
        self.writes = []

    def set_rgb(self, r, g, b):
        self.writes.append((r, g, b))


class TestBrightnessEnvelope:
    def test_solid_states_never_dim(self):
        assert all(brightness("radio", p / 10) == 1.0 for p in range(10))

    def test_pulse_breathes_smoothly_rather_than_strobing(self):
        levels = [brightness("pairing", p / 20) for p in range(21)]
        assert min(levels) == pytest.approx(0.0, abs=0.11)
        assert max(levels) == pytest.approx(1.0, abs=0.01)
        # No jump bigger than a tenth between adjacent samples.
        assert max(abs(b - a) for a, b in zip(levels, levels[1:])) < 0.15

    def test_flash_is_hard_on_off(self):
        assert set(brightness("offline", p / 10) for p in range(10)) == {0.0, 1.0}

    def test_unknown_state_falls_back_to_solid(self):
        assert brightness("nonsense", 0.5) == 1.0


class TestLamp:
    def test_every_named_state_has_a_colour(self):
        for state, spec in LAMP_STATES.items():
            assert len(spec["rgb"]) == 3, state
            assert all(0 <= c <= 255 for c in spec["rgb"]), state

    def test_solid_state_writes_once_not_every_tick(self):
        controller = FakeController()
        lamp = Lamp(controller)
        lamp.set_state("radio")
        for i in range(50):
            lamp.tick(i * 0.05)
        assert len(controller.writes) == 1, "a solid lamp should not spam the I2C bus"

    def test_changing_state_repaints(self):
        controller = FakeController()
        lamp = Lamp(controller)
        lamp.set_state("radio")
        lamp.tick(0.0)
        lamp.set_state("bluetooth")
        lamp.tick(0.05)
        assert controller.writes[-1] == LAMP_STATES["bluetooth"]["rgb"]

    def test_pulsing_state_does_animate(self):
        controller = FakeController()
        lamp = Lamp(controller)
        lamp.set_state("pairing")
        for i in range(40):
            lamp.tick(i * 0.05)
        assert len(controller.writes) > 10

    def test_standby_is_dim_but_not_off(self):
        # The lamp still says "there is a radio here", quietly.
        rgb = LAMP_STATES["standby"]["rgb"]
        assert 0 < sum(rgb) < 150


class TestSourceLamps:
    def test_ytm_has_its_own_solid_colour(self):
        assert LAMP_STATES["ytm"]["rgb"] == (255, 210, 160)
        assert brightness("ytm", 0.7) == 1.0
        others = {s["rgb"] for k, s in LAMP_STATES.items() if k != "ytm"}
        assert LAMP_STATES["ytm"]["rgb"] not in others

    def test_spotify_is_still_there(self):
        assert "spotify" in LAMP_STATES


class TestErrorFlash:
    def test_error_flashes_faster_than_offline(self):
        def toggles(state):
            levels = [brightness(state, p / 80) for p in range(80)]
            return sum(a != b for a, b in zip(levels, levels[1:]))

        assert toggles("error") > toggles("offline")

    def test_error_shows_for_about_three_seconds_then_steady_state_returns(self):
        controller = FakeController()
        lamp = Lamp(controller)
        lamp.set_state("radio")
        lamp.flash("error", now=10.0)
        assert lamp.showing(10.1) == "error"
        lamp.set_state("spotify")                 # main loop keeps setting state
        assert lamp.showing(12.9) == "error"
        assert lamp.showing(13.1) == "spotify"

    def test_error_flash_actually_reaches_the_bus(self):
        controller = FakeController()
        lamp = Lamp(controller)
        lamp.set_state("radio")
        lamp.flash("error", now=0.0)
        for i in range(20):
            lamp.tick(i * 0.05)
        assert LAMP_STATES["error"]["rgb"] in controller.writes
        assert (0, 0, 0) in controller.writes


class TestLampPriority:
    def test_source_maps_to_its_lamp(self):
        from haslerd.main import lamp_state

        assert lamp_state(False, True, "ytm") == "ytm"
        assert lamp_state(False, True, "spotify") == "spotify"
        assert lamp_state(False, True, "mystery") == "radio"

    def test_offline_overrides_internet_sources(self):
        from haslerd.main import lamp_state

        assert lamp_state(False, False, "radio") == "offline"
        assert lamp_state(False, False, "ytm") == "offline"

    def test_bluetooth_needs_no_internet(self):
        from haslerd.main import lamp_state

        assert lamp_state(False, False, "bluetooth") == "bluetooth"

    def test_standby_beats_offline(self):
        from haslerd.main import lamp_state

        assert lamp_state(True, False, "radio") == "standby"
