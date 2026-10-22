"""config/variants/hasler-hm-hat.yaml: the Hasler HM on the Hasler HAT (hardware/hat).

hasler-hm.yaml stays the prototype stripboard's mapping until the HAT is fitted; this file
is the one to deploy then (point haslerd.service's --variant at it).
"""

from conftest import VARIANTS

from haslerd.variant import load_variant

HAT = VARIANTS / "hasler-hm-hat.yaml"


def test_same_cabinet_as_the_stripboard_descriptor():
    hat, proto = load_variant(HAT), load_variant(VARIANTS / "hasler-hm.yaml")
    assert hat.name == proto.name == "hasler-hm"
    assert [c["id"] for c in hat.controls] == [c["id"] for c in proto.controls]


def test_pins_follow_the_hats_j2():
    # End-switch on GPA0 (J2 14), keys K1-K6 + PU on GPB0-6 (J2 1, 3-8); d7/d15 output-only.
    assert load_variant(HAT).pin_names() == ["d0", "d8", "d9", "d10", "d11", "d12", "d13", "d14"]


def test_expander_pinned_with_its_interrupt():
    io = load_variant(HAT).io["digital"]
    assert io["backend"] == "mcp23017"
    # Pinned: the HAT's CH224A can answer at 0x22/0x23, and auto refuses two devices.
    assert io["addr"] == 0x20
    assert io["int_gpio"] == 23


def test_adc_and_lamp_on_the_hats_pins():
    io = load_variant(HAT).io
    assert io["analog"]["backend"] == "ads1115" and io["analog"]["addr"] == 0x48
    assert io["lamp"]["pins"] == {"red": 17, "green": 22, "blue": 27}
    assert io["lamp"]["common"] == "anode"


def test_calibration_and_lamp_follow_the_stripboard_descriptor():
    # Same cabinet, same pots: only the pins differ. Catches one file being
    # re-calibrated and the other forgotten.
    hat, proto = load_variant(HAT), load_variant(VARIANTS / "hasler-hm.yaml")

    def strip(c):
        return {k: v for k, v in c.items() if k not in ("pin", "pins", "end_switch")}

    assert [strip(c) for c in hat.controls] == [strip(c) for c in proto.controls]
    assert hat.io["lamp"] == proto.io["lamp"]
    assert hat.actions == proto.actions
