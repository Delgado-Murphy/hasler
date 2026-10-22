"""Pilot lamp on three Pi GPIOs - the 4-leg RGB LED, no Power Controller.

``Lamp`` (hw/powerctl.py) owns the states and effects; this is just a
controller with the same ``set_rgb``. gpiozero's RGBLED does the PWM in
software through lgpio, so it needs no PWM peripheral - the Pi's own
analogue audio claims that - and dims, pulses and mixes any colour.

Superseded by lamp/ (engine + outputs, docs/lamp.md); kept for the old
``Lamp`` in hw/powerctl.py. Wiring is in the descriptor's ``io.lamp``. ``common`` must match the LED: ``cathode``
(long leg to GND, GPIO high = on) or ``anode`` (long leg to 3V3, GPIO low
= on). Get it backwards and the colours come out inverted, nothing worse.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

log = logging.getLogger(__name__)

# The lamp GPIOs both cabinets use (header pins 11, 15, 13). Not 23: on the HAT that is the
# MCP23017's push-pull INTA, so a default there would drive against it.
DEFAULT_PINS = {"red": 17, "green": 22, "blue": 27}


class GpioRgbController:
    def __init__(
        self,
        pins: dict[str, int] | None = None,
        common: str = "cathode",
        balance: dict[str, float] | None = None,
        factory: Callable[..., Any] | None = None,
    ) -> None:
        pins = {**DEFAULT_PINS, **(pins or {})}
        if common not in ("cathode", "anode"):
            raise ValueError(f"lamp common must be 'cathode' or 'anode', not {common!r}")
        # Per-channel ceiling, 0..1: a bare RGB LED's green is far brighter than
        # its red, so "amber" needs green held back to read as amber.
        self.balance = {"red": 1.0, "green": 1.0, "blue": 1.0, **(balance or {})}
        self._led = None
        try:
            if factory is None:
                from gpiozero import RGBLED

                factory = RGBLED
            self._led = factory(
                red=pins["red"], green=pins["green"], blue=pins["blue"],
                active_high=(common == "cathode"), pwm=True,
            )
        except Exception:
            log.warning("GPIO lamp unavailable on pins %s; lamp disabled", pins, exc_info=True)

    @property
    def available(self) -> bool:
        return self._led is not None

    def set_rgb(self, r: int, g: int, b: int) -> None:
        if self._led is None:
            return
        value = tuple(
            max(0.0, min(1.0, v / 255.0)) * self.balance[name]
            for v, name in ((r, "red"), (g, "green"), (b, "blue"))
        )
        try:
            self._led.value = value
        except Exception:
            log.debug("lamp write failed", exc_info=True)

    def close(self) -> None:
        if self._led is not None:
            try:
                self._led.off()
                self._led.close()
            except Exception:
                log.debug("lamp close failed", exc_info=True)
            self._led = None
