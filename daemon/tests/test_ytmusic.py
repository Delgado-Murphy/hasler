"""YtmCatalog against a fake ytmusicapi module. No network, no real ytmusicapi.

The fake answers in the shapes seen from ytmusicapi 1.12.3 on the Pi (search
results carry radioId / playlistId; watch tracks say "thumbnail"/"length",
playlist tracks "thumbnails"/"duration").
"""

import json
import stat
import time
import types

import pytest

from haslerd.player.sources.ytmusic import YtmCatalog
from haslerd.web import keymap

VID = "PWtJRaules4"
COOKIE = "SID=abc; HSID=def; SAPISID=sap; __Secure-3PAPISID=sap3; PREF=f6=40000000"
THUMBS = [{"url": "https://t/60", "width": 60}, {"url": "https://t/120", "width": 120}]


def track(vid, title="T", **extra):
    return {"videoId": vid, "title": title, "artists": [{"name": "Miles Davis", "id": "x"}],
            "album": {"name": "Kind Of Blue", "id": "y"}, "thumbnails": THUMBS, "duration": "9:22", **extra}


class FakeClient:
    """Records calls; ``auth`` is what the catalogue built it with."""

    instances = []

    def __init__(self, auth=None, **kwargs):
        self.auth = auth
        self.calls = []
        FakeClient.instances.append(self)
        self.headers = None
        if auth:
            with open(auth) as fh:
                self.headers = json.load(fh)

    def search(self, q, filter):
        self.calls.append(("search", q, filter))
        return SEARCH[filter]

    def get_playlist(self, pid, limit=None):
        self.calls.append(("get_playlist", pid))
        if pid == "LM" and not self.auth:
            raise KeyError("twoColumnBrowseResultsRenderer")
        return {"tracks": [track("aaaaaaaaaaa", "One"), track("bbbbbbbbbbb", "Two", isAvailable=False),
                           {"videoId": None, "title": "gone"}, track("ccccccccccc", "Three")]}

    def get_watch_playlist(self, videoId=None, playlistId=None, radio=False, limit=25):
        self.calls.append(("get_watch_playlist", videoId, playlistId, radio))
        return {"playlistId": playlistId, "tracks": [
            {"videoId": "ddddddddddd", "title": "Watch", "length": "3:23", "thumbnail": THUMBS,
             "artists": [{"name": "A"}, {"name": "B"}], "album": None},
        ]}

    def get_library_playlists(self, limit=25):
        self.calls.append(("get_library_playlists", limit))
        return [
            {"playlistId": "LM", "title": "Liked Music", "thumbnails": THUMBS},
            {"playlistId": "PLmine123", "title": "Kitchen", "count": "12", "thumbnails": THUMBS},
            {"playlistId": "SE", "title": "Episodes for Later"},
        ]

    def get_account_info(self):
        self.calls.append(("get_account_info",))
        if not self.auth or "good" not in json.dumps(self.headers):
            raise KeyError("accountName")
        return {"accountName": "Bill Murphy", "channelHandle": None, "accountPhotoUrl": ""}


def fake_setup(filepath=None, headers_raw=None):
    """ytmusicapi.setup's contract: needs cookie + x-goog-authuser; returns JSON."""
    headers = {}
    for line in headers_raw.split("\n"):
        key, sep, value = line.partition(": ")
        if sep:
            headers[key.lower()] = value
    missing = {"cookie", "x-goog-authuser"} - set(headers)
    if missing:
        raise ValueError("The following entries are missing in your headers: " + ", ".join(missing))
    headers.setdefault("origin", "https://music.youtube.com")
    return json.dumps(headers)


SEARCH = {
    "songs": [
        {"resultType": "song", "title": "Bluing", "videoId": VID, "artists": [{"name": "Miles Davis"}],
         "album": {"name": "Chronicles"}, "thumbnails": THUMBS},
        {"resultType": "song", "title": "No id", "videoId": None},
    ],
    "artists": [
        {"resultType": "artist", "artist": "Miles Davis", "radioId": "RDEM0oHMqbcZ6-H-ZGYZ0dLITw",
         "shuffleId": "RDAO0oHMqbcZ6", "browseId": "UCIev", "thumbnails": THUMBS},
        {"resultType": "artist", "artist": "No radio", "browseId": "UCx"},
    ],
    "albums": [
        {"resultType": "album", "title": "Kind Of Blue", "type": "Album", "year": "1959",
         "playlistId": "OLAK5uy_kQdrECE-ozwNQzDlLQT2vsgVQp8DfHElE", "browseId": "MPREb_iN",
         "artists": [{"name": "Miles Davis"}], "thumbnails": THUMBS},
        {"resultType": "album", "title": "No playlist", "browseId": "MPREb_x"},
    ],
    "playlists": [
        {"resultType": "playlist", "title": "This Is Jazz", "author": "Jazz & Blues",
         "browseId": "VLPL7gp579CMkT_r7HHEoeWl4LoZaHQJp0FQ", "thumbnails": THUMBS},
    ],
}


@pytest.fixture
def api():
    FakeClient.instances = []
    return types.SimpleNamespace(YTMusic=FakeClient, setup=fake_setup)


@pytest.fixture
def catalog(tmp_path, api):
    return YtmCatalog(tmp_path, api=api)


def good_headers(cookie=COOKIE):
    # "good" lets the fake's get_account_info accept this login.
    return (
        "POST /youtubei/v1/browse HTTP/2\nHost: music.youtube.com\n"
        "User-Agent: Mozilla/5.0 good\nX-Goog-AuthUser: 0\n"
        "Authorization: SAPISIDHASH 123_abc\n"
        f"Cookie: {cookie}\nSec-Fetch-Mode: same-origin"
    )


# -- availability ----------------------------------------------------------


def test_unavailable_without_ytmusicapi(tmp_path, monkeypatch):
    import builtins

    real = builtins.__import__

    def no_ytmusicapi(name, *args, **kwargs):
        if name == "ytmusicapi":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ytmusicapi)
    cat = YtmCatalog(tmp_path)
    assert cat.available is False
    assert cat.search("miles", "songs") == []
    assert cat.tracks("https://music.youtube.com/playlist?list=PLx") == []
    with pytest.raises(ValueError, match="ytmusicapi"):
        cat.sign_in(COOKIE)


# -- search ----------------------------------------------------------------


def test_song_result_is_a_song_radio(catalog):
    (song,) = catalog.search("miles", "songs")
    assert song == {
        "type": "song", "title": "Bluing", "subtitle": "Miles Davis · Chronicles",
        "thumb": "https://t/120",
        "uri": f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}",
        "name": "Bluing radio",
    }


def test_artist_result_is_the_artist_radio_and_artists_without_one_are_dropped(catalog):
    (artist,) = catalog.search("miles", "artists")
    assert artist["type"] == "artist" and artist["title"] == "Miles Davis"
    assert artist["uri"] == "https://music.youtube.com/watch?list=RDEM0oHMqbcZ6-H-ZGYZ0dLITw"
    assert artist["name"] == "Miles Davis radio"


def test_album_result_is_its_audio_playlist(catalog):
    (album,) = catalog.search("miles", "albums")
    assert album["uri"] == "https://music.youtube.com/playlist?list=OLAK5uy_kQdrECE-ozwNQzDlLQT2vsgVQp8DfHElE"
    assert album["subtitle"] == "Album · Miles Davis · 1959" and album["name"] == "Kind Of Blue"


def test_playlist_result_loses_its_vl_prefix(catalog):
    (pl,) = catalog.search("jazz", "playlists")
    assert pl["uri"] == "https://music.youtube.com/playlist?list=PL7gp579CMkT_r7HHEoeWl4LoZaHQJp0FQ"
    assert pl["subtitle"] == "Jazz & Blues"


def test_every_search_uri_is_a_valid_ytm_preset(catalog):
    for kind in ("songs", "artists", "albums", "playlists"):
        for item in catalog.search("x", kind):
            assert keymap.check_uri("ytm", item["uri"]) is None, item["uri"]


def test_empty_query_asks_nothing_and_bad_type_is_refused(catalog):
    assert catalog.search("  ", "songs") == []
    assert FakeClient.instances == []
    with pytest.raises(ValueError):
        catalog.search("x", "videos")


def test_a_failing_search_is_an_empty_list(catalog, monkeypatch):
    def boom(self, q, filter):
        raise ConnectionError("no network")

    monkeypatch.setattr(FakeClient, "search", boom)
    assert catalog.search("miles", "songs") == []


def test_search_is_unauthenticated_until_signed_in(catalog):
    catalog.search("x", "songs")
    assert FakeClient.instances[-1].auth is None


# -- tracks and radio ------------------------------------------------------


def test_tracks_skip_unavailable_and_idless_entries(catalog):
    tracks = catalog.tracks("https://music.youtube.com/playlist?list=PL68AC80CBF3649BBB")
    assert [t["videoId"] for t in tracks] == ["aaaaaaaaaaa", "ccccccccccc"]
    assert tracks[0] == {"videoId": "aaaaaaaaaaa", "title": "One", "artist": "Miles Davis",
                         "album": "Kind Of Blue", "thumb": "https://t/120", "duration": "9:22",
                         "explicit": False}


def test_liked_songs_need_the_sign_in(catalog):
    assert catalog.tracks("https://music.youtube.com/playlist?list=LM") == []
    assert all(c[0] != "get_playlist" for i in FakeClient.instances for c in i.calls)


def test_song_radio_seeds_from_the_video_and_continues_after_the_last_id(catalog):
    uri = f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}"
    tracks = catalog.radio(uri)
    assert tracks[0] == {"videoId": "ddddddddddd", "title": "Watch", "artist": "A, B", "album": "",
                         "thumb": "https://t/120", "duration": "3:23", "explicit": False}
    catalog.radio(uri, after="ccccccccccc")
    calls = [c for c in FakeClient.instances[-1].calls if c[0] == "get_watch_playlist"]
    assert calls == [
        ("get_watch_playlist", VID, f"RDAMVM{VID}", True),
        ("get_watch_playlist", "ccccccccccc", f"RDAMVM{VID}", True),
    ]


def test_artist_radio_needs_no_seed_video(catalog):
    catalog.radio("https://music.youtube.com/watch?list=RDEM0oHMqbcZ6-H-ZGYZ0dLITw")
    assert FakeClient.instances[-1].calls[-1] == ("get_watch_playlist", None, "RDEM0oHMqbcZ6-H-ZGYZ0dLITw", True)


# -- sign-in ---------------------------------------------------------------


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_sign_in_with_full_headers_writes_both_files_private(catalog, tmp_path):
    account = catalog.sign_in(good_headers())
    assert account == {"signed_in": True, "name": "Bill Murphy"}
    browser, cookies = tmp_path / "ytm-browser.json", tmp_path / "ytm-cookies.txt"
    assert mode(browser) == 0o600 and mode(cookies) == 0o600
    saved = json.loads(browser.read_text())
    assert saved["cookie"] == COOKIE and saved["x-goog-authuser"] == "0"
    assert "SAPISIDHASH" in saved["authorization"]
    assert catalog.account() == {"signed_in": True, "name": "Bill Murphy"}
    assert FakeClient.instances[-1].auth == str(browser)


def test_cookies_txt_is_netscape_format(catalog, tmp_path):
    catalog.sign_in(good_headers())
    lines = (tmp_path / "ytm-cookies.txt").read_text().splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    rows = [ln.split("\t") for ln in lines if not ln.startswith("#")]
    assert [r[5] for r in rows] == ["SID", "HSID", "SAPISID", "__Secure-3PAPISID", "PREF"]
    for domain, sub, path, secure, expiry, _name, _value in rows:
        assert (domain, sub, path, secure) == (".youtube.com", "TRUE", "/", "TRUE")
        assert int(expiry) > time.time() + 365 * 24 * 3600
    assert rows[4][6] == "f6=40000000"


def test_sign_in_with_a_bare_cookie_synthesises_the_headers(tmp_path, api, monkeypatch):
    # The fake only accepts logins whose headers mention "good".
    monkeypatch.setattr(FakeClient, "get_account_info", lambda self: {"accountName": "Owner"})
    cat = YtmCatalog(tmp_path, api=api)
    assert cat.sign_in(f"Cookie: {COOKIE}")["name"] == "Owner"
    saved = json.loads((tmp_path / "ytm-browser.json").read_text())
    assert saved["cookie"] == COOKIE
    assert saved["x-goog-authuser"] == "0"
    assert saved["authorization"].startswith("SAPISIDHASH")   # or ytmusicapi takes it for OAuth
    assert mode(tmp_path / "ytm-cookies.txt") == 0o600


def test_chrome_two_line_cookie_header_is_understood(catalog, tmp_path):
    raw = good_headers().replace(f"Cookie: {COOKIE}", f"cookie\n{COOKIE}")
    catalog.sign_in(raw)
    assert "__Secure-3PAPISID\tsap3" in (tmp_path / "ytm-cookies.txt").read_text()


def test_missing_sapisid_is_a_clear_error_and_writes_nothing(catalog, tmp_path):
    with pytest.raises(ValueError, match="SAPISID"):
        catalog.sign_in("SID=abc; HSID=def")
    assert not (tmp_path / "ytm-browser.json").exists()
    assert not (tmp_path / "ytm-cookies.txt").exists()


def test_rejected_login_deletes_the_files(catalog, tmp_path):
    bad = good_headers().replace("good", "stale")
    with pytest.raises(ValueError, match="did not accept") as exc:
        catalog.sign_in(bad)
    assert "sap3" not in str(exc.value)              # never echoes the cookie
    assert not (tmp_path / "ytm-browser.json").exists()
    assert not (tmp_path / "ytm-cookies.txt").exists()
    assert catalog.account() == {"signed_in": False, "name": ""}


def test_empty_paste_is_refused(catalog):
    with pytest.raises(ValueError, match="paste"):
        catalog.sign_in("   ")


def test_sign_out_deletes_both_files(catalog, tmp_path):
    catalog.sign_in(good_headers())
    assert catalog.sign_out() == {"signed_in": False, "name": ""}
    assert list(tmp_path.iterdir()) == []


def test_no_state_dir_means_no_sign_in(api):
    cat = YtmCatalog(None, api=api)
    assert cat.cookies_file is None and cat.signed_in is False
    with pytest.raises(ValueError):
        cat.sign_in(COOKIE)


# -- library ---------------------------------------------------------------


def test_library_is_empty_when_signed_out(catalog):
    assert catalog.library() == []


def test_library_lists_liked_first_then_playlists(catalog):
    catalog.sign_in(good_headers())
    items = catalog.library()
    assert [i["title"] for i in items] == ["Liked songs", "Kitchen"]
    assert items[0]["uri"] == "https://music.youtube.com/playlist?list=LM"
    assert items[1] == {"type": "playlist", "title": "Kitchen", "subtitle": "12 songs",
                        "thumb": "https://t/120", "uri": "https://music.youtube.com/playlist?list=PLmine123",
                        "name": "Kitchen"}
    assert all(keymap.check_uri("ytm", i["uri"]) is None for i in items)
    # Liked songs now load, authenticated.
    assert [t["videoId"] for t in catalog.tracks(items[0]["uri"])] == ["aaaaaaaaaaa", "ccccccccccc"]


# -- preset links ----------------------------------------------------------


@pytest.mark.parametrize("uri", [
    f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}",
    "https://music.youtube.com/watch?list=RDEM0oHMqbcZ6-H-ZGYZ0dLITw",
    "https://music.youtube.com/watch?list=RDAO0oHMqbcZ6-H-ZGYZ0dLITw",
    "https://music.youtube.com/playlist?list=OLAK5uy_kQdrECE-ozwNQzDlLQT2vsgVQp8DfHElE",
    "https://music.youtube.com/playlist?list=PL7gp579CMkT_r7HHEoeWl4LoZaHQJp0FQ",
    "https://music.youtube.com/playlist?list=LM",
])
def test_keymap_accepts_every_catalogue_link(uri):
    assert keymap.check_uri("ytm", uri) is None
