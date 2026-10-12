"""Running with nobody watching: state on disk, network checks, boot, wiring."""

import json
import os

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.main import boot
from haslerd.net import Connectivity
from haslerd.player.base import FakePlayer
from haslerd.state import StateStore, resolve_state_path
from haslerd.variant import Panel, gpio_mapping, load_variant, open_backends


class TestStateStore:
    def test_round_trip(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        store.save({"last_preset": 3, "standby": False})
        assert StateStore(tmp_path / "state.json").load() == {"last_preset": 3, "standby": False}

    def test_missing_or_corrupt_file_means_fresh_start(self, tmp_path):
        path = tmp_path / "state.json"
        assert StateStore(path).load() == {}
        path.write_text("{half a json")
        assert StateStore(path).load() == {}
        path.write_text("[1, 2]")
        assert StateStore(path).load() == {}

    def test_write_is_atomic_and_leaves_no_temp_files(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(path)
        for n in range(5):
            store.save({"last_preset": n, "standby": False})
        assert json.loads(path.read_text())["last_preset"] == 4
        assert os.listdir(tmp_path) == ["state.json"]

    def test_unchanged_state_is_not_rewritten(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(path)
        store.save({"last_preset": 1, "standby": False})
        path.unlink()
        store.save({"last_preset": 1, "standby": False})
        assert not path.exists()

    def test_unwritable_directory_is_logged_not_raised(self, tmp_path):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        try:
            StateStore(ro / "state.json").save({"last_preset": 1, "standby": False})
        finally:
            ro.chmod(0o700)

    def test_in_memory_store_for_fake_runs(self):
        store = StateStore(None)
        store.save({"last_preset": 2, "standby": True})
        assert store.load() == {"last_preset": 2, "standby": True}

    def test_configured_path_used_when_writable(self, tmp_path):
        target = tmp_path / "sub" / "state.json"
        assert resolve_state_path(target) == target

    def test_falls_back_to_the_user_state_dir(self, tmp_path, monkeypatch):
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o500)
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        try:
            path = resolve_state_path(ro / "nested" / "state.json")
        finally:
            ro.chmod(0o700)
        assert path == tmp_path / "home" / ".local/state/haslerd/state.json"


class TestBoot:
    def setup_method(self):
        self.variant = load_variant(VARIANTS / "hasler-hm.yaml")

    def test_boot_resumes_the_last_preset(self, tmp_path):
        StateStore(tmp_path / "s.json").save({"last_preset": 4, "standby": False})
        player = FakePlayer()
        actions = Actions(self.variant, player, state=StateStore(tmp_path / "s.json"))
        panel = Panel.from_variant(self.variant, fake=True)
        panel.digital.set("d8", True)                # knob on (end-switch, PB0)
        assert boot(panel, actions, settle_s=0.1) is True
        assert player.current().n == 4

    def test_knob_off_at_power_up_means_no_music(self, tmp_path):
        StateStore(tmp_path / "s.json").save({"last_preset": 4, "standby": False})
        player = FakePlayer()
        actions = Actions(self.variant, player, state=StateStore(tmp_path / "s.json"))
        panel = Panel.from_variant(self.variant, fake=True)  # d8 open: knob off
        boot(panel, actions, settle_s=0.1)
        assert actions.standby is True
        assert "play_preset" not in player.verbs()
        assert StateStore(tmp_path / "s.json").load()["standby"] is True


class TestConnectivity:
    def test_probes_on_the_interval_only(self):
        results = iter([True, False, True])
        probes = []

        def probe():
            probes.append(1)
            return next(results)

        net = Connectivity(probe=probe, interval_s=10, threaded=False)
        assert net.poll(0.0) is True
        assert net.poll(5.0) is True and len(probes) == 1
        assert net.poll(10.0) is False
        assert net.poll(20.0) is True
        assert len(probes) == 3

    def test_starts_optimistic_and_a_raising_probe_means_offline(self):
        def probe():
            raise OSError("no route")

        net = Connectivity(probe=probe, threaded=False)
        assert net.online is True
        assert net.poll(0.0) is False

    def test_threaded_probe_never_blocks_the_loop(self):
        import threading
        import time

        gate = threading.Event()
        net = Connectivity(probe=lambda: gate.wait(5) and False, interval_s=0.0)
        start = time.monotonic()
        net.poll(0.0)
        net.poll(1.0)                              # still in flight: no second thread
        assert time.monotonic() - start < 0.1
        gate.set()


# Mirrors the commented-out fallback block in config/variants/hasler-hm.yaml.
GPIO_FALLBACK = {"backend": "gpio", "pins": [5, 6, 12, 13, 16, 17, 24, 26, 25]}


class TestGpioPinIndex:
    def test_logical_index_picks_the_pin_not_the_order_referenced(self):
        # d0, d2, d5 referenced; zip() would have put d2 on pin 6 and d5 on 12.
        assert gpio_mapping(["d0", "d2", "d5"], [5, 6, 12, 13, 16, 17]) == {"d0": 5, "d2": 12, "d5": 17}

    def test_index_beyond_the_list_is_rejected(self):
        with pytest.raises(ValueError, match=r"pins\[8\]"):
            gpio_mapping(["d8"], [5, 6, 12])

    def test_non_dn_names_are_rejected(self):
        with pytest.raises(ValueError, match="form dN"):
            gpio_mapping(["k1"], [5])

    def test_open_backends_uses_the_index_mapping(self, monkeypatch):
        import haslerd.panel.io_gpio as io_gpio

        seen = {}

        class FakeGpio:
            def __init__(self, mapping):
                seen.update(mapping)

        monkeypatch.setattr(io_gpio, "GpioDigital", FakeGpio)
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        variant.io["digital"] = dict(GPIO_FALLBACK)
        variant.io["analog"] = {"backend": "none"}   # no ADS1115 on the laptop
        variant.controls = [{"id": "k1", "kind": "momentary", "pin": "d3"}]
        open_backends(variant)
        assert seen == {"d3": variant.io["digital"]["pins"][3]}

    def test_gpio_fallback_keeps_the_original_wiring(self, monkeypatch):
        # The commented fallback in hasler-hm.yaml: going back to direct GPIO
        # must land every key, and the end-switch (now d8), on its old pin.
        import haslerd.panel.io_gpio as io_gpio

        seen = {}
        monkeypatch.setattr(io_gpio, "GpioDigital", lambda m: seen.update(m))
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        variant.io["digital"] = dict(GPIO_FALLBACK)
        variant.io["analog"] = {"backend": "none"}   # no ADS1115 on the laptop
        open_backends(variant)
        assert seen == {"d0": 5, "d1": 6, "d2": 12, "d3": 13, "d4": 16, "d5": 17, "d6": 24, "d8": 25}
