"""The lamp changing hands without a dark gap (lamp/handover.py, docs/lamp.md
"Boot and shutdown"): the outputs' read/hold/period, the keeper's take-over,
watchdog and close, and scripts/hasler-lamp-boot itself on a fake sysfs."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import VARIANTS
from test_lamp_engine import duty, export_all, fake_sysfs
from test_lamp_policy import FakeDriver, FakeNet, FakePanel

from haslerd.actions import Actions
from haslerd.lamp import handover, policy
from haslerd.lamp.outputs import (FakeOutput, SysfsPwmOutput, open_output, period_for,
                                  remember_period, sysfs_frequency)
from haslerd.main import LampKeeper
from haslerd.panel.controls import EndSwitch, Event
from haslerd.player.base import FakePlayer
from haslerd.variant import load_variant

BOOT_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hasler-lamp-boot"


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def lit_sysfs(tmp_path, period=1250000, duties=(375000, 56000, 0)):
    """Three pwm-gpio channels as the boot breath leaves them: inversed, enabled, lit."""
    root = fake_sysfs(tmp_path, extra_hw_chip=False)
    export_all(root)
    for i, d in enumerate(duties):
        pwm = root / f"pwmchip{i}" / "pwm0"
        (pwm / "period").write_text(f"{period}\n")
        (pwm / "duty_cycle").write_text(f"{d}\n")
        (pwm / "enable").write_text("1\n")
        (pwm / "polarity").write_text("inversed\n")
    return root


# -- outputs -------------------------------------------------------------


class TestOutputs:
    def test_pwm_hz_is_an_option_and_800_stays_the_default(self):
        assert sysfs_frequency({}) == 800
        assert sysfs_frequency({"frequency_hz": 800}) == 800
        assert sysfs_frequency({"frequency_hz": 800, "pwm_hz": 400}) == 400
        assert sysfs_frequency({"pwm_hz": "nope", "frequency_hz": 1000}) == 1000
        assert period_for(800) == 1250000 and period_for(400) == 2500000

    def test_reads_back_what_the_boot_breath_left(self, tmp_path):
        root = lit_sysfs(tmp_path)
        out = SysfsPwmOutput(root=root)
        assert out.read() == pytest.approx((0.3, 0.0448, 0.0))
        assert duty(root, 0) == 375000          # opening it wrote nothing
        out.close()

    def test_a_new_period_keeps_the_glow_never_a_blink(self, tmp_path, monkeypatch):
        root = lit_sysfs(tmp_path)
        pwm = root / "pwmchip0" / "pwm0"
        seen = []
        real = type(root).write_text

        def write_text(self, data, *a, **kw):
            if self.parent == pwm:
                seen.append((self.name, data))
            return real(self, data, *a, **kw)

        monkeypatch.setattr(type(root), "write_text", write_text)
        out = SysfsPwmOutput(frequency_hz=400, root=root)       # 2.5 ms: longer, period first
        assert seen == [("period", "2500000"), ("duty_cycle", "750000")]
        assert ("duty_cycle", "0") not in seen and ("enable", "0") not in seen
        out.close()
        seen.clear()
        SysfsPwmOutput(frequency_hz=800, root=root).close()     # shorter: duty first
        assert seen == [("duty_cycle", "375000"), ("period", "1250000")]

    def test_hold_writes_after_the_engine_closed_it(self, tmp_path):
        root = lit_sysfs(tmp_path)
        out = SysfsPwmOutput(root=root)
        out.write(0.5, 0.0, 0.0)
        out.close()
        out.hold(0.047, 0.0, 0.0)
        assert duty(root, 0) == round(0.047 * 1250000) and duty(root, 1) == 0

    def test_open_output_honours_pwm_hz(self, tmp_path, monkeypatch):
        import haslerd.lamp.outputs as outputs

        root = lit_sysfs(tmp_path)
        monkeypatch.setattr(outputs.SysfsPwmOutput.__init__, "__defaults__", (None, "anode", 800, root))
        out = open_output({"backend": "pwm-sysfs", "frequency_hz": 800, "pwm_hz": 400})
        assert out.period_ns == 2500000
        out.close()

    def test_remember_period_for_the_next_boot(self, tmp_path):
        path = tmp_path / "lamp-period"
        assert remember_period(2500000, path) is True
        assert path.read_text() == "2500000\n"
        assert remember_period(2500000, path) is False      # no write when unchanged
        assert remember_period(1, tmp_path / "file" / "x") in (True, False)   # never raises


# -- lamp/handover.py ------------------------------------------------------


class TestHandoverNotes:
    def test_no_boot_script_returns_at_once(self, tmp_path):
        t = time.monotonic()
        assert handover.take_over(tmp_path) is None
        assert time.monotonic() - t < 0.05
        assert not (tmp_path / "takeover").exists()

    def test_asks_a_running_boot_script_and_waits_for_it(self, tmp_path):
        (tmp_path / "boot").write_text(json.dumps({"colour": "amber", "pid": os.getpid()}))

        def boot_script():      # notices the request, lets go
            while not (tmp_path / "takeover").exists():
                time.sleep(0.005)
            (tmp_path / "boot").write_text(json.dumps({"colour": "amber", "pid": None, "released": True}))

        thread = threading.Thread(target=boot_script)
        thread.start()
        t = time.monotonic()
        doc = handover.take_over(tmp_path, wait_s=2.0)
        thread.join()
        assert doc["released"] is True and time.monotonic() - t < 1.0

    def test_a_silent_boot_script_costs_only_the_wait(self, tmp_path):
        (tmp_path / "boot").write_text(json.dumps({"pid": os.getpid()}))
        t = time.monotonic()
        handover.take_over(tmp_path, wait_s=0.1)
        assert 0.09 < time.monotonic() - t < 0.5

    def test_a_dead_boot_script_is_not_waited_for(self, tmp_path):
        (tmp_path / "boot").write_text(json.dumps({"pid": 2 ** 22 + 12345}))
        t = time.monotonic()
        handover.take_over(tmp_path, wait_s=1.0)
        assert time.monotonic() - t < 0.1

    def test_handover_note(self, tmp_path):
        assert handover.write_handover("ember", tmp_path)
        doc = json.loads((tmp_path / "handover").read_text())
        assert doc["mode"] == "ember" and abs(doc["at"] - time.time()) < 5
        assert handover.write_handover("amber", tmp_path / "missing") is False

    def test_the_shutdown_ember_is_steady_pure_red(self):
        r, g, b = handover.hold_duty((1.0, 0.10, 0.0))
        assert r == pytest.approx(0.25 ** 2.2) and g == 0.0 and b == 0.0
        assert handover.hold_duty((1.0, 0.10, 0.0), night=True)[0] < r


# -- the keeper ------------------------------------------------------------


class Engine(FakeDriver):
    """The newer engine's extras, recorded (seed, watchdog, feed)."""

    REPLY_S = {"kick": 0.2, "refuse": 1.0, "refuse3": 1.8, "confirmed": 0.4, "signature": 1.6,
               "swell": 0.8, "stop": 0.8}

    def __init__(self, output=None):
        super().__init__()
        self.output = output if output is not None else FakeOutput()
        self.palette = {"ember": (1.0, 0.10, 0.0)}
        self.seeds, self.armed, self.feeds = [], [], 0
        self.calls = []

    def show(self, look, fade_s=0.0):
        self.calls.append("show")
        super().show(look, fade_s)

    def seed(self, rgb):
        self.calls.append("seed")
        self.seeds.append(rgb)

    def watchdog(self, seconds):
        self.armed.append(seconds)

    def feed(self):
        self.feeds += 1


@pytest.fixture
def lamp(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    clock = Clock()
    actions = Actions(variant, FakePlayer(), clock=clock)
    driver = Engine(FakeOutput(initial=(0.12, 0.018, 0.0)))
    switch = EndSwitch("power", "x", off_hold_s=5.0, bounce_s=0.0)
    keeper = LampKeeper(driver, actions, FakePanel([switch]), FakeNet(), None, clock=clock,
                        night=lambda when, settings: False)
    keeper.run_dir = tmp_path
    return clock, actions, driver, keeper


class TestKeeperHands:
    def test_the_engine_starts_from_the_duty_the_led_shows(self, lamp, monkeypatch):
        import haslerd.main as main

        monkeypatch.setattr(main, "remember_period", lambda *a: False)
        clock, _, driver, keeper = lamp
        keeper.take_over()
        keeper.tick(clock())
        assert driver.seeds == [(0.12, 0.018, 0.0)]
        assert driver.calls[:2] == ["seed", "show"]         # seeded before the first show

    def test_the_daemon_stage_breathes_the_boot_scripts_breath(self, lamp):
        clock, _, driver, keeper = lamp
        keeper.connectivity.seen_up = keeper.connectivity.online = False   # before the network
        (keeper.run_dir / "boot").write_text(json.dumps(
            {"colour": "amber", "floor": 0.25, "peak": 0.60, "period_s": 3.0, "released": True}))
        keeper.take_over()
        keeper.tick(clock())
        look = driver.shows[-1][0]
        assert (look.colour, look.motion, look.floor, look.level, look.period_s) == ("amber", "breath", 0.25, 0.60, 3.0)
        keeper.connectivity.seen_up = keeper.connectivity.online = True    # network: next stage
        clock.t += 1
        keeper.tick(clock())
        assert driver.shows[-1][0] != look                  # warm-up over: the policy's again

    def test_watchdog_armed_after_the_first_show_and_fed_every_tick(self, lamp):
        clock, _, driver, keeper = lamp
        keeper.boot_done()
        for _ in range(10):
            keeper.tick(clock())
            clock.t += 0.05
        assert driver.armed == [3.0]
        assert driver.feeds == 10
        assert len(driver.shows) == 1                       # show only on a change

    def test_an_old_engine_gets_no_watchdog_and_no_feed(self):
        clock = Clock()
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        driver = FakeDriver()
        keeper = LampKeeper(driver, Actions(variant, FakePlayer(), clock=clock), None, FakeNet(), None,
                            clock=clock, night=lambda w, s: False)
        keeper.tick(clock())
        keeper.take_over()                                   # nothing to read, nothing to seed
        assert driver.shows

    def test_replies_in_the_engines_words(self, lamp):
        _, actions, driver, keeper = lamp
        actions.reply("ack")
        actions.reply("fail")
        keeper.reply("confirmed")
        assert driver.replies == ["kick", "refuse3", "confirmed"]

    def test_older_engine_hears_the_older_words(self, box_old):
        driver, keeper = box_old
        keeper.reply("confirmed")
        keeper.reply("refuse3")
        keeper.reply("ack")
        assert driver.replies == ["ack", "fail", "ack"]

    def test_a_missing_panel_board_refuses_once(self, lamp):
        clock, actions, driver, keeper = lamp
        keeper.panel = FakePanel(absent=True)
        for _ in range(3):
            keeper.tick(clock())
        assert driver.replies == ["refuse3"]

    def test_a_pending_confirmation_is_in_the_box_state(self, lamp):
        clock, _, _, keeper = lamp

        class Broker:
            waiting = True

            def pending(self):
                return self.waiting

        keeper.confirm = Broker()
        state = keeper.gather(clock())
        if not hasattr(state, "confirming"):
            pytest.skip("this BoxState has no confirming")
        assert state.confirming is True

    def test_shutdown_leaves_a_steady_ember_for_the_halt_hook(self, lamp):
        _, actions, driver, keeper = lamp
        actions.shutting_down = True
        keeper.close()
        assert driver.closed is False
        assert driver.output.held == handover.hold_duty((1.0, 0.10, 0.0))
        assert json.loads((keeper.run_dir / "handover").read_text())["mode"] == "hold"

    def test_an_update_restart_hands_over_the_ember_breath(self, lamp):
        _, _, driver, keeper = lamp
        keeper.updating = lambda: True
        keeper.close()
        assert driver.closed is False and driver.output.held is None
        assert json.loads((keeper.run_dir / "handover").read_text())["mode"] == "ember"


@pytest.fixture
def box_old(tmp_path):
    clock = Clock()
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    driver = FakeDriver()
    keeper = LampKeeper(driver, Actions(variant, FakePlayer(), clock=clock), None, FakeNet(), None,
                        clock=clock, night=lambda w, s: False)
    import haslerd.main as main
    if "ack" not in getattr(__import__("haslerd.lamp.engine", fromlist=["REPLY_S"]), "REPLY_S", {}):
        pytest.skip("the engine in this tree speaks the newer words")
    assert main
    return driver, keeper


# -- the Excelsior's key 0 ---------------------------------------------------


def test_excelsior_has_the_hat_lamp():
    hat = load_variant(VARIANTS / "hasler-hm-hat.yaml").io["lamp"]
    exc = load_variant(VARIANTS / "excelsior.yaml").io["lamp"]
    for key in ("backend", "pins", "common", "frequency_hz", "min_duty"):
        assert exc[key] == hat[key]
    assert exc["pins"] == {"red": 17, "green": 22, "blue": 27}


def test_min_duty_floor_in_every_variant_with_a_lamp():
    for name in ("hasler-hm.yaml", "hasler-hm-hat.yaml", "excelsior.yaml"):
        assert load_variant(VARIANTS / name).io["lamp"]["min_duty"] == {"red": 0.006, "green": 0.006, "blue": 0.006}
        assert "pwm_hz" not in load_variant(VARIANTS / name).io["lamp"]      # an option, not the default


def test_excelsior_key_0_held_counts_down_like_the_hm_knob():
    from haslerd.variant import Panel

    variant = load_variant(VARIANTS / "excelsior.yaml")
    clock = Clock(0.0)
    panel = Panel.from_variant(variant, fake=True)
    k0 = next(c for c in panel.controls if c.id == "k0")
    assert k0.hold_s == 5.0
    player = FakePlayer()
    actions = Actions(variant, player, clock=clock)
    driver = FakeDriver()
    keeper = LampKeeper(driver, actions, panel, FakeNet(), None, clock=clock, night=lambda w, s: False)
    keeper.boot_done()
    keeper.arm()
    panel.digital.set("d0", True)
    while clock.t < 6.0:
        events = panel.poll(clock.t)
        actions.dispatch_all(panel.take_downs())
        actions.dispatch_all(events)
        keeper.tick(clock.t)
        clock.t = round(clock.t + 0.05, 3)
    counts = [c for c in driver.counts if c is not None]
    assert counts and counts == sorted(counts)              # dims steadily ...
    assert 0.0 <= counts[0] < 0.05 and counts[-1] > 0.95    # ... over the 5 s
    assert ("shutdown",) in player.calls and actions.shutting_down


def test_excelsior_key_0_tapped_is_standby_and_cancels_the_countdown():
    from haslerd.variant import Panel

    variant = load_variant(VARIANTS / "excelsior.yaml")
    clock = Clock(0.0)
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer(), clock=clock)
    driver = FakeDriver()
    keeper = LampKeeper(driver, actions, panel, FakeNet(), None, clock=clock, night=lambda w, s: False)
    keeper.arm()
    for i in range(40):
        panel.digital.set("d0", 2 <= i < 12)                 # held 0.5 s
        events = panel.poll(clock.t)
        actions.dispatch_all(panel.take_downs())
        actions.dispatch_all(events)
        keeper.tick(clock.t)
        clock.t = round(clock.t + 0.05, 3)
    assert actions.standby and not actions.shutting_down
    assert driver.counts[-1] is None and max(c for c in driver.counts if c is not None) < 0.15


# -- scripts/hasler-lamp-boot ------------------------------------------------


def run_boot(tmp_path, *args, env=None, timeout=10):
    full = {**os.environ, "HASLER_PWM_ROOT": str(tmp_path / "pwm"), "HASLER_LAMP_RUN": str(tmp_path / "run"),
            "HASLER_LAMP_PERIOD_FILE": str(tmp_path / "lamp-period"), **(env or {})}
    return subprocess.run([sys.executable, str(BOOT_SCRIPT), *args], env=full, capture_output=True,
                          text=True, timeout=timeout)


def frames(out):
    return [float(line.split("level=")[1].split()[0]) for line in out.splitlines() if "level=" in line]


class TestBootScript:
    def test_cold_boot_fades_up_from_dark_at_the_remembered_period(self, tmp_path):
        (tmp_path / "lamp-period").write_text("2500000\n")
        res = run_boot(tmp_path, "--fake-sysfs", "--print", "--seconds", "0.3")
        assert res.returncode == 0, res.stderr
        levels = frames(res.stdout)
        assert levels[0] < 0.01 and levels == sorted(levels)
        assert (tmp_path / "pwm" / "pwmchip0" / "pwm0" / "period").read_text().strip() == "2500000"
        state = json.loads((tmp_path / "run" / "boot").read_text())
        assert state["released"] is True and (state["floor"], state["peak"]) == (0.22, 0.70)

    def test_a_handover_breathes_on_from_the_leds_frame(self, tmp_path):
        root = lit_sysfs(tmp_path, period=2500000, duties=(int(0.3 * 2500000), 0, 0))
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "handover").write_text(json.dumps({"mode": "amber", "at": time.time()}))
        res = run_boot(tmp_path, "--print", "--seconds", "1.5")
        levels = frames(res.stdout)
        start = 0.3 ** (1 / 2.2)
        assert abs(levels[0] - start) < 0.02                # no dark frame, no jump
        assert max(abs(a - b) for a, b in zip(levels, levels[1:])) < 0.05
        assert 0.25 - 0.01 <= levels[-1] <= 0.60 + 0.01
        assert (root / "pwmchip0" / "pwm0" / "period").read_text().strip() == "2500000"   # kept
        assert not (tmp_path / "run" / "handover").exists()

    def test_an_update_handover_is_the_amber_busy_breath(self, tmp_path):
        root = lit_sysfs(tmp_path, duties=(60000, 0, 0))
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "handover").write_text(json.dumps({"mode": "ember", "at": time.time()}))
        res = run_boot(tmp_path, "--print", "--seconds", "1.2")
        levels = frames(res.stdout)
        assert 0.21 <= levels[-1] <= 0.71         # the busy breath, 22-70 %, amber (v2: never ember)
        assert duty(root, 1) > 0 and duty(root, 2) == 0
        assert json.loads((tmp_path / "run" / "boot").read_text())["colour"] == "amber"

    def test_a_stale_note_is_not_a_handover(self, tmp_path):
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "handover").write_text(json.dumps({"mode": "ember", "at": time.time() - 3600}))
        res = run_boot(tmp_path, "--fake-sysfs", "--print", "--seconds", "0.2")
        assert frames(res.stdout)[0] < 0.01                  # the cold fade

    def test_lets_go_when_haslerd_asks(self, tmp_path):
        full = {**os.environ, "HASLER_PWM_ROOT": str(tmp_path / "pwm"), "HASLER_LAMP_RUN": str(tmp_path / "run"),
                "HASLER_LAMP_PERIOD_FILE": str(tmp_path / "none")}
        proc = subprocess.Popen([sys.executable, str(BOOT_SCRIPT), "--fake-sysfs"], env=full,
                                stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            doc = None
            while time.monotonic() < deadline:
                doc = handover.read_boot(tmp_path / "run")
                if doc and doc.get("pid") == proc.pid:
                    break
                time.sleep(0.02)
            assert doc and doc["pid"] == proc.pid
            time.sleep(0.3)
            before = duty(tmp_path / "pwm", 0)
            t = time.monotonic()
            doc = handover.take_over(tmp_path / "run", wait_s=2.0)
            took = time.monotonic() - t
            proc.wait(timeout=2)
        finally:
            proc.kill()
        assert doc["released"] is True and took < 0.2
        assert duty(tmp_path / "pwm", 0) >= before > 0      # left lit, exactly as it was
        assert not (tmp_path / "run" / "takeover").exists()

    @pytest.fixture
    def systemctl(self, tmp_path):
        log = tmp_path / "systemctl.log"
        fake = tmp_path / "systemctl"
        fake.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n"
                        f"[ \"$1\" = is-system-running ] && echo \"${{STATE:-running}}\"\nexit 0\n")
        fake.chmod(0o755)
        return fake, log

    def test_handover_step_starts_the_breath_between_two_haslerds(self, tmp_path, systemctl):
        fake, log = systemctl
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "handover").write_text(json.dumps({"mode": "ember", "at": 0}))
        run_boot(tmp_path, "--handover", env={"HASLER_SYSTEMCTL": str(fake)})
        assert "--no-block start hasler-lamp-boot.service" in log.read_text()
        note = json.loads((tmp_path / "run" / "handover").read_text())
        assert note["mode"] == "ember" and time.time() - note["at"] < 5   # fresh for the service

    def test_handover_after_a_crash_without_a_note_is_amber(self, tmp_path, systemctl):
        fake, log = systemctl
        run_boot(tmp_path, "--handover", env={"HASLER_SYSTEMCTL": str(fake)})
        assert "start hasler-lamp-boot.service" in log.read_text()
        assert json.loads((tmp_path / "run" / "handover").read_text())["mode"] == "amber"

    def test_no_handover_at_a_shutdown(self, tmp_path, systemctl):
        fake, log = systemctl
        (tmp_path / "run").mkdir()
        (tmp_path / "run" / "handover").write_text(json.dumps({"mode": "hold", "at": time.time()}))
        run_boot(tmp_path, "--handover", env={"HASLER_SYSTEMCTL": str(fake)})
        assert "start" not in log.read_text() if log.exists() else True
        (tmp_path / "run" / "handover").write_text(json.dumps({"mode": "amber", "at": time.time()}))
        run_boot(tmp_path, "--handover", env={"HASLER_SYSTEMCTL": str(fake), "STATE": "stopping"})
        assert "start hasler" not in log.read_text()


def test_units_hand_the_lamp_over():
    config = Path(__file__).resolve().parents[2] / "config"
    for unit in (config / "haslerd.service", config / "legacy" / "haslerd.service"):
        text = unit.read_text()
        assert "ExecStartPre=-+/usr/bin/systemctl stop hasler-lamp-boot" not in text
        assert "ExecStartPost=-+/usr/bin/systemctl stop hasler-lamp-boot.service" in text
        assert "ExecStopPost=-+/usr/local/sbin/hasler-lamp-boot --handover" in text
