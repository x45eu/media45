"""Read-only, superuser-only local system overview. No external requests."""
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import F
from django.http import HttpResponseForbidden
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from . import covers
from .management.commands.auto_details import STATE_KEY as DETAILS_KEY
from .management.commands.auto_media import STATE_KEY as SCAN_KEY
from .models import Media, MediaDetails, ProviderThrottle, ScannedFile


@login_required
@require_GET
@never_cache
def system_status(request):
    if not request.user.is_active or not request.user.is_superuser:
        return HttpResponseForbidden("Zugriff nur für Administratoren.")

    now = timezone.now()
    items = Media.objects.filter(kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES))
    ids = items.values_list("pk", flat=True)
    total = items.count()

    # Cached, verified IMDb identity only. No requests to TMDB.
    current_details = MediaDetails.objects.filter(
        media__kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES),
        media__external_provider="imdb",
        media__external_id=F("imdb_id"),
        fetched_at__gt=now - timedelta(days=90),
    ).count()
    cover_count = sum(1 for pk in ids.iterator(chunk_size=200) if covers.available(pk))

    # These timestamps indicate completed scheduled cycles, NOT container liveness.
    stamps = dict(ProviderThrottle.objects.filter(
        provider__in=(DETAILS_KEY, SCAN_KEY),
    ).values_list("provider", "last_call"))

    # The scanner only sees read-only NAS paths; the web process never mounts them.
    return render(request, "catalog/system_status.html", {
        "total": total,
        "cover_count": cover_count,
        "cover_missing": total - cover_count,
        "details_count": current_details,
        "details_missing": total - current_details,
        "indexed_files": ScannedFile.objects.filter(root__in=("movies", "series")).count(),
        "last_details": stamps.get(DETAILS_KEY),
        "last_scan": stamps.get(SCAN_KEY),
        "checked_at": now,
    })
