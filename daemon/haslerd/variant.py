"""Cabinet descriptors.

One YAML file per cabinet. The daemon knows nothing about which radio it is
inside - it loads a descriptor, builds the backends the descriptor names, and
binds actions to control ids. Adding the Excelsior is a new file, not a new
code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .panel.controls import Control, Event, build_controls
from .panel.io_base import (
    AbsentDigital,
    AnalogBackend,
    DigitalBackend,
    FakeAnalog,
    FakeDigital,
    NullAnalog,
)

log = logging.getLogger(__name__)


@dataclass
class Variant:
    name: str
    io: dict[str, Any]
    controls: list[dict]
    actions: dict[str, Any] = field(default_factory=dict)
    presets: list[dict] = field(default_factory=list)
    path: Path | None = None
    # The box's calibration overlay (/etc/hasler/variant.d/<name>.yaml) when
    # one was laid over this descriptor - see boxconfig.load_effective_variant.
    overlay_path: Path | None = None

    def pin_names(self) -> list[str]:
        """Every logical pin the descriptor references, in `d0..dN` order."""
        names: set[str] = set()
        for spec in self.controls:
            if pin := spec.get("pin"):
                names.add(pin)
            for pin in spec.get("pins", []) or []:
                names.add(pin)
            if release := spec.get("release"):
                if pin := release.get("pin"):
                    names.add(pin)
            if end := spec.get("end_switch"):
                if pin := end.get("pin"):
                    names.add(pin)
        return sorted(names, key=lambda n: (len(n), n))

    def channels(self) -> list[int]:
        return sorted(
            {
                spec["channel"]
                for spec in self.controls
                if spec.get("kind") in ("pot", "analog_selector")
            }
        )


def load_variant(path: str | Path) -> Variant:
    path = Path(path)
    data = yaml.safe_load(path.read_text()) or {}
    missing = [k for k in ("variant", "controls") if k not in data]
    if missing:
        raise ValueError(f"{path}: descriptor is missing {', '.join(missing)}")
    return Variant(
        name=data["variant"],
        io=data.get("io", {}),
        controls=data["controls"],
        actions=data.get("actions", {}),
        presets=data.get("presets", []),
        path=path,
    )


def gpio_mapping(names: list[str], pins: list[int], variant_name: str = "") -> dict[str, int]:
    """Logical pin ``dN`` -> BCM pin ``pins[N]``.

    By index, not by zipping the referenced names against the list: a
    descriptor that skips a logical pin (say it uses d0, d1 and d3) would
    otherwise shift every later key onto its neighbour's wire - which looks
    exactly like a wiring fault and is miserable to find with a meter.
    """
    mapping: dict[str, int] = {}
    for name in names:
        if len(name) < 2 or name[0] != "d" or not name[1:].isdigit():
            raise ValueError(f"{variant_name}: pin {name!r} is not of the form dN")
        index = int(name[1:])
        if index >= len(pins):
            raise ValueError(
                f"{variant_name}: {name} needs io.digital.pins[{index}], "
                f"but only {len(pins)} pins are listed"
            )
        mapping[name] = int(pins[index])
    return mapping


def open_backends(
    variant: Variant, fake: bool = False
) -> tuple[DigitalBackend, AnalogBackend]:
    """Build the backends the descriptor asks for.

    ``fake=True`` forces in-memory backends so the probe and the tests run on a
    laptop. A missing or ``none`` analog backend is not an error - it is the
    expected state until the ADS1115 is fitted.
    """
    digital_spec = dict(variant.io.get("digital") or {"backend": "fake"})
    analog_spec = dict(variant.io.get("analog") or {"backend": "none"})
    dbackend = "fake" if fake else digital_spec.pop("backend", "fake")
    abackend = "fake" if fake else analog_spec.pop("backend", "none")

    names = variant.pin_names()

    if dbackend == "fake":
        digital: DigitalBackend = FakeDigital(names)
    elif dbackend == "gpio":
        from .panel.io_gpio import GpioDigital

        mapping = gpio_mapping(names, digital_spec.get("pins") or [], variant.name)
        digital = GpioDigital(mapping)
    elif dbackend == "mcp23017":
        from .panel.io_mcp23017 import Mcp23017Digital

        try:
            digital = Mcp23017Digital(names, **digital_spec)
        except (OSError, RuntimeError) as exc:
            # No board on the header: keep the page and the music up rather
            # than crash-looping. Controls stay unread until a restart.
            log.warning("panel board not found, running without controls: %s", exc)
            return AbsentDigital(str(exc)), NullAnalog()
    else:
        raise ValueError(f"unknown digital backend {dbackend!r}")

    if abackend in ("none", None):
        analog: AnalogBackend = NullAnalog()
    elif abackend == "fake":
        analog = FakeAnalog(dict.fromkeys(variant.channels(), 0))
    elif abackend == "ads1115":
        from .panel.io_ads1115 import Ads1115Analog

        analog = Ads1115Analog(variant.channels(), **analog_spec)
    else:
        raise ValueError(f"unknown analog backend {abackend!r}")

    return digital, analog


# What Panel.apply_specs may change on a running control. Not the pins or
# the channel (the backends were opened for those) and not a key's repeat,
# which the key page owns (keymap.tune_repeat).
LIVE_ATTRS = {
    "pot": ("taper", "median_n", "hysteresis", "curve", "counts_range"),
    "end_switch": ("off_hold_s",),
    "momentary": ("hold_s",),
    "interlocked_bank": ("hold_s",),
}


class Panel:
    """Backends plus controls. Poll it with a timestamp, get events back."""

    def __init__(
        self, controls: list[Control], digital: DigitalBackend, analog: AnalogBackend
    ) -> None:
        self.controls = controls
        self.digital = digital
        self.analog = analog
        self._bus_failing = False

    @classmethod
    def from_variant(cls, variant: Variant, fake: bool = False) -> "Panel":
        digital, analog = open_backends(variant, fake=fake)
        return cls(build_controls(variant.controls), digital, analog)

    @property
    def absent(self) -> bool:
        """True when the panel board was missing at start (see AbsentDigital)."""
        return getattr(self.digital, "absent", False)

    def poll(self, now: float) -> list[Event]:
        if self.absent:
            return []
        try:
            digital = self.digital.read()
            analog = self.analog.read()
        except OSError as exc:
            # A glitch on the cabinet ribbon (ESD, a bus collision) fails one
            # I2C transfer. Skip the tick rather than let it end haslerd -
            # and never hand the controls an empty reading: every contact
            # open looks like the power knob turned off.
            if not self._bus_failing:
                log.warning("panel read failed, skipping polls until it answers: %s", exc)
                self._bus_failing = True
            return []
        if self._bus_failing:
            log.info("panel reads again")
            self._bus_failing = False
        events: list[Event] = []
        for control in self.controls:
            events.extend(control.update(now, digital, analog))
        return events

    def states(self) -> dict[str, Any]:
        return {c.id: c.state() for c in self.controls}

    def raw(self) -> tuple[Mapping[str, bool], Mapping[int, int]]:
        return self.digital.read(), self.analog.read()

    def apply_specs(self, specs: list[dict]) -> list[str]:
        """Re-apply control specs to the running controls, in place, by id.

        For a changed calibration overlay (boxconfig): a pot's curve, range,
        taper, median and hysteresis, an end switch's hold, a key's hold time
        all change without a restart, and each control keeps its live state.
        A pot forgets its committed reading, so the next poll reports the
        knob through the new curve. A control that defines
        ``reconfigure(new_control)`` does it itself. Anything structural (a
        new id, another kind) needs a restart and is only logged. Returns the
        ids that changed.
        """
        fresh = {c.id: c for c in build_controls(specs)}
        changed: list[str] = []
        for control in self.controls:
            new = fresh.get(control.id)
            if new is None or type(new) is not type(control):
                log.warning("control %s changed shape; restart haslerd to apply it", control.id)
                continue
            reconfigure = getattr(control, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(new)
                changed.append(control.id)
                continue
            attrs = [a for a in LIVE_ATTRS.get(control.kind, ()) if hasattr(new, a)]
            if any(getattr(control, a) != getattr(new, a) for a in attrs):
                for attr in attrs:
                    setattr(control, attr, getattr(new, attr))
                if control.kind == "pot":
                    control._committed_counts = None
                changed.append(control.id)
        return changed

    def close(self) -> None:
        self.digital.close()
        self.analog.close()
