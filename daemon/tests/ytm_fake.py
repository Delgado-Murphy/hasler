"""A fake ``ytmusicapi`` module for the YouTube Music browser tests.

Shapes are ytmusicapi 1.12.3's as seen from Spain on 2026-10-24 (the
expert's ytm-samples.json): search results with radioId/playlistId and
isExplicit, get_charts' video playlists + artists, get_artist's nested
sections, get_album's tracks naming their album as a string, the Moods &
genres browse JSON walked by web/catalog.walk_playlists.
"""

from __future__ import annotations

import types

ROSALIA = "UCeYz6rzUGhVwqxRM37FUo8w"
BAD_BUNNY = "UCiY3z8HAGD6BlSNKVn2kSvQ"
AITANA = "UCkyzcFsGWVYSUGaR7YB9OYQ"
LUX_MPRE = "MPREb_G6BH2uQgVxU"
LUX_OLAK = "OLAK5uy_kqKSSUvhqlZJQUlvZzxdhm4fXg7mLtpVQ"
OMEGA_MPRE = "MPREb_fSnPEEFXEEr"
TOP_SONGS = "OLAK5uy_lcwyvNdYatMwksSPQpn5uBO7ZMvGs-j_8"
RADIO_ID = "RDEMsBxErNy3Wp_muH16IzJ9DA"
SHUFFLE_ID = "RDAOsBxErNy3Wp_muH16IzJ9DA"
PERLA = "w7pjt9ZH3NM"
CUTE = "D5S7cfXw30s"
TRENDING_ES = "OLAK5uy_mzYnlaHgFOvLaxqIPnnouEr-idiUn4NIM"
DAILY_ES = "PL4fGSI1pDJn70HL5aWGmCq6gQf2qUrh53"
TOP100_ES = "PL4fGSI1pDJn4jhQB4kb9M36dvVmJQPt4T"
CALMA = "ggMPOg1uX1JOQWZFeDByc2Jm"
AFRICANA = "ggMPOg1uX0UzWGxlRE5jMDVk"
COFFEE = "RDCLAK5uy_nBE4bLuBHUXWZrF59ZrkPEToKt8M_I3Vc"
COFFEE_NEW = "RDCLAK5uy_newCoffeeShopBlendReissued000000"
FEATURED = "RDCLAK5uy_n0_d5SZbBxft3QDuk3gjp2BO1Q4H6tclU"
COMMUNITY = "PLmQG3l2p4_O4-PoDS-0xkWO2gWquuHzCd"


def thumbs(url: str) -> list[dict]:
    return [{"url": url.replace("w544-h544", "w60-h60"), "width": 60}, {"url": url, "width": 544}]


ART = "https://yt3.googleusercontent.com/dTpYWRisnoZ56BD2GCI=w544-h544-l90-rj"
ARTIST_ART = "https://lh3.googleusercontent.com/gYQfHNwupieC5fsiFq=w120-h120-p-l90-rj"
CHART_ART = "https://yt3.googleusercontent.com/a9Z5oMv5lpJselzm2AF5=s576"

SONGS = [
    {"resultType": "song", "videoId": PERLA, "title": "La Perla",
     "artists": [{"name": "ROSALÍA", "id": ROSALIA}, {"name": "Yahritza Y Su Esencia", "id": "UC5oOy_NEadrnh0lXtJpzgjA"}],
     "album": {"name": "LUX", "id": LUX_MPRE}, "duration": "3:16", "isExplicit": True,
     "videoType": "MUSIC_VIDEO_TYPE_ATV", "thumbnails": thumbs(ART)},
    {"resultType": "song", "videoId": CUTE, "title": "CUUUUuuuuuute",
     "artists": [{"name": "ROSALÍA", "id": ROSALIA}], "album": {"name": "MOTOMAMI", "id": "MPREb_PKQ6G8iMVve"},
     "duration": "2:31", "isExplicit": False, "thumbnails": thumbs(ART)},
    {"resultType": "song", "videoId": None, "title": "No id"},
]
ALBUMS = [
    {"resultType": "album", "title": "LUX", "type": "Album", "year": "2025",
     "artists": [{"name": "ROSALÍA", "id": ROSALIA}], "browseId": LUX_MPRE, "playlistId": LUX_OLAK,
     "isExplicit": True, "thumbnails": thumbs(ART)},
    {"resultType": "album", "title": "Omega (feat. Ralphie Choo)", "type": "Single", "year": "2024",
     "artists": [{"name": "ROSALÍA", "id": ROSALIA}], "browseId": OMEGA_MPRE, "playlistId": None,
     "isExplicit": False, "thumbnails": thumbs(ART)},
]
ARTISTS = [
    {"resultType": "artist", "artist": "ROSALÍA", "browseId": ROSALIA, "radioId": RADIO_ID,
     "shuffleId": SHUFFLE_ID, "subscribers": "36.6M", "thumbnails": thumbs(ARTIST_ART)},
    {"resultType": "artist", "artist": "Bad Bunny", "browseId": BAD_BUNNY, "thumbnails": thumbs(ARTIST_ART)},
    {"resultType": "artist", "artist": "No channel", "browseId": "FEmusic_x"},
]
PLAYLISTS_FEATURED = [
    {"resultType": "playlist", "title": "Presenting Rosalía", "author": "YouTube Music", "itemCount": 25,
     "browseId": "VL" + FEATURED, "thumbnails": thumbs(ART)},
]
PLAYLISTS_COMMUNITY = [
    {"resultType": "playlist", "title": "Rosalia, Lollapalooza 2019", "author": "Ros Metrik", "itemCount": None,
     "browseId": "VL" + COMMUNITY, "thumbnails": thumbs("https://i.ytimg.com/vi/Z0b0vkjqZXo/hqdefault.jpg")},
]
SEARCH = {
    "songs": SONGS, "albums": ALBUMS, "artists": ARTISTS,
    "featured_playlists": PLAYLISTS_FEATURED, "playlists": PLAYLISTS_COMMUNITY + PLAYLISTS_FEATURED,
    "community_playlists": PLAYLISTS_COMMUNITY,
    None: [{"category": "Top result", **ARTISTS[0]}, SONGS[0],
           {"resultType": "video", "videoId": "lv-r_Tr6Tw8", "title": "Espejismo Azul (lyrics)",
            "artists": [{"name": "NovaLyrics", "id": "UC0bUcCSA81OCfGpaYO6tgYA"}],
            "thumbnails": thumbs("https://i.ytimg.com/vi/lv-r_Tr6Tw8/hqdefault.jpg")},
           ALBUMS[0], PLAYLISTS_FEATURED[0], {"resultType": "episode", "videoId": "eeeeeeeeeee", "title": "Pod"}],
}


def tile(pid: str, title: str, browse: bool = True) -> dict:
    nav = ({"browseEndpoint": {"browseId": "VL" + pid}} if browse
           else {"watchPlaylistEndpoint": {"playlistId": pid}})
    return {"musicTwoRowItemRenderer": {
        "title": {"runs": [{"text": title}]}, "navigationEndpoint": nav,
        "thumbnailRenderer": {"musicThumbnailRenderer": {"thumbnail": {"thumbnails": thumbs(ART)}}}}}


def mood_page(params: str, coffee: str = COFFEE) -> dict:
    if params == CALMA:
        tiles = [tile(coffee, "Coffee Shop Blend"), tile("RDCLAK5uy_l80pbx0UAQ7EWipFs57eQnUIB9KbaDEow", "Cafecito & Chill")]
    else:
        # A genre page: the "From The Start" shelf has a title with no link.
        tiles = [{"musicTwoRowItemRenderer": {"title": {"runs": [{"text": "Calm Down"}]}}},
                 tile("RDCLAK5uy_m7AKVv8wPv_keNJSwWzb77kdOPCwBGTYk", "The Hits: 80s & Beyond Nigeria", browse=False)]
    return {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [{"content": {"items": tiles}}]}}}


CHARTS = {
    "ES": {"countries": {"selected": {"text": "Spain"}, "options": ["ZZ", "ES", "US", "MX"]},
           "videos": [{"title": "Trending 20 Spain", "playlistId": TRENDING_ES, "thumbnails": thumbs(CHART_ART)},
                      {"title": "Daily Top Music Videos - Spain", "playlistId": DAILY_ES, "thumbnails": thumbs(CHART_ART)},
                      {"title": "Top 100 Music Videos Spain", "playlistId": TOP100_ES, "thumbnails": thumbs(CHART_ART)}],
           "artists": [{"title": "Shakira", "browseId": "UCo6JijJGA3IvIiPsawDK3Ww", "subscribers": "54.4M",
                        "thumbnails": thumbs(ARTIST_ART)}]},
    "US": {"countries": {"options": ["ZZ", "ES", "US"]},
           "videos": [{"title": "Top 100 Live Performances - United States",
                       "playlistId": "PL4fGSI1pDJn4yCNzulPkUbxgr4pl0gmI-", "thumbnails": []},
                      {"title": "Daily Top Music Videos - United States",
                       "playlistId": "PL4fGSI1pDJn61unMfmrUSz68RT8IFFnks", "thumbnails": []}],
           "genres": [{"title": "Top 50 Jazz Music Videos United States",
                       "playlistId": "PL4fGSI1pDJn5jazz00000000000000000", "thumbnails": []}],
           "artists": []},
}

ARTIST = {
    "name": "ROSALÍA", "channelId": "UCQt9awGIFZeldFsATZNeJag", "radioId": RADIO_ID, "shuffleId": SHUFFLE_ID,
    "monthlyListeners": "36.2M", "description": "Spanish singer.", "thumbnails": thumbs(ARTIST_ART),
    "songs": {"browseId": "VL" + TOP_SONGS, "results": [
        {"videoId": PERLA, "title": "La Perla", "album": {"name": "LUX", "id": LUX_MPRE}, "isExplicit": True,
         "artists": [{"name": "ROSALÍA", "id": ROSALIA}], "thumbnails": thumbs(ART)},
        {"videoId": "dN3y8kVNplA", "title": "DESPECHÁ", "album": {"name": "MOTOMAMI +", "id": "MPREb_VdtEnJZ2vqp"},
         "thumbnails": thumbs(ART)}]},
    "albums": {"browseId": "UCx", "params": "p", "results": [
        {"title": "LUX", "year": "2025", "browseId": LUX_MPRE, "audioPlaylistId": LUX_OLAK, "isExplicit": True,
         "thumbnails": thumbs(ART)}]},
    "singles": {"results": [{"title": "Omega", "year": "2024", "browseId": OMEGA_MPRE, "thumbnails": thumbs(ART)}]},
    "related": {"results": [{"title": "Aitana", "browseId": AITANA, "subscribers": "5M",
                             "thumbnails": thumbs(ARTIST_ART)}]},
}

ALBUM = {
    "title": "LUX", "type": "Album", "year": "2025", "trackCount": 2, "duration": "49 minutes",
    "audioPlaylistId": LUX_OLAK, "isExplicit": False, "artists": [{"name": "ROSALÍA", "id": ROSALIA}],
    "thumbnails": thumbs(ART),
    "tracks": [{"videoId": "m6z1sW_qtyg", "title": "Sexo, Violencia y Llantas", "album": "LUX", "trackNumber": 1,
                "isExplicit": False, "artists": [{"name": "ROSALÍA", "id": ROSALIA}], "thumbnails": None},
               {"videoId": "xPaSuWrBAQI", "title": "Reliquia", "album": "LUX", "trackNumber": 2,
                "isExplicit": True, "artists": None, "thumbnails": None}],
}

HOME = [
    {"title": "Quick picks", "contents": [
        {"title": "LA GRACIOSA", "videoId": "Kev3r6W4TPo", "artists": [{"name": "Quevedo", "id": "UC7PL9aor5qNRhvhWWVXyOqA"}],
         "thumbnails": thumbs(ART)}]},
    {"title": "New releases", "contents": [
        {"title": "Mi Rompecabezas", "browseId": "MPREb_S6zIuRRU6CK", "audioPlaylistId": "OLAK5uy_lT8IFyXIJpSQyPdUsBnJFDfOLvGNCiUyg",
         "type": "Single", "artists": [{"id": "UC0r04hp-02vxsjHBVlm4F3A", "name": "Galvan Real"}], "thumbnails": thumbs(ART)}]},
    {"title": "Featured playlists for you", "contents": [
        {"title": "Trending 20 Spain", "playlistId": TRENDING_ES, "thumbnails": thumbs(CHART_ART)}]},
    {"title": "Mixed for you", "contents": [
        {"title": "My Supermix", "playlistId": "RDTMAK5uy_kset8DisdE7LSD4TNjEVvrKRTmG7a56sY", "thumbnails": thumbs(ART)}]},
    {"title": "About the artist", "contents": "text"},
]

WATCH = {"playlistId": "RDAMVM" + PERLA, "lyrics": "MPLYt_G6BH2uQgVxU-7", "related": "MPTRt_G6BH2uQgVxU-7",
         "tracks": [{"videoId": PERLA, "title": "La Perla", "length": "3:16",
                     "artists": [{"name": "ROSALÍA", "id": ROSALIA}], "album": {"name": "LUX", "id": LUX_MPRE},
                     "thumbnail": thumbs(ART)}]}

RELATED = [
    {"title": "You might also like", "contents": [SONGS[1]]},
    {"title": "Similar artists", "contents": [{"title": "Aitana", "browseId": AITANA, "subscribers": "5M"}]},
    {"title": "About the artist", "contents": "Rosalía Vila Tobella..."},
]


class FakeYTMusic:
    """Records every call in ``calls`` (class-wide); ``fail`` names methods that raise."""

    calls: list = []
    fail: set = set()
    coffee = COFFEE

    def __init__(self, auth=None, language=None, **kwargs):
        self.auth = auth
        self.language = language

    def _rec(self, name, *args):
        FakeYTMusic.calls.append((name, *args))
        if name in FakeYTMusic.fail or "*" in FakeYTMusic.fail:
            raise ConnectionError(f"{name}: no network")

    def search(self, q, filter=None, limit=20):
        self._rec("search", q, filter)
        if self.language == "es" and filter in ("songs", "albums"):
            return []           # the real thing: language="es" empties filtered searches
        return list(SEARCH[filter])

    def get_search_suggestions(self, q):
        self._rec("get_search_suggestions", q)
        return ["rosalia", "rosalia la perla", "rosalía"]

    def get_mood_categories(self):
        self._rec("get_mood_categories", self.language)
        if self.language == "es":
            return {"Estados de ánimo y momentos": [{"title": "Calma", "params": CALMA}],
                    "Géneros": [{"title": "Africana", "params": AFRICANA}]}
        return {"Moods & moments": [{"title": "Chill", "params": CALMA}],
                "Genres": [{"title": "African", "params": AFRICANA}]}

    def _send_request(self, endpoint, body):
        self._rec("_send_request", endpoint, body.get("browseId"), body.get("params"))
        return mood_page(body.get("params"), FakeYTMusic.coffee)

    def get_charts(self, country="ZZ"):
        self._rec("get_charts", country)
        return CHARTS[country]

    def get_artist(self, channel_id):
        self._rec("get_artist", channel_id)
        return ARTIST

    def get_album_browse_id(self, audio_playlist_id):
        self._rec("get_album_browse_id", audio_playlist_id)
        return LUX_MPRE if audio_playlist_id == LUX_OLAK else None

    def get_album(self, browse_id):
        self._rec("get_album", browse_id)
        return ALBUM

    def get_home(self, limit=3):
        self._rec("get_home", bool(self.auth))
        return HOME

    def get_liked_songs(self, limit=100):
        self._rec("get_liked_songs")
        if not self.auth:
            raise KeyError("twoColumnBrowseResultsRenderer")
        return {"id": "LM", "tracks": [SONGS[0], SONGS[1]]}

    def get_library_playlists(self, limit=25):
        self._rec("get_library_playlists")
        return [{"playlistId": "LM", "title": "Liked Music"},
                {"playlistId": "PLmine1234", "title": "Kitchen", "count": "12", "thumbnails": thumbs(ART)},
                {"playlistId": "SE", "title": "Episodes for Later"}]

    def get_history(self):
        self._rec("get_history")
        return [{**SONGS[1], "played": "Today"}]

    def get_library_artists(self, limit=25):
        self._rec("get_library_artists")
        return [{"browseId": ROSALIA, "artist": "ROSALÍA", "subscribers": "36M", "thumbnails": thumbs(ARTIST_ART)}]

    def get_watch_playlist(self, videoId=None, playlistId=None, radio=False, limit=25):
        self._rec("get_watch_playlist", videoId, playlistId, radio)
        return WATCH

    def get_song_related(self, browse_id):
        self._rec("get_song_related", browse_id)
        return RELATED

    def get_lyrics(self, browse_id, timestamps=False):
        self._rec("get_lyrics", browse_id)
        return {"lyrics": "Hola, ladrón de paz\r\n...", "source": "Source: Musixmatch", "hasTimestamps": False}

    def rate_song(self, video_id, rating="INDIFFERENT"):
        self._rec("rate_song", video_id, rating)
        if not self.auth:
            raise Exception("Please provide authentication before using this function")
        return {"actions": []}

    def get_playlist(self, pid, limit=None):
        self._rec("get_playlist", pid)
        return {"tracks": [{"videoId": c * 11, "title": f"Track {c}", "artists": [{"name": "X"}],
                            "isExplicit": c == "b", "thumbnails": thumbs(ART)} for c in "abc"]}

    def get_account_info(self):
        self._rec("get_account_info")
        return {"accountName": "Owner"}


def fake_api() -> types.SimpleNamespace:
    FakeYTMusic.calls = []
    FakeYTMusic.fail = set()
    FakeYTMusic.coffee = COFFEE
    return types.SimpleNamespace(YTMusic=FakeYTMusic, setup=lambda **kw: "{}")


def calls(name: str) -> list:
    return [c for c in FakeYTMusic.calls if c[0] == name]


def sign_in(catalog) -> None:
    """Make ``catalog`` signed in without the paste dance: its browser file exists."""
    path = catalog.browser_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
