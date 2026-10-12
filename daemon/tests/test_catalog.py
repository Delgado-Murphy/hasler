"""The Browse catalogue: the checked-in shelves, and the live YouTube Music refresh."""

import json
import threading
import time
from pathlib import Path

import pytest

from haslerd.web import catalog as cat
from haslerd.web.catalog import Catalog
from haslerd.web.keymap import check_uri

STATIC = cat.STATIC_FILE

ORDER = [
    "recommended", "spanish_radio", "jazz", "rock", "metal", "hiphop", "electronic", "pop",
    "spanish", "latin", "soul", "roots", "classical", "world", "moods", "kids",
]


def doc():
    return json.loads(STATIC.read_text())


# -- the static file ----------------------------------------------------------


def test_static_file_items_are_presets():
    for section in doc()["sections"]:
        assert section["title"]
        for item in section["items"]:
            where = f"{section['id']}: {item.get('title')}"
            assert item["type"] in cat.TYPES, where
            assert item["source"] in ("radio", "ytm"), where
            assert item["title"].strip() and item["name"].strip(), where
            assert len(item["name"]) <= 60, where
            assert check_uri(item["source"], item["uri"]) is None, where
            if item["source"] == "ytm":
                assert item["uri"].startswith("https://music.youtube.com/"), where
            assert item["thumb"] == "" or item["thumb"].startswith("https://"), where


def test_no_duplicate_uris_within_a_section():
    for section in doc()["sections"]:
        uris = [i["uri"] for i in section["items"]]
        assert len(uris) == len(set(uris)), section["id"]


def test_section_order_and_sizes():
    sections = Catalog(static_file=STATIC).sections()
    assert [s["id"] for s in sections] == ORDER
    sizes = {s["id"]: len(s["items"]) for s in sections}
    assert sizes["kids"] >= 12
    assert sizes["spanish_radio"] >= 20
    assert all(n >= 6 for n in sizes.values()), sizes


def test_kids_has_spanish_and_english_and_radio():
    kids = next(s for s in Catalog(static_file=STATIC).sections() if s["id"] == "kids")
    text = " ".join(i["title"] + " " + i["subtitle"] for i in kids["items"]).lower()
    assert "infantiles" in text and "nanas" in text
    assert "nursery" in text and "lullabies" in text
    assert any(i["source"] == "radio" for i in kids["items"])


def test_sections_expose_only_the_item_contract():
    for section in Catalog(static_file=STATIC).sections():
        for item in section["items"]:
            assert set(item) == set(cat.ITEM_KEYS)


def test_missing_or_broken_static_file_gives_empty_shelves(tmp_path):
    assert Catalog(static_file=tmp_path / "nope.json").sections() == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert Catalog(static_file=bad).sections() == []


def test_malformed_items_are_dropped(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"sections": [{"id": "x", "title": "X", "items": [
        {"type": "radio", "title": "Good", "source": "radio", "uri": "https://a/stream"},
        {"type": "radio", "title": "No link", "source": "radio", "uri": ""},
        {"type": "weird", "title": "Bad type", "source": "radio", "uri": "https://b"},
        {"type": "playlist", "title": "Bad source", "source": "tape", "uri": "https://c"},
    ]}]}))
    items = Catalog(static_file=path).sections()[0]["items"]
    assert [i["title"] for i in items] == ["Good"]
    assert items[0]["name"] == "Good"


# -- refresh ------------------------------------------------------------------


OLD = "RDCLAK5uy_oldoldoldoldoldoldoldoldoldoldold1"
NEW = "RDCLAK5uy_newnewnewnewnewnewnewnewnewnewnew1"


@pytest.fixture
def small(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"sections": [
        {"id": "a", "title": "A", "items": [
            {"type": "playlist", "title": "Jazz for you", "ytm_title": "Essential Jazz", "subtitle": "s",
             "thumb": "https://old/thumb", "source": "ytm", "uri": f"https://music.youtube.com/playlist?list={OLD}",
             "name": "Jazz"},
            {"type": "radio", "title": "FIP", "subtitle": "", "thumb": "", "source": "radio",
             "uri": "https://icecast.radiofrance.fr/fip-hifi.aac", "name": "FIP"},
        ]},
    ]}))
    return path


def tile(pid, title, thumb="https://new/thumb"):
    return {"musicTwoRowItemRenderer": {
        "title": {"runs": [{"text": title}]},
        "navigationEndpoint": {"browseEndpoint": {"browseId": "VL" + pid}},
        "thumbnailRenderer": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": [
            {"url": "https://small"}, {"url": thumb}]}}},
    }}


class FakeClient:
    def __init__(self):
        self.browsed = []

    def get_mood_categories(self):
        return {"Genres": [{"title": "Jazz", "params": "p-jazz"}], "Moods": [{"title": "Chill", "params": "p-chill"}]}

    def _send_request(self, endpoint, body):
        self.browsed.append(body["params"])
        if body["params"] == "p-jazz":
            return {"contents": [{"musicCarouselShelfRenderer": {"contents": [
                tile(NEW, "Essential  jazz"), tile("RDCLAK5uy_other", "Other"),
                {"musicTwoRowItemRenderer": {"title": {"text": "From The Start"}}},   # no link: skipped
            ]}}]}
        return {"contents": []}


class FakeYtm:
    available = True

    def __init__(self, client=None, fail=False):
        self.client = client or FakeClient()
        self.fail = fail
        self.calls = 0

    def _call(self, what, fn, default):
        self.calls += 1
        if self.fail:
            return default
        return fn(self.client)


def test_refresh_takes_new_id_and_art_by_youtube_title(small, tmp_path):
    ytm = FakeYtm()
    c = Catalog(ytm, data_dir=tmp_path, static_file=small)
    assert c.refresh(force=True) is True
    items = c.sections()[0]["items"]
    assert items[0]["uri"].endswith(NEW)
    assert items[0]["thumb"] == "https://new/thumb"
    assert items[0]["title"] == "Jazz for you" and items[0]["name"] == "Jazz"   # curated text kept
    assert items[1]["uri"] == "https://icecast.radiofrance.fr/fip-hifi.aac"
    assert ytm.client.browsed == ["p-jazz", "p-chill"]
    # cached: a new instance starts with the refreshed data and isn't stale
    again = Catalog(FakeYtm(fail=True), data_dir=tmp_path, static_file=small)
    assert again.sections()[0]["items"][0]["uri"].endswith(NEW)
    assert not again.stale


def test_refresh_failure_keeps_static_data(small, tmp_path):
    c = Catalog(FakeYtm(fail=True), data_dir=tmp_path, static_file=small)
    assert c.refresh(force=True) is False
    assert c.sections()[0]["items"][0]["uri"].endswith(OLD)
    assert not (tmp_path / cat.CACHE_FILE).exists()

    def boom():
        raise RuntimeError("network down")

    c = Catalog(fetch=boom, data_dir=tmp_path, static_file=small)
    assert c.refresh(force=True) is False
    assert c.sections()[0]["items"][0]["uri"].endswith(OLD)


def test_refresh_ignores_titles_it_cannot_find_and_bad_ids(small):
    c = Catalog(fetch=lambda: {"something else": {"id": NEW, "thumb": ""}}, static_file=small)
    assert c.refresh(force=True)
    assert c.sections()[0]["items"][0]["uri"].endswith(OLD)
    c = Catalog(fetch=lambda: {"essential jazz": {"id": "PL-not-official", "thumb": "http://insecure"}},
                static_file=small)
    assert c.refresh(force=True)
    item = c.sections()[0]["items"][0]
    assert item["uri"].endswith(OLD) and item["thumb"] == "https://old/thumb"


def test_unreadable_cache_is_ignored(small, tmp_path):
    (tmp_path / cat.CACHE_FILE).write_text("garbage")
    c = Catalog(fetch=lambda: {}, data_dir=tmp_path, static_file=small)
    assert c.sections()[0]["items"][0]["uri"].endswith(OLD)


def test_sections_never_wait_for_the_network(small):
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(5)
        return {"essential jazz": {"id": NEW, "thumb": ""}}

    c = Catalog(fetch=slow, static_file=small)
    t0 = time.monotonic()
    first = c.sections()
    assert time.monotonic() - t0 < 0.5
    assert first[0]["items"][0]["uri"].endswith(OLD)      # static data, immediately
    assert started.wait(2)                                # refresh running in the background
    assert c.sections()[0]["items"][0]["uri"].endswith(OLD)
    assert c.refresh_async() is False                     # only one at a time
    release.set()
    c.wait(5)
    assert c.sections()[0]["items"][0]["uri"].endswith(NEW)


def test_background_refresh_runs_once_per_day(small):
    now = [1_000_000_000.0]
    calls = []

    def fetch():
        calls.append(now[0])
        return {"essential jazz": {"id": NEW, "thumb": ""}}

    c = Catalog(fetch=fetch, static_file=small, clock=lambda: now[0])
    c.sections()
    c.wait(5)
    c.sections()
    c.wait(5)
    assert len(calls) == 1
    now[0] += cat.MAX_AGE_S + 1
    c.sections()
    c.wait(5)
    assert len(calls) == 2


def test_failed_refresh_waits_before_retrying(small):
    now = [1_000_000_000.0]
    calls = []

    def fetch():
        calls.append(1)
        raise OSError("down")

    c = Catalog(fetch=fetch, static_file=small, clock=lambda: now[0])
    c.sections()
    c.wait(5)
    c.sections()
    c.wait(5)
    assert len(calls) == 1
    now[0] += cat.RETRY_S + 1
    c.sections()
    c.wait(5)
    assert len(calls) == 2


def test_no_ytm_means_no_refresh(small):
    c = Catalog(static_file=small)
    assert c.refresh_async(force=True) is False
    assert c.refresh(force=True) is False
    unavailable = FakeYtm()
    unavailable.available = False
    c = Catalog(unavailable, static_file=small)
    assert c.refresh(force=True) is False
    assert unavailable.calls == 0


def test_walk_playlists_handles_watch_tiles():
    raw = {"x": [{"musicTwoRowItemRenderer": {
        "title": {"runs": [{"text": "Mix"}]},
        "navigationEndpoint": {"watchPlaylistEndpoint": {"playlistId": "RDCLAK5uy_watch"}},
    }}]}
    assert cat.walk_playlists(raw) == [{"id": "RDCLAK5uy_watch", "title": "Mix", "thumb": ""}]
    assert cat.walk_playlists(None) == []


# -- more_radio ----------------------------------------------------------------


def test_more_radio_maps_search_results(small):
    found = [
        {"name": "Jazz FM", "url": "http://x/redirect", "url_resolved": "https://x/stream", "tags": "jazz",
         "favicon": "https://x/icon.png"},
        {"name": "Same", "url_resolved": "https://x/stream"},          # duplicate url
        {"name": "No url", "url": ""},
        {"name": "", "url": "https://nameless"},
        "junk",
    ]
    c = Catalog(radio_search=lambda q: found, static_file=small)
    items = c.more_radio("jazz")
    assert items == [{"type": "radio", "title": "Jazz FM", "subtitle": "jazz", "thumb": "https://x/icon.png",
                      "source": "radio", "uri": "https://x/stream", "name": "Jazz FM"}]
    assert check_uri("radio", items[0]["uri"]) is None
    assert c.more_radio("  ") == []
    assert Catalog(static_file=small).more_radio("jazz") == []

    def broken(q):
        raise RuntimeError("down")

    assert Catalog(radio_search=broken, static_file=small).more_radio("jazz") == []
