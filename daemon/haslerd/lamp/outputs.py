"""Where the lamp's linear duty goes - see docs/lamp.md "How it is built".

* ``SysfsPwmOutput``: the kernel ``pwm-gpio`` overlay, one
  ``/sys/class/pwm/pwmchipN`` per GPIO, hrtimer PWM at ~800 Hz. Flicker-free
  at night, needs no PWM peripheral (the amp's I2S keeps PCM) and never pigpio.
* ``GpiozeroOutput``: gpiozero/lgpio software PWM, the old way, as a fallback.
* ``NullOutput`` / ``FakeOutput``: no lamp, and a recorder for tests.

Every Output takes linear duty 0..1 meaning "LED on fraction" and handles the
LED's ``common`` itself: with a common anode the GPIO sinks current, so the
pin is lit while low (sysfs: polarity ``inversed``; gpiozero: active_high
off). None of them raise after construction.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from .contract import Output

log = logging.getLogger(__name__)

CHANNELS = ("red", "green", "blue")
DEFAULT_PINS = {"red": 17, "green": 22, "blue": 27}   # the descriptor's map
DEFAULT_FREQUENCY_HZ = 800
GPIOZERO_FREQUENCY_HZ = 400
SYSFS_PWM = Path("/sys/class/pwm")


class NullOutput:
    """No lamp: every write is dropped."""

    def write(self, r: float, g: float, b: float) -> None:
        pass

    def close(self) -> None:
        pass


class FakeOutput:
    """Records writes, for tests."""

    def __init__(self) -> None:
        self.writes: list[tuple[float, float, float]] = []
        self.closed = False

    @property
    def last(self) -> tuple[float, float, float] | None:
        return self.writes[-1] if self.writes else None

    def write(self, r: float, g: float, b: float) -> None:
        self.writes.append((r, g, b))

    def close(self) -> None:
        self.closed = True


def _clamp(v: float) -> float:
    return 0.0 if v != v or v < 0.0 else 1.0 if v > 1.0 else v   # NaN -> 0


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _chip_gpio(chip: Path) -> tuple[bool, int | None]:
    """(is a pwm-gpio chip, its GPIO) from the chip's device-tree node.

    ``of_node/gpios`` is the raw ``<&gpio N flags>`` property: big-endian
    u32 cells, the second is the line. Independent of node names and probe
    order, so it survives the overlays being declared in any order.
    """
    node = chip / "device" / "of_node"
    try:
        compatible = (node / "compatible").read_bytes()
    except OSError:
        return False, None
    if b"pwm-gpio" not in compatible:
        return False, None
    try:
        cells = (node / "gpios").read_bytes()
    except OSError:
        return True, None
    if len(cells) < 8:
        return True, None
    return True, int.from_bytes(cells[4:8], "big")


def discover_chips(pins: dict[str, int], root: Path = SYSFS_PWM) -> dict[str, Path] | None:
    """Map each colour to its pwmchip directory, or None if it can't be done.

    First by each pwm-gpio chip's ``of_node/gpios``; failing that, the
    pwm-gpio chips (or every chip, if none says) in pwmchip-number order are
    taken as red, green, blue - the order the overlays are declared in
    config.txt - with a warning.
    """
    try:
        chips = sorted(
            (p for p in root.glob("pwmchip*") if p.name[7:].isdigit()),
            key=lambda p: int(p.name[7:]),
        )
    except OSError:
        return None
    if not chips:
        return None
    info = {chip: _chip_gpio(chip) for chip in chips}
    by_gpio = {gpio: chip for chip, (_, gpio) in info.items() if gpio is not None}
    if all(pins[c] in by_gpio for c in CHANNELS):
        return {c: by_gpio[pins[c]] for c in CHANNELS}
    candidates = [chip for chip, (is_gpio, _) in info.items() if is_gpio] or chips
    if len(candidates) < 3:
        return None
    log.warning(
        "lamp: can't tell which pwmchip drives which GPIO; assuming %s are R/G/B (pins %s)",
        [c.name for c in candidates[:3]], pins,
    )
    return dict(zip(CHANNELS, candidates[:3]))


class _SysfsChannel:
    """One pwm-gpio channel. Same setup order as scripts/hasler-lamp-boot.

    A common anode uses polarity ``inversed`` rather than inverting the duty:
    pwm-gpio drives a *disabled* channel to the polarity's idle level, so with
    ``normal`` every disable (and every write while disabled) would pull the
    pin low and flash the LED full on. Inversed idles high = dark, and
    duty_cycle is always the lit time.
    """

    def __init__(self, chip: Path, period_ns: int, anode: bool) -> None:
        self.pwm = chip / "pwm0"
        self.period = period_ns
        if not self.pwm.exists():
            # The early boot service usually has exported it already; reuse then.
            (chip / "export").write_text("0")
            for _ in range(50):     # the kernel adds pwm0/ at once, udev follows
                if self.pwm.exists():
                    break
                time.sleep(0.02)
        polarity = "inversed" if anode else "normal"
        enabled = _read(self.pwm / "enable") == "1"
        if _read(self.pwm / "polarity") != polarity:
            # Polarity only changes while disabled; only disable if we must.
            if enabled:
                self._put("duty_cycle", 0)
                self._put("enable", 0)
            self._put("polarity", polarity)
            enabled = False
        if _read(self.pwm / "period") != str(period_ns):
            self._put("duty_cycle", 0)      # duty must never exceed period
            self._put("period", period_ns)
        if not enabled:
            self._put("duty_cycle", 0)
            self._put("enable", 1)
        # Otherwise the boot service's glow stays until our first frame.
        self._duty = self._open_duty()

    def _put(self, name: str, value: int) -> None:
        (self.pwm / name).write_text(str(value))

    def _open_duty(self) -> int:
        # Held open: a write per frame at 50 Hz shouldn't cost an open/close.
        # No O_TRUNC; each write() is one store to the attribute.
        return os.open(self.pwm / "duty_cycle", os.O_WRONLY)

    def _duty_ns(self, duty: float) -> int:
        return round(_clamp(duty) * self.period)

    def write(self, duty: float) -> None:
        os.lseek(self._duty, 0, os.SEEK_SET)
        os.write(self._duty, f"{self._duty_ns(duty)}\n".encode())

    def close(self) -> None:
        # Left exported and enabled at its last duty for the halt hook
        # (scripts/hasler-lamp-halt) to take over.
        os.close(self._duty)


class SysfsPwmOutput:
    """Kernel pwm-gpio channels under /sys/class/pwm (see ``discover_chips``)."""

    def __init__(
        self,
        pins: dict[str, int] | None = None,
        common: str = "anode",
        frequency_hz: float = DEFAULT_FREQUENCY_HZ,
        root: Path = SYSFS_PWM,
    ) -> None:
        pins = {**DEFAULT_PINS, **(pins or {})}
        chips = discover_chips(pins, root)
        if chips is None:
            raise OSError(f"no pwm-gpio chips for pins {pins} under {root}")
        period_ns = max(1000, round(1e9 / float(frequency_hz)))
        self.channels = [_SysfsChannel(chips[c], period_ns, common == "anode") for c in CHANNELS]
        self._failed = False

    def write(self, r: float, g: float, b: float) -> None:
        try:
            for channel, duty in zip(self.channels, (r, g, b)):
                channel.write(duty)
            self._failed = False
        except (OSError, ValueError):
            if not self._failed:   # once per outage, not 50 times a second
                log.warning("lamp: pwm write failed", exc_info=True)
            self._failed = True

    def close(self) -> None:
        for channel in self.channels:
            try:
                channel.close()
            except (OSError, ValueError):
                log.debug("lamp: pwm close failed", exc_info=True)


class GpiozeroOutput:
    """gpiozero RGBLED, software PWM through lgpio - the fallback backend."""

    def __init__(
        self,
        pins: dict[str, int] | None = None,
        common: str = "anode",
        frequency_hz: float = GPIOZERO_FREQUENCY_HZ,
        factory: Callable[..., Any] | None = None,
    ) -> None:
        pins = {**DEFAULT_PINS, **(pins or {})}
        if factory is None:
            from gpiozero import RGBLED

            factory = RGBLED
        self._led = factory(
            red=pins["red"], green=pins["green"], blue=pins["blue"],
            active_high=(common == "cathode"), pwm=True,
        )
        # lgpio's default 100 Hz flickers at low duty; ask for more.
        for led in getattr(self._led, "_leds", ()):
            try:
                led.frequency = frequency_hz
            except Exception:
                log.debug("lamp: can't set gpiozero PWM frequency", exc_info=True)

    def write(self, r: float, g: float, b: float) -> None:
        try:
            self._led.value = (_clamp(r), _clamp(g), _clamp(b))
        except Exception:
            log.debug("lamp: gpiozero write failed", exc_info=True)

    def close(self) -> None:
        try:
            self._led.off()
            self._led.close()
        except Exception:
            log.debug("lamp: gpiozero close failed", exc_info=True)


def open_output(spec: dict) -> Output:
    """The Output for the descriptor's ``io.lamp`` block; NullOutput on any failure.

    ``backend``: ``pwm-sysfs`` (default) | ``gpio`` | ``none``; plus ``pins``
    ({red, green, blue} BCM numbers), ``common`` (anode | cathode) and
    ``frequency_hz``.
    """
    backend = spec.get("backend", "pwm-sysfs")
    pins = {**DEFAULT_PINS, **(spec.get("pins") or {})}
    common = spec.get("common", "anode")
    if common not in ("anode", "cathode"):
        log.warning("lamp common must be 'anode' or 'cathode', not %r; no lamp", common)
        return NullOutput()
    try:
        if backend == "pwm-sysfs":
            freq = spec.get("frequency_hz", DEFAULT_FREQUENCY_HZ)
            return SysfsPwmOutput(pins, common, freq)
        if backend == "gpio":
            freq = spec.get("frequency_hz", GPIOZERO_FREQUENCY_HZ)
            return GpiozeroOutput(pins, common, freq)
        if backend != "none":
            log.warning("unknown lamp backend %r; no lamp", backend)
    except Exception:
        log.warning("lamp backend %r unavailable on pins %s; no lamp", backend, pins, exc_info=True)
    return NullOutput()
