"""HiFiBerry Power Controller: the pilot lamp, the button, and real power-off.

The colour LED replaces the original dial lamp and plugs into the Power
Controller rather than the Pi - I2C 0x77, so it costs zero GPIOs and can show
state before the Pi has finished booting.

Its rotary-encoder interrupt line is deliberately **left unwired**: it defaults
to GPIO 4, which the Amp4 Pro uses for amp MUTE. We do not need the encoder
(the cabinet's wheels are pots), so the button is polled over I2C instead.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

ADDR = 0x77
REG_LED_MODE = 0x01
REG_LED_R, REG_LED_G, REG_LED_B = 0x02, 0x03, 0x04
REG_BUTTON = 0x06
REG_POWEROFF_TIMER = 0x09

# What the lamp means. Amber is the original dial-lamp glow; everything else
# reads at a glance from across the room.
LAMP_STATES: dict[str, dict] = {
    "radio":     {"rgb": (255, 150, 40), "effect": "solid"},
    "bluetooth": {"rgb": (0, 80, 255),   "effect": "solid"},
    "pairing":   {"rgb": (0, 80, 255),   "effect": "pulse"},
    "airplay":   {"rgb": (0, 200, 220),  "effect": "solid"},
    "spotify":   {"rgb": (0, 220, 80),   "effect": "solid"},
    # Warm white: distinct from radio's amber at a glance, and nothing like
    # YouTube red, which would read as a fault.
    "ytm":       {"rgb": (255, 210, 160), "effect": "solid"},
    "dlna":      {"rgb": (220, 0, 200),  "effect": "solid"},
    "standby":   {"rgb": (40, 0, 0),     "effect": "solid"},
    "offline":   {"rgb": (255, 0, 0),    "effect": "flash"},
    # A key press that failed. Shown briefly over whatever else is going on
    # (see Lamp.flash) - fast, so it is not mistaken for "offline".
    "error":     {"rgb": (255, 40, 0),   "effect": "fast_flash"},
}

ERROR_FLASH_S = 3.0


def brightness(state: str, phase: float) -> float:
    """Effect envelope, 0.0-1.0, for a given point in the animation cycle.

    Pure maths so the lamp's behaviour is testable without an I2C bus.
    ``phase`` runs 0.0 to 1.0 across one cycle.
    """
    effect = LAMP_STATES.get(state, {}).get("effect", "solid")
    if effect == "solid":
        return 1.0
    if effect == "pulse":
        # Smooth triangle - a slow breathe, not a strobe.
        return 1.0 - abs(2.0 * (phase % 1.0) - 1.0)
    if effect == "flash":
        return 1.0 if (phase % 1.0) < 0.5 else 0.0
    if effect == "fast_flash":
        return 1.0 if (phase * 4.0) % 1.0 < 0.5 else 0.0
    return 1.0


class PowerController:
    """Thin I2C wrapper. Every call degrades to a log line if the bus is absent."""

    def __init__(self, addr: int = ADDR, bus: int = 1) -> None:
        self.addr = addr
        self._bus = None
        try:
            from smbus2 import SMBus

            self._bus = SMBus(bus)
        except Exception:
            log.warning("power controller not available on I2C; lamp and power-off disabled")

    @property
    def available(self) -> bool:
        return self._bus is not None

    def set_rgb(self, r: int, g: int, b: int) -> None:
        if self._bus is None:
            return
        try:
            self._bus.write_byte_data(self.addr, REG_LED_MODE, 0x01)
            for reg, value in ((REG_LED_R, r), (REG_LED_G, g), (REG_LED_B, b)):
                self._bus.write_byte_data(self.addr, reg, max(0, min(255, int(value))))
        except Exception:
            log.debug("lamp write failed", exc_info=True)

    def button_pressed(self) -> bool:
        if self._bus is None:
            return False
        try:
            return bool(self._bus.read_byte_data(self.addr, REG_BUTTON))
        except Exception:
            return False

    def power_off_in(self, seconds: int) -> None:
        """Tell the controller to cut power after the Pi has halted."""
        if self._bus is None:
            return
        try:
            self._bus.write_byte_data(self.addr, REG_POWEROFF_TIMER, max(1, min(255, seconds)))
        except Exception:
            log.exception("could not arm the power-off timer")

    def close(self) -> None:
        if self._bus is not None:
            self._bus.close()


class Lamp:
    """Drives the pilot lamp from a named state, with effects.

    Only writes when the colour actually changes, so a solid state costs one
    I2C transaction rather than fifty a second.
    """

    def __init__(self, controller: PowerController, cycle_s: float = 2.0) -> None:
        self.controller = controller
        self.cycle_s = cycle_s
        self.state = "standby"
        self._last: tuple[int, int, int] | None = None
        self._overlay: tuple[str, float] | None = None

    def set_state(self, state: str) -> None:
        if state != self.state:
            log.debug("lamp %s -> %s", self.state, state)
            self.state = state

    def flash(self, state: str, now: float, seconds: float = ERROR_FLASH_S) -> None:
        """Show ``state`` for ``seconds``, then fall back to the steady state.

        The main loop keeps calling ``set_state`` every tick; an overlay is how
        a momentary message survives that without the loop having to remember
        timers of its own.
        """
        self._overlay = (state, now + seconds)

    def showing(self, now: float) -> str:
        if self._overlay is not None:
            state, until = self._overlay
            if now < until:
                return state
            self._overlay = None
        return self.state

    def tick(self, now: float) -> tuple[int, int, int]:
        state = self.showing(now)
        spec = LAMP_STATES.get(state, LAMP_STATES["standby"])
        level = brightness(state, (now % self.cycle_s) / self.cycle_s)
        rgb = tuple(int(c * level) for c in spec["rgb"])
        if rgb != self._last:
            self._last = rgb
            self.controller.set_rgb(*rgb)
        return rgb
