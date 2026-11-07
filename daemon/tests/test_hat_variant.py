"""config/variants/hasler-hm-hat.yaml: the Hasler HM on the Hasler HAT (hardware/hat).

The default for a new HM radio (hasler.txt, hasler-install); hasler-hm.yaml is the
prototype stripboard's mapping. The HAT file carries only its own wiring (io,
controls) and takes the key behaviour from hasler-hm.yaml (``extends``).
"""

import yaml
from conftest import ROOT, VARIANTS

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
    # Same cabinet, same pots: only the pins differ - and the tone pot's
    # scaling, which the HAT lands the other way round (see below). Catches
    # one file being re-calibrated and the other forgotten.
    hat, proto = load_variant(HAT), load_variant(VARIANTS / "hasler-hm.yaml")
    SCALING = ("curve", "range", "taper", "hysteresis_counts", "direction")

    def strip(c):
        skip = ("pin", "pins", "end_switch") + (SCALING if c.get("id") == "tone" else ())
        return {k: v for k, v in c.items() if k not in skip}

    assert [strip(c) for c in hat.controls] == [strip(c) for c in proto.controls]
    assert hat.io["lamp"] == proto.io["lamp"]
    assert hat.actions == proto.actions


def test_both_pots_read_backwards_on_the_hat():
    # Metered 2026-11-07: volume ~26 fully clockwise, ~26 300 at the left end;
    # tone 724 fully clockwise, 26 258 just above the off click. Clockwise is
    # louder and brighter, so both curves fall with the count.
    pots = {c["id"]: c for c in load_variant(HAT).controls if c.get("kind") == "pot"}
    for cid in ("volume", "tone"):
        curve = pots[cid]["curve"]
        assert curve[0][1] == 100 and curve[-1][1] == 0, cid
        assert [p[0] for p in curve] == sorted(p[0] for p in curve), cid
    assert pots["tone"]["curve"][0][0] > 700          # the clockwise end, inside the measured 724
    assert "range" not in pots["tone"]                 # the stripboard's rising range is gone


def test_behaviour_is_inherited_not_copied():
    """The HAT file says `extends: hasler-hm` and carries no actions, combos or
    presets of its own: the key behaviour cannot drift between the two files."""
    raw = yaml.safe_load(HAT.read_text())
    assert raw["extends"] == "hasler-hm"
    assert not {"actions", "combos", "presets"} & set(raw)
    hat, proto = load_variant(HAT), load_variant(VARIANTS / "hasler-hm.yaml")
    assert hat.actions == proto.actions and hat.combos == proto.combos
    assert hat.slots == proto.slots == 6 and hat.presets == proto.presets
    assert hat.title == proto.title


def test_the_hat_is_the_default_for_a_new_radio():
    firstboot = (ROOT / "config" / "firstboot" / "hasler.txt").read_text()
    assert "variant = hasler-hm-hat" in firstboot and "starter = dial" in firstboot
    install = (ROOT / "scripts" / "hasler-install").read_text()
    assert '"variant": "hasler-hm-hat"' in install and '"starter": "dial"' in install
    assert "starter: {s['starter']}" in install
    example = yaml.safe_load((ROOT / "config" / "box.yaml.example").read_text())
    assert example["variant"] == "hasler-hm-hat" and example["starter"] == "dial"
