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
from django.db.models import Exists, OuterRef
from .models import (Media, MediaDetails, ProviderThrottle, ScannedFile,
                     MediaGroup, MetadataCandidate)
from .health_events import (SCAN_FAILURE, MATCH_FAILURE, DETAILS_FAILURE,
                            SCHEDULER_HEARTBEAT, DETAILS_HEARTBEAT)


def assess_freshness(timestamp, now, *, max_age=timedelta(hours=26)):
    """Only recorded execution age, never Docker service liveness."""
    if timestamp is None or timestamp > now:
        return {"level": "unknown", "label": "Unbekannt"}
    if now - timestamp <= max_age:
        return {"level": "ok", "label": "Im Zeitfenster"}
    return {"level": "warning", "label": "Überfällig"}


def assess_heartbeat(timestamp, now, *, max_age):
    """Recent DB contact, never a container health check."""
    if timestamp is None or timestamp > now:
        return {"level": "unknown", "label": "Keine Meldung"}
    if now - timestamp <= max_age:
        return {"level": "ok", "label": "Kürzlich gemeldet"}
    return {"level": "warning", "label": "Meldung überfällig"}


def assess_completeness(current, total):
    """Missing items are a warning, not proof of a failed service."""
    if total == 0:
        return {"level": "unknown", "label": "Keine Einträge"}
    if current == total:
        return {"level": "ok", "label": "Vollständig"}
    return {"level": "warning", "label": "Unvollständig"}


def assess_failure(health, failure_time, now):
    """Red means a recorded exception/config failure, never assumed downtime."""
    if failure_time is None:
        return health
    if failure_time > now:
        return {"level": "unknown", "label": "Unbekannt"}
    return {"level": "error", "label": "Fehler erfasst"}


def admin_work_summary():
    """Aggregate only: no media titles, files, candidate identities or network access."""
    active_files = ScannedFile.objects.filter(root__in=("movies", "series"))
    linked = active_files.filter(group_id=OuterRef("pk"), media__isnull=False)
    candidates = MetadataCandidate.objects.filter(group_id=OuterRef("pk"))
    actionable = MediaGroup.objects.filter(
        root__in=("movies", "series"), file_count__gt=0,
        review_deferred=False,
    ).annotate(has_linked=Exists(linked), has_candidates=Exists(candidates)).filter(
        has_linked=False,
    )
    return {
        "groups_with_suggestions": actionable.filter(has_candidates=True).count(),
        "groups_unsearched": actionable.filter(
            metadata_checked_at__isnull=True, has_candidates=False,
        ).count(),
        "groups_no_results": actionable.filter(
            metadata_checked_at__isnull=False, has_candidates=False,
        ).count(),
        "groups_deferred": MediaGroup.objects.filter(
            root__in=("movies", "series"), file_count__gt=0,
            review_deferred=True,
        ).count(),
        "files_without_media": active_files.filter(media__isnull=True).count(),
    }


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
        provider__in=(DETAILS_KEY, SCAN_KEY, SCAN_FAILURE, MATCH_FAILURE, DETAILS_FAILURE,
                     SCHEDULER_HEARTBEAT, DETAILS_HEARTBEAT),
    ).values_list("provider", "last_call"))

    # The scanner only sees read-only NAS paths; the web process never mounts them.
    work_summary = admin_work_summary()
    return render(request, "catalog/system_status.html", {
        "total": total,
        "cover_count": cover_count,
        "cover_missing": total - cover_count,
        "details_count": current_details,
        "details_missing": total - current_details,
        "indexed_files": ScannedFile.objects.filter(root__in=("movies", "series")).count(),
        "work_summary": work_summary,
        "last_details": stamps.get(DETAILS_KEY),
        "last_scan": stamps.get(SCAN_KEY),
        "last_scheduler_heartbeat": stamps.get(SCHEDULER_HEARTBEAT),
        "last_details_heartbeat": stamps.get(DETAILS_HEARTBEAT),
        "scheduler_heartbeat_health": assess_heartbeat(
            stamps.get(SCHEDULER_HEARTBEAT), now, max_age=timedelta(minutes=45)),
        "details_heartbeat_health": assess_heartbeat(
            stamps.get(DETAILS_HEARTBEAT), now, max_age=timedelta(hours=2, minutes=30)),
        "cover_health": assess_completeness(cover_count, total),
        "details_health": assess_completeness(current_details, total),
        "scan_health": assess_failure(assess_freshness(stamps.get(SCAN_KEY), now), stamps.get(SCAN_FAILURE), now),
        "match_health": assess_failure({"level": "unknown", "label": "Keine Laufzeitüberwachung"}, stamps.get(MATCH_FAILURE), now),
        "details_run_health": assess_failure(assess_freshness(stamps.get(DETAILS_KEY), now), stamps.get(DETAILS_FAILURE), now),
        "checked_at": now,
    })
