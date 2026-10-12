"""The action table, against a fake player. No Pi, no MPD, no network."""

import logging

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import load_variant


@pytest.fixture
def hasler():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    player = FakePlayer()
    return variant, player, Actions(variant, player)


@pytest.fixture
def excelsior():
    variant = load_variant(VARIANTS / "excelsior.yaml")
    player = FakePlayer()
    return variant, player, Actions(variant, player)


class TestPresets:
    def test_tap_plays_the_matching_station(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("k3", "tap"))
        assert player.playing.n == 3

    def test_tapping_the_playing_station_again_stops_it(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("k3", "tap"))
        actions.dispatch(Event("k3", "tap"))
        assert player.playing is None
        assert actions.standby is True

    def test_tapping_a_different_station_switches_rather_than_stopping(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("k3", "tap"))
        actions.dispatch(Event("k5", "tap"))
        assert player.playing.n == 5
        assert "stop" not in player.verbs()

    def test_preset_key_takes_the_audio_path_back_from_a_renderer(self, hasler):
        _, player, actions = hasler
        player.source = "bluetooth"
        actions.dispatch(Event("k2", "tap"))
        assert ("select_source", "radio") in player.calls
        assert player.playing.n == 2

    def test_preset_key_wakes_the_box_from_standby(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("power", "off"))
        assert actions.standby is True
        actions.dispatch(Event("k1", "tap"))
        assert actions.standby is False
        assert player.muted is False
        assert player.playing.n == 1

    def test_missing_preset_is_logged_not_fatal(self, hasler):
        variant, player, actions = hasler
        variant.actions["k1.tap"] = {"do": "preset", "n": 99}
        actions.dispatch(Event("k1", "tap"))
        assert player.playing is None


class TestStandbyAndPower:
    def test_knob_off_silences_but_stays_on_the_network(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("power", "off"))
        assert actions.standby is True
        assert player.muted is True
        assert player.shutdown_called is False, "standby must not power the box down"

    def test_knob_back_on_resumes_the_last_station(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("k4", "tap"))
        actions.dispatch(Event("power", "off"))
        actions.dispatch(Event("power", "on"))
        assert actions.standby is False
        assert player.muted is False
        assert player.playing.n == 4

    def test_holding_the_knob_off_shuts_down_properly(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("power", "off"))
        actions.dispatch(Event("power", "off_held"))
        assert player.shutdown_called is True
        assert player.verbs().index("stop") < player.verbs().index("shutdown")


class TestSources:
    def test_pu_key_cycles_through_the_renderers(self, hasler):
        _, player, actions = hasler
        seen = []
        for _ in range(4):
            actions.dispatch(Event("pu", "tap"))
            seen.append(player.active_source())
        assert seen == ["bluetooth", "airplay", "spotify", "radio"]

    def test_cycling_home_to_radio_restores_the_last_station(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("k1", "tap"))       # a radio preset in the shipped set
        for _ in range(4):
            actions.dispatch(Event("pu", "tap"))
        assert player.active_source() == "radio"
        assert player.playing.n == 1

    def test_holding_pu_opens_bluetooth_pairing(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("pu", "hold"))
        assert ("bluetooth_pair", 120) in player.calls


class TestWheels:
    def test_volume_change_reaches_the_mixer(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("volume", "change", 42.0))
        assert player.volume == 42.0

    def test_tone_change_is_tagged_with_its_control(self, excelsior):
        _, player, actions = excelsior
        actions.dispatch(Event("treble", "change", 60.0))
        actions.dispatch(Event("bass", "change", 30.0))
        assert player.tone == {"treble": 60.0, "bass": 30.0}

    def test_speaker_selector_routes(self, excelsior):
        _, player, actions = excelsior
        actions.dispatch(Event("speaker_sel", "position", 2))
        assert player.speaker == 2


class TestExcelsiorBindsToTheSameVerbs:
    """The interlocked bank drives presets without any cabinet-specific code."""

    def test_bank_selection_plays_a_preset(self, excelsior):
        variant, player, actions = excelsior
        variant.presets = [{"n": i, "name": f"P{i}", "url": f"http://x/{i}"} for i in range(1, 8)]
        actions = Actions(variant, player)
        actions.dispatch(Event("presets", "select", 2))   # 0-based -> preset 3
        assert player.playing.n == 3

    def test_bank_clear_drops_to_standby(self, excelsior):
        _, player, actions = excelsior
        actions.dispatch(Event("presets", "clear"))
        assert actions.standby is True

    def test_sp_toggle_mutes(self, excelsior):
        _, player, actions = excelsior
        actions.dispatch(Event("sp", "on"))
        assert player.muted is True
        actions.dispatch(Event("sp", "off"))
        assert player.muted is False


class TestRobustness:
    def test_unmapped_event_is_silently_ignored(self, hasler):
        _, player, actions = hasler
        actions.dispatch(Event("k1", "triple_click"))
        actions.dispatch(Event("nonexistent", "tap"))
        assert player.calls == []

    def test_unknown_verb_warns_but_does_not_raise(self, hasler, caplog):
        variant, player, actions = hasler
        variant.actions["k1.tap"] = {"do": "levitate"}
        with caplog.at_level(logging.WARNING):
            actions.dispatch(Event("k1", "tap"))
        assert "unknown action verb" in caplog.text

    def test_a_failing_handler_never_breaks_the_panel_loop(self, hasler):
        """HA unreachable, MPD restarting - the next key press must still work."""
        _, player, actions = hasler

        def explode(*_):
            raise ConnectionError("HA is down")

        actions.ha = explode
        actions.dispatch(Event("k4", "hold"))     # -> ha_scene, raises inside
        actions.dispatch(Event("k2", "tap"))      # must still work
        assert player.playing.n == 2

    def test_ha_with_no_transport_configured_is_a_no_op(self, hasler):
        variant, player, actions = hasler
        variant.actions["k4.hold"] = {"do": "ha_scene", "entity": "scene.relax"}
        actions.dispatch(Event("k4", "hold"))
        assert player.calls == []

    def test_ha_scene_fires_with_the_bound_entity(self, hasler):
        variant, player, actions = hasler
        variant.actions["k4.hold"] = {"do": "ha_scene", "entity": "scene.relax"}
        fired = []
        actions.ha = lambda kind, payload: fired.append((kind, payload))
        actions.dispatch(Event("k4", "hold"))
        assert fired == [("scene", {"entity": "scene.relax"})]

    def test_shipped_layout_assumes_no_particular_home_assistant(self, hasler):
        variant, _, _ = hasler
        assert not any(str(a.get("do", "")).startswith("ha_") for a in variant.actions.values())


class TestToneMapping:
    """Pure mapping - the wheel-to-dB curve, with no moOde in sight."""

    def test_centre_is_flat(self):
        from haslerd.player.moode import tone_gain

        assert tone_gain(50.0) == 0.0

    def test_ends_reach_full_span(self):
        from haslerd.player.moode import tone_gain

        assert tone_gain(0.0) == -12.0
        assert tone_gain(100.0) == 12.0

    def test_deadband_swallows_a_knob_parked_near_noon(self):
        # A 60-year-old pot is never exactly at centre. Without the deadband the
        # box would sit at a permanent tilt you can neither hear nor remove.
        from haslerd.player.moode import tone_gain

        assert tone_gain(51.0) == 0.0
        assert tone_gain(49.0) == 0.0

    def test_just_outside_the_deadband_does_take_effect(self):
        from haslerd.player.moode import tone_gain

        assert tone_gain(55.0) == 1.2

    def test_out_of_range_input_is_clamped_not_amplified(self):
        from haslerd.player.moode import tone_gain

        assert tone_gain(-40.0) == -12.0
        assert tone_gain(900.0) == 12.0


class TestVolumeCeiling:
    """The vintage cone takes ~1-3 W; the amp can deliver ~10 W into 16 ohm."""

    def test_knob_maximum_lands_on_the_ceiling_not_full_scale(self):
        from haslerd.player.moode import scale_volume

        assert scale_volume(100.0, limit=70) == 70

    def test_the_knob_keeps_its_full_sweep_and_resolution(self):
        from haslerd.player.moode import scale_volume

        assert scale_volume(0.0, limit=70) == 0
        assert scale_volume(50.0, limit=70) == 35
        # Distinct knob positions still give distinct mixer levels.
        levels = {scale_volume(v, limit=70) for v in range(0, 101, 5)}
        assert len(levels) == 21

    def test_a_limit_of_100_is_simply_unlimited(self):
        from haslerd.player.moode import scale_volume

        assert scale_volume(100.0, limit=100) == 100

    def test_absurd_limits_are_clamped_rather_than_trusted(self):
        from haslerd.player.moode import scale_volume

        assert scale_volume(100.0, limit=500) == 100
        assert scale_volume(100.0, limit=0) == 1

    def test_out_of_range_knob_values_cannot_exceed_the_ceiling(self):
        from haslerd.player.moode import scale_volume

        assert scale_volume(9999.0, limit=70) == 70
        assert scale_volume(-50.0, limit=70) == 0


# -- sources, "again", persistence ------------------------------------------

from haslerd.player.base import Preset  # noqa: E402
from haslerd.state import StateStore  # noqa: E402

MIXED = [
    {"n": 1, "name": "Radio", "radio": "http://example.invalid/r"},
    {"n": 2, "name": "Spotify", "spotify": "spotify:playlist:abc", "shuffle": True},
    {"n": 3, "name": "YTM", "ytm": "https://music.youtube.com/playlist?list=xyz"},
    {"n": 4, "name": "Legacy", "url": "http://example.invalid/legacy"},
]


@pytest.fixture
def mixed():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    variant.presets = [dict(p) for p in MIXED]
    player = FakePlayer()
    return variant, player, Actions(variant, player)


class CountingPlayer(FakePlayer):
    """Counts active_source() - on the real box each call costs HTTP + shell-outs."""

    def __init__(self):
        super().__init__()
        self.source_reads = 0

    def active_source(self):
        self.source_reads += 1
        return super().active_source()


class TestPresetSpecs:
    def test_each_source_key_builds_its_source(self, mixed):
        _, _, actions = mixed
        assert {n: p.source for n, p in actions.presets.items()} == {
            1: "radio", 2: "spotify", 3: "ytm", 4: "radio"
        }
        assert actions.presets[2].shuffle is True
        assert actions.presets[2].uri == "spotify:playlist:abc"

    def test_legacy_url_preset_is_a_radio_preset(self):
        preset = Preset.from_spec({"n": 4, "url": "http://x/s"})
        assert (preset.source, preset.uri, preset.url) == ("radio", "http://x/s", "http://x/s")
        assert preset.name == "Preset 4"

    @pytest.mark.parametrize(
        "spec, match",
        [
            ({"n": 1, "name": "nothing"}, "no radio/spotify/ytm/url"),
            ({"n": 1, "radio": "http://a", "spotify": "spotify:x"}, "more than one source"),
            ({"n": 1, "radio": "http://a", "url": "http://b"}, "more than one source"),
            ({"radio": "http://a"}, "has no n"),
            ({"n": "seven", "radio": "http://a"}, "not an integer"),
            ("http://a", "must be a mapping"),
        ],
    )
    def test_bad_specs_are_rejected_with_a_reason(self, spec, match):
        with pytest.raises(ValueError, match=match):
            Preset.from_spec(spec)

    def test_one_broken_preset_costs_one_key_not_the_box(self, hasler, caplog):
        variant, player, _ = hasler
        variant.presets = [dict(p) for p in MIXED] + [{"n": 5, "name": "typo"}]
        with caplog.at_level(logging.ERROR):
            actions = Actions(variant, player)
        assert sorted(actions.presets) == [1, 2, 3, 4]
        assert "ignoring preset" in caplog.text

    def test_the_shipped_hasler_presets_parse(self, hasler):
        _, _, actions = hasler
        assert sorted(actions.presets) == [1, 2, 3, 4, 5, 6]


class TestRouting:
    @pytest.mark.parametrize("key, source", [("k1", "radio"), ("k2", "spotify"), ("k3", "ytm"), ("k4", "radio")])
    def test_key_plays_its_preset_on_its_source(self, mixed, key, source):
        _, player, actions = mixed
        actions.dispatch(Event(key, "tap"))
        assert player.calls[-1] == ("play_preset", int(key[1:]), source)
        assert player.current().source == source

    def test_switching_between_sources_never_stops_into_standby(self, mixed):
        _, player, actions = mixed
        for key in ("k1", "k2", "k3", "k1"):
            actions.dispatch(Event(key, "tap"))
        assert actions.standby is False
        assert [c[2] for c in player.calls if c[0] == "play_preset"] == ["radio", "spotify", "ytm", "radio"]

    def test_a_key_press_reads_the_active_source_at_most_once(self, mixed):
        variant, _, _ = mixed
        player = CountingPlayer()
        actions = Actions(variant, player)
        for key in ("k1", "k1", "k2", "k2", "k3"):
            before = player.source_reads
            actions.dispatch(Event(key, "tap"))
            assert player.source_reads - before <= 1, key


class TestAgain:
    def test_again_on_radio_goes_to_standby(self, mixed):
        _, player, actions = mixed
        actions.dispatch(Event("k1", "tap"))
        actions.dispatch(Event("k1", "tap"))
        assert actions.standby is True
        assert "next_track" not in player.verbs()

    @pytest.mark.parametrize("key", ["k2", "k3"])
    def test_again_on_a_playlist_skips_to_the_next_track(self, mixed, key):
        _, player, actions = mixed
        actions.dispatch(Event(key, "tap"))
        actions.dispatch(Event(key, "tap"))
        actions.dispatch(Event(key, "tap"))
        assert player.verbs().count("next_track") == 2
        assert player.verbs().count("play_preset") == 1
        assert actions.standby is False

    def test_legacy_url_preset_again_still_means_standby(self, mixed):
        _, _, actions = mixed
        actions.dispatch(Event("k4", "tap"))
        actions.dispatch(Event("k4", "tap"))
        assert actions.standby is True

    def test_playlist_that_lost_the_device_restarts_rather_than_skipping(self, mixed):
        # The phone took Spotify over, or the playlist ended: the key replays.
        _, player, actions = mixed
        actions.dispatch(Event("k2", "tap"))
        player.source = "radio"
        actions.dispatch(Event("k2", "tap"))
        assert "next_track" not in player.verbs()
        assert player.verbs().count("play_preset") == 2

    def test_again_is_about_this_key_not_the_last_one(self, mixed):
        _, player, actions = mixed
        actions.dispatch(Event("k2", "tap"))
        actions.dispatch(Event("k3", "tap"))
        assert "next_track" not in player.verbs()
        assert player.current().n == 3

    def test_preset_key_after_standby_plays_rather_than_skipping(self, mixed):
        _, player, actions = mixed
        actions.dispatch(Event("k2", "tap"))
        actions.dispatch(Event("power", "off"))
        actions.dispatch(Event("k2", "tap"))
        assert "next_track" not in player.verbs()
        assert player.current().n == 2 and actions.standby is False

    def test_bluetooth_renderer_is_taken_back_for_a_spotify_preset(self, mixed):
        _, player, actions = mixed
        player.source = "bluetooth"
        actions.dispatch(Event("k2", "tap"))
        assert ("select_source", "radio") in player.calls
        assert player.current().source == "spotify"


class QuietRenderersPlayer(FakePlayer):
    """Like the real box: a renderer selected but not yet cast to reports "radio"."""

    def active_source(self):
        return self.source if self.source in ("spotify", "ytm") and self.playing else "radio"


class TestCycleWithPresetSources:
    def test_pu_from_a_spotify_preset_goes_home_to_the_last_station(self, mixed):
        _, player, actions = mixed
        actions.dispatch(Event("k3", "tap"))      # ytm
        actions.dispatch(Event("k2", "tap"))      # spotify
        actions.dispatch(Event("pu", "tap"))
        assert player.current().n == 3, "radio stop resumes the last MPD preset, not spotify"

    def test_pu_from_a_ytm_preset_moves_on_to_bluetooth(self, mixed):
        _, player, actions = mixed
        actions.dispatch(Event("k3", "tap"))
        actions.dispatch(Event("pu", "tap"))
        assert player.active_source() == "bluetooth"

    def test_spotify_stop_resumes_the_last_spotify_preset(self, mixed):
        _, player, actions = mixed
        actions.dispatch(Event("k2", "tap"))
        actions.dispatch(Event("k1", "tap"))
        for _ in range(3):                        # bluetooth, airplay, spotify
            actions.dispatch(Event("pu", "tap"))
        assert player.current().n == 2

    def test_full_cycle_with_nothing_cast_still_comes_home(self, mixed):
        variant, _, _ = mixed
        player = QuietRenderersPlayer()
        actions = Actions(variant, player)
        actions.dispatch(Event("k1", "tap"))
        selected = []
        for _ in range(4):
            actions.dispatch(Event("pu", "tap"))
            selected.append([c for c in player.calls if c[0] == "select_source"][-1][1])
        assert selected == ["bluetooth", "airplay", "spotify", "radio"]
        assert player.current().n == 1


class TestPlayErrors:
    def test_a_failing_play_reaches_the_error_hook_and_the_panel_survives(self, mixed):
        _, player, actions = mixed
        errors = []
        actions.on_play_error = errors.append
        player.fail_play = ConnectionError("go-librespot down")
        actions.dispatch(Event("k2", "tap"))
        assert len(errors) == 1 and isinstance(errors[0], ConnectionError)
        player.fail_play = None
        actions.dispatch(Event("k1", "tap"))
        assert player.current().n == 1

    def test_tapping_again_after_a_failed_play_retries(self, mixed):
        _, player, actions = mixed
        player.fail_play = ConnectionError("down")
        actions.dispatch(Event("k1", "tap"))
        player.fail_play = None
        actions.dispatch(Event("k1", "tap"))
        assert actions.standby is False
        assert player.current().n == 1


class TestVolumeStep:
    def test_step_is_relative_to_the_current_volume(self, hasler):
        variant, player, actions = hasler
        variant.actions["k6.hold"] = {"do": "volume_step", "delta": 5}
        player.volume = 40.0
        actions.dispatch(Event("k6", "hold"))
        actions.dispatch(Event("k6", "hold"))
        assert player.volume == 50.0

    def test_step_down_and_clamped_to_the_knob_range(self, hasler):
        variant, player, actions = hasler
        variant.actions["k6.hold"] = {"do": "volume_step", "delta": -30}
        player.volume = 20.0
        actions.dispatch(Event("k6", "hold"))
        assert player.volume == 0.0

    def test_unknown_volume_steps_from_a_quiet_default(self, hasler):
        variant, player, actions = hasler
        variant.actions["k6.hold"] = {"do": "volume_step", "delta": 5}
        actions.dispatch(Event("k6", "hold"))
        assert player.volume == 45.0

    def test_zero_delta_is_a_spare_key(self, hasler):
        variant, player, actions = hasler
        variant.actions["k6.hold"] = {"do": "volume_step", "delta": 0}
        actions.dispatch(Event("k6", "hold"))
        assert player.calls == []

    def test_shipped_hasler_holds_k5_down_and_k6_up(self, hasler):
        _, player, actions = hasler
        player.volume = 40.0
        actions.dispatch(Event("k6", "hold"))
        actions.dispatch(Event("k6", "hold"))
        assert player.volume == 44.0
        actions.dispatch(Event("k5", "hold"))
        assert player.volume == 42.0


class TestPersistence:
    def make(self, variant, path):
        player = FakePlayer()
        return player, Actions(variant, player, state=StateStore(path))

    def test_playing_a_preset_is_remembered(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        _, actions = self.make(variant, path)
        actions.dispatch(Event("k3", "tap"))
        assert StateStore(path).load() == {"last_preset": 3, "standby": False}

    def test_standby_is_remembered(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        _, actions = self.make(variant, path)
        actions.dispatch(Event("k2", "tap"))
        actions.dispatch(Event("power", "off"))
        assert StateStore(path).load() == {"last_preset": 2, "standby": True}

    def test_power_cut_then_boot_resumes_the_last_preset(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        _, before = self.make(variant, path)
        before.dispatch(Event("k3", "tap"))

        player, after = self.make(variant, path)
        after.restore()
        assert player.calls == [], "restore alone must not play"
        assert after.resume_last() is True
        assert player.current().n == 3 and player.current().source == "ytm"

    def test_boot_in_standby_stays_silent(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        _, before = self.make(variant, path)
        before.dispatch(Event("k1", "tap"))
        before.dispatch(Event("k1", "tap"))         # again -> standby

        player, after = self.make(variant, path)
        after.restore()
        after.resume_last()
        assert after.standby is True
        assert player.muted is True
        assert "play_preset" not in player.verbs()
        # ...but the knob coming on still knows where it was.
        after.dispatch(Event("power", "on"))
        assert player.current().n == 1

    def test_knob_off_at_boot_wins_over_remembered_playing(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        StateStore(path).save({"last_preset": 2, "standby": False})
        player, actions = self.make(variant, path)
        actions.restore()
        actions.dispatch(Event("power", "off"))     # EndSwitch boot assumption
        actions.resume_last()
        assert "play_preset" not in player.verbs()

    def test_failed_resume_reports_so_it_can_be_retried(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        StateStore(path).save({"last_preset": 2, "standby": False})
        player, actions = self.make(variant, path)
        player.fail_play = OSError("no network yet")
        actions.restore()
        assert actions.resume_last() is False
        player.fail_play = None
        assert actions.resume_last() is True

    def test_a_preset_that_no_longer_exists_is_forgotten(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        StateStore(path).save({"last_preset": 42, "standby": False})
        player, actions = self.make(variant, path)
        actions.restore()
        assert actions.resume_last() is True
        assert player.calls == []

    def test_next_track_does_not_rewrite_the_state_file(self, mixed, tmp_path):
        variant, _, _ = mixed
        path = tmp_path / "state.json"
        _, actions = self.make(variant, path)
        actions.dispatch(Event("k2", "tap"))
        mtime = path.stat().st_mtime_ns
        actions.dispatch(Event("k2", "tap"))        # again -> next track
        assert path.stat().st_mtime_ns == mtime
