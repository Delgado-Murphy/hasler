"""The world station collection: the shipped file, its builder, the browser's API."""

import gzip
import importlib.util
import json
import re
import urllib.error
import urllib.request

import pytest
from conftest import ROOT, VARIANTS

from haslerd.actions import Actions
from haslerd.player.base import FakePlayer, Preset
from haslerd.player.sources.radio import RadioSource
from haslerd.variant import Panel, load_variant
from haslerd.web import stations, stations_api
from haslerd.web.server import EXTRA_PREFIXES, WebApp, WebServer
from haslerd.web.stations_api import StationsApi, sniff

COLLECTION = ROOT / "config" / "stations" / "collection.json"


def _builder():
    spec = importlib.util.spec_from_file_location("build_collection", ROOT / "scripts" / "stations" / "build_collection.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _builder()


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setattr(stations, "DIRECTORY", None)


# -- the shipped file -----------------------------------------------------------

@pytest.fixture(scope="module")
def doc():
    return json.loads(COLLECTION.read_text(encoding="utf-8"))


def test_shipped_collection_shape(doc):
    s = doc["stations"]
    assert 550 <= len(s) <= 700 and doc["version"] == 1 and re.match(r"\d{4}-\d\d-\d\d$", doc["verified"])
    ids = [e["id"] for e in s]
    assert len(set(ids)) == len(ids)
    assert all(re.match(r"^[a-z]{2}-[a-z0-9-]+$", i) for i in ids)
    keys = [stations._key(e["url"]) for e in s]
    assert len(set(keys)) == len(keys), "duplicate stream"
    assert len({(stations.fold(e["name"]), e["country"]) for e in s}) == len(s), "duplicate name in a country"
    region_of = {c: r for r, v in doc["regions"].items() for c in v["countries"]}
    assert len(doc["regions"]) == 6
    for e in s:
        assert e["country"] in doc["countries"] and re.match(r"^[A-Z]{2}$", e["country"]), e
        assert e["country"] == "ZZ" or e["country"] in region_of, e
        assert e["language"] in doc["languages"], e
        assert e["genres"] and all(g in doc["genres"] for g in e["genres"]), e
        assert e["url"].startswith(("http://", "https://")) and e["name"].strip() and e["genre"], e
        assert e["bitrate"] is None or isinstance(e["bitrate"], int)
    assert all(set(v) == {"en", "es"} for v in doc["genres"].values())
    assert len(doc["countries"]) >= 100


def test_shipped_collection_keeps_the_spanish_list_first(doc):
    first = doc["stations"][:5]
    assert [e["name"] for e in first][:2] == ["RNE Radio Nacional", "RNE Radio Clásica"]
    assert all(e["country"] == "ES" for e in first)
    catalan = next(e for e in doc["stations"] if e["name"] == "RAC1")
    assert catalan["language"] == "ca" and "news" in catalan["genres"]


def test_dropped_on_pi_are_out(doc):
    dropped = {stations._key(d["url"]) for d in doc["dropped_on_pi"]}
    assert dropped and all(d["reason"] for d in doc["dropped_on_pi"])
    assert not dropped & {stations._key(e["url"]) for e in doc["stations"]}


def test_shipped_collection_is_small_gzipped():
    raw, packed, etag = stations.collection_doc(COLLECTION)
    assert len(packed) < 100_000 and gzip.decompress(packed) == raw and etag.startswith('"')


def test_build_is_reproducible_from_the_shipped_file(tmp_path):
    out = tmp_path / "c.json"
    build.main(["--base", str(COLLECTION), "--out", str(out)])
    assert out.read_text(encoding="utf-8") == COLLECTION.read_text(encoding="utf-8")


# -- the builder ----------------------------------------------------------------

def _entry(name, url, country="ES", **kw):
    return {"name": name, "url": url, "genre": "Pop", "genres": ["pop"], "region": "", "country": country,
            "language": "es", "homepage": "", "codec": "mp3", "bitrate": 128, "keywords": "", "why": "", **kw}


def _curator(tmp_path, name, entries):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps({"curator": name, "verified": "2026-10-24", "stations": entries}))
    return path


def test_normalises_countries_languages_codecs():
    e = build.normalise(_entry("X", "https://x/a.m3u8", country="USA", language="English / Spanish", codec="HLS/AAC",
                               genres=["Jazz", "nonsense"], bitrate=None))
    assert (e["country"], e["language"], e["codec"], e["genres"], e["bitrate"]) == ("US", "en", "hls", ["jazz"], None)
    assert build.normalise(_entry("Y", "http://y", country="Türkiye", language="Instrumental"))["country"] == "TR"
    assert build.language_code("Arabic (Egyptian)") == "ar" and build.language_code("zxx") == "zxx"
    with pytest.raises(ValueError):
        build.country_code("Atlantis")


def test_merge_dedupes_by_url_and_name_and_is_deterministic(tmp_path):
    a = _curator(tmp_path, "a", [_entry("Radio Uno", "https://one/live"), _entry("Radio Dos", "http://two/")])
    b = _curator(tmp_path, "b", [
        _entry("Radio One (copy)", "http://ONE/live/", genres=["rock"], why="filled in"),  # same stream
        _entry("Radio Dós", "https://elsewhere/2"),                                       # same name, same country
        _entry("Radio Dos", "https://mx/2", country="MX"),                                # same name, other country
        _entry("Radio Uno", "https://one-b/", country="ES"),                              # name dup again
    ])
    one = build.build([a, b], None, {})
    two = build.build([a, b], None, {})
    assert one == two
    s = one["stations"]
    assert [(e["id"], e["url"]) for e in s] == [
        ("es-radio-uno", "https://one/live"), ("es-radio-dos", "http://two/"), ("mx-radio-dos", "https://mx/2")]
    assert s[0]["genres"] == ["pop", "rock"] and s[0]["why"] == "filled in"


def test_ids_clash_get_suffixes_and_base_ids_stay(tmp_path):
    a = _curator(tmp_path, "a", [_entry("Radio 1", "http://a/1"), _entry("Radio-1", "http://a/2")])
    first = build.build([a], None, {})
    assert [e["id"] for e in first["stations"]] == ["es-radio-1", "es-radio-1-2"]
    base = tmp_path / "base.json"
    base.write_text(build.dump(first))
    # A later curator file adds a station whose slug clashes; the old ids do not move.
    c = _curator(tmp_path, "c", [_entry("Radio 1!", "http://c/3"), _entry("Radio-1", "http://a/2")])
    again = build.build([c], None, {}, base)
    assert [e["id"] for e in again["stations"]] == ["es-radio-1", "es-radio-1-2", "es-radio-1-3"]


def test_drop_list_removes_and_records(tmp_path):
    a = _curator(tmp_path, "a", [_entry("Gone", "https://gone/"), _entry("Kept", "https://kept/")])
    out = build.build([a], None, {build.url_key("http://gone"): "HTTP 404 from the radio"})
    assert [e["name"] for e in out["stations"]] == ["Kept"]
    assert out["dropped_on_pi"] == [{"name": "Gone", "url": "https://gone/", "reason": "HTTP 404 from the radio"}]


def test_es_list_is_enriched(tmp_path):
    es = tmp_path / "es.json"
    es.write_text(json.dumps({"verified": "2026-10-12", "stations": [
        {"name": "RAC1", "url": "https://rac/1", "genre": "News, talk (Catalan)", "region": "Spain · Catalonia"},
        {"name": "LOS40 Urban", "url": "https://l40/u", "genre": "Urban, reggaeton", "region": "Spain · National"},
        {"name": "Radio Galega", "url": "https://g/p.m3u8", "genre": "News, talk", "region": "Spain · Galicia"},
    ]}))
    s = build.build([], es, {})["stations"]
    assert [(e["language"], e["country"]) for e in s] == [("ca", "ES"), ("es", "ES"), ("gl", "ES")]
    assert s[0]["genres"] == ["news", "talk"] and "hip-hop" in s[1]["genres"] and s[2]["codec"] == "hls"
    assert "public" in s[2]["genres"]


# -- search ---------------------------------------------------------------------

@pytest.fixture
def small(tmp_path):
    path = tmp_path / "collection.json"
    entries = [
        {"id": "es-radio-clasica", "name": "Radio Clásica", "url": "https://rne/rc", "genre": "Classical",
         "genres": ["classical"], "region": "Spain · National", "country": "ES", "language": "es"},
        {"id": "at-oe1", "name": "Ö1", "url": "https://orf/oe1", "genre": "Culture, classical",
         "genres": ["classical", "culture"], "region": "Austria", "country": "AT", "language": "de"},
        {"id": "es-rac1", "name": "RAC1", "url": "https://rac/1", "genre": "News", "genres": ["news"],
         "region": "Spain · Catalonia", "country": "ES", "language": "ca", "keywords": "Catalunya"},
    ]
    path.write_text(json.dumps({"countries": {"ES": "Spain", "AT": "Austria"},
                                "languages": {"es": "Español", "de": "Deutsch", "ca": "Català"},
                                "stations": [entries[1], entries[0], entries[2]]}))
    return path


class FakeDir:
    def __init__(self, found):
        self.found, self.calls = found, []

    def search(self, q):
        self.calls.append(q)
        return [dict(s) for s in self.found]


def rb(name, url):
    return {"name": name, "url": url, "genre": "", "country": "", "source": "radio-browser", "checked": False}


def test_collection_search_spanish_first_with_ids_and_terms(small, tmp_path):
    found = stations.search_curated("classical", small)
    assert [s["id"] for s in found] == ["es-radio-clasica", "at-oe1"]
    assert found[0]["checked"] is True and found[0]["cc"] == "ES" and found[0]["country"] == "Spain · National"
    assert [s["id"] for s in stations.search_curated("deutsch", small)] == ["at-oe1"]     # language name
    assert [s["id"] for s in stations.search_curated("austria", small)] == ["at-oe1"]     # country name
    assert [s["name"] for s in stations.load_curated(small)] == ["Radio Clásica", "RAC1"]  # empty search: Spain


def test_internet_only_below_five_checked_matches(small, tmp_path, monkeypatch):
    db = str(tmp_path / "none.db")
    d = FakeDir([rb("Klassik", "http://k/")])
    found = stations.search("classical", db, curated=small, directory=d)
    assert [s["name"] for s in found] == ["Radio Clásica", "Ö1", "Klassik"] and d.calls == ["classical"]
    monkeypatch.setattr(stations, "INTERNET_BELOW", 2)
    d.calls.clear()
    assert [s["name"] for s in stations.search("classical", db, curated=small, directory=d)] == ["Radio Clásica", "Ö1"]
    assert d.calls == []


def test_search_internet_is_the_directory_alone_unchecked(small):
    d = FakeDir([rb("Ö1 copy", "http://orf/oe1/"), dict(rb("Klassik", "http://k/"), checked=True)])
    found = stations.search_internet("klassik", curated=small, directory=d)
    assert found == [rb("Klassik", "http://k/")]       # ours left out, everything unchecked
    assert stations.search_internet("x", curated=small, directory=None) == []


def test_by_id_and_id_for_url(small):
    assert stations.by_id("es-rac1", small)["url"] == "https://rac/1"
    assert stations.by_id("nope", small) is None
    assert stations.id_for_url("HTTP://rac/1/", small) == "es-rac1"
    assert stations.id_for_url("https://other/", small) is None


# -- the API --------------------------------------------------------------------

class FakeApp:
    def __init__(self):
        self.played = []

    def play(self, raw):
        self.played.append(raw)


@pytest.fixture
def api(tmp_path, small):
    now = [1_000_000.0]
    probes = []

    def probe(url):
        probes.append(url)
        if "html" in url:
            return {"ok": False, "codec": "", "bitrate": None, "error": "that link is a web page, not a stream"}
        return {"ok": True, "codec": "mp3", "bitrate": 128, "error": ""}

    a = StationsApi(FakeApp(), tmp_path / "state", collection=small, probe=probe, clock=lambda: now[0])
    a.now, a.probes = now, probes
    return a


def test_routes_are_offered_by_the_server():
    assert set(stations_api.PATHS) <= set(EXTRA_PREFIXES)


def test_mine_round_trip_and_validation(api, tmp_path):
    assert api.route("GET", "/api/stations/mine", None) == (200, stations_api.empty_mine())
    status, doc = api.route("PUT", "/api/stations/mine", {
        "favourites": ["es-rac1", "https://x/stream", "es-rac1"],
        "custom": [{"name": "My pirate", "url": "http://pirate/live"}],
        "order": ["https://x/stream", "es-rac1"]})
    assert status == 200 and doc["ok"] and doc["favourites"] == ["es-rac1", "https://x/stream"]
    cid = doc["custom"][0]["id"]
    assert cid.startswith("custom-") and doc["custom"][0]["name"] == "My pirate"
    saved = json.loads((tmp_path / "state" / "stations.json").read_text())
    assert saved == {k: doc[k] for k in ("favourites", "custom", "order", "recent")}
    # A partial PUT keeps the rest.
    api.route("PUT", "/api/stations/mine", {"order": []})
    assert api.mine()["favourites"] == ["es-rac1", "https://x/stream"] and api.mine()["custom"][0]["id"] == cid
    for bad in ({"favourites": "es-rac1"}, {"favourites": ["javascript:alert(1)"]},
                {"custom": [{"name": "", "url": "http://a"}]}, {"custom": [{"name": "a", "url": "ftp://a"}]}, []):
        with pytest.raises(ValueError):
            api.put_mine(bad)


def test_mine_writes_are_atomic(api, tmp_path, monkeypatch):
    api.put_mine({"favourites": ["es-rac1"]})
    path = tmp_path / "state" / "stations.json"
    before = path.read_text()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(stations_api.os, "replace", boom)
    with pytest.raises(OSError):
        api.put_mine({"favourites": ["at-oe1"]})
    assert path.read_text() == before
    assert [p.name for p in path.parent.iterdir()] == ["stations.json"]     # no temp file left behind


def test_unreadable_mine_starts_empty(api, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "stations.json").write_text("{nope")
    assert api.mine() == stations_api.empty_mine()


def test_recent_is_appended_on_every_radio_play(api):
    api.played(Preset(n=1, name="RAC", source="radio", uri="http://rac/1/"))
    api.now[0] += 10
    api.played(Preset(n=0, name="", source="radio", uri="https://unknown/live"))
    api.now[0] += 10
    api.played(Preset(n=2, name="RAC again", source="radio", uri="https://rac/1"))
    assert api.mine()["recent"] == [{"id": "es-rac1", "t": 1_000_020}, {"url": "https://unknown/live", "t": 1_000_010}]
    for i in range(60):
        api.played(Preset(n=0, name="", source="radio", uri=f"https://s/{i}"))
    recent = api.mine()["recent"]
    assert len(recent) == stations_api.RECENT_MAX and recent[0] == {"url": "https://s/59", "t": 1_000_020}
    api.played(Preset(n=0, name="", source="radio", uri="not a url"))   # ignored, never raises


def test_radio_source_calls_its_on_play_hook():
    class Client:
        def __getattr__(self, name):
            return lambda *a: None

    src = RadioSource(mpd=lambda: Client())
    seen = []
    src.on_play = seen.append
    preset = Preset(n=1, name="X", source="radio", uri="http://x/")
    src.play(preset)
    assert seen == [preset]
    src.on_play = lambda p: 1 / 0          # a broken hook never stops the music
    src.play(preset)


def test_try_by_id_plays_without_probing(api):
    status, doc = api.route("POST", "/api/stations/try", {"id": "es-rac1"})
    assert status == 200 and doc["ok"] and doc["url"] == "https://rac/1" and doc["name"] == "RAC1"
    assert api.app.played == [{"source": "radio", "uri": "https://rac/1", "name": "RAC1"}] and api.probes == []
    assert api.route("POST", "/api/stations/try", {"id": "es-nope"})[0] == 404


def test_try_custom_id(api):
    cid = api.put_mine({"custom": [{"name": "Pirate", "url": "http://pirate/live"}]})["custom"][0]["id"]
    assert api.route("POST", "/api/stations/try", {"id": cid})[1]["url"] == "http://pirate/live"


def test_try_url_probes_first(api):
    status, doc = api.route("POST", "/api/stations/try", {"url": "https://new/stream", "name": "New"})
    assert status == 200 and doc == {"ok": True, "id": None, "url": "https://new/stream", "name": "New",
                                     "codec": "mp3", "bitrate": 128, "error": ""}
    assert api.probes == ["https://new/stream"]
    status, doc = api.route("POST", "/api/stations/try", {"url": "https://a/page.html"})
    assert status == 200 and doc["ok"] is False and "web page" in doc["error"]
    assert len(api.app.played) == 1                     # the bad link played nothing
    # A pasted link that is in the collection gets its id and name.
    doc = api.route("POST", "/api/stations/try", {"url": "http://rac/1"})[1]
    assert doc["id"] == "es-rac1" and doc["name"] == "RAC1"
    with pytest.raises(ValueError):
        api.try_station({"url": "file:///etc/passwd"})


def test_sniff():
    assert sniff("audio/mpeg", b"ID3\x03" + b"\0" * 100, "128")["codec"] == "mp3"
    assert sniff("audio/mpeg", b"\xff\xfb" + b"\0" * 100)["bitrate"] is None
    assert sniff("audio/aacp", b"\xff\xf1" + b"\0" * 100)["codec"] == "aac"
    assert sniff("application/ogg", b"OggS\0\x02" + b"\0" * 22 + b"OpusHead")["codec"] == "opus"
    assert sniff("application/vnd.apple.mpegurl", b"#EXTM3U\n#EXT-X-VERSION:3\n")["codec"] == "hls"
    assert sniff("audio/x-scpls", b"[playlist]\nFile1=http://x\n")["codec"] == "playlist"
    assert sniff("text/html", b"<!doctype html><html>")["ok"] is False
    assert sniff("audio/mpeg", b"")["ok"] is False
    assert sniff("application/json", b'{"a":1}')["ok"] is False


def test_report_is_logged_and_rate_limited(api, tmp_path):
    assert api.route("POST", "/api/stations/report", {"id": "es-rac1", "reason": "doesnt-play", "note": "silence"}) \
        == (200, {"ok": True})
    api.route("POST", "/api/stations/report", {"url": "https://rac/1", "reason": "bad-sound"})
    lines = [json.loads(x) for x in (tmp_path / "state" / "reports.jsonl").read_text().splitlines()]
    assert lines[0] == {"t": 1_000_000, "id": "es-rac1", "url": "https://rac/1", "name": "RAC1",
                        "reason": "doesnt-play", "note": "silence"}
    assert lines[1]["id"] == "es-rac1" and lines[1]["reason"] == "bad-sound"
    for _ in range(8):
        assert api.route("POST", "/api/stations/report", {"id": "at-oe1"})[0] == 200
    assert api.route("POST", "/api/stations/report", {"id": "at-oe1"})[0] == 429
    api.now[0] += 3601
    assert api.route("POST", "/api/stations/report", {"id": "at-oe1"})[0] == 200
    for bad in ({}, {"id": "es-rac1", "reason": "hate it"}, {"id": "../x"}):
        with pytest.raises(ValueError):
            api.report(bad)


# -- HTTP -----------------------------------------------------------------------

@pytest.fixture
def base(tmp_path, small, monkeypatch):
    monkeypatch.setattr(stations, "CURATED_FILE", small)
    variant = load_variant(VARIANTS / "hasler-hm.yaml")
    panel = Panel.from_variant(variant, fake=True)
    player = FakePlayer()
    app = WebApp(Actions(variant, player), panel, tmp_path / "keys.yaml", {"radio": True}, ha=False,
                 station_db=str(tmp_path / "none.db"))
    app.extra_routes.append(StationsApi(app, tmp_path / "state", collection=small, probe=lambda u: {}))
    server = WebServer(app, host="127.0.0.1", port=0)
    server.start()
    yield f"http://127.0.0.1:{server.port}", player
    server.close()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_http_collection_gzip_etag_304(base, small):
    url, _ = base
    status, headers, body = get(url + "/api/stations/collection", {"Accept-Encoding": "gzip"})
    assert status == 200 and headers["Content-Encoding"] == "gzip" and headers["Cache-Control"] == "no-cache"
    assert json.loads(gzip.decompress(body)) == json.loads(small.read_text())
    etag = headers["ETag"]
    status, headers, body = get(url + "/api/stations/collection", {"If-None-Match": etag})
    assert status == 304 and body == b"" and headers["ETag"] == etag
    status, headers, body = get(url + "/api/stations/collection")           # no gzip asked: plain
    assert status == 200 and "Content-Encoding" not in headers and json.loads(body)["stations"]


def test_http_search_scopes_and_mine(base):
    url, player = base
    status, _, body = get(url + "/api/stations?q=classical")
    assert status == 200 and [s["name"] for s in json.loads(body)] == ["Radio Clásica", "Ö1"]
    assert json.loads(get(url + "/api/stations?q=classical&scope=internet")[2]) == []   # directory off
    status, _, body = get(url + "/api/stations/mine")
    assert status == 200 and json.loads(body) == stations_api.empty_mine()
    req = urllib.request.Request(url + "/api/stations/try", data=json.dumps({"id": "es-rac1"}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert json.loads(resp.read())["ok"] is True
    assert player.playing.uri == "https://rac/1" and player.playing.source == "radio"
