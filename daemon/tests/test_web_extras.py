"""Key page extras: the home-screen manifest and icons, the owner's own short
key labels, and what the printed key card reads (power switches)."""

import json
import struct
import urllib.error
import urllib.request

import pytest
import yaml
from conftest import VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer, Preset
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.server import ICONS, WebApp, WebServer


@pytest.fixture
def rig(tmp_path):
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    actions = Actions(variant, FakePlayer())
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True}, ha=False)
    return app, actions


def doc_from(app):
    cfg = app.config()
    return {"keys": [{k: v for k, v in key.items() if k != "label"} for key in cfg["keys"]], "presets": cfg["presets"]}


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read()


def png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


@pytest.fixture
def base(rig):
    app, _ = rig
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", app
    server.close()


class TestManifest:
    def test_installable_shape(self, base):
        url, _ = base
        status, headers, body = get(url + "/manifest.webmanifest")
        assert status == 200
        assert headers["Content-Type"].startswith("application/manifest+json")
        assert headers["Cache-Control"] == "no-cache"
        doc = json.loads(body)
        assert doc["name"] == "Hasler" and doc["short_name"] == "Hasler"
        assert doc["start_url"] == "/" and doc["scope"] == "/" and doc["display"] == "standalone"
        assert doc["theme_color"] == "#17100B" == doc["background_color"]
        sizes = {(i["sizes"], i["purpose"]) for i in doc["icons"]}
        assert {("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes

    def test_named_after_the_room(self, base):
        url, app = base
        app.room.saved["name"] = "Kitchen"
        doc = json.loads(get(url + "/manifest.webmanifest")[2])
        assert doc["name"] == "Hasler (Kitchen)" and doc["short_name"] == "Kitchen"
        app.room.saved["name"] = "The long dining room"
        doc = json.loads(get(url + "/manifest.webmanifest")[2])
        assert doc["name"] == "Hasler (The long dining room)" and len(doc["short_name"]) <= 12

    def test_every_listed_icon_is_served_at_its_size(self, base):
        url, _ = base
        doc = json.loads(get(url + "/manifest.webmanifest")[2])
        for icon in doc["icons"]:
            status, headers, data = get(url + icon["src"])
            assert status == 200 and headers["Content-Type"] == "image/png"
            w, h = png_size(data)
            assert f"{w}x{h}" == icon["sizes"]

    def test_apple_touch_icon_at_the_root_and_under_icons(self, base):
        url, _ = base
        for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/icons/apple-touch-icon.png"):
            status, headers, data = get(url + path)
            assert status == 200 and headers["Content-Type"] == "image/png" and png_size(data) == (180, 180)

    @pytest.mark.parametrize("path", ["/icons/nope.png", "/icons/../server.py", "/icons/icon-192.svg", "/icons/"])
    def test_no_other_files_under_icons(self, base, path):
        url, _ = base
        assert get(url + path)[0] == 404

    def test_page_links_the_manifest_and_icons(self, base):
        url, _ = base
        page = get(url + "/")[2].decode()
        assert '<link rel="manifest" href="/manifest.webmanifest">' in page
        assert 'rel="apple-touch-icon" href="/icons/apple-touch-icon.png"' in page
        assert "serviceWorker" not in page          # the live page is never cached

    def test_icons_are_committed(self):
        for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png", "favicon-32.png"):
            assert (ICONS / name).is_file()


class TestShortLabel:
    def test_shipped_labels_and_old_configs_without_them_stay_valid(self, rig):
        app, actions = rig
        # The starter set gives every key a label (config/presets); a layout without any is fine too.
        assert all(0 < len(p["short"]) <= keymap.SHORT_MAX for p in app.config()["presets"])
        doc = doc_from(app)
        for p in doc["presets"]:
            del p["short"]
        app.save(doc)                                            # no "short" anywhere: fine
        assert all(p.short == "" for p in actions.presets.values())
        assert all("short" not in p for p in app.config()["presets"])

    def test_round_trip_through_save_file_and_restart(self, rig, tmp_path):
        app, actions = rig
        doc = doc_from(app)
        doc["presets"][0]["short"] = "  Swiss   Cl. "
        cfg = app.save(doc)
        assert cfg["presets"][0]["short"] == "Swiss Cl."         # whitespace tidied
        assert actions.presets[1].short == "Swiss Cl."
        saved = yaml.safe_load(app.keys_file.read_text())
        assert saved["presets"][0]["short"] == "Swiss Cl."
        # what the page sends back unchanged validates to the same thing
        again = doc_from(app)
        assert again["presets"][0]["short"] == "Swiss Cl."
        # a fresh daemon reads it from the overlay
        variant = load_variant(VARIANTS / "hasler-hm.yaml")
        fresh_actions = Actions(variant, FakePlayer())
        fresh = WebApp(fresh_actions, None, tmp_path / "keys.yaml", {}, ha=False)
        fresh.load_saved()
        assert fresh_actions.presets[1].short == "Swiss Cl."
        assert fresh.config()["presets"][0]["short"] == "Swiss Cl."

    def test_status_keys_carry_it_for_the_bank(self, rig):
        app, _ = rig
        doc = doc_from(app)
        doc["presets"][0]["short"] = "CLASSIC"
        del doc["presets"][1]["short"]
        app.save(doc)
        keys = {k["id"]: k for k in app.status()["keys"]}
        assert keys["k1"]["short"] == "CLASSIC" and "short" not in keys["k2"]

    def test_blank_clears_it(self, rig):
        app, actions = rig
        doc = doc_from(app)
        doc["presets"][0]["short"] = "X"
        app.save(doc)
        doc = doc_from(app)
        doc["presets"][0]["short"] = "   "
        cfg = app.save(doc)
        assert "short" not in cfg["presets"][0] and actions.presets[1].short == ""
        assert "short" not in yaml.safe_load(app.keys_file.read_text())["presets"][0]

    @pytest.mark.parametrize("value, message", [
        ("Thirteen char", "12 characters"),
        (12, "text"),
        (["a"], "text"),
    ])
    def test_refused_when_it_cannot_be_a_label(self, rig, value, message):
        app, _ = rig
        doc = doc_from(app)
        doc["presets"][2]["short"] = value
        with pytest.raises(keymap.Invalid) as exc:
            app.save(doc)
        (err,) = exc.value.errors
        assert err["where"] == "presets[2].short" and message in err["message"]

    def test_exactly_twelve_is_fine_and_unicode_counts_characters(self, rig):
        app, actions = rig
        doc = doc_from(app)
        doc["presets"][0]["short"] = "Clásica ñü12"
        assert len(doc["presets"][0]["short"]) == 12
        app.save(doc)
        assert actions.presets[1].short == "Clásica ñü12"

    def test_ytm_reference_presets_keep_it(self):
        errors: list = []
        spec = keymap.preset_from_page({"n": 4, "name": "Chart", "source": "ytm", "kind": "chart",
                                        "country": "ES", "short": "TOP ES"}, "p", errors)
        assert errors == [] and spec["short"] == "TOP ES"
        page = keymap.preset_to_page(Preset.from_spec(spec))
        assert page["short"] == "TOP ES" and page["kind"] == "chart"

    def test_a_descriptor_preset_may_carry_one(self):
        p = Preset.from_spec({"n": 1, "name": "Radio Swiss Classic", "radio": "http://x/y", "short": "SWISS"})
        assert p.short == "SWISS" and keymap.preset_to_page(p)["short"] == "SWISS"

    def test_config_says_the_limit(self, rig):
        app, _ = rig
        assert app.config()["short_max"] == keymap.SHORT_MAX == 12


class TestPowerDoc:
    def test_tone_knob_end_switch(self, rig):
        app, _ = rig
        assert app.config()["power"] == [{"kind": "end_switch", "control": "tone", "off": "standby",
                                          "off_held": "shutdown", "held_s": 5.0}]

    def test_interlocked_bank_release_key(self):
        variant = load_variant(VARIANTS / "excelsior.yaml")
        actions = Actions(variant, FakePlayer())
        assert keymap.power_doc(actions) == [{"kind": "release_key", "control": "presets", "tap": "standby",
                                              "hold": "shutdown"}]

    def test_nothing_when_the_switch_does_not_standby(self, rig):
        _, actions = rig
        actions.variant.actions.pop("power.off")
        assert keymap.power_doc(actions) == []
