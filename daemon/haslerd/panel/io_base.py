"""IO backends: turn wiring into logical state.

A digital backend maps logical pin names (``d0``, ``d1``, ...) to booleans where
**True means the contact is closed**. Keys wire to ground with pull-ups, so the
electrical level is inverted - that inversion happens here, once, so nothing
downstream ever has to think about it.

An analog backend maps channel numbers to raw ADC counts. Scaling to a usable
0-100 is the pot control's job, not the backend's.

Swapping GPIO for an MCP23017, or a real ADC for nothing at all, is a backend
swap. Controls and actions never notice.
"""

from __future__ import annotations

from typing import Protocol


class DigitalBackend(Protocol):
    """Reads panel contacts. True = closed."""

    def read(self) -> dict[str, bool]: ...

    def close(self) -> None: ...


class AnalogBackend(Protocol):
    """Reads pot wipers as raw ADC counts."""

    def read(self) -> dict[int, int]: ...

    def close(self) -> None: ...


class FakeDigital:
    """In-memory digital backend for tests and ``panel-probe --fake``."""

    def __init__(self, pins: list[str] | None = None) -> None:
        self.state = dict.fromkeys(pins or [], False)

    def set(self, pin: str, closed: bool) -> None:
        self.state[pin] = closed

    def press_only(self, pin: str) -> None:
        """Close one pin and open every other - an interlocked bank in one call."""
        for key in self.state:
            self.state[key] = key == pin

    def clear(self) -> None:
        for key in self.state:
            self.state[key] = False

    def read(self) -> dict[str, bool]:
        return dict(self.state)

    def close(self) -> None:
        pass


class FakeAnalog:
    """In-memory analog backend for tests."""

    def __init__(self, channels: dict[int, int] | None = None) -> None:
        self.state = dict(channels or {})

    def set(self, channel: int, counts: int) -> None:
        self.state[channel] = counts

    def read(self) -> dict[int, int]:
        return dict(self.state)

    def close(self) -> None:
        pass


class NullAnalog:
    """The ``backend: none`` case - no ADC fitted yet.

    Pot controls silently emit nothing rather than crashing, so the whole daemon
    runs with the wheels dead until the ADS1115 arrives.
    """

    def read(self) -> dict[int, int]:
        return {}

    def close(self) -> None:
        pass
