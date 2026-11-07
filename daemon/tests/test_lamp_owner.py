"""The owner's lamp: meanings, Show me, brightness, key replies, Identify, status "lamp".

lamp/meanings.py, web/lamp_api.py, the WebApp endpoints and the LampKeeper's
side. Each engine call is tried against an engine of the new light language
(preview_look, preview_reply, set_brightness, set_key_replies, identify) and
against today's (preview, reply only), which must still answer.
"""

import json
import urllib.error
import urllib.request

import pytest
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.lamp import engine as eng
from haslerd.lamp import meanings
from haslerd.lamp.contract import BoxState, Look
from haslerd.lamp.engine import LampEngine
from haslerd.lamp.outputs import FakeOutput
from haslerd.main import LampKeeper
from haslerd.multiroom.peers import Peer, PeerUnreachable
from haslerd.player.base import FakePlayer
from haslerd.variant import Panel, load_variant
from haslerd.web import lamp_api
from haslerd.web.server import WebApp, WebServer, load_settings


class OldLamp:
    """Today's engine as far as these calls go: preview and reply only."""

    def __init__(self):
        self.palette = dict(eng.DEFAULT_PALETTE)
        self.base_palette = dict(eng.DEFAULT_PALETTE)
        self.previews, self.replies, self.shows = [], [], []

    def preview(self, rgb, level=1.0, hold_s=eng.PREVIEW_S):
        self.previews.append((rgb, level, hold_s))

    def reply(self, name):
        self.replies.append(name)

    def show(self, look, fade_s=0.0):
        self.shows.append(look)

    def countdown(self, *a): ...

    def set_night(self, *a): ...

    def set_palette(self, *a): ...


class NewLamp(OldLamp):
    """The new light language's engine, as far as the page calls it."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def preview_look(self, name, seconds):
        self.calls.append(("look", name, seconds))

    def preview_reply(self, name):
        self.calls.append(("reply", name))

    def stop_preview(self):
        self.calls.append(("stop",))

    def set_brightness(self, name):
        self.calls.append(("brightness", name))

    def set_key_replies(self, on):
        self.calls.append(("key_replies", on))

    def identify(self, seconds):
        self.calls.append(("identify", seconds))


def make_app(tmp_path, lamp=None, settings_file=True):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 settings_file=tmp_path / "settings.json" if settings_file else None)
    app.lamp = lamp
    return app


# -- meanings ---------------------------------------------------------------


class TestMeaningOf:
    @pytest.mark.parametrize("state, want", [
        (BoxState(shutting_down=True, standby=True), "busy"),
        (BoxState(updating=True, casting=True), "busy"),
        (BoxState(hotspot=True, standby=True), "setup"),
        (BoxState(casting=True, standby=True), "phone"),
        (BoxState(standby=True), "asleep"),
        (BoxState(boot="daemon"), "starting"),
        (BoxState(pairing=True), "pairing"),
        (BoxState(loading=True, playing=False), "starting"),
        (BoxState(group="joining"), "starting"),
        (BoxState(group="following", group_playing=True), "playing"),
        (BoxState(offline_s=120.0), "offline"),
        (BoxState(offline_s=120.0, night=True), "ready"),
        (BoxState(offline_s=30.0), "ready"),
        (BoxState(playing=True), "playing"),
        (BoxState(), "ready"),
    ])
    def test_follows_the_policys_priority(self, state, want):
        assert meanings.meaning_of(state) == want

    def test_a_policy_that_knows_better_wins(self):
        class Policy:
            @staticmethod
            def meaning_for(state):
                return "phone"
        assert meanings.meaning_of(BoxState(standby=True), Policy) == "phone"

    def test_a_policy_answer_outside_the_list_is_ignored(self):
        class Policy:
            @staticmethod
            def meaning_for(state):
                return "disco"
        assert meanings.meaning_of(BoxState(standby=True), Policy) == "asleep"

    def test_every_show_me_has_a_fallback(self):
        for name in meanings.LOOKS + meanings.REPLIES:
            assert name in meanings.FALLBACK_LOOK or name in meanings.FALLBACK_REPLY, name


class TestPrefs:
    def test_defaults(self):
        assert meanings.prefs_doc({}) == {"brightness": "normal", "key_replies": True}
        assert meanings.prefs_doc({"lamp": {"brightness": "loud", "key_replies": "yes"}}) == \
            {"brightness": "normal", "key_replies": True}
        assert meanings.prefs_doc({"lamp": "low"}) == {"brightness": "normal", "key_replies": True}

    @pytest.mark.parametrize("bad", [None, {}, "low", {"brightness": "dazzling"},
                                     {"key_replies": 1}, {"brightness": "low", "colour": "red"}])
    def test_rejects(self, bad):
        with pytest.raises(ValueError):
            meanings.prefs_from(bad)


# -- the API: Show me ---------------------------------------------------------


class TestPreviewNamed:
    def test_a_look_goes_to_the_new_engine_by_name(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        doc = app.lamp_preview({"look": "asleep"})
        assert doc == {"lamp": True, "showing": True, "look": "asleep", "expires_s": meanings.SHOW_S}
        assert app.lamp.calls == [("look", "asleep", meanings.SHOW_S)]
        assert app.lamp.previews == []

    def test_a_reply_goes_to_the_new_engine_by_name(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        doc = app.lamp_preview({"reply": "heard"})
        assert doc["reply"] == "heard" and doc["showing"] and doc["expires_s"] == 0
        assert app.lamp.calls == [("reply", "heard")]

    def test_null_ends_it(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        assert app.lamp_preview({"look": None})["showing"] is False
        assert app.lamp.calls == [("stop",)]
        old = make_app(tmp_path, OldLamp())
        old.lamp_preview({"look": None})
        assert old.lamp.previews == [(None, 1.0, eng.PREVIEW_S)]

    def test_todays_engine_shows_a_still_colour_for_five_seconds(self, tmp_path):
        app = make_app(tmp_path, OldLamp())
        app.lamp_preview({"look": "phone"})
        assert app.lamp.previews == [(eng.DEFAULT_PALETTE["blue"], 0.80, meanings.SHOW_S)]

    def test_todays_engine_uses_its_own_palette(self, tmp_path):
        lamp = OldLamp()
        lamp.palette["amber"] = (1.0, 0.3, 0.0)
        app = make_app(tmp_path, lamp)
        app.lamp_preview({"look": "playing"})
        assert lamp.previews[-1][0] == (1.0, 0.3, 0.0)

    @pytest.mark.parametrize("name, reply", [("heard", "ack"), ("no", "fail")])
    def test_todays_engine_plays_the_old_reply(self, tmp_path, name, reply):
        app = make_app(tmp_path, OldLamp())
        app.lamp_preview({"reply": name})
        assert app.lamp.replies == [reply]

    def test_asking_on_todays_engine_is_the_confirm_double_dip(self, tmp_path):
        app = make_app(tmp_path, OldLamp())
        app.lamp_preview({"look": "asking"})
        assert app.lamp.replies == ["ha"] and app.lamp.previews == []

    def test_every_meaning_works_on_both_engines(self, tmp_path):
        for lamp in (OldLamp(), NewLamp()):
            app = make_app(tmp_path, lamp)
            for name in meanings.LOOKS:
                assert app.lamp_preview({"look": name})["showing"]
            for name in meanings.REPLIES:
                assert app.lamp_preview({"reply": name})["showing"]

    def test_no_lamp_answers_without_showing(self, tmp_path):
        app = make_app(tmp_path, None)
        assert app.lamp_preview({"look": "ready"}) == \
            {"lamp": False, "showing": False, "look": "ready", "expires_s": meanings.SHOW_S}

    @pytest.mark.parametrize("bad", [{"look": "disco"}, {"reply": "ready"}, {"look": "ready", "reply": "no"},
                                     {"reply": None}, {"look": 3}, {}])
    def test_rejects(self, tmp_path, bad):
        app = make_app(tmp_path, NewLamp())
        with pytest.raises(ValueError):
            app.lamp_preview(bad)
        assert app.lamp.calls == []

    def test_the_rgb_preview_for_maker_tools_is_unchanged(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        app.lamp_preview({"rgb": [1, 0.1, 0], "level": "full"})
        assert app.lamp.previews == [([1.0, 0.1, 0.0], 1.0, eng.PREVIEW_S)]
        assert app.lamp.calls == []


# -- the API: brightness and key replies ---------------------------------------


class TestPrefsApi:
    def test_settings_show_the_defaults_and_the_lamp(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        doc = app.settings()["lamp"]
        callsign = doc.pop("callsign")
        assert doc == {"brightness": "normal", "key_replies": True, "present": True,
                       "show_s": meanings.SHOW_S, "identify_s": meanings.IDENTIFY_S,
                       "geek": False, "callsign_saved": "",
                       "looks": list(meanings.LOOKS), "replies": list(meanings.REPLIES)}
        assert len(callsign) == 1 and callsign.isalnum() and callsign.isupper()   # the room's initial
        assert make_app(tmp_path, None).settings()["lamp"]["present"] is False

    def test_geek_mode_and_callsign_persist_and_reach_the_engine(self, tmp_path):
        from haslerd.web import lamp_api
        app = make_app(tmp_path, NewLamp())
        heard = []
        app.lamp_listeners.append(heard.append)
        app.put_settings({"lamp": {"brightness": "low"}})
        doc = app.put_settings({"lamp": {"geek": True, "callsign": " pa3 "}})
        assert doc["lamp"]["geek"] is True and doc["lamp"]["callsign"] == "PA3"
        assert doc["lamp"]["brightness"] == "low"                    # the rest kept
        assert heard[-1]["lamp"] == {"brightness": "low", "key_replies": True, "geek": True, "callsign": "PA3"}
        app.put_settings({"lamp": {"brightness": "bright"}})
        assert app.settings()["lamp"]["geek"] is True                # brightness alone keeps geek
        app.put_settings({"lamp": {"callsign": ""}})
        assert app.settings()["lamp"]["callsign_saved"] == ""        # back to the room's initial
        for bad in ({"geek": "yes"}, {"callsign": "ABCD"}, {"callsign": "a-b"}, {"callsign": 3}, {}):
            with pytest.raises(ValueError):
                app.put_settings({"lamp": bad})

        class Geeky:
            def __init__(self):
                self.calls = []

            def set_geek(self, on):
                self.calls.append(("geek", on))

            def set_callsign(self, sign):
                self.calls.append(("callsign", sign))

            def play_egg(self, name):
                self.calls.append(("egg", name))

        g = Geeky()
        assert lamp_api.apply_geek(g, lamp_api.geek_doc({"lamp": {"geek": True}, "room": {"name": "kitchen"}})) == \
            {"geek": True, "callsign": True}
        assert g.calls == [("geek", True), ("callsign", "K")]
        assert lamp_api.apply_geek(object(), {"geek": True}) == {"geek": False, "callsign": False}
        app.lamp = g
        app.put_settings({"lamp": {"geek": False}})
        assert app.lamp_egg({}) == {"played": False, "geek": False}  # no surprises outside geek mode
        app.put_settings({"lamp": {"geek": True}})
        assert app.lamp_egg({})["played"] is True and g.calls[-1][0] == "egg"

    def test_put_persists_merges_and_tells_the_listeners(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        heard = []
        app.lamp_listeners.append(heard.append)
        app.put_settings({"night": "off"})
        doc = app.put_settings({"lamp": {"brightness": "low"}})
        assert doc["lamp"]["brightness"] == "low" and doc["lamp"]["key_replies"] is True
        app.put_settings({"lamp": {"key_replies": False}})
        saved = load_settings(tmp_path / "settings.json")
        assert saved["lamp"] == {"brightness": "low", "key_replies": False}
        assert saved["night"] == "off"
        assert heard[-1]["lamp"] == {"brightness": "low", "key_replies": False}
        assert app.settings()["lamp"]["key_replies"] is False

    def test_without_a_settings_file_it_lives_in_memory(self, tmp_path):
        app = make_app(tmp_path, NewLamp(), settings_file=False)
        app.put_settings({"lamp": {"brightness": "bright"}})
        assert app.settings()["lamp"]["brightness"] == "bright"

    @pytest.mark.parametrize("bad", [{"lamp": {"brightness": "max"}}, {"lamp": {"key_replies": "off"}},
                                     {"lamp": {}}, {"lamp": None}])
    def test_rejects_and_keeps_what_was_saved(self, tmp_path, bad):
        app = make_app(tmp_path, NewLamp())
        app.put_settings({"lamp": {"brightness": "low"}})
        with pytest.raises(ValueError):
            app.put_settings(bad)
        assert load_settings(tmp_path / "settings.json")["lamp"]["brightness"] == "low"


# -- the API: identify --------------------------------------------------------


class FakePeers:
    def __init__(self, answer=(200, {"ok": True, "lamp": True, "seconds": 10.0}), raises=None):
        self.answer, self.raises, self.sent = answer, raises, []
        self.peer = Peer(id="kitchen-id", name="Kitchen", host="192.168.1.20", port=8080)

    def get(self, peer_id):
        return self.peer if peer_id == self.peer.id else None

    def fetch(self, method, url, body=None, timeout=3.0):
        self.sent.append((method, url, body))
        if self.raises:
            raise self.raises
        return self.answer


class FakeGroup:
    def __init__(self, peers):
        self.peers = peers


class TestIdentify:
    def test_default_ten_seconds_on_the_new_engine(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        assert app.lamp_identify({}) == {"lamp": True, "seconds": 10.0}
        assert app.lamp_identify(None)["seconds"] == 10.0
        assert app.lamp.calls == [("identify", 10.0), ("identify", 10.0)]

    def test_seconds(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        app.lamp_identify({"seconds": 3})
        assert app.lamp.calls == [("identify", 3.0)]

    def test_todays_engine_shows_white_for_that_long(self, tmp_path):
        app = make_app(tmp_path, OldLamp())
        app.lamp_identify({"seconds": 4})
        # The old engine's palette had a warm white; today's falls back to amber.
        colour = eng.DEFAULT_PALETTE.get("white", eng.DEFAULT_PALETTE["amber"])
        assert [(tuple(c), lv, s) for c, lv, s in app.lamp.previews] == [(tuple(colour), 1.0, 4.0)]

    def test_no_lamp(self, tmp_path):
        assert make_app(tmp_path, None).lamp_identify({}) == {"lamp": False, "seconds": 0}

    @pytest.mark.parametrize("bad", [{"seconds": 0}, {"seconds": 61}, {"seconds": "10"}, {"seconds": True},
                                     {"colour": "red"}, [], {"peer": ""}, {"peer": 3}])
    def test_rejects(self, tmp_path, bad):
        app = make_app(tmp_path, NewLamp())
        with pytest.raises(ValueError):
            app.lamp_identify(bad)
        assert app.lamp.calls == []

    def test_asks_another_hasler(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        peers = FakePeers()
        app.group = FakeGroup(peers)
        doc = app.lamp_identify({"peer": "kitchen-id", "seconds": 5})
        assert doc == {"lamp": True, "seconds": 10.0, "peer": "kitchen-id"}
        assert peers.sent == [("POST", "http://192.168.1.20:8080/api/lamp/identify", {"seconds": 5.0})]
        assert app.lamp.calls == []          # not this radio's lamp

    @pytest.mark.parametrize("peers, words", [
        (FakePeers(raises=PeerUnreachable("x")), "didn't answer"),
        (FakePeers(answer=(404, {})), "software update"),
        (FakePeers(answer=(500, {})), "error 500"),
    ])
    def test_another_hasler_that_cant(self, tmp_path, peers, words):
        from haslerd.web.server import Unavailable
        app = make_app(tmp_path, NewLamp())
        app.group = FakeGroup(peers)
        with pytest.raises(Unavailable, match=words):
            app.lamp_identify({"peer": "kitchen-id"})

    def test_an_unknown_hasler_or_no_multiroom(self, tmp_path):
        from haslerd.web.server import Unavailable
        app = make_app(tmp_path, NewLamp())
        with pytest.raises(Unavailable):
            app.lamp_identify({"peer": "kitchen-id"})
        app.group = FakeGroup(FakePeers())
        with pytest.raises(Unavailable):
            app.lamp_identify({"peer": "garage-id"})


# -- /api/status "lamp" -------------------------------------------------------


class TestStatus:
    def test_none_without_a_lamp(self, tmp_path):
        app = make_app(tmp_path, None)
        app.lamp_meaning = lambda: "asleep"
        assert app.status()["lamp"] is None

    def test_the_keepers_meaning(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        app.lamp_meaning = lambda: "phone"
        assert app.status()["lamp"] == "phone"

    def test_a_pending_key_press_is_asking(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        app.lamp_meaning = lambda: "ready"
        app.confirm.pending = lambda: True
        assert app.status()["lamp"] == "asking"

    def test_a_broken_or_odd_meaning_is_none(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        app.lamp_meaning = lambda: "disco"
        assert app.status()["lamp"] is None
        app.lamp_meaning = lambda: 1 / 0
        assert app.status()["lamp"] is None


# -- over HTTP, behind the guard ------------------------------------------------


class TestHttp:
    @pytest.fixture
    def base(self, tmp_path):
        app = make_app(tmp_path, NewLamp())
        server = WebServer(app, host="127.0.0.1", port=0)
        server.start()
        yield f"http://127.0.0.1:{server.port}", app
        server.close()

    def call(self, url, method="GET", body=None, ctype="application/json", headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_round_trip(self, base):
        url, app = base
        status, doc = self.call(url + "/api/lamp/preview", "POST", {"look": "busy"})
        assert status == 200 and doc["ok"] and doc["look"] == "busy"
        status, doc = self.call(url + "/api/lamp/identify", "POST", {"seconds": 2})
        assert status == 200 and doc == {"ok": True, "lamp": True, "seconds": 2.0}
        status, doc = self.call(url + "/api/lamp/identify", "POST")
        assert status == 200 and doc["seconds"] == 10.0
        status, doc = self.call(url + "/api/settings", "PUT", {"lamp": {"brightness": "low"}})
        assert status == 200 and doc["lamp"]["brightness"] == "low"
        assert app.lamp.calls[:3] == [("look", "busy", meanings.SHOW_S), ("identify", 2.0), ("identify", 10.0)]

    def test_bad_input_is_a_400(self, base):
        url, _ = base
        assert self.call(url + "/api/lamp/preview", "POST", {"look": "disco"})[0] == 400
        assert self.call(url + "/api/lamp/identify", "POST", {"seconds": 600})[0] == 400
        assert self.call(url + "/api/settings", "PUT", {"lamp": {"brightness": "max"}})[0] == 400

    def test_an_unknown_peer_is_a_503(self, base):
        url, _ = base
        status, doc = self.call(url + "/api/lamp/identify", "POST", {"peer": "nobody"})
        assert status == 503 and "Haslers" in doc["error"]

    def test_writes_follow_the_csrf_rules(self, base):
        url, app = base
        for method, path, body in (("POST", "/api/lamp/preview", {"look": "ready"}),
                                   ("POST", "/api/lamp/identify", {}),
                                   ("PUT", "/api/settings", {"lamp": {"brightness": "low"}})):
            status, _ = self.call(url + path, method, body, ctype="text/plain")
            assert status == 403, (method, path)
            status, _ = self.call(url + path, method, body, headers={"Origin": "http://evil.example"})
            assert status == 403, ("origin", method, path)
            status, _ = self.call(url + path, method, body, headers={"Host": "evil.example"})
            assert status == 403, ("host", method, path)
        assert app.lamp.calls == []

    def test_status_carries_lamp(self, base):
        url, app = base
        app.lamp_meaning = lambda: "asleep"
        status, doc = self.call(url + "/api/status")
        assert status == 200 and doc["lamp"] == "asleep"


# -- the keeper: brightness, key replies, meaning -------------------------------


def keeper_with(lamp):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    actions = Actions(variant, FakePlayer())
    return LampKeeper(lamp, actions, None, None, None)


class TestKeeper:
    def test_start_and_change_hand_the_new_engine_both_prefs(self):
        lamp = NewLamp()
        keeper = keeper_with(lamp)
        keeper.apply_prefs({})
        keeper.settings_changed({"lamp": {"brightness": "bright", "key_replies": False}})
        assert lamp.calls == [("brightness", "normal"), ("key_replies", True),
                              ("brightness", "bright"), ("key_replies", False)]

    def test_new_engine_gets_every_reply_and_unscaled_looks(self):
        lamp = NewLamp()
        keeper = keeper_with(lamp)
        keeper.apply_prefs({"lamp": {"brightness": "low", "key_replies": False}})
        keeper.reply("ack")
        assert lamp.replies == ["ack"]           # the engine drops it itself
        assert keeper._scaled(Look("amber", 1.0)) == Look("amber", 1.0)

    def test_todays_engine_key_replies_off_drops_key_dips_only(self):
        lamp = OldLamp()
        keeper = keeper_with(lamp)
        keeper.apply_prefs({"lamp": {"key_replies": False}})
        for name in ("ack", "next", "nudge", "fail", "ha"):
            keeper.reply(name)
        assert lamp.replies == ["fail", "ha"]
        keeper.apply_prefs({"lamp": {"key_replies": True}})
        keeper.reply("ack")
        assert lamp.replies[-1] == "ack"

    def test_todays_engine_brightness_scales_looks_but_never_the_ember(self):
        lamp = OldLamp()
        keeper = keeper_with(lamp)
        keeper.apply_prefs({"lamp": {"brightness": "low"}})
        assert keeper._scaled(Look("amber", 1.0, "breath", 1.2, 0.25)) == Look("amber", 0.6, "breath", 1.2, 0.15)
        assert keeper._scaled(Look("ember", 0.22)) == Look("ember", 0.22)
        keeper.apply_prefs({"lamp": {"brightness": "bright"}})
        assert keeper._scaled(Look("amber", 1.0)).level == 1.0
        assert keeper._scaled(Look("amber", 0.35)).level == pytest.approx(0.4375)

    def test_a_brightness_change_shows_again_on_todays_engine(self):
        lamp = OldLamp()
        keeper = keeper_with(lamp)
        keeper.actions.standby = False
        keeper.refresh(0.0)
        first = lamp.shows[-1]
        keeper.settings_changed({"lamp": {"brightness": "low"}})
        keeper.refresh(0.1)
        assert lamp.shows[-1].level == pytest.approx(first.level * 0.6)

    def test_meaning_follows_the_state(self):
        keeper = keeper_with(NewLamp())
        keeper.state = BoxState(standby=True)
        assert keeper.meaning() == "asleep"
        keeper.state = BoxState(casting=True)
        assert keeper.meaning() == "phone"

    def test_a_broken_engine_call_doesnt_break_the_keeper(self):
        class Broken(OldLamp):
            def set_brightness(self, name):
                raise RuntimeError("no")
        keeper = keeper_with(Broken())
        keeper.apply_prefs({"lamp": {"brightness": "low"}})    # logged, no error


def test_the_real_engine_has_the_named_calls():
    """Today's LampEngine offers every call the page uses (no fallback needed)."""
    lamp = LampEngine(FakeOutput(), threaded=False)
    for name in ("preview_look", "preview_reply", "stop_preview", "set_brightness",
                 "set_key_replies", "identify"):
        assert callable(getattr(lamp, name, None)), name
    lamp_api._show(lamp, "look", "phone", 5.0)
    lamp_api._show(lamp, "reply", "no", 5.0)
    lamp_api._show(lamp, "look", "asking", 5.0)
    assert lamp_api.identify(lamp, 3.0)
    lamp_api.stop_preview(lamp)
