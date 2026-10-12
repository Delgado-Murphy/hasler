"""YouTube Music playlists for moOde, and naming tracks moOde loads."""

import json

import pytest

from haslerd.player.base import Preset
from haslerd.player.sources.ytm import YtmSource
from haslerd.player.sources.ytm_playlists import (
    PREFIX, PlaylistSync, render_m3u, safe_name, seconds,
)

T1 = {"videoId": "aaaaaaaaaaa", "title": "DESPECHÁ", "artist": "ROSALÍA", "album": "MOTOMAMI",
      "thumb": "https://lh3.example/a", "duration": "2:37"}
T2 = {"videoId": "bbbbbbbbbbb", "title": "NUEVAYoL", "artist": "Bad Bunny", "album": "", "thumb": "", "duration": ""}


class FakeCatalog:
    available = True

    def __init__(self, signed_in=False):
        self.signed_in = signed_in

    def tracks(self, uri):
        return [T1, T2] if "PLx" in uri or "LM" in uri else []

    def radio(self, uri, after=None):
        return [T2]

    def library(self):
        return [{"title": "Liked songs", "uri": "https://music.youtube.com/playlist?list=LM", "thumb": ""}]


class FakeSource:
    prefix = "http://127.0.0.1:8099/ytm/"

    def __init__(self):
        self.remembered = []

    def _remember(self, tracks):
        self.remembered += [t["videoId"] for t in tracks]


@pytest.mark.parametrize("d, s", [("2:37", 157), ("1:02:03", 3723), ("", -1), ("x", -1)])
def test_durations(d, s):
    assert seconds(d) == s


def test_names_are_safe_file_names():
    assert safe_name("YT · AC/DC\\Hits\n") == "YT · AC DC Hits"
    assert safe_name("...hidden") == "hidden"


def test_m3u_carries_names_and_durations():
    text = render_m3u([T1, T2], FakeSource.prefix)
    assert text.splitlines() == [
        "#EXTM3U",
        "#EXTINF:157,ROSALÍA - DESPECHÁ", "http://127.0.0.1:8099/ytm/aaaaaaaaaaa",
        "#EXTINF:-1,Bad Bunny - NUEVAYoL", "http://127.0.0.1:8099/ytm/bbbbbbbbbbb",
    ]


@pytest.fixture
def dirs(tmp_path):
    pl, covers, state = tmp_path / "playlists", tmp_path / "covers", tmp_path / "state"
    for d in (pl, covers, state):
        d.mkdir()
    return pl, covers, state


def make(dirs, signed_in=False):
    pl, covers, state = dirs
    src = FakeSource()
    sync = PlaylistSync(src, FakeCatalog(signed_in), state, playlist_dir=pl, covers_dir=covers,
                        fetch=lambda url: b"\xff\xd8jpeg")
    return sync, src


def test_one_playlist_per_ytm_preset_with_cover(dirs):
    pl, covers, _ = dirs
    sync, src = make(dirs)
    sync._presets = [
        Preset(n=5, name="Classical", uri="https://music.youtube.com/playlist?list=PLx", source="ytm"),
        Preset(n=6, name="Radio", uri="https://music.youtube.com/watch?v=bbbbbbbbbbb&list=RDAMVMbbbbbbbbbbb", source="ytm"),
    ]
    assert sorted(sync.sync_once()) == [PREFIX + "Classical", PREFIX + "Radio"]
    assert (pl / f"{PREFIX}Classical.m3u").read_text().startswith("#EXTM3U\n#EXTIMG:local\n#EXTINF:157,ROSALÍA")
    assert (covers / f"{PREFIX}Classical.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert "aaaaaaaaaaa" in src.remembered        # names known for tag_queue


def test_library_only_when_signed_in(dirs):
    assert make(dirs)[0].sync_once() == []
    assert make(dirs, signed_in=True)[0].sync_once() == [PREFIX + "Liked songs"]


def test_dropped_playlists_are_pruned_and_others_left_alone(dirs):
    pl, covers, state = dirs
    (pl / "Favorites.m3u").write_text("mine")
    sync, _ = make(dirs, signed_in=True)
    sync.sync_once()
    assert (pl / f"{PREFIX}Liked songs.m3u").exists()
    sync.catalog.signed_in = False                 # signed out: Liked songs goes
    sync.sync_once()
    assert not (pl / f"{PREFIX}Liked songs.m3u").exists()
    assert (pl / "Favorites.m3u").read_text() == "mine"
    assert json.loads((state / "ytm-playlists.json").read_text()) == []


def test_unwritable_covers_dir_is_skipped(dirs):
    pl, covers, _ = dirs
    covers.chmod(0o555)
    try:
        sync, _ = make(dirs, signed_in=True)
        assert sync.sync_once() == [PREFIX + "Liked songs"]
    finally:
        covers.chmod(0o755)


# -- tag_queue -----------------------------------------------------------------

class QueueMPD:
    """A queue moOde loaded from a YT playlist: Name tags only."""

    def __init__(self, prefix):
        self.version = 7
        self.tags = []
        self.cleared = []
        self.queue = [
            {"id": "1", "file": prefix + "aaaaaaaaaaa", "name": "ROSALÍA - DESPECHÁ"},
            {"id": "2", "file": "http://radio.example/stream"},
            {"id": "3", "file": prefix + "ccccccccccc", "name": "unknown to us"},
        ]

    def status(self):
        return {"playlist": str(self.version)}

    def playlistinfo(self):
        return self.queue

    def addtagid(self, songid, tag, value):
        self.tags.append((songid, tag, value))
        self.version += 1

    def cleartagid(self, songid, tag):
        self.cleared.append((songid, tag))
        self.version += 1


def test_tag_queue_names_our_tracks_once_per_queue_change():
    src = YtmSource(mpd=lambda: None, port=0)
    mpd = QueueMPD(src.prefix)
    src._mpd = lambda: mpd
    src._remember([T1])
    assert src.tag_queue() == 1
    assert mpd.tags == [("1", "Title", "DESPECHÁ"), ("1", "Artist", "ROSALÍA"), ("1", "Album", "MOTOMAMI")]
    assert mpd.cleared == [("1", "Name")]
    assert src.tag_queue() == 0                   # its own edits don't trigger a rescan


def test_warm_hands_presets_to_the_playlist_sync():
    seen = []
    src = YtmSource(mpd=lambda: None, port=0)
    src.playlists = type("S", (), {"update": lambda self, presets: seen.append([p.n for p in presets]),
                                   "close": lambda self: None})()
    src.warm([Preset(n=5, name="x", uri="https://music.youtube.com/playlist?list=PLx", source="ytm"),
              Preset(n=1, name="r", uri="http://r", source="radio")], interval_s=3600)
    src.close()
    assert seen == [[5]]


def test_no_cover_flag_without_a_cover(dirs):
    pl, covers, _ = dirs
    covers.chmod(0o555)
    try:
        sync, _ = make(dirs, signed_in=True)
        sync.sync_once()
    finally:
        covers.chmod(0o755)
    assert "#EXTIMG" not in (pl / f"{PREFIX}Liked songs.m3u").read_text()
