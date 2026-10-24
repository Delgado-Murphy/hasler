#!/usr/bin/env python3
"""Build config/stations/collection.json: the radio's world station collection.

Stdlib only. Merges the curated Spanish list (config/stations/es.json, kept
first and in its order) with curator files of the same shape::

    {"curator", "verified", "stations": [{name, url, genre, genres[], region,
      country, language, homepage, codec, bitrate, keywords, why}], ...}

and writes one document::

    {"version", "verified", "genres": {tag: {"en", "es"}},
     "languages": {code: name}, "regions": {slug: {"en", "es", "countries"}},
     "countries": {code: name}, "stations": [{id, name, url, genre, genres,
     region, country, language, homepage, codec, bitrate, keywords, why}],
     "dropped_on_pi": [{name, url, reason}]}

Normalisation: countries become ISO 3166 alpha-2 codes ("ZZ" = internet-only),
languages ISO 639-1 codes (the first one for a bilingual station; "zxx" for
instrumental), codecs lower-case (mp3, aac, aac+, opus, vorbis, hls).

Dedupe: by stream URL (scheme, case and trailing slash ignored - the same key
as haslerd's search) and by folded name + country; the first source wins and
later duplicates only fill its empty fields and add genres.

Ids are ``<country>-<slug of name>`` (lower case), with ``-2``, ``-3`` for a
clash in input order. A station that already has an id keeps it: to add a new
curator file later, pass the shipped collection as ``--base`` (its stations
come first, ids untouched) so saved favourites keep resolving.

The first build (2026-10-24) merged the old config/stations/es.json (50
Spanish stations, since removed: they live on in the collection) with six
curator files, then dropped what failed to play on the radio itself
(scripts/stations/dropped-on-pi.json).

    build_collection.py CURATOR.json... [--base config/stations/collection.json]
        [--es es.json] [--drop dropped.json] [--out config/stations/collection.json]
        [--urls urls.tsv]   # also write "id<TAB>codec<TAB>url" for probing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = 1

GENRES: dict[str, tuple[str, str]] = {
    "news": ("News", "Noticias"), "talk": ("Talk", "Tertulia"), "public": ("Public radio", "Radio pública"),
    "culture": ("Culture", "Cultura"), "community": ("Community", "Comunitaria"),
    "college": ("College radio", "Radio universitaria"), "sports": ("Sports", "Deportes"),
    "comedy": ("Comedy", "Humor"), "kids": ("Kids", "Infantil"), "world": ("World", "Músicas del mundo"),
    "pop": ("Pop", "Pop"), "hits": ("Hits", "Éxitos"), "oldies": ("Oldies", "Clásicos"),
    "rock": ("Rock", "Rock"), "indie": ("Indie", "Indie"), "metal": ("Metal", "Metal"),
    "electronic": ("Electronic", "Electrónica"), "dance": ("Dance", "Dance"),
    "ambient": ("Ambient", "Ambient"), "chill": ("Chill", "Chill"), "lounge": ("Lounge", "Lounge"),
    "sleep": ("Sleep", "Para dormir"), "experimental": ("Experimental", "Experimental"),
    "jazz": ("Jazz", "Jazz"), "blues": ("Blues", "Blues"), "soul": ("Soul & R&B", "Soul y R&B"),
    "funk": ("Funk", "Funk"), "hip-hop": ("Hip-hop", "Hip-hop"), "reggae": ("Reggae", "Reggae"),
    "latin": ("Latin", "Latina"), "folk": ("Folk", "Folk"), "country": ("Country", "Country"),
    "gospel": ("Gospel", "Gospel"), "classical": ("Classical", "Clásica"),
    "soundtrack": ("Soundtracks", "Bandas sonoras"),
}

LANGUAGES: dict[str, str] = {
    "en": "English", "es": "Español", "pt": "Português", "fr": "Français", "de": "Deutsch",
    "it": "Italiano", "ca": "Català", "eu": "Euskara", "gl": "Galego", "nl": "Nederlands",
    "ar": "العربية", "he": "עברית", "fa": "فارسی", "tr": "Türkçe", "ja": "日本語", "ko": "한국어",
    "zh": "中文", "yue": "粵語", "hi": "हिन्दी", "bn": "বাংলা", "ta": "தமிழ்", "te": "తెలుగు",
    "ur": "اردو", "ne": "नेपाली", "si": "සිංහල", "th": "ไทย", "vi": "Tiếng Việt", "id": "Bahasa Indonesia",
    "ms": "Bahasa Melayu", "jv": "Basa Jawa", "iba": "Iban", "tl": "Filipino", "km": "ខ្មែរ",
    "my": "မြန်မာ", "mn": "Монгол", "bo": "བོད་སྐད", "kk": "Қазақ", "uz": "Oʻzbek", "hy": "Հայերեն",
    "ka": "ქართული", "sw": "Kiswahili", "am": "አማርኛ", "yo": "Yorùbá", "pcm": "Naijá", "tw": "Twi",
    "wo": "Wolof", "bm": "Bamanankan", "kea": "Kabuverdianu", "ln": "Lingála", "lg": "Luganda",
    "rw": "Kinyarwanda", "sn": "chiShona", "zu": "isiZulu", "kab": "Taqbaylit", "ht": "Kreyòl ayisyen",
    "sm": "Gagana Sāmoa", "to": "Lea faka-Tonga", "gil": "Taetae ni Kiribati", "no": "Norsk",
    "da": "Dansk", "sv": "Svenska", "is": "Íslenska", "fi": "Suomi", "et": "Eesti", "lv": "Latviešu",
    "pl": "Polski", "cs": "Čeština", "sk": "Slovenčina", "hu": "Magyar", "sl": "Slovenščina",
    "hr": "Hrvatski", "sr": "Српски", "bg": "Български", "ro": "Română", "el": "Ελληνικά",
    "uk": "Українська", "ru": "Русский", "ga": "Gaeilge", "la": "Latina", "zxx": "Instrumental",
}

# Free-text language as the curators wrote it -> code (first language wins).
LANG_NAMES = {
    "english": "en", "spanish": "es", "portuguese": "pt", "french": "fr", "german": "de", "arabic": "ar",
    "hebrew": "he", "persian": "fa", "turkish": "tr", "japanese": "ja", "korean": "ko", "mandarin": "zh",
    "cantonese": "yue", "hindi": "hi", "bengali": "bn", "tamil": "ta", "telugu": "te", "urdu": "ur",
    "nepali": "ne", "sinhala": "si", "thai": "th", "vietnamese": "vi", "indonesian": "id", "malay": "ms",
    "javanese": "jv", "iban": "iba", "filipino": "tl", "khmer": "km", "burmese": "my", "mongolian": "mn",
    "tibetan": "bo", "kazakh": "kk", "uzbek": "uz", "armenian": "hy", "georgian": "ka", "swahili": "sw",
    "sheng": "sw", "amharic": "am", "yoruba": "yo", "nigerian pidgin": "pcm", "twi": "tw", "wolof": "wo",
    "bambara": "bm", "kriolu": "kea", "lingala": "ln", "kinyarwanda": "rw", "shona": "sn",
    "tamazight": "kab", "darija": "ar", "haitian creole": "ht", "samoan": "sm", "tongan": "to",
    "gilbertese": "gil", "instrumental": "zxx", "none": "zxx",
}

COUNTRIES: dict[str, str] = {
    "AD": "Andorra", "AE": "United Arab Emirates", "AM": "Armenia", "AO": "Angola", "AR": "Argentina",
    "AT": "Austria", "AU": "Australia", "BB": "Barbados", "BD": "Bangladesh", "BE": "Belgium",
    "BG": "Bulgaria", "BM": "Bermuda", "BO": "Bolivia", "BR": "Brazil", "BS": "Bahamas", "CA": "Canada",
    "CD": "DR Congo", "CH": "Switzerland", "CI": "Côte d'Ivoire", "CL": "Chile", "CM": "Cameroon",
    "CN": "China", "CO": "Colombia", "CR": "Costa Rica", "CU": "Cuba", "CV": "Cape Verde",
    "CZ": "Czechia", "DE": "Germany", "DK": "Denmark", "DO": "Dominican Republic", "DZ": "Algeria",
    "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt", "ES": "Spain", "ET": "Ethiopia", "FI": "Finland",
    "FR": "France", "GB": "United Kingdom", "GE": "Georgia", "GH": "Ghana", "GR": "Greece",
    "GT": "Guatemala", "GY": "Guyana", "HK": "Hong Kong", "HR": "Croatia", "HT": "Haiti",
    "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IN": "India", "IR": "Iran",
    "IS": "Iceland", "IT": "Italy", "JM": "Jamaica", "JO": "Jordan", "JP": "Japan", "KE": "Kenya",
    "KH": "Cambodia", "KI": "Kiribati", "KN": "Saint Kitts and Nevis", "KR": "South Korea",
    "KZ": "Kazakhstan", "LB": "Lebanon", "LC": "Saint Lucia", "LK": "Sri Lanka", "LV": "Latvia",
    "MA": "Morocco", "ML": "Mali", "MM": "Myanmar", "MN": "Mongolia", "MX": "Mexico", "MY": "Malaysia",
    "MZ": "Mozambique", "NG": "Nigeria", "NL": "Netherlands", "NO": "Norway", "NP": "Nepal",
    "NZ": "New Zealand", "OM": "Oman", "PE": "Peru", "PF": "French Polynesia", "PH": "Philippines",
    "PK": "Pakistan", "PL": "Poland", "PR": "Puerto Rico", "PS": "Palestine", "PT": "Portugal",
    "PY": "Paraguay", "RO": "Romania", "RS": "Serbia", "RU": "Russia", "RW": "Rwanda", "SD": "Sudan",
    "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "SN": "Senegal",
    "SV": "El Salvador", "SY": "Syria", "TH": "Thailand", "TN": "Tunisia", "TO": "Tonga",
    "TR": "Türkiye", "TT": "Trinidad and Tobago", "TW": "Taiwan", "TZ": "Tanzania", "UA": "Ukraine",
    "UG": "Uganda", "US": "United States", "UY": "Uruguay", "UZ": "Uzbekistan",
    "VC": "Saint Vincent and the Grenadines", "VE": "Venezuela", "VN": "Vietnam", "ZA": "South Africa",
    "ZW": "Zimbabwe", "ZZ": "Internet",
}
COUNTRY_ALIASES = {"usa": "US", "united states (hawaii)": "US", "türkiye": "TR", "turkey": "TR",
                   "uk": "GB", "dr congo": "CD"}

REGIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "europe": ("Europe", "Europa", (
        "AD", "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GB", "GR", "HR", "HU",
        "IE", "IS", "IT", "LV", "NL", "NO", "PL", "PT", "RO", "RS", "RU", "SE", "SI", "SK", "UA")),
    "north-america": ("North America", "Norteamérica", ("BM", "CA", "US")),
    "latin-america": ("Latin America & Caribbean", "Latinoamérica y Caribe", (
        "AR", "BB", "BO", "BR", "BS", "CL", "CO", "CR", "CU", "DO", "EC", "GT", "GY", "HT", "JM", "KN",
        "LC", "MX", "PE", "PR", "PY", "SV", "TT", "UY", "VC", "VE")),
    "africa": ("Africa", "África", (
        "AO", "CD", "CI", "CM", "CV", "DZ", "EG", "ET", "GH", "KE", "MA", "ML", "MZ", "NG", "RW", "SD",
        "SN", "TN", "TZ", "UG", "ZA", "ZW")),
    "middle-east": ("Middle East", "Oriente Medio", (
        "AE", "AM", "GE", "IL", "IR", "JO", "LB", "OM", "PS", "SY", "TR")),
    "asia-oceania": ("Asia & Oceania", "Asia y Oceanía", (
        "AU", "BD", "CN", "HK", "ID", "IN", "JP", "KH", "KI", "KR", "KZ", "LK", "MM", "MN", "MY", "NP",
        "NZ", "PF", "PH", "PK", "SG", "TH", "TO", "TW", "UZ", "VN")),
}

# es.json predates tags: its free-text genre (and name) -> tags.
ES_GENRE_TAGS = (
    ("news", ("news",)), ("talk", ("talk",)), ("sports", ("sport",)), ("classical", ("classical",)),
    ("indie", ("indie", "alternative")), ("pop", ("pop",)), ("hits", ("hits",)),
    ("oldies", ("80s", "90s", "oldies")), ("hip-hop", ("urban", "reggaeton")),
    ("latin", ("reggaeton", "spanish music", "spanish pop")), ("dance", ("dance", "house")),
    ("electronic", ("electronic", "house")), ("rock", ("rock",)), ("world", ("international",)),
)
ES_PUBLIC = ("RNE", "Catalunya", "iCat", "Canal Sur", "Canal Fiesta", "Radio Euskadi", "Euskadi Irratia",
             "Gaztea", "EITB", "Radio Galega", "IB3", "Canarias Radio", "Onda Regional", "Canal Extremadura")


def fold(text: str) -> str:
    """The same folding as haslerd.web.stations.fold."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def url_key(url: str) -> str:
    """The same dedupe key as haslerd.web.stations._key."""
    return url.strip().split("://", 1)[-1].rstrip("/").lower()


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(text)).strip("-")
    return s[:48].rstrip("-") or "station"


def country_code(raw: str) -> str:
    raw = (raw or "").strip()
    if len(raw) == 2 and raw.upper() in COUNTRIES:
        return raw.upper()
    low = raw.casefold()
    if low in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[low]
    for code, name in COUNTRIES.items():
        if name.casefold() == low:
            return code
    raise ValueError(f"unknown country {raw!r}")


def language_code(raw: str) -> str:
    raw = (raw or "").strip()
    if raw in LANGUAGES:
        return raw
    first = re.split(r"\s*[/,]\s*", raw)[0]
    first = re.sub(r"\s*\(.*\)", "", first).strip().casefold()
    if first in LANG_NAMES:
        return LANG_NAMES[first]
    raise ValueError(f"unknown language {raw!r}")


def codec(raw: str, url: str) -> str:
    c = (raw or "").strip().lower()
    if "hls" in c or ".m3u8" in url.lower():
        return "hls"
    return c


def region_of(country: str) -> str:
    for key, (_, _, members) in REGIONS.items():
        if country in members:
            return key
    return ""


def enrich_es(e: dict) -> dict:
    """An es.json entry in the collection's shape."""
    text = fold(e.get("genre", ""))
    tags = [t for t, words in ES_GENRE_TAGS if any(w in text for w in words)]
    if any(e["name"].startswith(p) for p in ES_PUBLIC):
        tags.append("public")
    lang = "es"
    if "(catalan)" in text or "catalan music" in text:
        lang = "ca"
    elif "(basque)" in text:
        lang = "eu"
    elif "(galician)" in text or e["name"].startswith("Radio Galega"):
        lang = "gl"
    url = e["url"]
    return {
        "name": e["name"], "url": url, "genre": e.get("genre", ""), "genres": list(dict.fromkeys(tags)),
        "region": e.get("region", "Spain"), "country": "ES", "language": lang, "homepage": "",
        "codec": "hls" if ".m3u8" in url.lower() else "", "bitrate": None,
        "keywords": e.get("keywords", ""), "why": "",
    }


def normalise(e: dict) -> dict:
    url = str(e.get("url", "")).strip()
    country = country_code(e.get("country", ""))
    genres = [g.strip().lower() for g in e.get("genres") or [] if g and g.strip().lower() in GENRES]
    bitrate = e.get("bitrate")
    return {
        "name": str(e["name"]).strip(), "url": url, "genre": (e.get("genre") or "").strip(),
        "genres": list(dict.fromkeys(genres)), "region": (e.get("region") or COUNTRIES[country]).strip(),
        "country": country, "language": language_code(e.get("language", "")),
        "homepage": (e.get("homepage") or "").strip(), "codec": codec(e.get("codec", ""), url),
        "bitrate": bitrate if isinstance(bitrate, int) and bitrate > 0 else None,
        "keywords": (e.get("keywords") or "").strip(), "why": (e.get("why") or "").strip(),
    }


def merge(sources: list[list[dict]], dropped: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """(stations with ids, dropped entries). Deterministic in the input order."""
    dropped = dropped or {}
    out: list[dict] = []
    by_url: dict[str, dict] = {}
    by_name: dict[tuple[str, str], dict] = {}
    gone: list[dict] = []
    for entries in sources:
        for s in entries:
            if not s["url"].startswith(("http://", "https://")) or not s["name"]:
                continue
            k = url_key(s["url"])
            if k in dropped:
                if not any(url_key(g["url"]) == k for g in gone):
                    gone.append({"name": s["name"], "url": s["url"], "reason": dropped[k]})
                continue
            nk = (fold(s["name"]), s["country"])
            first = by_url.get(k) or by_name.get(nk)
            if first is not None:
                for field, value in s.items():
                    if field == "genres":
                        first["genres"] = list(dict.fromkeys(first["genres"] + value))
                    elif not first.get(field) and value:
                        first[field] = value
                by_url.setdefault(k, first)
                by_name.setdefault(nk, first)
                continue
            s = dict(s)
            by_url[k] = by_name[nk] = s
            out.append(s)
    ids: set[str] = {s["id"] for s in out if s.get("id")}
    for s in out:
        if s.get("id"):
            continue
        base = f"{s['country'].lower()}-{slug(s['name'])}"
        sid, n = base, 2
        while sid in ids:
            sid, n = f"{base}-{n}", n + 1
        ids.add(sid)
        s["id"] = sid
    order = ("id", "name", "url", "genre", "genres", "region", "country", "language", "homepage",
             "codec", "bitrate", "keywords", "why")
    return [{f: s.get(f) for f in order} for s in out], gone


def build(curators: list[Path], es: Path | None, dropped: dict[str, str], base: Path | None = None) -> dict:
    sources: list[list[dict]] = []
    verified: list[str] = []
    if base is not None:
        base_doc = json.loads(base.read_text(encoding="utf-8"))
        sources.append([dict(e) for e in base_doc["stations"]])
        verified.append(base_doc.get("verified", ""))
    if es is not None:
        es_doc = json.loads(es.read_text(encoding="utf-8"))
        sources.append([enrich_es(e) for e in es_doc["stations"]])
        verified.append(es_doc.get("verified", ""))
    for path in curators:
        doc = json.loads(path.read_text(encoding="utf-8"))
        sources.append([normalise(e) for e in doc["stations"]])
        verified.append(doc.get("verified", ""))
    stations, gone = merge(sources, dropped)
    if base is not None:   # earlier drops stay on record
        known = {url_key(g["url"]) for g in gone}
        gone = [g for g in base_doc.get("dropped_on_pi", []) if url_key(g["url"]) not in known] + gone
    used_countries = sorted({s["country"] for s in stations})
    used_langs = sorted({s["language"] for s in stations})
    used_genres = {g for s in stations for g in s["genres"]}
    return {
        "_comment": "The radio's world station collection (scripts/stations/build_collection.py). "
                    "Every stream was checked from the radio itself (a Pi in Spain) on 'verified'. "
                    "Ids are stable: presets may reference them; the url stays the source of truth.",
        "version": VERSION,
        "verified": max(v for v in verified if v),
        "genres": {g: {"en": en, "es": es_} for g, (en, es_) in GENRES.items() if g in used_genres},
        "languages": {c: LANGUAGES[c] for c in used_langs},
        "regions": {k: {"en": en, "es": es_, "countries": [c for c in members if c in used_countries]}
                    for k, (en, es_, members) in REGIONS.items()},
        "countries": {c: COUNTRIES[c] for c in used_countries},
        "stations": stations,
        "dropped_on_pi": gone,
    }


def dump(doc: dict) -> str:
    """Readable and diffable: one station (one dropped entry) per line."""
    def line(v: object) -> str:
        return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    parts = []
    for key, value in doc.items():
        if key in ("stations", "dropped_on_pi"):
            body = ",\n".join("  " + line(v) for v in value)
            parts.append(f" {line(key)}:[\n{body}\n ]" if value else f" {line(key)}:[]")
        else:
            parts.append(f" {line(key)}:{line(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("curators", nargs="*", type=Path)
    ap.add_argument("--base", type=Path, help="an existing collection.json: kept first, ids untouched")
    ap.add_argument("--es", type=Path, help="an es.json-style list (name, url, genre, region, keywords)")
    ap.add_argument("--drop", type=Path, help="JSON list of {url, reason} to leave out")
    ap.add_argument("--out", type=Path, default=ROOT / "config" / "stations" / "collection.json")
    ap.add_argument("--urls", type=Path)
    a = ap.parse_args(argv)
    dropped = {}
    if a.drop:
        dropped = {url_key(d["url"]): d["reason"] for d in json.loads(a.drop.read_text())}
    doc = build(a.curators, a.es, dropped, a.base)
    a.out.write_text(dump(doc), encoding="utf-8")
    if a.urls:
        a.urls.write_text("".join(f"{s['id']}\t{s['codec']}\t{s['url']}\n" for s in doc["stations"]))
    print(f"{len(doc['stations'])} stations, {len(doc['countries'])} countries, "
          f"{len(doc['dropped_on_pi'])} dropped -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
