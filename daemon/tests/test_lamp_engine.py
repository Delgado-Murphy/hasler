"""The lamp engine and its outputs - fake clock, fake sysfs, no pins touched."""

import threading
import time

import pytest

from haslerd.lamp import engine as eng
from haslerd.lamp.contract import Look
from haslerd.lamp.engine import LampEngine, open_lamp
from haslerd.lamp.outputs import (
    FakeOutput, GpiozeroOutput, NullOutput, SysfsPwmOutput, discover_chips, open_output,
)

AMBER = Look("amber", 1.0)
READY = Look("amber", 0.35)
BREATH = Look("amber", 0.7, "breath", 1.2, 0.25)
NO_FLOOR = (0.0, 0.0, 0.0)


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    out = FakeOutput()
    kw.setdefault("min_duty", NO_FLOOR)
    lamp = LampEngine(out, clock=clock, threaded=False, **kw)
    return lamp, out, clock


def red(lamp, t):
    return lamp.render(t)[0][0]


def level(lamp, t):
    """Perceptual level of the red channel (all palette colours but blue have R=1)."""
    return red(lamp, t) ** (1 / eng.GAMMA)


# -- looks, fades, breath, gamma --------------------------------------------


def test_starts_dark_and_steady():
    lamp, out, clock = make()
    assert lamp.render(clock.t) == ((0.0, 0.0, 0.0), False)


def test_gamma_and_palette_ratio():
    lamp, _, clock = make()
    lamp.show(READY)
    (r, g, b), moving = lamp.render(clock.t)
    assert not moving
    assert r == pytest.approx(0.35 ** 2.2)
    assert g == pytest.approx(0.15 * 0.35 ** 2.2)   # ratio holds at any level
    assert b == 0.0


def test_balance_applies_after_gamma():
    lamp, _, clock = make(balance={"green": 0.5})
    lamp.show(AMBER)
    r, g, _ = lamp.render(clock.t)[0]
    assert (r, g) == pytest.approx((1.0, 0.075))


def test_palette_override_from_the_descriptor():
    lamp, _, clock = make(palette={"amber": [1.0, 0.2, 0.0]})
    lamp.show(AMBER)
    assert lamp.render(clock.t)[0][1] == pytest.approx(0.2)


def test_crossfade_endpoints_and_easing():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.show(AMBER, fade_s=1.5)
    t0 = clock.t
    assert level(lamp, t0) == pytest.approx(0.35)
    assert level(lamp, t0 + 0.75) == pytest.approx((0.35 + 1.0) / 2)   # eased midpoint
    assert level(lamp, t0 + 0.1) < 0.35 + 0.65 * 0.1                   # slow start
    (_, moving) = lamp.render(t0 + 1.5)
    assert level(lamp, t0 + 1.5) == pytest.approx(1.0) and not moving


def test_crossfade_between_colours_lands_on_the_new_colour():
    lamp, _, clock = make()
    lamp.show(AMBER)
    lamp.show(Look("blue", 0.8), fade_s=1.5)
    r, g, b = lamp.render(clock.t + 2)[0]
    assert r == 0.0 and b == pytest.approx(0.8 ** 2.2) and g == pytest.approx(0.25 * 0.8 ** 2.2)


def test_same_look_again_is_a_no_op():
    lamp, _, clock = make()
    lamp.show(BREATH)
    clock.t += 0.3
    lamp.show(BREATH, fade_s=2)       # policy repeats itself every tick
    assert lamp.render(clock.t)[1] and lamp._from is None


def test_breath_bounds_and_period():
    lamp, _, clock = make()
    lamp.show(BREATH)
    t0 = clock.t
    levels = [level(lamp, t0 + i * 0.01) for i in range(240)]
    assert min(levels) == pytest.approx(0.25, abs=1e-3)
    assert max(levels) == pytest.approx(0.7, abs=1e-3)
    assert level(lamp, t0) == pytest.approx(0.25)          # starts at the trough
    assert level(lamp, t0 + 0.6) == pytest.approx(0.7)     # peak at half period
    assert level(lamp, t0 + 1.2) == pytest.approx(0.25)
    assert lamp.render(t0 + 5)[1]                          # a breath never settles


def test_breath_keeps_its_phase_when_only_the_floor_moves():
    lamp, _, clock = make()
    lamp.show(Look("amber", 0.35, "breath", 3.0, 0.10))
    clock.t += 1.5                                         # at the peak
    lamp.show(Look("amber", 0.35, "breath", 3.0, 0.20))
    assert level(lamp, clock.t) == pytest.approx(0.35)


# -- night -----------------------------------------------------------------


def test_night_caps_at_35_percent_and_ember_at_2():
    lamp, _, clock = make()
    lamp.set_night(True)
    lamp.show(AMBER)
    assert level(lamp, clock.t) == pytest.approx(0.35)
    lamp.show(Look("ember", 0.06))
    assert level(lamp, clock.t) == pytest.approx(0.02)


def test_night_breath_keeps_its_shape():
    lamp, _, clock = make()
    lamp.set_night(True)
    lamp.show(Look("white", 0.6, "breath", 3.0, 0.15))
    t0 = clock.t
    assert level(lamp, t0) == pytest.approx(0.15 * 0.35 / 0.6)
    assert level(lamp, t0 + 1.5) == pytest.approx(0.35)


def test_night_slows_fades_and_switching_crossfades():
    lamp, _, clock = make()
    lamp.show(AMBER)
    lamp.set_night(True)
    t0 = clock.t
    assert level(lamp, t0) == pytest.approx(1.0)                 # dusk is a fade, not a step
    assert level(lamp, t0 + eng.NIGHT_SWITCH_S) == pytest.approx(0.35)
    clock.t = t0 + 10
    lamp.show(Look("amber", 0.1), fade_s=1.0)
    assert lamp._fade_s == pytest.approx(1.5)


# -- replies ---------------------------------------------------------------


def test_ack_dips_to_30_percent_and_back():
    lamp, _, clock = make()
    lamp.show(AMBER)
    lamp.reply("ack")
    t0 = clock.t
    assert level(lamp, t0) == pytest.approx(1.0)
    assert level(lamp, t0 + 0.12) == pytest.approx(0.3)
    assert level(lamp, t0 + 0.22) == pytest.approx(0.65)
    assert level(lamp, t0 + 0.3199) == pytest.approx(1.0, abs=1e-3)
    lamp.render(t0 + 0.33)
    assert lamp._reply is None
    assert lamp.render(t0 + 0.4) == (lamp.render(t0 + 0.4)[0], False)


def test_next_is_the_same_dip():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.reply("next")
    assert level(lamp, clock.t + 0.12) == pytest.approx(0.35 * 0.3)


def test_ha_is_two_150ms_dips():
    lamp, _, clock = make()
    lamp.show(AMBER)
    lamp.reply("ha")
    t0 = clock.t
    assert level(lamp, t0 + 0.06) == pytest.approx(0.3)
    assert level(lamp, t0 + 0.15) == pytest.approx(1.0)
    assert level(lamp, t0 + 0.20) == pytest.approx(1.0)    # the gap
    assert level(lamp, t0 + 0.31) == pytest.approx(0.3)
    assert level(lamp, t0 + 0.40) == pytest.approx(1.0)
    assert lamp._reply is None


def test_stop_fades_100_to_35_over_800ms_then_the_look():
    lamp, _, clock = make()
    lamp.show(AMBER)
    lamp.reply("stop")
    lamp.show(READY)                    # the policy's next Look, set straight away
    t0 = clock.t
    assert level(lamp, t0) == pytest.approx(1.0)
    assert level(lamp, t0 + 0.4) == pytest.approx(0.675)
    assert level(lamp, t0 + 0.79) == pytest.approx(0.35, abs=0.01)
    assert level(lamp, t0 + 0.81) == pytest.approx(0.35)   # no restore fade needed
    assert not lamp.render(t0 + 1.2)[1]


def test_fail_is_three_soft_red_pulses_at_60_percent():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.reply("fail")
    t0 = clock.t
    for k in range(3):
        r, g, b = lamp.render(t0 + 0.3 + 0.6 * k)[0]
        assert r ** (1 / 2.2) == pytest.approx(0.6)
        assert g == pytest.approx(0.03 * 0.6 ** 2.2)        # red, not amber
        assert level(lamp, t0 + 0.6 * (k + 1) - 1e-6) < 0.01   # trough between pulses
    # then back to the Look, blending in rather than snapping
    mid = level(lamp, t0 + 1.8 + 0.15)
    assert 0.0 < mid < 0.35
    assert level(lamp, t0 + 1.8 + eng.REPLY_RESTORE_S + 0.01) == pytest.approx(0.35)


def test_reply_returns_to_a_look_that_changed_meanwhile():
    lamp, _, clock = make()
    lamp.show(READY)
    lamp.reply("fail")
    t0 = clock.t
    clock.t = t0 + 1.0
    lamp.show(Look("blue", 0.8), fade_s=0.2)
    lamp.render(t0 + 1.9)
    r, _, b = lamp.render(t0 + 3)[0]
    assert r == 0.0 and b == pytest.approx(0.8 ** 2.2)


def test_a_newer_reply_replaces_an_older():
    lamp, _, clock = make()
    lamp.show(AMBER)
    lamp.reply("fail")
    clock.t += 0.3
    lamp.reply("ack")
    assert lamp._reply == "ack"
    assert level(lamp, clock.t + 0.12) == pytest.approx(0.3)


# -- countdown -------------------------------------------------------------


def test_countdown_dims_ember_linearly_to_2_percent():
    lamp, _, clock = make()
    lamp.show(Look("ember", 0.06))
    for p in (0.0, 0.5, 1.0):
        lamp.countdown(p)
        assert level(lamp, clock.t) == pytest.approx(0.06 + (0.02 - 0.06) * p)
    assert not lamp.render(clock.t)[1]       # steady between progress updates


def test_countdown_cancel_restores_within_half_a_second():
    lamp, _, clock = make()
    lamp.show(AMBER)
    lamp.countdown(0.8)
    lamp.render(clock.t)
    lamp.countdown(None)
    t0 = clock.t
    assert level(lamp, t0) < 0.5                     # no snap
    assert level(lamp, t0 + 0.5) == pytest.approx(1.0)
    assert not lamp.render(t0 + 0.6)[1]


def test_countdown_outranks_replies():
    lamp, _, clock = make()
    lamp.show(Look("ember", 0.06))
    lamp.countdown(1.0)
    lamp.reply("ack")
    assert level(lamp, clock.t + 0.12) == pytest.approx(0.02)


# -- duty floor, writes, thread, close ---------------------------------------


def test_min_duty_keeps_deep_embers_green():
    lamp, _, clock = make(min_duty=None)       # the defaults
    lamp.show(Look("ember", 0.02))
    r, g, b = lamp.render(clock.t)[0]
    assert r == eng.DEFAULT_MIN_DUTY[0] and g == eng.DEFAULT_MIN_DUTY[1] and b == 0.0
    lamp.show(Look("ember", 0.0))
    assert lamp.render(clock.t)[0] == (0.0, 0.0, 0.0)   # dark is dark


def test_step_writes_only_when_it_changes():
    lamp, out, clock = make()
    lamp.show(READY)
    for _ in range(10):
        lamp.step(clock.t)
    assert len(out.writes) == 1
    lamp.show(AMBER, fade_s=1.0)
    t0 = clock.t
    for i in range(60):
        lamp.step(t0 + i * 0.02)
    assert out.last == pytest.approx((1.0, 0.15, 0.0))     # exact once settled
    assert 10 < len(out.writes) < 62


def test_thread_sleeps_when_steady_and_renders_fades():
    out = FakeOutput()
    lamp = LampEngine(out, min_duty=NO_FLOOR)
    try:
        assert lamp._thread.name == "lamp" and lamp._thread.daemon
        lamp.show(READY)
        time.sleep(0.1)
        n = len(out.writes)
        assert out.last == pytest.approx((0.35 ** 2.2, 0.15 * 0.35 ** 2.2, 0.0))
        time.sleep(0.3)
        assert len(out.writes) == n                        # steady: no writes
        lamp.show(AMBER, fade_s=0.3)
        time.sleep(0.5)
        assert len(out.writes) > n + 5                     # ~50 Hz during the fade
        assert out.last == pytest.approx((1.0, 0.15, 0.0))
    finally:
        lamp.close()


def test_callers_never_wait_on_a_slow_output():
    gate = threading.Event()

    class Slow(FakeOutput):
        def write(self, r, g, b):
            gate.wait(2)
            super().write(r, g, b)

    lamp = LampEngine(Slow())
    try:
        lamp.show(AMBER, fade_s=1)
        time.sleep(0.05)                                   # the thread is stuck writing
        t = time.monotonic()
        lamp.show(READY)
        lamp.reply("ack")
        lamp.countdown(0.5)
        assert time.monotonic() - t < 0.05
    finally:
        gate.set()
        lamp.close()


def test_close_leaves_it_dark_and_stops_the_thread():
    out = FakeOutput()
    lamp = LampEngine(out)
    lamp.show(AMBER)
    time.sleep(0.05)
    lamp.close()
    assert out.last == (0.0, 0.0, 0.0) and out.closed
    assert not lamp._thread.is_alive()
    lamp.close()                                           # twice is fine


def test_close_without_halt_dark_leaves_the_last_frame():
    out = FakeOutput()
    lamp = LampEngine(out, threaded=False)
    lamp.show(AMBER)
    lamp.step(0)
    lamp.close(halt_dark=False)
    assert out.last == pytest.approx((1.0, 0.15, 0.0)) and out.closed


# -- sysfs pwm ---------------------------------------------------------------


def fake_sysfs(tmp_path, gpios=(17, 22, 27), with_gpios_prop=True, extra_hw_chip=True):
    """pwmchipN dirs as the pwm-gpio overlay makes them; export creates pwm0."""
    root = tmp_path / "pwm"
    n = 0
    if extra_hw_chip:   # the SoC's own PWM, which must be skipped
        chip = root / f"pwmchip{n}"
        (chip / "device" / "of_node").mkdir(parents=True)
        (chip / "device" / "of_node" / "compatible").write_bytes(b"brcm,bcm2835-pwm\0")
        n += 1
    for gpio in gpios:
        chip = root / f"pwmchip{n}"
        node = chip / "device" / "of_node"
        node.mkdir(parents=True)
        (node / "compatible").write_bytes(b"pwm-gpio\0")
        if with_gpios_prop:
            (node / "gpios").write_bytes(
                (0x7F).to_bytes(4, "big") + gpio.to_bytes(4, "big") + (0).to_bytes(4, "big")
            )
        n += 1
    return root


def export_all(root):
    for chip in root.glob("pwmchip*"):
        pwm = chip / "pwm0"
        pwm.mkdir()
        for name in ("period", "duty_cycle", "enable"):
            (pwm / name).write_text("0")
        (pwm / "polarity").write_text("normal")


def duty(root, n):
    # The fake is a plain file: a shorter write leaves the old tail behind.
    return int((root / f"pwmchip{n}" / "pwm0" / "duty_cycle").read_text().split()[0])


def test_discovery_by_device_tree_gpio(tmp_path):
    root = fake_sysfs(tmp_path, gpios=(27, 17, 22))   # declared in any order
    chips = discover_chips({"red": 17, "green": 22, "blue": 27}, root)
    assert {c: p.name for c, p in chips.items()} == {
        "red": "pwmchip2", "green": "pwmchip3", "blue": "pwmchip1",
    }


def test_discovery_falls_back_to_declaration_order(tmp_path, caplog):
    root = fake_sysfs(tmp_path, with_gpios_prop=False)
    chips = discover_chips({"red": 17, "green": 22, "blue": 27}, root)
    assert [chips[c].name for c in ("red", "green", "blue")] == ["pwmchip1", "pwmchip2", "pwmchip3"]
    assert "can't tell" in caplog.text


def test_discovery_without_chips(tmp_path):
    assert discover_chips({"red": 17, "green": 22, "blue": 27}, tmp_path / "nothing") is None


def test_sysfs_anode_uses_inversed_polarity_at_800hz(tmp_path, monkeypatch):
    root = fake_sysfs(tmp_path, extra_hw_chip=False)
    export_all(root)
    pwm = root / "pwmchip0" / "pwm0"
    (pwm / "enable").write_text("1")            # running with the wrong polarity
    order = []
    real_write = type(root).write_text

    def write_text(self, data, *a, **kw):
        if self.parent == pwm:
            order.append((self.name, data))
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(type(root), "write_text", write_text)
    out = SysfsPwmOutput(common="anode", frequency_hz=800, root=root)
    # pwm-gpio idles a disabled channel at the polarity's idle level: switch
    # polarity only while disabled, and with duty 0 first, then period, enable.
    assert order == [
        ("duty_cycle", "0"), ("enable", "0"), ("polarity", "inversed"),
        ("duty_cycle", "0"), ("period", "1250000"), ("duty_cycle", "0"), ("enable", "1"),
    ]
    assert (pwm / "polarity").read_text() == "inversed"
    out.write(1.0, 0.25, 0.0)
    assert (duty(root, 0), duty(root, 1), duty(root, 2)) == (1250000, 312500, 0)   # lit time
    out.close()


def test_sysfs_cathode_is_direct_and_exports(tmp_path, monkeypatch):
    root = fake_sysfs(tmp_path, extra_hw_chip=False)
    for chip in root.glob("pwmchip*"):    # export makes pwm0, as the kernel would
        (chip / "export").write_text("")
    real_write = type(root).write_text

    def write_text(self, data, *a, **kw):
        if self.name == "export":
            pwm = self.parent / "pwm0"
            pwm.mkdir(exist_ok=True)
            for name in ("period", "duty_cycle", "enable"):
                (pwm / name).write_text("0")
            (pwm / "polarity").write_text("normal")
        return real_write(self, data, *a, **kw)

    monkeypatch.setattr(type(root), "write_text", write_text)
    out = SysfsPwmOutput(common="cathode", frequency_hz=1000, root=root)
    out.write(0.5, 0.0, 1.0)
    assert (duty(root, 0), duty(root, 1), duty(root, 2)) == (500000, 0, 1000000)
    out.close()


def test_sysfs_keeps_the_boot_services_glow(tmp_path):
    root = fake_sysfs(tmp_path, extra_hw_chip=False)
    export_all(root)
    for chip in root.glob("pwmchip*"):
        (chip / "pwm0" / "period").write_text("1250000")
        (chip / "pwm0" / "duty_cycle").write_text("90000")
        (chip / "pwm0" / "enable").write_text("1")
        (chip / "pwm0" / "polarity").write_text("inversed")
    SysfsPwmOutput(root=root).close()
    assert duty(root, 0) == 90000


def test_open_output_degrades_to_null(tmp_path, monkeypatch):
    import haslerd.lamp.outputs as outputs

    monkeypatch.setattr(outputs, "SYSFS_PWM", tmp_path / "none")
    monkeypatch.setattr(outputs.SysfsPwmOutput.__init__, "__defaults__",
                        (None, "anode", 800, tmp_path / "none"))
    assert isinstance(open_output({"backend": "pwm-sysfs"}), NullOutput)
    assert isinstance(open_output({"backend": "none"}), NullOutput)
    assert isinstance(open_output({"backend": "neon"}), NullOutput)
    assert isinstance(open_output({"common": "annode"}), NullOutput)


def test_gpiozero_output_raises_frequency_and_inverts_anode():
    made = {}

    class Led:
        frequency = 100

    class RGBLED:
        def __init__(self, red, green, blue, active_high, pwm):
            made.update(pins=(red, green, blue), active_high=active_high, pwm=pwm)
            self._leds = (Led(), Led(), Led())
            self.value = (0, 0, 0)

        def off(self):
            self.value = (0, 0, 0)

        def close(self):
            made["closed"] = True

    out = GpiozeroOutput(common="anode", factory=RGBLED)
    assert made == {"pins": (17, 22, 27), "active_high": False, "pwm": True}
    assert all(led.frequency == 400 for led in out._led._leds)
    out.write(1.5, 0.2, -1)
    assert out._led.value == (1.0, 0.2, 0.0)
    out.close()
    assert made["closed"]


def test_open_lamp(monkeypatch):
    import haslerd.lamp.engine as engine

    assert open_lamp({"backend": "none"}) is None
    monkeypatch.setattr(engine, "open_output", lambda spec: NullOutput())
    assert open_lamp({"backend": "pwm-sysfs"}) is None     # nothing works -> no lamp
    seen = []

    def fake(spec):
        seen.append(spec["backend"])
        return FakeOutput() if spec["backend"] == "gpio" else NullOutput()

    monkeypatch.setattr(engine, "open_output", fake)
    lamp = open_lamp({"backend": "pwm-sysfs", "palette": {"amber": [1, 0.2, 0]}})
    try:
        assert seen == ["pwm-sysfs", "gpio"]                  # falls back to gpiozero
        assert isinstance(lamp, LampEngine) and lamp.palette["amber"] == (1.0, 0.2, 0.0)
    finally:
        lamp.close()


def test_hasler_descriptor_lamp_block():
    from conftest import VARIANTS
    from haslerd.variant import load_variant

    spec = load_variant(VARIANTS / "hasler-hm.yaml").io["lamp"]
    assert spec["backend"] == "pwm-sysfs" and spec["common"] == "anode"
    assert spec["pins"] == {"red": 17, "green": 22, "blue": 27}
    assert spec["frequency_hz"] == 800
