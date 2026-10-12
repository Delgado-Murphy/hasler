"""Home Assistant: settings store, client errors, areas, discovery, verbs, HaApi routes.

No network anywhere: the client takes an injected ``fetch``, discovery an
injected ``runner``/``probe``, and HaApi an injected client factory.
"""

import json
import logging
import os
import socket
import ssl
import stat
import subprocess
import urllib.error

import pytest
import yaml
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.ha import config as ha_config
from haslerd.ha import discover
from haslerd.ha.rest import AREAS_TEMPLATE, HaError, HomeAssistant, parse_areas
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer
from haslerd.variant import load_variant
from haslerd.web import keymap
from haslerd.web.ha_api import HaApi, SENSOR

TOKEN = "eyJhbGciOiJIUzI1NiJ9.secret-token-value"
URL = "http://192.168.1.10:8123"


# -- fakes ------------------------------------------------------------------


def fake_fetch(routes, calls=None):
    """routes: {(method, path): (status, body) | Exception}; body dict/list/str/bytes."""
    def fetch(method, url, headers, body, timeout):
        path = url.split("8123", 1)[-1] if "8123" in url else url
        if calls is not None:
            calls.append((method, path, headers, json.loads(body) if body else None))
        answer = routes.get((method, path))
        if answer is None:
            return 404, b"404: Not Found"
        if isinstance(answer, BaseException):
            raise answer
        status, doc = answer
        if isinstance(doc, (dict, list)):
            doc = json.dumps(doc)
        if isinstance(doc, str):
            doc = doc.encode()
        return status, doc
    return fetch


HA_OK = {
    ("GET", "/api/"): (200, {"message": "API running."}),
    ("GET", "/api/config"): (200, {"location_name": "Home", "version": "2026.9.4", "time_zone": "Europe/Madrid",
                                   "latitude": 40.41, "longitude": -3.70}),
}


class FakeClient:
    """Stands in for HomeAssistant inside HaApi."""

    def __init__(self, url, token, *, info=None, fail=None, states=None, areas=None):
        self.url, self.token = url, token
        self.info = info or {"location_name": "Home", "version": "2026.9.4", "time_zone": "Europe/Madrid",
                             "latitude": 40.41, "longitude": -3.70}
        self.fail = fail
        self._states = states if states is not None else [
            {"entity_id": "scene.evening", "state": "scening", "attributes": {"friendly_name": "Evening"}},
            {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "Kitchen light"}},
            {"entity_id": "sensor.temp", "state": "21", "attributes": {"friendly_name": "Temp"}},
            {"entity_id": "script.good_night", "state": "off", "attributes": {}},
        ]
        self._areas = areas if areas is not None else [
            {"area_id": "kitchen", "name": "Kitchen", "entities": ["light.kitchen"]},
        ]
        self.missing = frozenset()
        self.fired, self.ran, self.pushed, self.deleted = [], [], [], []
        self.state_calls = 0

    def __call__(self, kind, payload):
        self.fired.append((kind, payload))

    def check(self):
        if self.fail:
            raise HaError(self.fail)
        return dict(self.info)

    def states(self):
        self.state_calls += 1
        return list(self._states)

    def areas(self):
        return list(self._areas)

    def run(self, kind, entity):
        self.ran.append((kind, entity))

    def set_state_async(self, entity_id, state, attributes):
        self.pushed.append((entity_id, state, attributes))

    def delete_state_async(self, entity_id):
        self.deleted.append(entity_id)


class Factory:
    def __init__(self, **kw):
        self.kw, self.made = kw, []

    def __call__(self, url, token):
        client = FakeClient(url, token, **self.kw)
        self.made.append(client)
        return client


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def actions():
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    return Actions(variant, FakePlayer())


def make_api(actions, tmp_path, yaml_conf=None, factory=None, discover_fn=None, clock=None):
    return HaApi(actions, tmp_path, yaml_conf, client_factory=factory or Factory(),
                 discover=discover_fn or (lambda: ([], None)), clock=clock or Clock())


# -- settings store ------------------------------------------------------------


class TestConfigStore:
    def test_saved_file_is_private_and_round_trips(self, tmp_path):
        s = ha_config.HaSettings(url=URL, token=TOKEN, publish=True, instance={"name": "Home"})
        assert ha_config.save(tmp_path, s)
        path = tmp_path / "ha.json"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        back = ha_config.load(tmp_path)
        assert (back.url, back.token, back.publish, back.source, back.instance) == (URL, TOKEN, True, "file", {"name": "Home"})

    def test_write_is_atomic_no_temp_files_left(self, tmp_path):
        ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token=TOKEN))
        ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token="other"))
        assert sorted(p.name for p in tmp_path.iterdir()) == ["ha.json"]

    def test_failed_write_keeps_the_old_file(self, tmp_path, monkeypatch):
        ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token=TOKEN))
        monkeypatch.setattr(ha_config.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("card full")))
        with pytest.raises(OSError):
            ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token="new"))
        assert ha_config.load(tmp_path).token == TOKEN
        assert sorted(p.name for p in tmp_path.iterdir()) == ["ha.json"]

    def test_token_never_in_repr_or_logs(self, tmp_path, caplog):
        s = ha_config.HaSettings(url=URL, token=TOKEN)
        assert TOKEN not in repr(s) and TOKEN not in str(s)
        with caplog.at_level(logging.DEBUG):
            ha_config.save(tmp_path, s)
            ha_config.load(tmp_path)
            ha_config.forget(tmp_path)
        assert TOKEN not in caplog.text

    def test_falls_back_to_yaml(self, tmp_path):
        s = ha_config.load(tmp_path, {"url": "http://ha.lan:8123/", "token": "t"})
        assert (s.url, s.token, s.source) == ("http://ha.lan:8123", "t", "yaml")
        assert ha_config.load(tmp_path, {"url": "http://ha.lan:8123", "token": ""}) is None
        assert ha_config.load(None, None) is None

    def test_file_wins_over_yaml(self, tmp_path):
        ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token=TOKEN))
        assert ha_config.load(tmp_path, {"url": "http://old:8123", "token": "x"}).url == URL

    def test_corrupt_file_falls_back(self, tmp_path):
        (tmp_path / "ha.json").write_text("{nope")
        assert ha_config.load(tmp_path, {"url": URL, "token": "y"}).source == "yaml"

    def test_forget_deletes_or_tombstones(self, tmp_path):
        ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token=TOKEN))
        ha_config.forget(tmp_path)
        assert not (tmp_path / "ha.json").exists()
        yaml_conf = {"url": "http://old:8123", "token": "x"}
        ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token=TOKEN))
        ha_config.forget(tmp_path, yaml_conf)
        assert TOKEN not in (tmp_path / "ha.json").read_text()
        assert ha_config.load(tmp_path, yaml_conf) is None     # disconnect sticks

    @pytest.mark.parametrize("raw, want", [
        ("http://homeassistant.local:8123/", "http://homeassistant.local:8123"),
        ("192.168.1.10:8123", "http://192.168.1.10:8123"),
        (" https://ha.example.org ", "https://ha.example.org"),
        ("http://ha.lan:8123/lovelace/0", "http://ha.lan:8123"),
        ("http://ha.lan:8123/api/", "http://ha.lan:8123"),
        ("http://[fe80::1]:8123", "http://[fe80::1]:8123"),
    ])
    def test_normalise_url(self, raw, want):
        assert ha_config.normalise_url(raw) == want

    @pytest.mark.parametrize("raw", ["", "ftp://x", "http://", "http://bad host", "http://h:99999"])
    def test_normalise_url_rejects(self, raw):
        with pytest.raises(ValueError):
            ha_config.normalise_url(raw)

    def test_fingerprint_and_identity(self):
        a = ha_config.fingerprint({"location_name": "Home", "time_zone": "Europe/Madrid", "latitude": 40.41, "longitude": -3.7})
        assert "40.41" not in json.dumps(a)                  # coordinates are hashed
        b = ha_config.fingerprint({"location_name": "Home", "time_zone": "Europe/London", "latitude": 51.5, "longitude": 0.1})
        assert ha_config.same_instance(a, dict(a))
        assert not ha_config.same_instance(a, b)
        assert ha_config.same_instance(dict(a, uuid="u1"), dict(b, uuid="u1"))   # uuid decides
        assert not ha_config.same_instance(dict(a, uuid="u1"), dict(a, uuid="u2"))
        assert ha_config.same_instance(None, a)


# -- client ---------------------------------------------------------------------


class TestClient:
    def test_check_ok(self):
        info = HomeAssistant(URL, TOKEN, fetch=fake_fetch(HA_OK)).check()
        assert info["location_name"] == "Home" and info["version"] == "2026.9.4"
        assert info["time_zone"] == "Europe/Madrid"

    def test_sends_bearer_token(self):
        calls = []
        HomeAssistant(URL, TOKEN, fetch=fake_fetch(HA_OK, calls)).check()
        assert calls[0][2]["Authorization"] == f"Bearer {TOKEN}"

    def test_401_is_a_bad_token(self):
        fetch = fake_fetch({("GET", "/api/"): (401, "401: Unauthorized")})
        with pytest.raises(HaError, match="rejected the token"):
            HomeAssistant(URL, TOKEN, fetch=fetch).check()

    @pytest.mark.parametrize("exc, words", [
        (urllib.error.URLError(ConnectionRefusedError(111, "refused")), "Can't reach that address"),
        (urllib.error.URLError(socket.gaierror(-2, "Name or service not known")), "Can't find that host name"),
        (urllib.error.URLError(TimeoutError("timed out")), "no answer within 3 seconds"),
        (TimeoutError("timed out"), "no answer within 3 seconds"),
        (urllib.error.URLError(ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")), "certificate isn't trusted"),
        (urllib.error.URLError(ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER]")), "http://"),
        (OSError("boom"), "Can't reach that address"),
    ])
    def test_transport_errors_are_human(self, exc, words):
        with pytest.raises(HaError, match=words.replace("(", r"\(").replace(")", r"\)")):
            HomeAssistant(URL, TOKEN, fetch=fake_fetch({("GET", "/api/"): exc})).check()

    @pytest.mark.parametrize("answer", [(200, "<html>router</html>"), (200, {"hello": 1}), (404, "nope")])
    def test_not_home_assistant(self, answer):
        with pytest.raises(HaError, match="isn't Home Assistant"):
            HomeAssistant(URL, TOKEN, fetch=fake_fetch({("GET", "/api/"): answer})).check()

    def test_states(self):
        fetch = fake_fetch({("GET", "/api/states"): (200, [{"entity_id": "light.a"}, {"bad": 1}])})
        assert HomeAssistant(URL, TOKEN, fetch=fetch).states() == [{"entity_id": "light.a"}]

    def test_areas_via_template(self):
        calls = []
        rendered = json.dumps([{"area_id": "kitchen", "name": "Kitchen", "entities": ["light.k", 3]},
                               {"area_id": "attic", "name": None, "entities": None}])
        fetch = fake_fetch({("POST", "/api/template"): (200, rendered)}, calls)
        areas = HomeAssistant(URL, TOKEN, fetch=fetch).areas()
        assert areas == [{"area_id": "kitchen", "name": "Kitchen", "entities": ["light.k"]},
                         {"area_id": "attic", "name": "attic", "entities": []}]
        assert calls[0][3] == {"template": AREAS_TEMPLATE}

    def test_template_uses_area_functions(self):
        for fn in ("areas()", "area_name(a)", "area_entities(a)", "namespace(", "to_json"):
            assert fn in AREAS_TEMPLATE

    @pytest.mark.parametrize("answer", [(400, {"message": "Error rendering template"}), (401, ""),
                                        urllib.error.URLError("down"), (200, "not json at all")])
    def test_areas_degrade_to_none(self, answer):
        assert HomeAssistant(URL, TOKEN, fetch=fake_fetch({("POST", "/api/template"): answer})).areas() == []

    def test_parse_areas_tolerates_junk(self):
        assert parse_areas("[1, {\"x\": 2}]") == []
        assert parse_areas({"a": 1}) == []

    def test_run_calls_the_service_synchronously(self):
        calls = []
        fetch = fake_fetch({("POST", "/api/services/homeassistant/toggle"): (200, [])}, calls)
        HomeAssistant(URL, TOKEN, fetch=fetch).run("toggle", "light.kitchen")
        assert calls[0][1] == "/api/services/homeassistant/toggle"
        assert calls[0][3] == {"entity_id": "light.kitchen"}

    @pytest.mark.parametrize("kind, path", [
        ("scene", "/api/services/scene/turn_on"),
        ("script", "/api/services/script/turn_on"),
        ("toggle", "/api/services/homeassistant/toggle"),
        ("automation", "/api/services/automation/trigger"),
    ])
    def test_key_kinds_map_to_services(self, kind, path):
        ha = HomeAssistant(URL, TOKEN)
        sent = []
        ha._background = lambda method, p, data: sent.append((method, p, data))
        ha(kind, {"entity": "x.y"})
        assert sent == [("POST", path, {"entity_id": "x.y"})]

    def test_missing_entity_does_nothing_and_logs_once(self, caplog):
        ha = HomeAssistant(URL, TOKEN)
        sent = []
        ha._background = lambda *a: sent.append(a)
        ha.missing = frozenset({"scene.gone"})
        with caplog.at_level(logging.WARNING):
            for _ in range(3):
                ha("scene", {"entity": "scene.gone"})
        assert sent == []
        assert caplog.text.count("scene.gone") == 1

    def test_background_post_swallows_failures(self, caplog):
        ha = HomeAssistant(URL, TOKEN, fetch=fake_fetch({("POST", "/api/services/scene/turn_on"): OSError("down")}))
        with caplog.at_level(logging.DEBUG):
            ha._post("/api/services/scene/turn_on", {"entity_id": "scene.a"})
        assert TOKEN not in caplog.text


# -- discovery ------------------------------------------------------------------

# Real `avahi-browse -rtp _home-assistant._tcp` output shape (moOde, Pi 4),
# with a second, made-up instance using escapes and no internal_url.
AVAHI = """\
+;wlan0;IPv6;Home;_home-assistant._tcp;local
+;wlan0;IPv4;Home;_home-assistant._tcp;local
=;wlan0;IPv4;Home;_home-assistant._tcp;local;0123abcd.local;192.168.88.159;8123;"base_url=https://ha.example.net" "internal_url=http://192.168.88.159:8123" "external_url=https://ha.example.net" "version=2026.9.4" "uuid=0123abcd" "location_name=Home"
=;wlan0;IPv6;Home;_home-assistant._tcp;local;0123abcd.local;192.168.88.159;8123;"base_url=https://ha.example.net" "internal_url=http://192.168.88.159:8123" "external_url=https://ha.example.net" "version=2026.9.4" "uuid=0123abcd" "location_name=Home"
=;eth0;IPv6;Casa\\032Nueva;_home-assistant._tcp;local;casa.local;fe80::1234;8123;"version=2024.1.0" "uuid=feedbeef"
=;eth0;IPv4;Casa\\032Nueva;_home-assistant._tcp;local;casa.local;192.168.1.50;8123;"version=2024.1.0" "uuid=feedbeef"
"""


class TestDiscovery:
    def test_parse_real_output(self):
        found = discover.parse_avahi(AVAHI)
        assert found == [
            {"name": "Home", "url": "http://192.168.88.159:8123", "uuid": "0123abcd"},
            {"name": "Casa Nueva", "url": "http://192.168.1.50:8123", "uuid": "feedbeef"},
        ]

    def test_txt_parsing(self):
        assert discover.parse_txt('"a=1" "b=two words" "junk"') == {"a": "1", "b": "two words"}

    def test_missing_avahi_says_so(self):
        def runner(*a, **k):
            raise FileNotFoundError("avahi-browse")
        instances, note = discover.discover(runner=runner, probe=lambda url: False)
        assert instances == [] and "avahi-utils" in note

    def test_timeout_keeps_partial_output(self):
        def runner(*a, **k):
            raise subprocess.TimeoutExpired(a[0], 4, output=AVAHI.encode())
        instances, _ = discover.browse(runner=runner)
        assert len(instances) == 2

    def test_fallback_probe_adds_homeassistant_local(self):
        runner = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr="")
        instances, note = discover.discover(runner=runner, probe=lambda url: True)
        assert instances == [{"name": "homeassistant.local", "url": "http://homeassistant.local:8123"}]
        assert note is None

    def test_nothing_found(self):
        runner = lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr="")
        instances, note = discover.discover(runner=runner, probe=lambda url: False)
        assert instances == [] and "No Home Assistant" in note

    def test_probe_accepts_401_without_a_token(self):
        seen = []

        def fetch(method, url, headers, body, timeout):
            seen.append(headers)
            return 401, b"401: Unauthorized"
        assert discover.probe_fallback(fetch=fetch)
        assert seen == [{}]
        assert not discover.probe_fallback(fetch=lambda *a: (_ for _ in ()).throw(OSError()))


# -- verbs: page vocabulary and actions ----------------------------------------------


class TestVerbs:
    @pytest.mark.parametrize("verb, entity, kind", [
        ("ha_scene", "scene.evening", "scene"),
        ("ha_script", "script.good_night", "script"),
        ("ha_toggle", "light.kitchen", "toggle"),
        ("ha_toggle", "cover.blinds", "toggle"),
        ("ha_automation", "automation.lights_out", "automation"),
    ])
    def test_round_trip_and_dispatch(self, actions, verb, entity, kind):
        errors = []
        entry = keymap.action_from_page({"do": verb, "entity": entity}, "keys[0].hold", set(), None, errors)
        assert errors == [] and entry == {"do": verb, "entity": entity}
        assert keymap.to_page(entry) == {"do": verb, "entity": entity}
        fired = []
        actions.ha = lambda k, p: fired.append((k, p))
        actions.variant.actions["k1.hold"] = entry
        actions.dispatch(Event("k1", "hold"))
        assert fired == [(kind, {"entity": entity})]

    @pytest.mark.parametrize("verb, entity, words", [
        ("ha_scene", "light.kitchen", "isn't a scene"),
        ("ha_script", "scene.evening", "isn't a script"),
        ("ha_toggle", "scene.evening", "isn't a light, switch"),
        ("ha_toggle", "sensor.temp", "isn't a light"),
        ("ha_automation", "script.x", "isn't a automation"),
        ("ha_scene", "Scene.Bad", "scene.living_room"),
        ("ha_toggle", "kitchen", "light.kitchen"),
        ("ha_toggle", "", "pick a Home Assistant entity"),
    ])
    def test_validation_names_the_field(self, verb, entity, words):
        errors = []
        assert keymap.action_from_page({"do": verb, "entity": entity}, "keys[2].hold", set(), None, errors) is None
        assert errors[0]["where"] == "keys[2].hold"
        assert words in errors[0]["message"]

    def test_menu_carries_domain_hints(self):
        verbs = {v["do"]: v for v in keymap.VERBS}
        assert verbs["ha_toggle"]["domains"] == ["light", "switch", "input_boolean", "fan", "cover", "media_player"]
        assert verbs["ha_script"]["params"] == ["entity"]
        assert set(keymap.HA_VERBS) == {"ha_scene", "ha_script", "ha_toggle", "ha_automation"}

    def test_config_doc_ha_flag_accepts_callable_or_api(self, actions, tmp_path):
        assert keymap.config_doc(actions, {}, lambda: True)["ha"] is True
        assert keymap.config_doc(actions, {}, False)["ha"] is False
        api = make_api(actions, tmp_path)
        assert keymap.config_doc(actions, {}, api)["ha"] is False
        api.connect({"url": URL, "token": TOKEN})
        assert keymap.config_doc(actions, {}, api)["ha"] is True

    def test_not_connected_logs_once(self, actions, caplog):
        actions.variant.actions["k1.hold"] = {"do": "ha_toggle", "entity": "light.kitchen"}
        with caplog.at_level(logging.INFO):
            for _ in range(3):
                actions.dispatch(Event("k1", "hold"))
        assert caplog.text.count("not connected") == 1


# -- HaApi ------------------------------------------------------------------------


def assert_no_token(doc):
    assert TOKEN not in json.dumps(doc)


class TestHaApi:
    def test_load_without_settings(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        actions.ha = "stale"
        api.load(verify=False)
        assert actions.ha is None and not api.connected

    def test_load_from_yaml_sets_actions_ha(self, actions, tmp_path):
        factory = Factory()
        api = make_api(actions, tmp_path, {"url": URL, "token": TOKEN}, factory)
        api.load(verify=False)
        assert api.connected and actions.ha is factory.made[0] and factory.made[0].token == TOKEN

    def test_load_verifies_in_background(self, actions, tmp_path):
        ha_config.save(tmp_path, ha_config.HaSettings(url=URL, token=TOKEN))
        api = make_api(actions, tmp_path)
        api.load()
        api._verifier.join(2)
        assert api.info["location_name"] == "Home"
        assert ha_config.load(tmp_path).instance["name"] == "Home"   # fingerprint persisted

    def test_get_status_shape_and_no_token(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        code, doc = api.route("GET", "/api/ha", None)
        assert code == 200
        assert doc == {"configured": False, "connected": False, "url": "", "location": "", "version": "",
                       "publish": False, "error": None, "missing": [], "previous_instance": None}
        api.connect({"url": URL, "token": TOKEN})
        code, doc = api.route("GET", "/api/ha", None)
        assert doc["connected"] and doc["url"] == URL and doc["location"] == "Home" and doc["version"] == "2026.9.4"
        assert_no_token(doc)

    def test_post_connects_saves_and_hot_swaps(self, actions, tmp_path):
        factory = Factory()
        api = make_api(actions, tmp_path, factory=factory)
        code, doc = api.route("POST", "/api/ha", {"url": "192.168.1.10:8123/", "token": TOKEN, "publish": True})
        assert code == 200 and doc["connected"] and doc["publish"] and doc["url"] == URL
        assert_no_token(doc)
        assert actions.ha is factory.made[-1]
        saved = json.loads((tmp_path / "ha.json").read_text())
        assert saved["token"] == TOKEN and saved["url"] == URL

    def test_post_rejects_bad_url_and_missing_token(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        code, doc = api.route("POST", "/api/ha", {"url": "ftp://x", "token": TOKEN})
        assert code == 400 and doc["ok"] is False
        code, doc = api.route("POST", "/api/ha", {"url": URL, "token": ""})
        assert code == 400 and "long-lived access token" in doc["error"]
        assert actions.ha is None

    def test_post_bad_token_is_400_and_keeps_old_client(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        api.connect({"url": URL, "token": TOKEN})
        old = actions.ha
        api.client_factory = Factory(fail="Home Assistant rejected the token - create a new one")
        code, doc = api.route("POST", "/api/ha", {"url": "http://other:8123", "token": "wrong"})
        assert code == 400 and doc == {"ok": False, "error": "Home Assistant rejected the token - create a new one"}
        assert actions.ha is old

    def test_empty_token_keeps_saved_one_for_same_url(self, actions, tmp_path):
        factory = Factory()
        api = make_api(actions, tmp_path, factory=factory)
        api.connect({"url": URL, "token": TOKEN})
        code, _ = api.route("POST", "/api/ha", {"url": URL + "/", "token": "", "publish": True})
        assert code == 200 and factory.made[-1].token == TOKEN
        code, _ = api.route("POST", "/api/ha", {"url": "http://elsewhere:8123", "token": ""})
        assert code == 400

    def test_patch_publish(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        assert api.route("PATCH", "/api/ha", {"publish": True})[0] == 409
        api.connect({"url": URL, "token": TOKEN})
        code, doc = api.route("PATCH", "/api/ha", {"publish": True})
        assert code == 200 and doc["publish"] is True
        assert json.loads((tmp_path / "ha.json").read_text())["publish"] is True
        assert api.route("PATCH", "/api/ha", {"publish": "yes"})[0] == 400
        api.route("PATCH", "/api/ha", {"publish": False})
        assert api.client.deleted == [SENSOR]

    def test_delete_forgets_and_clears_actions_ha(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        api.connect({"url": URL, "token": TOKEN})
        code, doc = api.route("DELETE", "/api/ha", None)
        assert code == 200 and not doc["configured"] and not doc["connected"]
        assert actions.ha is None and not (tmp_path / "ha.json").exists()

    def test_discover_route_strips_uuid_but_remembers_it(self, actions, tmp_path):
        found = [{"name": "Home", "url": URL, "uuid": "u-1"}]
        api = make_api(actions, tmp_path, discover_fn=lambda: (found, None))
        code, doc = api.route("GET", "/api/ha/discover", None)
        assert code == 200 and doc == {"instances": [{"name": "Home", "url": URL}], "note": None}
        api.connect({"url": URL, "token": TOKEN})
        assert api.settings.instance["uuid"] == "u-1"

    def test_discover_failure_is_a_note(self, actions, tmp_path):
        def boom():
            raise RuntimeError("x")
        code, doc = make_api(actions, tmp_path, discover_fn=boom).route("GET", "/api/ha/discover", None)
        assert code == 200 and doc["instances"] == [] and doc["note"]

    def test_entities_grouped_filtered_and_cached(self, actions, tmp_path):
        clock = Clock()
        api = make_api(actions, tmp_path, clock=clock)
        assert api.route("GET", "/api/ha/entities", None) == (409, {"error": "not connected"})
        api.connect({"url": URL, "token": TOKEN})
        client = api.client
        code, doc = api.route("GET", "/api/ha/entities", None)
        assert code == 200
        assert doc["areas"] == [{"id": "kitchen", "name": "Kitchen"}]
        ids = [e["entity_id"] for e in doc["entities"]]
        assert ids == ["scene.evening", "script.good_night", "light.kitchen"]     # sensor dropped, domain order
        light = doc["entities"][2]
        assert light == {"entity_id": "light.kitchen", "name": "Kitchen light", "domain": "light",
                         "area": "kitchen", "state": "on"}
        assert doc["entities"][1]["name"] == "script.good_night"                # no friendly_name
        calls = client.state_calls
        api.route("GET", "/api/ha/entities", None)
        assert client.state_calls == calls                                       # cached
        api.route("GET", "/api/ha/entities?refresh=1", None)
        assert client.state_calls == calls + 1
        clock.t += 61
        api.route("GET", "/api/ha/entities", None)
        assert client.state_calls == calls + 2

    def test_try(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        assert api.route("POST", "/api/ha/try", {"do": "ha_toggle", "entity": "light.kitchen"})[0] == 409
        api.connect({"url": URL, "token": TOKEN})
        assert api.route("POST", "/api/ha/try", {"do": "ha_toggle", "entity": "light.kitchen"}) == (200, {"ok": True})
        assert api.client.ran == [("toggle", "light.kitchen")]
        code, doc = api.route("POST", "/api/ha/try", {"do": "ha_scene", "entity": "light.kitchen"})
        assert code == 400 and "isn't a scene" in doc["error"]
        assert api.route("POST", "/api/ha/try", {"do": "preset", "entity": "x.y"})[0] == 400

    def test_try_failure_is_reported(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        api.connect({"url": URL, "token": TOKEN})

        def fail(kind, entity):
            raise HaError("Can't reach that address")
        api.client.run = fail
        assert api.route("POST", "/api/ha/try", {"do": "ha_script", "entity": "script.x"}) == \
            (502, {"ok": False, "error": "Can't reach that address"})

    def test_routing_edges(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        assert api.route("GET", "/api/config", None) is None
        assert api.route("GET", "/api/hammer", None) is None
        assert api.route("GET", "/api/ha/nope", None)[0] == 404
        assert api.route("PUT", "/api/ha", {})[0] == 405
        assert api.route("GET", "/api/ha/", None)[0] == 200

    def test_token_never_echoed_by_any_route(self, actions, tmp_path):
        api = make_api(actions, tmp_path, discover_fn=lambda: ([{"name": "H", "url": URL}], None))
        bodies = [
            ("POST", "/api/ha", {"url": URL, "token": TOKEN, "publish": True}),
            ("GET", "/api/ha", None), ("PATCH", "/api/ha", {"publish": False}),
            ("GET", "/api/ha/discover", None), ("GET", "/api/ha/entities", None),
            ("POST", "/api/ha/try", {"do": "ha_scene", "entity": "scene.evening"}),
            ("POST", "/api/ha/clear-missing", None), ("POST", "/api/ha", {"url": "bad url", "token": TOKEN}),
            ("DELETE", "/api/ha", None),
        ]
        for method, path, body in bodies:
            assert_no_token(api.route(method, path, body))

    def test_internal_error_is_500_without_details(self, actions, tmp_path):
        api = make_api(actions, tmp_path)
        api.connect({"url": URL, "token": TOKEN})
        api.client.states = lambda: (_ for _ in ()).throw(RuntimeError(TOKEN))
        api._states = None
        code, doc = api.route("GET", "/api/ha/entities", None)
        assert code == 500 and TOKEN not in json.dumps(doc)


# -- moving house: missing actions and instance identity -------------------------------


class TestMissing:
    def bind(self, actions):
        actions.variant.actions["k1.hold"] = {"do": "ha_scene", "entity": "scene.evening"}     # exists
        actions.variant.actions["k2.hold"] = {"do": "ha_scene", "entity": "scene.old_home"}    # doesn't
        actions.variant.actions["k3.hold"] = {"do": "ha_toggle", "entity": "light.attic"}      # doesn't

    def test_not_connected_reports_every_ha_action(self, actions, tmp_path):
        self.bind(actions)
        doc = make_api(actions, tmp_path).route("GET", "/api/ha", None)[1]
        assert {(m["key"], m["reason"]) for m in doc["missing"]} == \
            {("k1", "not connected"), ("k2", "not connected"), ("k3", "not connected")}

    def test_connected_reports_unknown_entities(self, actions, tmp_path):
        self.bind(actions)
        api = make_api(actions, tmp_path)
        api.connect({"url": URL, "token": TOKEN})
        missing = api.route("GET", "/api/ha", None)[1]["missing"]
        assert missing == [
            {"key": "k2", "event": "hold", "do": "ha_scene", "entity": "scene.old_home",
             "label": "Key 2 hold: scene.old_home", "reason": "not found"},
            {"key": "k3", "event": "hold", "do": "ha_toggle", "entity": "light.attic",
             "label": "Key 3 hold: light.attic", "reason": "not found"},
        ]
        # The key bound to a missing entity is gated in the client.
        assert api.client.missing == frozenset({"scene.old_home", "light.attic"})

    def test_pressing_a_missing_key_does_nothing_and_logs_once(self, actions, tmp_path, caplog):
        self.bind(actions)
        ha = HomeAssistant(URL, TOKEN, fetch=fake_fetch({}))
        ha.missing = frozenset({"scene.old_home"})
        sent = []
        ha._background = lambda *a: sent.append(a)
        actions.ha = ha
        with caplog.at_level(logging.WARNING):
            for _ in range(4):
                actions.dispatch(Event("k2", "hold"))
            actions.dispatch(Event("k1", "hold"))
        assert len(sent) == 1                                  # only k1's scene went out
        assert caplog.text.count("scene.old_home") == 1

    def test_clear_missing_updates_layout_and_keys_yaml(self, actions, tmp_path):
        self.bind(actions)
        actions.variant.actions["k5.hold"] = {"do": "volume_step", "delta": -2}
        api = make_api(actions, tmp_path)
        api.connect({"url": URL, "token": TOKEN})
        code, doc = api.route("POST", "/api/ha/clear-missing", None)
        assert code == 200 and doc["cleared"] == 2 and doc["missing"] == []
        table = actions.variant.actions
        assert "k2.hold" not in table and "k3.hold" not in table
        assert table["k1.hold"] == {"do": "ha_scene", "entity": "scene.evening"}
        saved = yaml.safe_load((tmp_path / "keys.yaml").read_text())
        assert saved["actions"]["k2.hold"] is None and saved["actions"]["k3.hold"] is None
        assert saved["actions"]["k1.hold"] == {"do": "ha_scene", "entity": "scene.evening"}
        assert saved["actions"]["k5.hold"] == {"do": "volume_step", "delta": -2}
        assert saved["actions"]["k1.tap"] == {"do": "preset", "n": 1}
        # The overlay reloads to the same layout.
        fresh = Actions(load_variant(VARIANTS / "hasler-hm.yaml"), FakePlayer())
        keymap.apply_saved(tmp_path / "keys.yaml", fresh, None)
        assert "k2.hold" not in fresh.variant.actions and fresh.variant.actions["k1.hold"]["entity"] == "scene.evening"

    def test_clear_missing_when_not_connected_clears_all_ha_actions(self, actions, tmp_path):
        self.bind(actions)
        doc = make_api(actions, tmp_path).route("POST", "/api/ha/clear-missing", None)[1]
        assert doc["cleared"] == 3 and keymap.ha_entries(actions) == []

    def test_another_home_is_reported(self, actions, tmp_path):
        self.bind(actions)
        old = Factory(states=[{"entity_id": e, "state": "", "attributes": {}}
                              for e in ("scene.evening", "scene.old_home", "light.attic")],
                      info={"location_name": "Casa Vieja", "time_zone": "Europe/Madrid", "latitude": 40.4, "longitude": -3.7})
        api = make_api(actions, tmp_path, factory=old)
        api.connect({"url": URL, "token": TOKEN})
        doc = api.route("GET", "/api/ha", None)[1]
        assert doc["missing"] == [] and doc["previous_instance"] is None
        assert ha_config.load_layout_instance(tmp_path)["name"] == "Casa Vieja"
        api.route("DELETE", "/api/ha", None)
        # Moved house: a different HA, which has none of those entities.
        api.client_factory = Factory(states=[], info={"location_name": "Flat", "time_zone": "Europe/London",
                                                      "latitude": 51.5, "longitude": -0.1})
        api.route("POST", "/api/ha", {"url": "http://10.0.0.2:8123", "token": "t2"})
        doc = api.route("GET", "/api/ha", None)[1]
        assert doc["previous_instance"] == "Casa Vieja" and doc["location"] == "Flat"
        assert len(doc["missing"]) == 3
        doc = api.route("POST", "/api/ha/clear-missing", None)[1]
        assert doc["previous_instance"] is None and doc["missing"] == []
        assert ha_config.load_layout_instance(tmp_path)["name"] == "Flat"

    def test_same_home_new_address_is_not_another_home(self, actions, tmp_path):
        self.bind(actions)
        api = make_api(actions, tmp_path)
        api.connect({"url": URL, "token": TOKEN})
        ha_config.save_layout_instance(tmp_path, api.settings.instance)
        api.route("POST", "/api/ha", {"url": "http://10.0.0.9:8123", "token": TOKEN})
        assert api.route("GET", "/api/ha", None)[1]["previous_instance"] is None


# -- publishing ---------------------------------------------------------------------


PLAYING = {"playing": {"n": 1, "name": "Radio Clásica", "source": "radio"}, "standby": False,
           "source": "radio", "waiting": False, "track": None}


class TestPublish:
    def rig(self, actions, tmp_path, publish=True):
        clock = Clock()
        api = make_api(actions, tmp_path, clock=clock)
        api.connect({"url": URL, "token": TOKEN, "publish": publish})
        return api, clock

    def test_off_by_default(self, actions, tmp_path):
        api, _ = self.rig(actions, tmp_path, publish=False)
        assert api.publish(PLAYING) is False and api.client.pushed == []

    def test_sensor_payload(self, actions, tmp_path):
        api, _ = self.rig(actions, tmp_path)
        assert api.publish(PLAYING)
        entity, state, attrs = api.client.pushed[0]
        assert entity == "sensor.hasler_radio" and state == "playing"
        assert attrs == {"friendly_name": "Hasler radio", "icon": "mdi:radio", "station": "Radio Clásica",
                         "title": "", "artist": "", "source": "radio"}

    def test_states(self):
        assert HaApi.sensor({"standby": True, "playing": PLAYING["playing"]})["state"] == "standby"
        assert HaApi.sensor({"source": "radio"})["state"] == "idle"
        assert HaApi.sensor({"source": "bluetooth", "waiting": True})["state"] == "idle"
        doc = HaApi.sensor({"source": "ytm", "playing": {"name": "Mix"}, "track": {"title": "Song", "artist": "Band"}})
        assert doc["attributes"]["title"] == "Song" and doc["attributes"]["artist"] == "Band"

    def test_throttled_and_only_on_change(self, actions, tmp_path):
        api, clock = self.rig(actions, tmp_path)
        assert api.publish(PLAYING)
        clock.t += 1
        assert not api.publish({**PLAYING, "standby": True})         # within 5 s
        clock.t += 5
        assert not api.publish(PLAYING)                              # unchanged
        assert api.publish({**PLAYING, "standby": True})             # changed, 6 s later
        clock.t += 601
        assert api.publish({**PLAYING, "standby": True})             # re-push after HA may have restarted
        assert [p[1] for p in api.client.pushed] == ["playing", "standby", "standby"]

    def test_never_raises(self, actions, tmp_path):
        api, _ = self.rig(actions, tmp_path)
        api.client.set_state_async = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
        assert api.publish(PLAYING) is False
        assert api.publish(None) is False

    def test_disconnect_removes_the_sensor(self, actions, tmp_path):
        api, _ = self.rig(actions, tmp_path)
        client = api.client
        api.route("DELETE", "/api/ha", None)
        assert client.deleted == [SENSOR]
        assert api.publish(PLAYING) is False
