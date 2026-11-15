"""Fixed-host, server-side metadata providers; no URL input is accepted from browsers."""
import logging
import re
import time
from datetime import timedelta

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.utils import timezone
from .models import ProviderThrottle, SearchAttempt

log = logging.getLogger(__name__)
SALT = "media45.request.selection.v1"
KINDS = ("movie", "series")
IMDB_RE = re.compile(r"^tt[0-9]{7,12}$")
USER_AGENT = "media45/0.3 (metadata lookup)"


class ProviderError(Exception):
    pass


def search_quota(user):
    """DB-backed per-user budget. Serialize per user; max 6 searches per 60 seconds."""
    since = timezone.now() - timedelta(seconds=60)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        if SearchAttempt.objects.filter(user=user, created_at__gte=since).count() >= 6:
            raise ProviderError("Zu viele Suchanfragen. Bitte in einer Minute erneut versuchen.")
        SearchAttempt.objects.create(user=user)


def get_json(url, *, params=None, headers=None):
    """Bounded retries for temporary provider failures; no arbitrary host input."""
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers or {}, timeout=(3, 8))
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            log.warning("Metadata provider HTTP status %s (%s)", code, url.split("/")[2])
            if code in (429, 502, 503, 504) and attempt < 2:
                header = exc.response.headers.get("Retry-After", "") if exc.response is not None else ""
                seconds = int(header) if header.isdigit() else 3 * (attempt + 1)
                time.sleep(max(2, min(seconds, 20)))
                continue
            if code == 429:
                raise ProviderError("Metadatendienst begrenzt Anfragen (HTTP 429). Bitte später fortsetzen.") from exc
            raise ProviderError(f"Metadatendienst antwortet mit HTTP {code or 'Fehler'}.") from exc
        except (requests.RequestException, ValueError) as exc:
            log.warning("Metadata provider request failed: %s", type(exc).__name__)
            raise ProviderError("Metadatendienst derzeit nicht erreichbar.") from exc


def wikidata_get(*, params):
    return get_json(
        "https://www.wikidata.org/w/api.php",
        params=dict(params, format="json"),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def movie_search(phrase):
    """Search Wikidata labels, then read P345 (IMDb) from candidate entities.

    This is an *unverified* shortlist. Never infer an IMDb ID from a title.
    """
    seen = set()
    entities = []
    for language in ("de", "en"):
        data = wikidata_get(params={
            "action": "wbsearchentities", "search": phrase,
            "language": language, "type": "item", "limit": 12,
        })
        for result in data.get("search", []):
            qid = result.get("id", "")
            if isinstance(qid, str) and re.fullmatch(r"Q[1-9][0-9]*", qid) and qid not in seen:
                seen.add(qid)
                entities.append(qid)
        if len(entities) >= 12:
            break
    if not entities:
        return []
    result = wikidata_get(params={
        "action": "wbgetentities", "ids": "|".join(entities[:18]),
        "props": "claims|labels|descriptions", "languages": "de|en",
    })
    hits = []
    used = set()
    for qid in entities[:18]:
        entity = result.get("entities", {}).get(qid, {})
        claims = entity.get("claims") or {}
        descriptions = entity.get("descriptions") or {}
        description = " ".join(
            (descriptions.get(lang) or {}).get("value", "") for lang in ("de", "en")
        ).lower()
        # Strongly exclude obvious non-film records (TV episodes, video games,
        # books, soundtracks); ambiguous results are still manually reviewed.
        blocked = ("television series", "fernsehserie", "tv series", "video game",
                   "videospiel", "roman", "novel", "soundtrack", "album",
                   "fernsehepisode", "television episode")
        if any(word in description for word in blocked):
            continue
        imdb = None
        for claim in claims.get("P345", []):
            snak = claim.get("mainsnak") or {}
            value = (snak.get("datavalue") or {}).get("value")
            if isinstance(value, str) and IMDB_RE.fullmatch(value):
                imdb = value
                break
        if not imdb or imdb in used:
            continue
        labels = entity.get("labels") or {}
        title = ((labels.get("de") or labels.get("en") or {}).get("value") or "").strip()
        if not title:
            continue
        year = None
        for claim in claims.get("P577", []):
            value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            date = value.get("time", "") if isinstance(value, dict) else ""
            match = re.match(r"^[+-]([0-9]{4})-", date)
            if match:
                year = int(match.group(1))
                break
        used.add(imdb)
        hits.append({"kind": "movie", "title": title[:300], "year": year,
                     "external_provider": "imdb", "external_id": imdb})
        if len(hits) == 8:
            break
    return hits


def series_search(phrase):
    """TVmaze public search; only accept shows with an IMDb external ID."""
    data = get_json(
        "https://api.tvmaze.com/search/shows",
        params={"q": phrase},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    hits = []
    used = set()
    for item in data[:12] if isinstance(data, list) else []:
        show = item.get("show") if isinstance(item, dict) else None
        if not isinstance(show, dict):
            continue
        imdb = (show.get("externals") or {}).get("imdb")
        title = show.get("name")
        if not isinstance(imdb, str) or not IMDB_RE.fullmatch(imdb):
            continue
        if not isinstance(title, str) or not title.strip() or imdb in used:
            continue
        premiered = show.get("premiered") or ""
        year = int(premiered[:4]) if isinstance(premiered, str) and premiered[:4].isdigit() else None
        used.add(imdb)
        hits.append({"kind": "series", "title": title.strip()[:300], "year": year,
                     "external_provider": "imdb", "external_id": imdb})
        if len(hits) == 8:
            break
    return hits


TMDB_API = "https://api.themoviedb.org/3"


def tmdb_get(endpoint, params=None):
    """Fixed TMDB endpoints; credential stays server-side."""
    token = (getattr(settings, "TMDB_READ_ACCESS_TOKEN", "") or "").strip()
    if not token:
        raise ProviderError("TMDB_READ_ACCESS_TOKEN fehlt in der .env.")
    if not re.fullmatch(r"/(?:search/(?:movie|tv)|(?:movie|tv)/[1-9][0-9]*(?:/external_ids)?|find/tt[0-9]{7,12})", endpoint):
        raise ProviderError("Ungültiger TMDB-Endpunkt.")
    try:
        response = requests.get(TMDB_API + endpoint, params=params or {},
            headers={"Authorization": "Bearer " + token, "Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=(3, 9), allow_redirects=False)
        if response.status_code in (401, 403):
            raise ProviderError("TMDB-Token ungültig oder nicht berechtigt.")
        if response.status_code == 429:
            raise ProviderError("TMDB begrenzt Anfragen (HTTP 429).")
        if response.status_code != 200:
            raise ProviderError(f"TMDB antwortet mit HTTP {response.status_code}.")
        data = response.json()
        if not isinstance(data, dict):
            raise ProviderError("TMDB-Antwort ungültig.")
        return data
    except (requests.RequestException, ValueError) as exc:
        raise ProviderError("TMDB derzeit nicht erreichbar.") from exc


def tmdb_title_variants(phrase):
    """Conservative alternate searches. Does not change stored folder names."""
    import unicodedata

    title = re.sub(r"\s+", " ", phrase.replace("_", " ").replace(".", " ")).strip(" -()")
    # Keep colon/hyphen wording intact first; variants are used only on no hits.
    candidates = [phrase.strip(), title]
    pretty = re.sub(r"\s*[-–—:]\s*", " ", title)
    candidates.append(pretty)
    normalized = pretty
    normalized = re.sub(r"\bstartrek\b", "Star Trek", normalized, flags=re.I)
    normalized = re.sub(r"\bstar\s*trek\s*tng\b", "Star Trek The Next Generation", normalized, flags=re.I)
    normalized = re.sub(r"\bstar\s*trek\s*tos\b", "Star Trek The Original Series", normalized, flags=re.I)
    normalized = re.sub(r"\bstar\s*trek\s*ds9\b", "Star Trek Deep Space Nine", normalized, flags=re.I)
    candidates.append(normalized)
    # German transliterations and English catalog titles can differ.
    german = re.sub("ae", "ä", normalized, flags=re.I)
    german = re.sub("oe", "ö", german, flags=re.I)
    german = re.sub("ue", "ü", german, flags=re.I)
    candidates.append(german)
    # ASCII spellings and missing spaces are frequent in folder names.
    ascii_title = unicodedata.normalize("NFKD", normalized)
    ascii_title = "".join(c for c in ascii_title if not unicodedata.combining(c))
    candidates.append(ascii_title)
    # Use only specific known aliases, never a general fuzzy/automatic assignment.
    aliases = {
        "star trek enterprise": "Star Trek Enterprise",
        "star trek voyager": "Star Trek Voyager",
        "star trek the next generation": "Star Trek The Next Generation",
        "vier faeuste gegen rio": "Vier Fäuste gegen Rio",
        "out come the wolfes": "Out Come the Wolves",
    }
    key = ascii_title.casefold()
    if key in aliases:
        candidates.append(aliases[key])
    unique = []
    seen = set()
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if 2 <= len(candidate) <= 100 and candidate.casefold() not in seen:
            seen.add(candidate.casefold())
            unique.append(candidate)
    return unique[:5]


def tmdb_search(kind, phrase):
    """Search variants only as needed; validate IMDb IDs before making proposals."""
    if kind not in ("movie", "series") or not 2 <= len(phrase) <= 100:
        raise ProviderError("Ungültige TMDB-Suche.")
    category = "movie" if kind == "movie" else "tv"
    year_match = re.search(r"(?:^|[ (._-])((?:19|20)[0-9]{2})(?:[ )._-]|$)", phrase)
    year = int(year_match.group(1)) if year_match else None
    results = []
    used = set()
    checked_tmdb_ids = set()
    # Stop once a query yielded a valid IMDb-based result; minimize TMDB traffic.
    for variant in tmdb_title_variants(phrase):
        query = re.sub(r"\s+", " ", re.sub(r"(?<!\w)(?:19|20)[0-9]{2}(?!\w)", "", variant)).strip(" ._-()")
        if len(query) < 2:
            continue
        params = {"query": query, "language": "de-DE", "include_adult": "false", "page": 1}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        data = tmdb_get("/search/" + category, params)
        rows = data.get("results", [])
        if not isinstance(rows, list):
            continue
        for row in rows[:6]:
            if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
                continue
            tmdb_id = row["id"]
            if tmdb_id in checked_tmdb_ids:
                continue
            checked_tmdb_ids.add(tmdb_id)
            name = row.get("title" if kind == "movie" else "name")
            if not isinstance(name, str) or not name.strip():
                name = row.get("original_title" if kind == "movie" else "original_name")
            if not isinstance(name, str) or not name.strip():
                continue
            ids = tmdb_get(f"/{category}/{tmdb_id}/external_ids")
            imdb = ids.get("imdb_id")
            if not isinstance(imdb, str) or not IMDB_RE.fullmatch(imdb) or imdb in used:
                continue
            date = row.get("release_date" if kind == "movie" else "first_air_date")
            release_year = int(date[:4]) if isinstance(date, str) and re.match(r"^[0-9]{4}", date) else None
            if release_year is not None and not 1888 <= release_year <= 2100:
                release_year = None
            used.add(imdb)
            results.append({"kind": kind, "title": name.strip()[:300], "year": release_year,
                            "external_provider": "imdb", "external_id": imdb})
        if results:
            break
    return results


def tmdb_lookup(kind, identifier):
    """Exact IMDb-ID lookup, keeping movie vs TV type separate."""
    if kind not in ("movie", "series") or not IMDB_RE.fullmatch(identifier):
        raise ProviderError("Bitte eine gültige IMDb-ID eingeben.")
    data = tmdb_get("/find/" + identifier,
                    {"external_source": "imdb_id", "language": "de-DE"})
    rows = data.get("movie_results" if kind == "movie" else "tv_results", [])
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ProviderError("TMDB konnte diese IMDb-ID nicht eindeutig zuordnen.")
    row = rows[0]
    name = row.get("title" if kind == "movie" else "name")
    if not isinstance(name, str) or not name.strip():
        name = row.get("original_title" if kind == "movie" else "original_name")
    if not isinstance(name, str) or not name.strip():
        raise ProviderError("Kein gültiger TMDB-Titel.")
    date = row.get("release_date" if kind == "movie" else "first_air_date")
    release_year = int(date[:4]) if isinstance(date, str) and re.match(r"^[0-9]{4}", date) else None
    return {"kind": kind, "title": name.strip()[:300],
            "year": release_year if release_year is not None and 1888 <= release_year <= 2100 else None,
            "external_provider": "imdb", "external_id": identifier}


def search(kind, phrase):
    if kind in KINDS:
        return tmdb_search(kind, phrase)
    raise ProviderError("Ungültiger Medientyp.")


def lookup_identity(kind, raw_id):
    if kind not in KINDS:
        raise ProviderError("Dieser Medientyp wird nicht unterstützt.")
    return tmdb_lookup(kind, raw_id.strip())


def sign_result(hit):
    return signing.dumps(hit, salt=SALT, compress=True)


def verify_result(value):
    try:
        hit = signing.loads(value, salt=SALT, max_age=15 * 60)
    except signing.BadSignature as exc:
        raise ProviderError("Auswahl ungültig oder abgelaufen. Bitte erneut suchen.") from exc
    if not isinstance(hit, dict) or hit.get("kind") not in KINDS:
        raise ProviderError("Ungültige Titelauswahl.")
    if hit.get("external_provider") != "imdb" or not IMDB_RE.fullmatch(str(hit.get("external_id", ""))):
        raise ProviderError("IMDb-Zuordnung fehlt.")
    title = hit.get("title")
    if not isinstance(title, str) or not 0 < len(title) <= 300:
        raise ProviderError("Ungültiger Medientitel.")
    return hit
