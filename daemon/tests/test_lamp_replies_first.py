"""The lamp answers the hand, not the player (actions.py "Replies come first",
panel/controls.py "THE PRESS HINT")."""

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.panel.controls import Event, build_controls
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web.server import WebApp


class LoggingPlayer(FakePlayer):
    """Puts the slow calls in the same log as the lamp's replies."""

    def __init__(self, log):
        super().__init__()
        self.log = log

    def active_source(self):
        self.log.append("active_source")
        return super().active_source()

    def select_source(self, source):
        self.log.append(f"select_source:{source}")
        super().select_source(source)

    def set_mute(self, muted):
        self.log.append(f"set_mute:{muted}")
        super().set_mute(muted)

    def play_preset(self, preset):
        self.log.append("play_preset")
        super().play_preset(preset)


@pytest.fixture
def hm():
    log = []
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    clock = [100.0]
    actions = Actions(variant, LoggingPlayer(log), clock=lambda: clock[0])
    actions.on_reply = lambda name: log.append(f"reply:{name}")
    return actions, log, clock


def first(log, prefix):
    return next(i for i, entry in enumerate(log) if entry.startswith(prefix))


class TestRepliesBeforeSlowCalls:
    def test_a_preset_key_dips_before_asking_the_player(self, hm):
        actions, log, _ = hm
        actions.dispatch(Event("k1", "tap"))
        assert first(log, "reply:ack") < first(log, "active_source") < first(log, "play_preset")
        assert log.count("reply:ack") == 1

    def test_again_still_waits_for_the_player_to_say_stop(self, hm):
        actions, log, clock = hm
        actions.dispatch(Event("k1", "tap"))
        clock[0] += 2
        log.clear()
        actions.dispatch(Event("k1", "tap"))
        assert [e for e in log if e.startswith("reply")] == ["reply:stop"]

    def test_a_preset_whose_source_died_is_not_again_and_dips_once(self, hm):
        actions, log, clock = hm
        actions.dispatch(Event("k1", "tap"))
        actions.player.source = "airplay"           # a phone took over
        clock[0] += 2
        log.clear()
        actions.dispatch(Event("k1", "tap"))
        assert [e for e in log if e.startswith("reply")] == ["reply:ack"]
        assert first(log, "reply:ack") < first(log, "select_source")

    def test_pu_signature_before_select_source_and_waking(self, hm):
        actions, log, _ = hm
        actions.standby = True
        actions.dispatch(Event("pu", "tap"))
        reply = first(log, "reply:source:")
        assert reply < first(log, "select_source") and reply < first(log, "set_mute")

    def test_pu_signature_says_where_it_resumes(self, hm):
        actions, log, _ = hm
        actions.dispatch(Event("k1", "tap"))
        actions.player.stop()
        actions.source_cycle, actions._cycle_at = ("radio", "bluetooth"), "bluetooth"
        log.clear()
        actions.dispatch(Event("pu", "tap"))
        assert log[first(log, "reply:")] == "reply:source:radio:radio"
        assert first(log, "reply:") < first(log, "select_source")

    def test_bluetooth_key_blue_before_select_source(self):
        log = []
        variant = load_variant(VARIANTS / "excelsior.yaml")
        actions = Actions(variant, LoggingPlayer(log))
        actions.on_reply = lambda name: log.append(f"reply:{name}")
        actions.dispatch(Event("pu", "tap"))
        assert first(log, "reply:source:bluetooth") < first(log, "select_source:bluetooth")

    def test_test_button_dips_and_breathes_like_a_key(self, hm, tmp_path):
        actions, log, _ = hm
        started = []
        actions.on_start = lambda preset: log.append("loading")
        panel = Panel.from_variant(actions.variant, fake=True)
        app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False)
        app.play({"name": "Try", "source": "radio", "uri": "http://x.example/s"})
        assert log.index("reply:ack") < log.index("loading") < log.index("play_preset")
        assert not started

    def test_test_button_failing_pulses_red_and_tells_the_page(self, hm, tmp_path):
        actions, log, _ = hm
        actions.player.fail_play = RuntimeError("no")
        app = WebApp(actions, Panel.from_variant(actions.variant, fake=True), tmp_path / "k.yaml",
                     {"radio": True}, ha=False)
        with pytest.raises(RuntimeError):
            app.play({"name": "Try", "source": "radio", "uri": "http://x.example/s"})
        assert log[-1] == "reply:fail"


class TestPressHint:
    def keys(self, spec):
        return build_controls([spec])

    def test_a_key_hints_on_the_first_closed_poll_and_once_per_press(self):
        (key,) = self.keys({"id": "k1", "kind": "momentary", "pin": "d1"})
        seq = [False, True, False, True, True, True, False, False, False, False, True, True, True, False, False, False]
        events = []
        for i, closed in enumerate(seq):
            events += [(i, str(e)) for e in key.update(i * 0.02, {"d1": closed}, {})]
        downs = [i for i, e in events if e == "k1.down"]
        assert downs == [1, 10]                     # bounce on the way down: one hint
        assert [e for _, e in events].count("k1.tap") == 2

    def test_a_glitch_hints_but_never_acts(self):
        (key,) = self.keys({"id": "k1", "kind": "momentary", "pin": "d1"})
        events = [str(e) for i, c in enumerate([False, True, False, False, False])
                  for e in key.update(i * 0.02, {"d1": c}, {})]
        assert events == ["k1.down"]

    def test_hand_built_controls_have_no_hint(self):
        from haslerd.panel.controls import Momentary

        key = Momentary("k1", "d1")
        assert [str(e) for i in range(5) for e in key.update(i * 0.02, {"d1": True}, {})] == []

    def test_a_latching_bank_hints_the_incoming_key_not_the_one_down(self):
        (bank,) = self.keys({"id": "presets", "kind": "interlocked_bank", "pins": ["a", "b"], "settle_ms": 60})
        out = []
        t = 0.0
        for raw in [(1, 0)] * 6 + [(0, 0)] * 1 + [(0, 1)] * 6:
            out += [str(e) for e in bank.update(t, {"a": bool(raw[0]), "b": bool(raw[1])}, {})]
            t += 0.02
        assert out == ["presets.down=0", "presets.select=0", "presets.down=1", "presets.select=1"]

    def test_panel_keeps_hints_out_of_its_events(self):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        panel = Panel.from_variant(variant, fake=True)
        pin = next(c["pin"] for c in variant.controls if c["id"] == "k2")
        panel.digital.set(pin, True)
        events = panel.poll(0.0)
        assert all(e.kind != "down" for e in events)
        assert [str(e) for e in panel.take_downs()] == ["k2.down"]
        assert panel.take_downs() == []

    def test_hushed_keys_do_not_hint(self):
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        panel = Panel.from_variant(variant, fake=True)
        panel.hush(10.0)
        panel.digital.set(next(c["pin"] for c in variant.controls if c["id"] == "k2"), True)
        panel.poll(0.0)
        assert panel.take_downs() == []


class TestKick:
    def test_only_bound_keys_kick(self, hm):
        actions, log, _ = hm
        actions.dispatch(Event("k1", "down"))
        actions.dispatch(Event("nothing", "down"))
        assert log == ["reply:ack"]                 # and nothing played
        assert actions.player.calls == []

    def test_the_tap_after_a_kick_does_not_dip_again_but_stop_still_shows(self, hm):
        actions, log, clock = hm
        actions.dispatch(Event("k1", "down"))
        actions.dispatch(Event("k1", "tap"))
        assert [e for e in log if e.startswith("reply")] == ["reply:ack"]
        clock[0] += 2
        log.clear()
        actions.dispatch(Event("k1", "down"))
        actions.dispatch(Event("k1", "tap"))         # again: stop
        assert [e for e in log if e.startswith("reply")] == ["reply:ack", "reply:stop"]

    def test_a_bouncing_press_kicks_once(self, hm, monkeypatch):
        import haslerd.actions as actions_mod

        monkeypatch.setattr(actions_mod, "TAP_BOUNCE_S", 0.5)   # conftest turns it off
        actions, log, clock = hm
        actions.dispatch(Event("k1", "down"))
        clock[0] += 0.1
        actions.dispatch(Event("k1", "down"))
        assert log == ["reply:ack"]

    def test_a_kick_long_ago_does_not_mute_a_later_tap(self, hm):
        actions, log, clock = hm
        actions.dispatch(Event("k3", "down"))
        clock[0] += 5
        actions.dispatch(Event("k1", "tap"))
        assert log.count("reply:ack") == 2

    def test_no_kick_while_shutting_down(self, hm):
        actions, log, _ = hm
        actions.shutting_down = True
        actions.dispatch(Event("k1", "down"))
        assert log == []

    def test_excelsior_bank_kick_then_select_dips_once(self):
        log = []
        variant = load_variant(VARIANTS / "excelsior.yaml")
        actions = Actions(variant, LoggingPlayer(log))
        actions.on_reply = lambda name: log.append(f"reply:{name}")
        actions.dispatch(Event("presets", "down", 2))
        actions.dispatch(Event("presets", "select", 2))
        assert [e for e in log if e.startswith("reply")] == ["reply:ack"]
        assert "play_preset" in log
