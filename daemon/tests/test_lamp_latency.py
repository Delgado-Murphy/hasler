"""How fast the pilot lamp answers - measured, not eyeballed (docs/lamp.md).

Two numbers, each against the old behaviour on the same simulated clock:

* press -> first changed lamp frame: the panel polled at 50 Hz, the key's
  press hint (panel/controls.py) answered by Actions.key_down, a real
  LampEngine (no thread, fake output) rendering a frame every 20 ms. Target
  < 50 ms whatever the poll phase; before, the dip waited for the tap, i.e.
  for the key to be let go.
* a phone casting -> the lamp told blue: moOde's renderer flag set at t, the
  keeper ticked every 50 ms. Target < 0.6 s plus the engine's fade; before,
  the 2 s source poll behind the player's 1 s cache.

Run with ``-s`` to see the numbers.
"""

import inspect

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.lamp import policy
from haslerd.lamp.engine import LampEngine
from haslerd.lamp.outputs import FakeOutput
from haslerd.main import LampKeeper
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant

POLL_S = 0.02           # main.py POLL_HZ
FRAME_S = 0.02          # the engine's 50 Hz
TAP_S = 0.15            # a quick, deliberate tap: down this long


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeNet:
    online, hotspot, seen_up = True, False, True


def engine_without_thread(clock, output):
    params = inspect.signature(LampEngine).parameters
    if "threaded" not in params or not hasattr(LampEngine, "step"):
        pytest.skip("this lamp engine can't be stepped by hand")
    return LampEngine(output, clock=clock, threaded=False, min_duty=(0.0, 0.0, 0.0))


def rig(clock):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    actions = Actions(variant, player, clock=clock)
    output = FakeOutput(clock=clock)
    lamp = engine_without_thread(clock, output)
    keeper = LampKeeper(lamp, actions, panel, FakeNet(), None, clock=clock,
                        night=lambda when, settings: False)
    keeper.boot_done()
    keeper.arm()
    return panel, actions, player, lamp, output, keeper


def press_to_frame(phase: float, use_hint: bool) -> float:
    """Seconds from the contact closing to the first lamp frame that differs
    from the steady one. ``use_hint=False`` is the old main loop (no
    take_downs: the dip came with the tap)."""
    clock = Clock()
    panel, actions, player, lamp, output, keeper = rig(clock)
    pin = next(c["pin"] for c in actions.variant.controls if c["id"] == "k1")
    # The tone knob on (its end switch closed), then settle into ready.
    for spec in actions.variant.controls:
        if spec.get("end_switch"):
            panel.digital.set(spec["end_switch"]["pin"], True)
    t, next_frame = clock.t, clock.t
    while clock.t < t + 3.0:
        actions.dispatch_all(panel.poll(clock.t))
        panel.take_downs()
        keeper.tick(clock.t)
        lamp.step(clock.t)
        clock.t = round(clock.t + POLL_S, 6)
    steady = output.last
    assert steady is not None and steady[0] > 0
    pressed_at = clock.t + phase            # between two polls
    released_at = pressed_at + TAP_S
    next_poll, next_frame = clock.t, clock.t + phase / 2
    end = pressed_at + 1.0
    while clock.t < end:
        now = clock.t
        panel.digital.set(pin, pressed_at <= now < released_at)
        if now >= next_poll - 1e-9:
            next_poll += POLL_S
            events = panel.poll(now)
            downs = panel.take_downs()
            if use_hint:
                actions.dispatch_all(downs)
            actions.dispatch_all(events)
        if now >= next_frame - 1e-9:
            next_frame += FRAME_S
            lamp.step(now)
            if output.last != steady:
                return now - pressed_at
        clock.t = round(now + 0.001, 6)
    raise AssertionError("the lamp never answered")


PHASES = [i * POLL_S / 10 for i in range(10)]


def test_press_to_first_changed_frame_is_under_50_ms():
    after = [press_to_frame(p, use_hint=True) for p in PHASES]
    before = [press_to_frame(p, use_hint=False) for p in PHASES]
    print(f"\npress -> first changed frame, over {len(PHASES)} poll phases:"
          f"\n  before (dip on the tap, {TAP_S * 1000:.0f} ms press): "
          f"{min(before) * 1000:.0f}-{max(before) * 1000:.0f} ms"
          f"\n  after  (dip on the press hint): {min(after) * 1000:.0f}-{max(after) * 1000:.0f} ms")
    assert max(after) < 0.050
    assert min(before) > TAP_S          # the old dip came after letting go


def test_one_dip_per_press_not_two():
    clock = Clock()
    panel, actions, player, lamp, output, keeper = rig(clock)
    replies = []
    actions.on_reply = replies.append
    pin = next(c["pin"] for c in actions.variant.controls if c["id"] == "k1")
    for i in range(20):
        now = clock.t + i * POLL_S
        panel.digital.set(pin, i < 8)
        events = panel.poll(now)
        actions.dispatch_all(panel.take_downs())
        actions.dispatch_all(events)
    assert replies == ["ack"]                       # the hint's dip; the tap's is not drawn again
    assert ("play_preset", 1, "radio") in player.calls   # ... and the tap still played


class CastingPlayer(FakePlayer):
    """moOde as the keeper sees it: renderer flags in sqlite, active_source
    behind a 1 s cache (player/moode.py SOURCE_CACHE_S)."""

    def __init__(self, clock, flags=True):
        super().__init__()
        self.clock = clock
        self.bt_flag = ""
        self._cache = None
        if not flags:
            self.renderer_flags = None      # an older player: no cheap flags

    def renderer_flags(self):
        return {"bluetooth": self.bt_flag, "airplay": ""}

    def forget_source(self):
        self._cache = None

    def active_source(self):
        now = self.clock()
        if self._cache is not None and now - self._cache[0] < 1.0:
            return self._cache[1]
        source = "bluetooth" if self.bt_flag == "1" else "radio"
        self._cache = (now, source)
        return source


def cast_to_blue(offset: float, flags: bool) -> float:
    clock = Clock()
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    player = CastingPlayer(clock, flags)
    actions = Actions(variant, player, clock=clock)
    shows = []

    class Driver:
        def show(self, look, fade_s=0.0):
            shows.append((clock.t, look))

        def reply(self, name):
            pass

        def countdown(self, progress):
            pass

        def set_night(self, night):
            pass

    keeper = LampKeeper(Driver(), actions, None, FakeNet(), None, clock=clock,
                        night=lambda when, settings: False)
    keeper.boot_done()
    start = clock.t
    while clock.t < start + 3.0 + offset:
        keeper.tick(clock.t)
        clock.t = round(clock.t + 0.05, 6)
    cast_at = clock.t - 0.025                   # the flag flips between two ticks
    player.bt_flag = "1"
    while clock.t < cast_at + 5.0:
        keeper.tick(clock.t)
        if shows and shows[-1][1] == policy.CASTING:
            return shows[-1][0] - cast_at
        clock.t = round(clock.t + 0.05, 6)
    raise AssertionError("never blue")


def test_a_phone_casting_turns_the_lamp_blue_within_0_6_s():
    offsets = [i * 0.1 for i in range(20)]      # every phase of the 2 s poll
    after = [cast_to_blue(o, flags=True) for o in offsets]
    before = [cast_to_blue(o, flags=False) for o in offsets]
    print(f"\ncast -> lamp told blue (then the engine's {policy.FADE_CAST} s fade):"
          f"\n  before (2 s source poll): {min(before):.2f}-{max(before):.2f} s"
          f"\n  after  (renderer flags every 0.25 s): {min(after):.2f}-{max(after):.2f} s")
    assert max(after) < 0.6
    assert max(before) > 1.0
