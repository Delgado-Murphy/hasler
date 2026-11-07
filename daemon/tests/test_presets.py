"""Starter sets (haslerd/presets.py): the packs, their resolution, the factory
check, "Start from a set", untouched keys following the release, migration
002, `extends`, the sleep verb."""

import json
from pathlib import Path

import pytest
import yaml
from conftest import ROOT, VARIANTS

from haslerd import boxconfig, migrations, presets
from haslerd.actions import Actions, build_presets
from haslerd.boxconfig import Paths
from haslerd.panel.controls import Event
from haslerd.player.base import FakePlayer, Preset
from haslerd.sleep import SleepError
from haslerd.variant import Panel, load_variant
from haslerd.web import keymap
from haslerd.web.server import WebApp

PACKS = ("dial", "mixtape", "levelup", "deepcuts", "afterhours", "encore", "passport", "sobremesa",
         "saturday", "nightlight")
# The sheet's headings: occasions, never ages.
GROUPS = {"dial": "Every day", "mixtape": "Every day", "levelup": "Every day", "deepcuts": "Every day",
          "afterhours": "For a mood", "encore": "For a mood", "passport": "Go somewhere", "sobremesa": "Go somewhere",
          "saturday": "Family time", "nightlight": "Family time"}
# Every old set id -> the set that took its place (1.5's pop and raices, 1.6's ten).
OLD_IDS = {"eclectic": "dial", "classical": "encore", "jazz": "afterhours", "oldies": "mixtape",
           "espana": "sobremesa", "world": "passport", "little": "nightlight", "family": "saturday",
           "teens": "levelup", "indie": "deepcuts", "pop": "sobremesa", "raices": "sobremesa"}
# Sets a child or a grandparent may be given: nothing explicit on them.
CLEAN_PACKS = ("nightlight", "saturday", "mixtape")
# Catalogue sections and title words that mean "explicit lyrics likely" while
# the catalogue carries no explicit flag of its own (it may one day: checked too).
EXPLICIT_SECTIONS = {"hiphop", "metal"}
EXPLICIT_WORDS = ("trap", "reggaeton", "urban", "rap", "grime", "drill")
# YouTube Music lists that are not YouTube's own editorial ones (RDCLAK.. lists,
# OLAK.. albums, RDEM.. artist radios) can vanish with their owner. Only where
# YouTube has no editorial alternative, and only from the official channel:
# (none today: every shipped key is an editorial list, an album or an artist radio).
USER_LISTS_ALLOWED: set[str] = set()


@pytest.fixture(scope="module")
def starters():
    return presets.Starters(ROOT, 7)


# -- the packs -------------------------------------------------------------------


def test_the_ten_packs_in_page_order(starters):
    assert starters.names() == list(PACKS)
    assert starters.default == "dial"
    doc = starters.doc()
    assert [d["id"] for d in doc if d["default"]] == ["dial"]
    # Grouped by occasion, the default's group first; a group's sets sit together.
    groups = [d["group"] for d in doc]
    assert all(groups) and groups == sorted(groups, key=groups.index)
    assert {d["id"]: d["group"] for d in doc} == GROUPS
    assert list(dict.fromkeys(groups)) == ["Every day", "For a mood", "Go somewhere", "Family time"]
    assert len({d["title"] for d in doc}) == len(doc)
    # Named by taste, not by age or by country (at most two lean Spanish: Sobremesa, and nothing else).
    titles = " ".join(d["title"] for d in doc).lower()
    for word in ("eclectic", "teens", "kids", "little", "grown", "oldies", "españa"):
        assert word not in titles, word


@pytest.mark.parametrize("name", PACKS)
def test_every_pack_entry_resolves(starters, name):
    pack = starters.pack(name)
    assert pack.title and pack.blurb and pack.group
    assert [e["n"] for e in pack.entries] == [1, 2, 3, 4, 5, 6, 7]
    assert len({e["name"] for e in pack.entries}) == 7 and len({e["short"] for e in pack.entries}) == 7
    stations, lists = presets.collection(ROOT), presets.catalogue(ROOT)
    for entry in pack.entries:
        assert entry["why"], entry
        assert 0 < len(entry["short"]) <= keymap.SHORT_MAX, entry
        if entry.get("station"):
            assert entry["station"] in stations, entry
            # Shipped keys play over https (a station the collection only has on http stays out).
            assert stations[entry["station"]]["url"].startswith("https://"), entry
        else:
            assert entry["ytm"] in lists, entry
            assert entry["ytm"].startswith(("RDCLAK", "OLAK", "RDEM")) or entry["ytm"] in USER_LISTS_ALLOWED, entry
    specs = starters.specs(name)
    assert len(specs) == 7
    for spec in specs:
        p = Preset.from_spec(spec)
        assert p.uri.startswith("https://") and p.name == spec["name"] and p.short == spec["short"]
        if p.source == "radio":
            # One spelling of every shipped stream: the collection's.
            assert p.station and stations[p.station]["url"] == p.uri
        else:
            assert p.source == "ytm" and "list=" in p.uri


@pytest.mark.parametrize("name", PACKS)
def test_every_pack_shows_what_the_radio_can_do(starters, name):
    """On the six keys every cabinet has: live radio and YouTube Music side by
    side, and key 1 is music at once (a list, or a station that isn't talk)."""
    stations = presets.collection(ROOT)
    six = [e for e in starters.pack(name).entries if e["n"] <= 6]
    assert sum(1 for e in six if e.get("station")) >= 2, name
    assert sum(1 for e in six if e.get("ytm")) >= 2, name
    first = six[0]
    if first.get("station"):
        genres = set(stations[first["station"]].get("genres") or []) | {stations[first["station"]]["genre"].lower()}
        assert not genres & {"news", "talk", "news, talk"}, first


@pytest.mark.parametrize("name", CLEAN_PACKS)
def test_family_packs_carry_nothing_explicit(starters, name):
    lists = presets.catalogue(ROOT)
    catalog = json.loads((ROOT / presets.CATALOG_FILE).read_text())
    section_of = {}
    for section in catalog["sections"]:
        for item in section["items"]:
            section_of.setdefault(item.get("uri"), section["id"])
    for entry in starters.pack(name).entries:
        if not entry.get("ytm"):
            continue
        item = lists[entry["ytm"]]
        assert not item.get("explicit"), entry
        assert section_of[item["uri"]] not in EXPLICIT_SECTIONS, entry
        words = f"{item['title']} {item.get('subtitle', '')} {entry['name']}".lower().replace("-", " ").split()
        assert not set(words) & set(EXPLICIT_WORDS), entry


def test_a_cabinet_takes_only_its_slots():
    assert [s["n"] for s in presets.Starters(ROOT, 6).specs("dial")] == [1, 2, 3, 4, 5, 6]
    doc = presets.Starters(ROOT, 6).doc()
    assert [d["id"] for d in doc] == list(PACKS)
    dial = next(d for d in doc if d["default"])
    assert dial["id"] == "dial" and dial["group"] == "Every day" and dial["title"] == "Turn the Dial"
    assert all(len(d["keys"]) == 6 for d in doc)
    assert dial["keys"][0] == {"n": 1, "name": "FIP", "short": "FIP", "source": "radio",
                               "why": dial["keys"][0]["why"]}
    # Key 3 of the default is a station, not a list (the owner's choice, 2026-11-07).
    assert dial["keys"][2]["name"] == "RNE Radio 3" and dial["keys"][2]["source"] == "radio"
    assert {k["source"] for d in doc for k in d["keys"]} == {"radio", "ytm"}


def test_chosen_pack_page_over_box_over_default(starters, caplog):
    assert starters.chosen("", {}) == "dial"
    assert starters.chosen("afterhours", {}) == "afterhours" and starters.source("afterhours", {}) == "box"
    assert starters.chosen("afterhours", {"starter": "levelup"}) == "levelup"
    assert starters.source("afterhours", {"starter": "levelup"}) == "page"
    assert starters.chosen("nope", {"starter": "nope"}) == "dial"      # unknown: logged, the default
    assert "doesn't have" in caplog.text


def test_a_dropped_set_is_followed_to_its_successor(starters, caplog):
    """Every set id an older release shipped (1.5's pop and raices, 1.6's ten,
    named by age) names a set of this one: a radio whose settings.json or
    box.yaml still says one plays its successor, quietly."""
    assert presets.PACK_ALIASES == OLD_IDS
    assert all(new in starters.packs and old not in starters.packs for old, new in presets.PACK_ALIASES.items())
    caplog.clear()
    with caplog.at_level("INFO", logger="haslerd.presets"):
        for old, new in OLD_IDS.items():
            assert starters.chosen("", {"starter": old}) == new and starters.source("", {"starter": old}) == "page"
            assert starters.chosen(old, {}) == new and starters.source(old, {}) == "box"
            assert starters.specs(old) == starters.specs(new)
    assert "which is 'dial' now" in caplog.text and "doesn't have" not in caplog.text
    assert starters.specs("pop")[0]["name"] == "Pop español"
    assert presets.default_specs(VARIANTS / "hasler-hm.yaml", 6, "raices") == presets.Starters(ROOT, 6).specs("sobremesa")
    assert load_variant(VARIANTS / "hasler-hm.yaml", pack="pop").presets[1]["station"] == "es-flamenco-radio"
    assert load_variant(VARIANTS / "hasler-hm.yaml", pack="eclectic").presets[0]["station"] == "fr-fip"


# What 1.6's Eclectic shipped on an HM before 2026-11-07 (Coffee Shop on key 3).
ECLECTIC_1_6 = [
    {"n": 1, "name": "FIP", "short": "FIP", "station": "fr-fip"},
    {"n": 2, "name": "Radio Swiss Jazz", "short": "Swiss Jazz", "station": "ch-radio-swiss-jazz"},
    {"n": 3, "name": "Coffee Shop Blend", "short": "Coffee Shop",
     "ytm": "https://music.youtube.com/playlist?list=RDCLAK5uy_nBE4bLuBHUXWZrF59ZrkPEToKt8M_I3Vc"},
    {"n": 4, "name": "Shonan Beach FM", "short": "Shonan Beach", "station": "jp-shonan-beach-fm-78-9"},
    {"n": 5, "name": "Buena Vista Social Club radio", "short": "Buena Vista",
     "ytm": "https://music.youtube.com/watch?list=RDEMkIlmPguUoTgHzbw6yV9n_g"},
    {"n": 6, "name": "Radio Nova", "short": "Nova", "station": "fr-radio-nova"},
]


def test_a_radio_on_a_dropped_set_keeps_its_own_keys_and_follows_the_rest(tmp_path):
    """A radio on 1.6's "eclectic" with two keys of its own: after the update
    the page says Turn the Dial, the untouched keys are the new set's, the
    owner's keys stay."""
    stations = presets.collection(ROOT)
    shipped = [dict(s, radio=stations[s["station"]]["url"]) if s.get("station") else dict(s) for s in ECLECTIC_1_6]
    mine4 = {"n": 4, "name": "My station", "radio": "http://mine.example/live"}
    mine6 = {"n": 6, "name": "Köln Concert", "ytm": "https://music.youtube.com/playlist?list=PLmine"}
    saved = [shipped[0], shipped[1], shipped[2], mine4, shipped[4], mine6]
    v = load_variant(VARIANTS / "hasler-hm.yaml")
    table = {k: val for k, val in v.actions.items()}
    (tmp_path / "keys.yaml").write_text(yaml.safe_dump({
        "schema": keymap.KEYS_SCHEMA, "presets": saved, "actions": table,
        "shipped": {"presets": shipped, "actions": table}, "defaults_rev": presets.layout_rev(shipped, table)}))
    app, actions = rig(tmp_path, settings={"starter": "eclectic"})
    cfg = app.config()
    assert cfg["starter"] == "dial" and cfg["starter_from"] == "page"
    assert actions.presets[1].name == "FIP" and actions.presets[2].name == "Radio Swiss Jazz"
    assert actions.presets[3].name == "RNE Radio 3"             # was the shipped Coffee Shop: follows
    assert actions.presets[4].name == "My station"              # the owner's
    assert actions.presets[5].name == "Buena Vista Social Club radio"
    assert actions.presets[6].name == "Köln Concert"            # the owner's
    assert cfg["factory"] is False
    after = yaml.safe_load((tmp_path / "keys.yaml").read_text())
    assert after["shipped"]["presets"][5]["name"] == "Classic Feel-Good Pop"
    # And a 1.5 radio on "pop": España's successor, every key untouched.
    (tmp_path / "pop").mkdir()
    app, actions = rig(tmp_path / "pop", settings={"starter": "pop"})
    assert app.config()["starter"] == "sobremesa" and actions.presets[2].name == "Flamenco Radio"


def test_no_release_means_no_packs(tmp_path):
    s = presets.Starters(tmp_path, 6)
    assert not s and s.names() == [] and s.specs("eclectic") == [] and s.chosen("x", {}) is None


def test_a_broken_pack_costs_that_pack_only(tmp_path, caplog):
    d = tmp_path / "config" / "presets"
    d.mkdir(parents=True)
    (d / "good.yaml").write_text("pack: good\ntitle: Good\npresets:\n  - {n: 1, name: A, radio: 'http://x/a'}\n")
    (d / "bad.yaml").write_text("pack: bad\npresets:\n  - {n: 1, name: A, radio: 'http://x', spotify: 'spotify:album:x'}\n")
    (d / "renamed.yaml").write_text("pack: other\npresets:\n  - {n: 1, name: A, radio: 'http://x'}\n")
    packs = presets.load_packs(d)
    assert list(packs) == ["good"]
    assert "bad.yaml" in caplog.text and "renamed.yaml" in caplog.text
    assert presets.Starters(tmp_path, 6).specs("good") == [{"n": 1, "name": "A", "radio": "http://x/a"}]


# -- station ids on presets -------------------------------------------------------


def test_preset_from_spec_takes_a_station_id():
    p = Preset.from_spec({"n": 1, "name": "FIP", "station": "fr-fip"})
    assert p.source == "radio" and p.station == "fr-fip" and p.uri == ""
    p = Preset.from_spec({"n": 1, "name": "FIP", "station": "fr-fip", "radio": "https://x/fip"})
    assert p.station == "fr-fip" and p.uri == "https://x/fip"
    with pytest.raises(ValueError, match="more than one source"):
        Preset.from_spec({"n": 1, "name": "x", "station": "fr-fip", "spotify": "spotify:album:x"})


def test_build_presets_resolves_the_collections_url():
    fip = presets.collection(ROOT)["fr-fip"]["url"]
    built = build_presets([
        {"n": 1, "name": "FIP", "station": "fr-fip", "radio": "http://old.example/fip"},   # a stale spelling
        {"n": 2, "name": "Gone", "station": "zz-no-such-station", "radio": "http://kept.example/s"},
        {"n": 3, "name": "Gone too", "station": "zz-no-such-station"},
        {"n": 4, "name": "Pasted", "radio": "http://mine.example/s"},
    ])
    assert built[1].uri == fip and built[1].station == "fr-fip"
    assert built[2].uri == "http://kept.example/s" and built[2].station == "zz-no-such-station"
    assert 3 not in built                   # no id, no link: that key only
    assert built[4].uri == "http://mine.example/s" and built[4].station == ""


def test_the_page_keeps_a_station_id_while_the_link_is_the_collections():
    fip = presets.collection(ROOT)["fr-fip"]["url"]
    errors: list = []
    spec = keymap.preset_from_page({"n": 1, "name": "FIP", "source": "radio", "uri": fip, "station": "fr-fip"}, "p", errors)
    assert spec == {"n": 1, "name": "FIP", "radio": fip, "station": "fr-fip"}
    spec = keymap.preset_from_page({"n": 1, "name": "FIP", "source": "radio", "uri": "http://mine/x", "station": "fr-fip"},
                                   "p", errors)
    assert spec == {"n": 1, "name": "FIP", "radio": "http://mine/x"} and not errors
    assert keymap.preset_to_page(Preset.from_spec({"n": 1, "name": "FIP", "station": "fr-fip", "radio": fip}))["station"] == "fr-fip"


def test_same_preset_compares_stations_by_id():
    a = {"n": 1, "name": "FIP", "station": "fr-fip", "radio": "https://a/x"}
    b = {"n": 1, "name": "FIP", "station": "fr-fip", "radio": "https://b/y"}
    assert presets.same_preset(a, b)
    assert not presets.same_preset(a, {**a, "name": "Mine"})
    # An id against a pasted link: the same link is the same music (a file saved by an older page).
    assert presets.same_preset(a, {"n": 1, "name": "FIP", "radio": "https://a/x"})
    assert not presets.same_preset(a, {"n": 1, "name": "FIP", "radio": "https://b/y"})
    assert presets.same_preset({"n": 2, "name": "L", "ytm": "https://music.youtube.com/playlist?list=RDCLAKx"},
                               {"n": 2, "name": "L", "ytm": "https://music.youtube.com/playlist?list=RDCLAKx"})


# -- descriptors ------------------------------------------------------------------


@pytest.mark.parametrize("name, slots, seventh", [("hasler-hm", 6, None), ("hasler-hm-hat", 6, None),
                                                  ("excelsior", 7, "KEXP")])
def test_descriptors_come_with_the_default_set(name, slots, seventh):
    v = load_variant(VARIANTS / f"{name}.yaml")
    assert v.slots == slots and [p["n"] for p in v.presets] == list(range(1, slots + 1))
    assert v.presets[0]["name"] == "FIP" and v.presets[0]["station"] == "fr-fip"
    if seventh:
        assert v.presets[6]["name"] == seventh
    jazz = load_variant(VARIANTS / f"{name}.yaml", pack="afterhours")
    assert jazz.presets[0]["name"] == "TSF Jazz"


def test_extends_takes_the_bases_behaviour_and_keeps_its_own_wiring(tmp_path):
    hm = load_variant(VARIANTS / "hasler-hm.yaml")
    hat = load_variant(VARIANTS / "hasler-hm-hat.yaml")
    raw = yaml.safe_load((VARIANTS / "hasler-hm-hat.yaml").read_text())
    assert raw["extends"] == "hasler-hm" and "actions" not in raw and "combos" not in raw and "presets" not in raw
    assert hat.actions == hm.actions and hat.combos == hm.combos and hat.presets == hm.presets
    assert hat.io["digital"]["addr"] == 0x20 and hm.io["digital"]["addr"] == "auto"
    # A child's own entry wins over the base's; everything else is inherited.
    (tmp_path / "base.yaml").write_text("variant: base\ncontrols: [{id: k1, kind: momentary, pin: d0}]\n"
                                        "actions: {k1.tap: {do: preset, n: 1}, k1.hold: {do: standby}}\n"
                                        "combos: [{keys: [k1, k1], hold_s: 1, do: standby}]\n"
                                        "presets: [{n: 1, name: A, radio: 'http://x/a'}]\n")
    (tmp_path / "child.yaml").write_text("variant: base\nextends: base\ncontrols: [{id: k1, kind: momentary, pin: d5}]\n"
                                         "actions: {k1.hold: {do: shutdown}}\n")
    child = load_variant(tmp_path / "child.yaml")
    assert child.actions == {"k1.tap": {"do": "preset", "n": 1}, "k1.hold": {"do": "shutdown"}}
    assert child.combos == [{"keys": ["k1", "k1"], "hold_s": 1, "do": "standby"}]
    assert child.presets == [{"n": 1, "name": "A", "radio": "http://x/a"}] and child.controls[0]["pin"] == "d5"
    (tmp_path / "orphan.yaml").write_text("variant: o\nextends: nowhere\ncontrols: []\n")
    with pytest.raises(ValueError, match="extends"):
        load_variant(tmp_path / "orphan.yaml")


# -- the web app -------------------------------------------------------------------


def rig(tmp_path, variant="hasler-hm", starter_box="", settings=None):
    v = load_variant(VARIANTS / f"{variant}.yaml")
    starters = presets.Starters(ROOT, v.slots)
    v.presets = starters.specs(starters.chosen(starter_box, settings or {}))
    panel = Panel.from_variant(v, fake=True)
    actions = Actions(v, FakePlayer())
    settings_file = tmp_path / "settings.json"
    if settings:
        settings_file.write_text(json.dumps(settings))
    app = WebApp(actions, panel, tmp_path / "keys.yaml", {"radio": True, "spotify": True, "ytm": True}, ha=False,
                 settings_file=settings_file, starters=starters, starter_box=starter_box)
    app.confirm.allow_unconfirmed = True
    app.load_saved()
    return app, actions


def page_doc(app):
    cfg = app.config()
    return {"keys": [{k: v for k, v in key.items() if k != "label"} for key in cfg["keys"]], "presets": cfg["presets"]}


class TestConfig:
    def test_reports_the_sets_and_the_factory_state(self, tmp_path):
        app, actions = rig(tmp_path)
        cfg = app.config()
        assert cfg["starter"] == "dial" and cfg["starter_from"] == "default" and cfg["factory"] is True
        assert [s["id"] for s in cfg["starters"]] == list(PACKS)
        assert all(len(s["keys"]) == 6 for s in cfg["starters"])
        assert [p["name"] for p in cfg["presets"]][:2] == ["FIP", "Radio Swiss Jazz"]
        doc = page_doc(app)
        doc["presets"][1]["name"] = "Mine"
        app.save(doc)
        assert app.config()["factory"] is False
        assert app.reset()["factory"] is True and actions.presets[2].name == "Radio Swiss Jazz"

    def test_box_yaml_and_the_page_choose_the_set(self, tmp_path):
        app, actions = rig(tmp_path, starter_box="afterhours")
        cfg = app.config()
        assert cfg["starter"] == "afterhours" and cfg["starter_from"] == "box" and cfg["factory"]
        assert actions.presets[1].name == "TSF Jazz"
        app, actions = rig(tmp_path, starter_box="afterhours", settings={"starter": "encore"})
        cfg = app.config()
        assert cfg["starter"] == "encore" and cfg["starter_from"] == "page"
        assert actions.presets[1].name == "Radio Swiss Classic" and cfg["factory"]

    def test_excelsior_sets_have_seven_keys(self, tmp_path):
        app, actions = rig(tmp_path, "excelsior")
        cfg = app.config()
        assert all(len(s["keys"]) == 7 for s in cfg["starters"])
        assert actions.presets[7].name == "KEXP" and cfg["factory"]

    def test_without_sets_the_descriptors_presets_are_the_defaults(self, tmp_path):
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        actions = Actions(v, FakePlayer())
        app = WebApp(actions, None, tmp_path / "keys.yaml", {}, ha=False)
        cfg = app.config()
        assert cfg["starter"] is None and cfg["starters"] == [] and cfg["factory"] is True


class TestStartFromASet:
    def test_puts_the_set_on_every_key_and_remembers_it(self, tmp_path):
        app, actions = rig(tmp_path)
        doc = page_doc(app)
        doc["presets"][0]["name"] = "Mine"
        app.save(doc)
        cfg = app.set_starter({"pack": "afterhours"})
        assert cfg["starter"] == "afterhours" and cfg["starter_from"] == "page" and cfg["factory"] is True
        assert actions.presets[1].name == "TSF Jazz" and actions.presets[2].name == "Late Night Jazz"
        assert json.loads((tmp_path / "settings.json").read_text())["starter"] == "afterhours"
        saved = yaml.safe_load((tmp_path / "keys.yaml").read_text())
        assert saved["schema"] == keymap.KEYS_SCHEMA and saved["defaults_rev"]
        assert saved["presets"] == saved["shipped"]["presets"] and saved["presets"][0]["station"] == "fr-tsf-jazz"
        assert saved["actions"]["k5.hold"] == {"do": "volume_step", "delta": -2}
        assert saved["combos"] == [{"keys": ["k1", "k6"], "hold_s": 1.0, "do": "bt_pair", "seconds": 120}]
        # Reset now means that set.
        doc = page_doc(app)
        doc["presets"][0]["name"] = "Mine"
        app.save(doc)
        assert not app.config()["factory"]
        cfg = app.reset()
        assert cfg["factory"] and cfg["starter"] == "afterhours" and actions.presets[1].name == "TSF Jazz"
        assert not (tmp_path / "keys.yaml").exists()
        # ... and after a restart too.
        app2, actions2 = rig(tmp_path, settings=json.loads((tmp_path / "settings.json").read_text()))
        assert app2.config()["starter"] == "afterhours" and actions2.presets[2].name == "Late Night Jazz"

    def test_refuses_an_unknown_set_and_needs_the_press(self, tmp_path):
        app, _ = rig(tmp_path)
        with pytest.raises(ValueError, match="pick one of"):
            app.set_starter({"pack": "disco"})
        app.confirm.allow_unconfirmed = False
        from haslerd.web.confirm import ConfirmError
        with pytest.raises(ConfirmError):
            app.set_starter({"pack": "afterhours"})
        assert app.config()["starter"] == "dial"


class TestUntouchedKeysFollowTheRelease:
    def test_saved_keys_equal_to_the_old_shipped_set_take_the_new_one(self, tmp_path):
        app, actions = rig(tmp_path)
        doc = page_doc(app)
        doc["presets"][1].update(name="My station", uri="http://mine.example/live")     # key 2: the owner's
        next(k for k in doc["keys"] if k["id"] == "k3")["hold"] = {"do": "standby_toggle"}   # k3.hold: the owner's
        app.save(doc)
        before = yaml.safe_load((tmp_path / "keys.yaml").read_text())
        assert before["defaults_rev"] and before["shipped"]["presets"][0]["name"] == "FIP"
        # The next release ships another set on the same radio (here: box.yaml says so).
        app2, actions2 = rig(tmp_path, starter_box="encore")
        assert actions2.presets[1].name == "Radio Swiss Classic"       # followed the release
        assert actions2.presets[2].name == "My station" and actions2.presets[2].uri == "http://mine.example/live"
        assert actions2.presets[6].name == "Classical Romance"
        assert actions2.variant.actions["k3.hold"] == {"do": "standby_toggle"}
        assert actions2.variant.actions["k5.hold"] == {"do": "volume_step", "delta": -2}
        after = yaml.safe_load((tmp_path / "keys.yaml").read_text())
        assert after["defaults_rev"] != before["defaults_rev"]
        assert after["shipped"]["presets"][0]["name"] == "Radio Swiss Classic"
        assert after["presets"][1]["name"] == "My station"
        assert app2.config()["factory"] is False

    def test_follow_release_substitutes_actions_and_new_slots(self):
        old_specs = [{"n": 1, "name": "A", "radio": "http://x/a"}, {"n": 2, "name": "B", "radio": "http://x/b"}]
        new_specs = [{"n": 1, "name": "A2", "radio": "http://x/a2"}, {"n": 2, "name": "B", "radio": "http://x/b"},
                     {"n": 3, "name": "C", "radio": "http://x/c"}]
        old_table = {"k1.tap": {"do": "preset", "n": 1}, "k5.hold": {"do": "volume_step", "delta": -2}}
        new_table = {"k1.tap": {"do": "preset", "n": 1}, "k5.hold": {"do": "volume_step", "delta": -3},
                     "k6.hold": {"do": "volume_step", "delta": 3}}
        saved_specs = [{"n": 1, "name": "A", "radio": "http://x/a"}, {"n": 2, "name": "Mine", "radio": "http://y"}]
        saved_table = {"k1.tap": {"do": "preset", "n": 1}, "k5.hold": {"do": "volume_step", "delta": -2},
                       "k6.hold": {"do": "standby_toggle"}}
        known = {"k1.tap", "k5.hold", "k6.hold"}
        specs, table, changed = presets.follow_release(saved_specs, saved_table, old_specs, old_table,
                                                       new_specs, new_table, known)
        assert [s["name"] for s in specs] == ["A2", "Mine", "C"]
        assert table == {"k1.tap": {"do": "preset", "n": 1}, "k5.hold": {"do": "volume_step", "delta": -3},
                         "k6.hold": {"do": "standby_toggle"}}
        assert changed == 3
        # The same rev: nothing to do.
        rev = presets.layout_rev(old_specs, old_table)
        assert rev == presets.layout_rev(list(old_specs), {**old_table, "k9.tap": None}) and len(rev) == 12

    def test_a_file_without_a_snapshot_is_read_as_before(self, tmp_path):
        (tmp_path / "keys.yaml").write_text("schema: 1\npresets:\n  - {n: 1, name: Old, radio: 'http://x/old'}\n"
                                            "actions: {k1.tap: {do: preset, n: 1}}\n")
        app, actions = rig(tmp_path)
        assert actions.presets[1].name == "Old"
        assert "shipped" not in yaml.safe_load((tmp_path / "keys.yaml").read_text())


# -- migration 002 ---------------------------------------------------------------------


OLD_KEYS = """# Written by the haslerd web page. Delete to go back to the descriptor.
schema: 1
presets:
- {n: 1, name: Radio Swiss Classic, radio: 'http://stream.srg-ssr.ch/m/rsc_de/mp3_128'}
- {n: 2, name: Radio Swiss Jazz, url: 'http://stream.srg-ssr.ch/m/rsj/mp3_128/'}
- {n: 3, name: Mine, radio: 'http://mine.example/live'}
- {n: 4, name: Today's Top Hits, spotify: 'spotify:playlist:37i9dQZF1DXcBWIGoYBM5M', shuffle: true}
actions:
  k1.tap: {do: preset, n: 1}
  k2.tap: {do: preset, n: 2}
  k3.hold: {do: standby_toggle}
combos:
- {keys: [k1, k6], hold_s: 1.0, do: bt_pair, seconds: 120}
"""


class TestMigration002:
    def paths(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        (state / "keys.yaml").write_text(OLD_KEYS)
        paths = Paths(release=ROOT, etc=tmp_path / "etc", state=state)
        boxconfig.write_box_file(paths, {"variant": "hasler-hm", "migrations": ["001_from_rsync_tree"]})
        return paths

    def test_rewrites_stations_by_id_and_stamps_the_snapshot(self, tmp_path):
        paths = self.paths(tmp_path)
        [(mid, lines)] = migrations.run(paths, apply=False)
        assert mid == "002_presets_by_id" and (paths.state / "keys.yaml").read_text() == OLD_KEYS
        assert any("would write preset 1 as station ch-radio-swiss-classic" in line for line in lines)
        [(mid, lines)] = migrations.run(paths, apply=True)
        text = (paths.state / "keys.yaml").read_text()
        doc = yaml.safe_load(text)
        srg = presets.collection(ROOT)
        assert doc["schema"] == keymap.KEYS_SCHEMA
        assert doc["presets"][0] == {"n": 1, "name": "Radio Swiss Classic", "station": "ch-radio-swiss-classic",
                                     "radio": srg["ch-radio-swiss-classic"]["url"]}
        assert doc["presets"][1] == {"n": 2, "name": "Radio Swiss Jazz", "station": "ch-radio-swiss-jazz",
                                     "radio": srg["ch-radio-swiss-jazz"]["url"]}
        assert doc["presets"][2] == {"n": 3, "name": "Mine", "radio": "http://mine.example/live"}
        assert doc["presets"][3]["spotify"].startswith("spotify:") and doc["presets"][3]["shuffle"] is True
        assert doc["actions"] == {"k1.tap": {"do": "preset", "n": 1}, "k2.tap": {"do": "preset", "n": 2},
                                  "k3.hold": {"do": "standby_toggle"}}
        assert doc["combos"][0]["keys"] == ["k1", "k6"]
        shipped = doc["shipped"]["presets"]
        assert [s["n"] for s in shipped] == [1, 2, 3, 4, 5, 6]
        assert shipped[0]["station"] == "ch-radio-swiss-classic" and shipped[2]["station"] == "es-rne-radio-3"
        assert shipped[3]["spotify"] and doc["shipped"]["actions"]["k5.hold"] == {"do": "volume_step", "delta": -2}
        assert doc["defaults_rev"] == presets.layout_rev(shipped, doc["shipped"]["actions"])
        assert text.startswith("# Written by the haslerd web page.")
        assert migrations.pending(paths) == []
        assert migrations.load("002_presets_by_id").up(paths) == [f"{paths.state / 'keys.yaml'} already has defaults_rev"]

    def test_then_the_untouched_keys_follow_the_release(self, tmp_path):
        paths = self.paths(tmp_path)
        migrations.run(paths, apply=True)
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        starters = presets.Starters(ROOT, v.slots)
        v.presets = starters.specs("dial")
        actions = Actions(v, FakePlayer())
        app = WebApp(actions, None, paths.state / "keys.yaml", {}, ha=False, starters=starters)
        app.load_saved()
        # Keys 1, 2 and 4 were the 1.4 built-in stations: they follow the release. 3 is the owner's.
        assert actions.presets[1].name == "FIP" and actions.presets[2].name == "Radio Swiss Jazz"
        assert actions.presets[3].name == "Mine" and actions.presets[4].name == "Shonan Beach FM"
        assert actions.variant.actions["k3.hold"] == {"do": "standby_toggle"}

    def test_runs_by_itself_at_startup(self, tmp_path):
        paths = self.paths(tmp_path)
        boxconfig.load_box_config(paths)
        assert "station" in (paths.state / "keys.yaml").read_text()
        assert migrations.applied(paths) == ["001_from_rsync_tree", "002_presets_by_id"]

    def test_excelsior_snapshot_has_tape(self, tmp_path):
        paths = self.paths(tmp_path)
        boxconfig.write_box_file(paths, {"variant": "excelsior", "migrations": ["001_from_rsync_tree"]})
        migrations.run(paths, apply=True)
        doc = yaml.safe_load((paths.state / "keys.yaml").read_text())
        assert [s["n"] for s in doc["shipped"]["presets"]] == [1, 2, 3, 4, 5, 6, 7]
        assert doc["shipped"]["presets"][6]["station"] == "ch-radio-swiss-pop"
        assert "tape.tap" in doc["shipped"]["actions"]

    def test_no_keys_file_nothing_to_do(self, tmp_path):
        paths = Paths(release=ROOT, etc=tmp_path / "etc", state=tmp_path / "state")
        (tmp_path / "state").mkdir()
        assert migrations.load("002_presets_by_id").up(paths) == [f"no {paths.state / 'keys.yaml'}: nothing to migrate"]


# -- the sleep verb ---------------------------------------------------------------------


class FakeSleep:
    def __init__(self):
        self.set_minutes: list = []
        self.refuse = None

    def set(self, minutes):
        if self.refuse:
            raise SleepError(self.refuse, 409)
        self.set_minutes.append(minutes)
        return {"minutes": minutes}

    def hand(self, event):
        pass


class TestSleepVerb:
    def test_a_hold_starts_the_timer(self):
        v = load_variant(VARIANTS / "hasler-hm.yaml")
        v.actions["k3.hold"] = {"do": "sleep", "minutes": 20}
        v.actions["k4.hold"] = {"do": "sleep"}
        actions = Actions(v, FakePlayer())
        actions.dispatch(Event("k3", "hold"))           # no timer wired: logs, nothing else
        timer = FakeSleep()
        actions.sleep = timer
        actions.dispatch(Event("k3", "hold"))
        actions.dispatch(Event("k4", "hold"))
        assert timer.set_minutes == [20, 30]
        timer.refuse = "the radio is in standby"
        actions.dispatch(Event("k3", "hold"))           # refused: logged, the panel loop goes on
        assert timer.set_minutes == [20, 30]

    def test_the_page_round_trips_and_checks_the_minutes(self, tmp_path):
        app, actions = rig(tmp_path)
        assert any(v["do"] == "sleep" and v["params"] == ["minutes"] for v in app.config()["verbs"])
        doc = page_doc(app)
        next(k for k in doc["keys"] if k["id"] == "k3")["hold"] = {"do": "sleep", "minutes": 45}
        cfg = app.save(doc)
        assert actions.variant.actions["k3.hold"] == {"do": "sleep", "minutes": 45}
        assert next(k for k in cfg["keys"] if k["id"] == "k3")["hold"] == {"do": "sleep", "minutes": 45}
        next(k for k in doc["keys"] if k["id"] == "k4")["hold"] = {"do": "sleep"}
        app.save(doc)
        assert actions.variant.actions["k4.hold"] == {"do": "sleep", "minutes": 30}
        for bad in (0, 241, "20", True):
            next(k for k in doc["keys"] if k["id"] == "k4")["hold"] = {"do": "sleep", "minutes": bad}
            with pytest.raises(keymap.Invalid) as exc:
                app.save(doc)
            assert exc.value.errors[0]["where"] == "keys[3].hold" and "1–240" in exc.value.errors[0]["message"]
        # ... and on a combination.
        doc = page_doc(app)
        doc["combos"] = [{"keys": ["k2", "k3"], "hold_s": 1.0, "do": "sleep", "minutes": 15}]
        app.save(doc)
        assert actions.variant.combos == [{"keys": ["k2", "k3"], "hold_s": 1.0, "do": "sleep", "minutes": 15}]
