"""Digital backend: keys wired straight to Pi GPIO.

Stage 1. Both cabinets fit here - the small Hasler needs 8 inputs and the
Excelsior 12, against 17 GPIOs left free by the HiFiBerry stack. The MCP23017
is a wiring-tidiness upgrade, not a prerequisite.

Keys go to ground with the internal pull-ups enabled, so `is_pressed` is True
when the contact is closed. gpiozero's own debounce is left off: debouncing
happens in the control layer, where an interlocked bank can be settled as a
whole vector rather than pin by pin.
"""

from __future__ import annotations


class GpioDigital:
    def __init__(self, mapping: dict[str, int]) -> None:
        from gpiozero import Button

        self.mapping = dict(mapping)
        self._buttons = {
            name: Button(pin, pull_up=True, bounce_time=None)
            for name, pin in self.mapping.items()
        }

    def read(self) -> dict[str, bool]:
        return {name: b.is_pressed for name, b in self._buttons.items()}

    def close(self) -> None:
        for b in self._buttons.values():
            b.close()
