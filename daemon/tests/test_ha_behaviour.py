"""Home Assistant behaviours: lights (brightness, colour, white, fade, groups),
scenes with a fade, the fenced generic service call, and the page API around
them (/api/ha/lights, /scenes, /services, /try)."""

from __future__ import annotations

import pytest
from conftest import VARIANTS
from test_ha import TOKEN, URL, Clock, Factory, FakeClient, assert_no_token, fake_fetch, make_api

from haslerd.actions import Actions
from haslerd.ha import behaviour as bh
from haslerd.ha.rest import HomeAssistant
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import load_variant
from haslerd.web import keymap


def light(eid, modes, state="on", **attrs):
    return {"entity_id": eid, "state": state,
            "attributes": {"supported_color_modes": modes, "friendly_name": eid.split(".")[1].title(), **attrs}}


STATES = [
    light("light.sofa", ["hs", "color_temp"], brightness=128, rgb_color=[255, 136, 0],
          min_color_temp_kelvin=2200, max_color_temp_kelvin=6500),
    light("light.desk", ["color_temp"], brightness=255, color_temp_kelvin=3000,
          min_color_temp_kelvin=2700, max_color_temp_kelvin=5000),
    light("light.hall", ["brightness"], state="off"),
    light("light.plug", ["onoff"]),
    {"entity_id": "light.gone", "state": "unavailable", "attributes": {"friendly_name": "Gone"}},
    light("light.living", ["hs"], entity_id=["light.sofa", "light.desk"]),
    {"entity_id": "scene.evening", "state": "scening", "attributes": {"friendly_name": "Evening"}},
    {"entity_id": "script.good_night", "state": "off", "attributes": {}},
    {"entity_id": "media_player.kitchen", "state": "idle", "attributes": {}},
]
AREAS = [{"area_id": "living", "name": "Living room", "entities": ["light.sofa", "light.desk"]},
         {"area_id": "hall", "name": "Hall", "entities": ["light.hall"]}]
CAPS = {s["entity_id"]: bh.light_caps(s) for s in STATES if s["entity_id"].startswith("light.")}


@pytest.fixture
def actions():
    return Actions(load_variant(VARIANTS / "hasler-hm.yaml"), FakePlayer())


# -- validating ---------------------------------------------------------------------


class TestNormalise:
    def test_light_minimal_and_full(self):
        assert bh.normalise({"do": "ha_light", "entity": "light.sofa"}) == \
            {"do": "ha_light", "entities": ["light.sofa"], "mode": "on"}
        spec = bh.normalise({"do": "ha_light", "entities": ["light.sofa", "light.desk", "light.sofa"],
                             "mode": "toggle", "brightness": "40", "color": "FF8800", "kelvin": None,
                             "transition": 2.04})
        assert spec == {"do": "ha_light", "entities": ["light.sofa", "light.desk"], "mode": "toggle",
                        "brightness": 40, "color": "#ff8800", "transition": 2.0}

    def test_off_drops_what_off_cannot_use(self):
        assert bh.normalise({"do": "ha_light", "entity": "light.sofa", "mode": "off", "brightness": 50,
                             "color": "#ffffff", "transition": 3}) == \
            {"do": "ha_light", "entities": ["light.sofa"], "mode": "off", "transition": 3.0}

    @pytest.mark.parametrize("raw, words", [
        ({"do": "ha_light"}, "pick at least one light"),
        ({"do": "ha_light", "entity": "switch.fan"}, "isn't a light"),
        ({"do": "ha_light", "entity": "Light.Bad"}, "isn't an entity id"),
        ({"do": "ha_light", "entity": "light.a", "mode": "blink"}, "on, off or toggle"),
        ({"do": "ha_light", "entity": "light.a", "brightness": 101}, "0 to 100"),
        ({"do": "ha_light", "entity": "light.a", "brightness": True}, "0 to 100"),
        ({"do": "ha_light", "entity": "light.a", "color": "orange"}, "#ff8800"),
        ({"do": "ha_light", "entity": "light.a", "kelvin": 500}, "white temperature"),
        ({"do": "ha_light", "entity": "light.a", "color": "#ffffff", "kelvin": 3000}, "not both"),
        ({"do": "ha_light", "entity": "light.a", "transition": 1000}, "fade time"),
        ({"do": "ha_light", "entities": [f"light.l{i}" for i in range(30)]}, "at most"),
        ({"do": "ha_scene", "entity": "scene.a", "transition": -1}, "fade time"),
        ({"do": "ha_scene", "entity": "light.a"}, "isn't a scene"),
        ({"do": "preset"}, "pick a Home Assistant action"),
    ])
    def test_problems_are_sentences(self, raw, words):
        with pytest.raises(bh.Problem) as exc:
            bh.normalise(raw)
        assert words in str(exc.value)

    def test_original_verbs_keep_their_stored_form(self):
        for verb, entity in (("ha_scene", "scene.evening"), ("ha_script", "script.x"),
                             ("ha_toggle", "switch.fan"), ("ha_automation", "automation.y")):
            assert bh.normalise({"do": verb, "entity": entity}) == {"do": verb, "entity": entity}
        assert bh.normalise({"do": "ha_scene", "entity": "scene.evening", "transition": 4}) == \
            {"do": "ha_scene", "entity": "scene.evening", "transition": 4.0}

    @pytest.mark.parametrize("raw, words", [
        ({"do": "ha_service", "service": "lock.unlock", "entity": "lock.front"}, "isn't a service"),
        ({"do": "ha_service", "service": "shell_command.rm", "entity": "shell_command.rm"}, "isn't a service"),
        ({"do": "ha_service", "service": "homeassistant.restart", "entity": "light.a"}, "isn't a service"),
        ({"do": "ha_service", "service": "light.turn_on", "entity": "switch.a"}, "isn't a light"),
        ({"do": "ha_service", "service": "media_player.volume_set", "entity": "media_player.k"}, "needs volume_level"),
        ({"do": "ha_service", "service": "media_player.volume_set", "entity": "media_player.k",
          "data": {"volume_level": 3}}, "won't take"),
        ({"do": "ha_service", "service": "switch.turn_on", "entity": "switch.a", "data": {"code": "1234"}},
         "doesn't take 'code'"),
    ])
    def test_service_is_fenced(self, raw, words):
        with pytest.raises(bh.Problem) as exc:
            bh.normalise(raw)
        assert words in str(exc.value)

    def test_service_ok(self):
        spec = bh.normalise({"do": "ha_service", "service": "media_player.volume_set",
                             "entities": ["media_player.kitchen"], "data": {"volume_level": 0.3}})
        assert spec == {"do": "ha_service", "service": "media_player.volume_set",
                        "entities": ["media_player.kitchen"], "data": {"volume_level": 0.3}}
        assert bh.calls(spec) == [("media_player", "volume_set",
                                   {"entity_id": "media_player.kitchen", "volume_level": 0.3})]

    def test_services_doc_lists_the_fence(self):
        doc = {s["service"]: s for s in bh.services_doc()["services"]}
        assert "light.turn_on" in doc and "lock.unlock" not in doc
        assert doc["media_player.volume_set"]["required"] == ["volume_level"]
        assert "brightness_pct" in doc["light.turn_on"]["data"]


# -- what lights can do, and the calls -------------------------------------------------


class TestLightCalls:
    def test_caps(self):
        assert CAPS["light.sofa"] == {"brightness": True, "color": True, "color_temp": [2200, 6500]}
        assert CAPS["light.desk"] == {"brightness": True, "color": False, "color_temp": [2700, 5000]}
        assert CAPS["light.hall"] == {"brightness": True, "color": False, "color_temp": None}
        assert CAPS["light.plug"] == {"brightness": False, "color": False, "color_temp": None}
        assert CAPS["light.gone"] is None

    def test_mixed_group_gets_what_each_can_do(self):
        spec = bh.normalise({"do": "ha_light", "entities": ["light.sofa", "light.desk", "light.hall",
                                                            "light.plug", "light.gone"],
                             "brightness": 40, "color": "#ff8800", "transition": 2})
        assert bh.calls(spec, CAPS) == [
            ("light", "turn_on", {"entity_id": ["light.sofa", "light.gone"], "brightness_pct": 40,
                                  "rgb_color": [255, 136, 0], "transition": 2.0}),
            ("light", "turn_on", {"entity_id": ["light.desk", "light.hall"], "brightness_pct": 40,
                                  "transition": 2.0}),
            ("light", "turn_on", {"entity_id": "light.plug", "transition": 2.0}),
        ]
        words = bh.warnings(spec, CAPS, {"light.desk": "Desk lamp"})
        assert "Desk lamp can't show colour; it does the rest" in words
        assert any(w.startswith("light.plug can't show colour or brightness") for w in words)

    def test_kelvin_is_clamped_and_shown_as_colour_where_needed(self):
        spec = bh.normalise({"do": "ha_light", "entities": ["light.desk", "light.living", "light.hall"],
                             "kelvin": 2200})
        assert bh.calls(spec, CAPS) == [
            ("light", "turn_on", {"entity_id": "light.desk", "color_temp_kelvin": 2700}),
            ("light", "turn_on", {"entity_id": "light.living", "color_temp_kelvin": 2200}),
            ("light", "turn_on", {"entity_id": "light.hall"}),
        ]

    def test_unknown_lights_get_everything(self):
        spec = bh.normalise({"do": "ha_light", "entities": ["light.a", "light.b"], "brightness": 10})
        assert bh.calls(spec) == [("light", "turn_on", {"entity_id": ["light.a", "light.b"], "brightness_pct": 10})]

    def test_off_and_toggle(self):
        off = bh.normalise({"do": "ha_light", "entities": ["light.a", "light.b"], "mode": "off", "transition": 5})
        assert bh.calls(off, CAPS) == [("light", "turn_off", {"entity_id": ["light.a", "light.b"], "transition": 5.0})]
        tog = bh.normalise({"do": "ha_light", "entity": "light.sofa", "mode": "toggle", "brightness": 80})
        assert bh.calls(tog, CAPS) == [("light", "toggle", {"entity_id": "light.sofa", "brightness_pct": 80})]

    def test_skip_leaves_out_missing_entities(self):
        spec = bh.normalise({"do": "ha_light", "entities": ["light.a", "light.b"]})
        assert bh.calls(spec, {}, {"light.a"}) == [("light", "turn_on", {"entity_id": "light.b"})]
        assert bh.calls(spec, {}, {"light.a", "light.b"}) == []

    def test_original_verbs_make_the_original_calls(self):
        assert bh.calls({"do": "ha_scene", "entity": "scene.e"}) == [("scene", "turn_on", {"entity_id": "scene.e"})]
        assert bh.calls({"do": "ha_scene", "entity": "scene.e", "transition": 3.0}) == \
            [("scene", "turn_on", {"entity_id": "scene.e", "transition": 3.0})]
        assert bh.calls({"do": "ha_toggle", "entity": "fan.x"}) == [("homeassistant", "toggle", {"entity_id": "fan.x"})]
        assert bh.calls({"do": "ha_automation", "entity": "automation.y"}) == \
            [("automation", "trigger", {"entity_id": "automation.y"})]


# -- keys: keymap, dispatch, the transport ----------------------------------------------


class TestKeys:
    def test_page_round_trip_through_keys_yaml(self, actions, tmp_path):
        doc = keymap.config_doc(actions, {}, False)
        doc["keys"][0]["hold"] = {"do": "ha_light", "entities": ["light.sofa", "light.desk"], "mode": "on",
                                  "brightness": 30, "color": None, "kelvin": 2700, "transition": None}
        doc["keys"][1]["hold"] = {"do": "ha_scene", "entity": "scene.evening", "transition": 2}
        specs, table = keymap.parse(doc, actions)
        assert table["k1.hold"] == {"do": "ha_light", "entities": ["light.sofa", "light.desk"], "mode": "on",
                                    "brightness": 30, "kelvin": 2700}
        path = tmp_path / "keys.yaml"
        keymap.save(path, specs, table)
        _, loaded = keymap.load(path)
        assert loaded["k1.hold"] == table["k1.hold"]
        page = keymap.to_page(loaded["k1.hold"])
        assert page == {"do": "ha_light", "entities": ["light.sofa", "light.desk"], "entity": "light.sofa",
                        "mode": "on", "brightness": 30, "color": None, "kelvin": 2700, "transition": None}
        assert keymap.to_page(loaded["k2.hold"]) == {"do": "ha_scene", "entity": "scene.evening", "transition": 2.0}

    def test_invalid_light_names_the_field(self, actions):
        doc = keymap.config_doc(actions, {}, False)
        doc["keys"][2]["tap"] = {"do": "ha_light", "entity": "light.a", "color": "#fff", "kelvin": None}
        with pytest.raises(keymap.Invalid) as exc:
            keymap.parse(doc, actions)
        assert exc.value.errors[0]["where"] == "keys[2].tap" and "#ff8800" in exc.value.errors[0]["message"]

    def test_dispatch_sends_the_behaviour(self, actions):
        fired, replies = [], []
        actions.ha = lambda k, p: fired.append((k, p))
        actions.on_reply = replies.append
        actions.variant.actions["k1.hold"] = {"do": "ha_light", "entities": ["light.a"], "mode": "off"}
        actions.variant.actions["k2.hold"] = {"do": "ha_service", "service": "switch.toggle", "entities": ["switch.x"]}
        actions.dispatch(Event("k1", "hold"))
        actions.dispatch(Event("k2", "hold"))
        assert fired == [("light", {"entities": ["light.a"], "mode": "off"}),
                         ("service", {"service": "switch.toggle", "entities": ["switch.x"]})]

    def test_lamp_fails_only_when_every_light_is_missing(self, actions):
        replies = []
        actions._reply = replies.append

        class Ha:
            missing = frozenset({"light.a"})

            def __call__(self, kind, payload):
                pass
        actions.ha = Ha()
        actions.variant.actions["k1.hold"] = {"do": "ha_light", "entities": ["light.a", "light.b"]}
        actions.variant.actions["k2.hold"] = {"do": "ha_light", "entities": ["light.a"]}
        actions.dispatch(Event("k1", "hold"))
        actions.dispatch(Event("k2", "hold"))
        assert replies == ["ha", "fail"]

    def test_transport_fits_each_light(self):
        client = HomeAssistant(URL, TOKEN, fetch=fake_fetch({}))
        sent = []
        client.call_service = lambda d, s, data: sent.append((d, s, data))
        client.lights = {"light.sofa": CAPS["light.sofa"], "light.hall": CAPS["light.hall"]}
        client.missing = frozenset({"light.old"})
        client("light", {"entities": ["light.sofa", "light.hall", "light.old"], "mode": "on", "color": "#0000ff"})
        assert sent == [("light", "turn_on", {"entity_id": "light.sofa", "rgb_color": [0, 0, 255]}),
                        ("light", "turn_on", {"entity_id": "light.hall"})]
        sent.clear()
        client("scene", {"entity": "scene.e"})
        client("service", {"service": "lock.unlock", "entities": ["lock.door"]})     # off the fence: nothing
        assert sent == [("scene", "turn_on", {"entity_id": "scene.e"})]

    def test_run_calls_returns_changed_states(self):
        calls = []
        routes = {("POST", "/api/services/light/turn_on"): (200, [{"entity_id": "light.sofa", "state": "on"}])}
        client = HomeAssistant(URL, TOKEN, fetch=fake_fetch(routes, calls))
        assert client.run_calls([("light", "turn_on", {"entity_id": "light.sofa", "brightness_pct": 5})]) == \
            [{"entity_id": "light.sofa", "state": "on"}]
        assert calls[0][3] == {"entity_id": "light.sofa", "brightness_pct": 5}


# -- the page API ---------------------------------------------------------------------


def connected(actions, tmp_path, clock=None):
    api = make_api(actions, tmp_path, factory=Factory(states=STATES, areas=AREAS), clock=clock)
    api.connect({"url": URL, "token": TOKEN})
    return api


class TestPageApi:
    def test_lights(self, actions, tmp_path):
        api = connected(actions, tmp_path)
        code, doc = api.route("GET", "/api/ha/lights", None)
        assert code == 200
        assert doc["areas"] == [{"id": "hall", "name": "Hall"}, {"id": "living", "name": "Living room"}]
        by_id = {x["entity_id"]: x for x in doc["lights"]}
        assert by_id["light.sofa"] == {
            "entity_id": "light.sofa", "name": "Sofa", "area": "living", "area_name": "Living room", "state": "on",
            "brightness": 50, "rgb": [255, 136, 0], "color": "#ff8800", "kelvin": None,
            "supported": {"brightness": True, "color": True, "color_temp": [2200, 6500]}, "members": None}
        assert by_id["light.hall"]["brightness"] is None and by_id["light.hall"]["state"] == "off"
        assert by_id["light.desk"]["kelvin"] == 3000
        assert by_id["light.gone"]["supported"] is None
        assert by_id["light.living"]["members"] == ["light.sofa", "light.desk"]
        assert "scene.evening" not in by_id
        assert [x["area_name"] for x in doc["lights"]][:3] == ["Hall", "Living room", "Living room"]
        assert_no_token(doc)

    def test_lights_cache_is_short_and_spares_the_areas_template(self, actions, tmp_path):
        clock = Clock()
        api = connected(actions, tmp_path, clock)
        client = api.client
        areas_calls = []
        client.areas = lambda: areas_calls.append(1) or list(AREAS)
        n = client.state_calls
        api.route("GET", "/api/ha/lights", None)
        assert client.state_calls == n                 # connect just fetched
        clock.t += 4
        api.route("GET", "/api/ha/lights", None)
        assert client.state_calls == n + 1 and areas_calls == []
        api.route("GET", "/api/ha/entities", None)     # 60 s cache: no new fetch
        assert client.state_calls == n + 1

    def test_lights_teach_the_transport(self, actions, tmp_path):
        api = connected(actions, tmp_path)
        assert api.client.lights["light.desk"]["color_temp"] == [2700, 5000]

    def test_scenes_and_domain_filter(self, actions, tmp_path):
        api = connected(actions, tmp_path)
        code, doc = api.route("GET", "/api/ha/scenes", None)
        assert code == 200 and [e["entity_id"] for e in doc["entities"]] == ["scene.evening"]
        code, doc = api.route("GET", "/api/ha/entities?domains=script,scene", None)
        assert {e["entity_id"] for e in doc["entities"]} == {"scene.evening", "script.good_night"}

    def test_not_connected(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        for path in ("/api/ha/lights", "/api/ha/scenes"):
            assert api.route("GET", path, None)[0] == 409
        assert api.route("GET", "/api/ha/services", None)[0] == 200
        assert api.route("POST", "/api/ha/lights", {})[0] == 405

    def test_try_a_light_behaviour(self, actions, tmp_path):
        api = connected(actions, tmp_path)
        code, doc = api.route("POST", "/api/ha/try", {"action": {
            "do": "ha_light", "entities": ["light.sofa", "light.hall"], "brightness": 25, "color": "#00ff00"}})
        assert code == 200 and doc["ok"]
        assert doc["action"] == {"do": "ha_light", "entities": ["light.sofa", "light.hall"], "mode": "on",
                                 "brightness": 25, "color": "#00ff00"}
        assert doc["calls"] == [
            {"service": "light.turn_on", "data": {"entity_id": "light.sofa", "brightness_pct": 25,
                                                  "rgb_color": [0, 255, 0]}},
            {"service": "light.turn_on", "data": {"entity_id": "light.hall", "brightness_pct": 25}}]
        assert doc["warnings"] == ["Hall can't show colour; it does the rest"]
        assert doc["changed"] == [{"entity_id": "light.sofa", "state": "on"}, {"entity_id": "light.hall", "state": "on"}]

    def test_try_refuses_a_bad_behaviour(self, actions, tmp_path):
        api = connected(actions, tmp_path)
        code, doc = api.route("POST", "/api/ha/try", {"action": {"do": "ha_service", "service": "lock.unlock",
                                                                 "entity": "lock.front"}})
        assert code == 400 and "isn't a service" in doc["error"]
        assert api.client.ran == []

    def test_missing_lists_each_missing_light(self, actions, tmp_path):
        api = connected(actions, tmp_path)
        actions.variant.actions["k1.hold"] = {"do": "ha_light", "entities": ["light.sofa", "light.attic"]}
        api._states = None
        doc = api.status()
        assert [(m["key"], m["entity"]) for m in doc["missing"]] == [("k1", "light.attic")]
        assert api.client.missing == frozenset({"light.attic"})
