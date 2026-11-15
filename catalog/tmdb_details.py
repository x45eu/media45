"""Offline-only enrichment using exact, uniquely resolved IMDb identities.

No network calls occur during user-facing page requests. TMDB metadata is
optional and cannot change a catalog title, status, or media-file association.
"""
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from .models import Media, MediaDetails
from .providers import ProviderError, tmdb_get

TTL = timedelta(days=90)
IMDB = re.compile(r"tt[0-9]{7,12}\Z")


class DetailsUnavailable(Exception):
    pass


def current(item, now=None):
    """Return a valid identity-bound cache record, or None (no I/O)."""
    cache = getattr(item, "_details_cache", None)
    if cache is None:
        try:
            cache = item.details
        except MediaDetails.DoesNotExist:
            return None
    if item.external_provider != "imdb" or cache.imdb_id != item.external_id:
        return None
    if cache.fetched_at > (now or timezone.now()) - TTL:
        return cache
    return None


def fetch(item):
    """Fetch and save data for an explicitly verified movie or TV entry."""
    if item.kind not in (Media.Kind.MOVIE, Media.Kind.SERIES):
        raise DetailsUnavailable("Nur Film- und Serienmetadaten unterstützt.")
    if item.external_provider != "imdb" or not IMDB.fullmatch(item.external_id):
        raise DetailsUnavailable("Keine gültige bestätigte IMDb-ID.")

    endpoint = "movie" if item.kind == Media.Kind.MOVIE else "tv"
    try:
        found = tmdb_get(f"/find/{item.external_id}", {
            "external_source": "imdb_id", "language": "de-DE",
        })
    except ProviderError as exc:
        raise DetailsUnavailable(str(exc)) from exc
    matches = found.get("movie_results" if endpoint == "movie" else "tv_results")
    if not isinstance(matches, list) or len(matches) != 1 or not isinstance(matches[0], dict):
        raise DetailsUnavailable("Keine eindeutige TMDB-Zuordnung zur IMDb-ID.")
    tmdb_id = matches[0].get("id")
    if type(tmdb_id) is not int or tmdb_id < 1:
        raise DetailsUnavailable("Ungültige TMDB-Kennung.")

    try:
        data = tmdb_get(f"/{endpoint}/{tmdb_id}", {"language": "de-DE"})
    except ProviderError as exc:
        raise DetailsUnavailable(str(exc)) from exc
    if data.get("id") != tmdb_id:
        raise DetailsUnavailable("TMDB-Detailantwort gehört zu einer anderen Kennung.")

    synopsis = data.get("overview")
    synopsis = synopsis.strip()[:4000] if isinstance(synopsis, str) else ""
    raw_genres = data.get("genres")
    genres = []
    if isinstance(raw_genres, list):
        for genre in raw_genres[:15]:
            if isinstance(genre, dict) and isinstance(genre.get("name"), str):
                name = genre["name"].strip()
                if name and len(name) <= 60 and name not in genres:
                    genres.append(name)
    raw_runtime = data.get("runtime") if endpoint == "movie" else (
        (data.get("episode_run_time") or [None])[0]
        if isinstance(data.get("episode_run_time"), list) else None
    )
    runtime = raw_runtime if type(raw_runtime) is int and 1 <= raw_runtime <= 1440 else None

    raw_rating = data.get("vote_average")
    rating = None
    if type(raw_rating) in (int, float, str):
        try:
            value = Decimal(str(raw_rating))
            if value.is_finite() and 0 <= value <= 10:
                rating = value.quantize(Decimal("0.1"))
        except (InvalidOperation, ValueError):
            pass
    # Do not present unrated items as a genuine zero-star rating.
    if not isinstance(data.get("vote_count"), int) or data["vote_count"] < 1:
        rating = None

    with transaction.atomic():
        # The IMDb identity must still be exactly the one we fetched.
        locked = Media.objects.select_for_update().get(pk=item.pk)
        if (locked.kind, locked.external_provider, locked.external_id) != (
            item.kind, "imdb", item.external_id,
        ):
            raise DetailsUnavailable("IMDb-Zuordnung wurde zwischenzeitlich geändert.")
        cached, _ = MediaDetails.objects.update_or_create(
            media=locked,
            defaults={
                "imdb_id": item.external_id,
                "tmdb_id": tmdb_id,
                "synopsis": synopsis,
                "genres": genres,
                "runtime_minutes": runtime,
                "rating": rating,
                "fetched_at": timezone.now(),
            },
        )
    return cached
