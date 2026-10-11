"""Cabinet descriptors.

One YAML file per cabinet. The daemon knows nothing about which radio it is
inside - it loads a descriptor, builds the backends the descriptor names, and
binds actions to control ids. Adding the Excelsior is a new file, not a new
code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from .panel.controls import Control, Event, build_controls
from .panel.io_base import AnalogBackend, DigitalBackend, FakeAnalog, FakeDigital, NullAnalog


@dataclass
class Variant:
    name: str
    io: dict[str, Any]
    controls: list[dict]
    actions: dict[str, Any] = field(default_factory=dict)
    presets: list[dict] = field(default_factory=list)
    path: Path | None = None

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
            {spec["channel"] for spec in self.controls if spec.get("kind") == "pot"}
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

        digital = Mcp23017Digital(names, **digital_spec)
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


class Panel:
    """Backends plus controls. Poll it with a timestamp, get events back."""

    def __init__(
        self, controls: list[Control], digital: DigitalBackend, analog: AnalogBackend
    ) -> None:
        self.controls = controls
        self.digital = digital
        self.analog = analog

    @classmethod
    def from_variant(cls, variant: Variant, fake: bool = False) -> "Panel":
        digital, analog = open_backends(variant, fake=fake)
        return cls(build_controls(variant.controls), digital, analog)

    def poll(self, now: float) -> list[Event]:
        digital = self.digital.read()
        analog = self.analog.read()
        events: list[Event] = []
        for control in self.controls:
            events.extend(control.update(now, digital, analog))
        return events

    def states(self) -> dict[str, Any]:
        return {c.id: c.state() for c in self.controls}

    def raw(self) -> tuple[Mapping[str, bool], Mapping[int, int]]:
        return self.digital.read(), self.analog.read()

    def close(self) -> None:
        self.digital.close()
        self.analog.close()
