"""Fetch verified catalog art offline. The web process serves only cached local files.

All network hosts are fixed/allowlisted, redirects validated, sizes limited,
and image contents decoded and rewritten by Pillow before they are stored.
"""
import io
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, quote

import requests
from django.conf import settings
from PIL import Image, UnidentifiedImageError

from .models import Media

log = logging.getLogger(__name__)
AGENT = "media45/0.4 (cached artwork)"
TMDB_API = "https://api.themoviedb.org/3"
TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"
TMDB_TTL_SECONDS = 150 * 86400
IMAGE_HOSTS = {"image.tmdb.org", "upload.wikimedia.org", "thumb.wikimedia.org", "commons.wikimedia.org", "archive.org", "ia600.us.archive.org",
               "ia800.us.archive.org"}
MAX_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 16_000_000
TIMEOUT = (4, 12)
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class CoverUnavailable(Exception):
    pass


def cover_path(media_id):
    return Path(settings.COVER_DIR) / f"{int(media_id)}.webp"


def tmdb_stamp(media_id):
    return Path(settings.COVER_DIR) / f"{int(media_id)}.tmdb"


def available(media_id):
    target = cover_path(media_id)
    if not target.is_file():
        return False
    stamp = tmdb_stamp(media_id)
    return not stamp.is_file() or time.time() - stamp.stat().st_mtime < TMDB_TTL_SECONDS


def get_json(url, params=None):
    response = requests.get(url, params=params, headers={"User-Agent": AGENT},
                            timeout=TIMEOUT, allow_redirects=False)
    if response.status_code != 200:
        raise CoverUnavailable(f"Metadaten HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise CoverUnavailable("Ungültige Metadaten.")
    return data


def tmdb_image_for(item):
    """Resolve only a verified IMDb ID to one TMDB movie/TV poster."""
    if item.kind not in (Media.Kind.MOVIE, Media.Kind.SERIES):
        raise CoverUnavailable("TMDB ist nur für Filme und Serien vorgesehen.")
    if item.external_provider != "imdb" or not re.fullmatch(r"tt[0-9]{7,12}", item.external_id):
        raise CoverUnavailable("Für TMDB ist eine bestätigte IMDb-ID nötig.")
    token = (getattr(settings, "TMDB_READ_ACCESS_TOKEN", "") or "").strip()
    if not token:
        raise CoverUnavailable("TMDB_READ_ACCESS_TOKEN fehlt in der .env.")
    response = requests.get(
        f"{TMDB_API}/find/{item.external_id}",
        params={"external_source": "imdb_id", "language": "de-DE"},
        headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": AGENT},
        timeout=TIMEOUT, allow_redirects=False,
    )
    if response.status_code == 401:
        raise CoverUnavailable("TMDB-Token ungültig (HTTP 401).")
    if response.status_code == 429:
        raise CoverUnavailable("TMDB begrenzt Anfragen (HTTP 429).")
    if response.status_code != 200:
        raise CoverUnavailable(f"TMDB HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise CoverUnavailable("TMDB sendet kein gültiges JSON.") from exc
    if not isinstance(data, dict):
        raise CoverUnavailable("TMDB-Antwort ungültig.")
    key = "movie_results" if item.kind == Media.Kind.MOVIE else "tv_results"
    results = data.get(key, [])
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise CoverUnavailable("TMDB hat keine eindeutige Zuordnung gefunden.")
    poster = results[0].get("poster_path")
    if not isinstance(poster, str) or not re.fullmatch(r"/[A-Za-z0-9_-]{5,100}\.(?:jpg|jpeg|png|webp)", poster, re.I):
        raise CoverUnavailable("Kein gültiges TMDB-Poster vorhanden.")
    return TMDB_IMAGE + poster


def wikimedia_image_for(item):
    """Wikidata claims must match the exact IMDb ID; prefer dedicated film posters."""
    if not re.fullmatch(r"tt[0-9]{7,12}", item.external_id):
        raise CoverUnavailable("Keine gültige IMDb-ID.")

    # Search by IMDb ID, rather than relying on German/English title aliases.
    ids = []
    query = get_json("https://www.wikidata.org/w/api.php", {
        "action": "query", "format": "json", "list": "search",
        "srsearch": "haswbstatement:P345=" + item.external_id,
        "srlimit": "20",
    })
    for hit in query.get("query", {}).get("search", []):
        title = hit.get("title", "")
        if isinstance(title, str) and re.fullmatch(r"Q[1-9][0-9]*", title):
            ids.append(title)

    # Fallback to title search, but still verify each candidate's IMDb claim.
    if not ids:
        for lang in ("de", "en"):
            data = get_json("https://www.wikidata.org/w/api.php", {
                "action": "wbsearchentities", "format": "json",
                "language": lang, "search": item.title,
                "type": "item", "limit": "12",
            })
            for hit in data.get("search", []):
                qid = hit.get("id", "")
                if (isinstance(qid, str) and re.fullmatch(r"Q[1-9][0-9]*", qid)
                        and qid not in ids):
                    ids.append(qid)
    if not ids:
        raise CoverUnavailable("Kein passender Wikidata-Eintrag.")

    data = get_json("https://www.wikidata.org/w/api.php", {
        "action": "wbgetentities", "format": "json",
        "ids": "|".join(ids[:24]), "props": "claims",
    })
    for entity in data.get("entities", {}).values():
        claims = entity.get("claims") or {}
        imdbs = [
            claim.get("mainsnak", {}).get("datavalue", {}).get("value")
            for claim in claims.get("P345", [])
        ]
        if item.external_id not in imdbs:
            continue
        # Wikidata P3383 is a proper film poster, P18 is general imagery.
        for prop in ("P3383", "P18"):
            for claim in claims.get(prop, []):
                filename = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if not isinstance(filename, str) or not filename.strip() or len(filename) > 250:
                    continue
                # Wikimedia's own special redirect resolves the filename to a CDN URL.
                # Every redirect is still validated in fetch_image().
                return (
                    "https://commons.wikimedia.org/wiki/Special:Redirect/file/"
                    + quote(filename.replace(" ", "_"), safe="")
                    + "?width=600"
                )
    raise CoverUnavailable("Kein eindeutig zugeordnetes Wikimedia-Bild.")


def safe_image_url(url):
    """Permit HTTPS imagery only from explicit hosts, never URL credentials."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.port not in (None, 443)):
            return False
        host = parsed.hostname or ""
        return (host in IMAGE_HOSTS
                or re.fullmatch(r"ia[0-9]{3,6}\.us\.archive\.org", host) is not None)
    except ValueError:
        # Malformed port, bracketed IP, etc.
        return False


def fetch_image(url):
    for _ in range(4):
        if not safe_image_url(url):
            raise CoverUnavailable("Bildquelle nicht freigegeben: " + (urlsplit(url).hostname or "unbekannt"))
        with requests.get(url, headers={"User-Agent": AGENT}, timeout=TIMEOUT,
                          stream=True, allow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = response.headers.get("Location", "")
                if url.startswith("http://"):
                    url = "https://" + url[7:]
                continue
            if response.status_code != 200:
                raise CoverUnavailable(f"Bild HTTP {response.status_code}")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise CoverUnavailable("Coverdatei zu groß.")
        try:
            with Image.open(io.BytesIO(content)) as image:
                if image.width * image.height > MAX_PIXELS:
                    raise CoverUnavailable("Bildabmessungen zu groß.")
                image = image.convert("RGB")
                image.thumbnail((720, 1080))
                output = io.BytesIO()
                image.save(output, format="WEBP", quality=82, method=4)
                return output.getvalue()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise CoverUnavailable("Ungültige Bilddaten.") from exc
    raise CoverUnavailable("Zu viele Bildweiterleitungen.")


def acquire(item):
    if item.kind not in (Media.Kind.MOVIE, Media.Kind.SERIES):
        raise CoverUnavailable("Cover nur für Filme und Serien.")
    url = tmdb_image_for(item)
    image = fetch_image(url)
    target = cover_path(item.pk)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Use atomic replacement; web readers must never see partial images.
    import os
    from tempfile import NamedTemporaryFile
    with NamedTemporaryFile(dir=target.parent, prefix=".cover-", suffix=".webp",
                            delete=False) as tmp:
        temp_path = Path(tmp.name)
        try:
            tmp.write(image)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    os.replace(temp_path, target)
    stamp = tmdb_stamp(item.pk)
    stamp.touch()
    return target
