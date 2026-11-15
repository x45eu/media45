from django.contrib import messages
from django.core.exceptions import ValidationError
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Exists, OuterRef, Prefetch
from django.core.paginator import Paginator
from django.http import HttpResponseForbidden, FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods
from .models import Media, MediaRequest, Notification, MediaGroup, MetadataCandidate, ScannedFile
from . import providers, covers


def index(request):
    """Filter verified catalog rows with locally cached, identity-bound metadata."""
    from decimal import Decimal
    from datetime import timedelta
    from django.db.models import F
    from django.utils import timezone
    from .models import MediaDetails

    phrase = request.GET.get("q", "").strip()[:100]
    kind = request.GET.get("kind", "")
    state = request.GET.get("state", "")
    sort = request.GET.get("sort", "title")
    genre = request.GET.get("genre", "").strip()[:60]
    raw_year = request.GET.get("year", "")
    raw_rating = request.GET.get("rating", "")

    if kind not in Media.Kind.values:
        kind = ""
    if state not in Media.State.values:
        state = ""
    if sort not in ("title", "newest", "year", "rating"):
        sort = "title"

    # Only current records for the *same verified IMDb identity* may supply
    # TMDB-based filter results. No API requests or user-provided SQL fragments.
    cutoff = timezone.now() - timedelta(days=90)
    details = MediaDetails.objects.filter(
        media__external_provider="imdb",
        media__external_id=F("imdb_id"),
        fetched_at__gte=cutoff,
    )
    genres = sorted({
        name for names in details.values_list("genres", flat=True)
        if isinstance(names, list)
        for name in names
        if isinstance(name, str) and 0 < len(name) <= 60
    }, key=str.casefold)
    years = list(
        Media.objects.filter(kind__in=Media.Kind.values, year__isnull=False)
        .order_by("-year").values_list("year", flat=True).distinct()
    )
    year = int(raw_year) if raw_year.isdecimal() and raw_year in {str(y) for y in years} else None
    if genre not in genres:
        genre = ""
    rating_choices = (("6", "Ab 6/10"), ("7", "Ab 7/10"),
                      ("8", "Ab 8/10"), ("9", "Ab 9/10"))
    rating = raw_rating if raw_rating in dict(rating_choices) else ""

    media = Media.objects.filter(kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES))
    if phrase:
        media = media.filter(Q(title__icontains=phrase) | Q(external_id__icontains=phrase))
    if kind:
        media = media.filter(kind=kind)
    if state:
        media = media.filter(state=state)
    if year is not None:
        media = media.filter(year=year)
    if genre or rating:
        matched = details
        if genre:
            matched = matched.filter(genres__contains=[genre])
        if rating:
            matched = matched.filter(rating__gte=Decimal(rating))
        media = media.filter(pk__in=matched.values("media_id"))

    if sort == "rating":
        # Preserve un-rated media at the bottom instead of making them vanish.
        from django.db.models import Case, When, DecimalField, Value
        media = media.annotate(
            cached_rating=Case(
                When(
                    external_provider="imdb",
                    external_id=F("details__imdb_id"),
                    details__fetched_at__gte=cutoff,
                    then=F("details__rating"),
                ),
                default=Value(None),
                output_field=DecimalField(max_digits=3, decimal_places=1),
            )
        ).order_by(F("cached_rating").desc(nulls_last=True), "title", "pk")
    else:
        order = {
            "title": ("title", "pk"),
            "newest": ("-created_at", "-pk"),
            "year": ("-year", "title", "pk"),
        }
        media = media.order_by(*order[sort])
    page = Paginator(media, 24).get_page(request.GET.get("page"))
    for item in page.object_list:
        item.has_cover = covers.available(item.pk)

    active_advanced = bool(genre or year is not None or rating or sort != "title")
    return render(request, "catalog/index.html", {
        "page_obj": page, "query": phrase, "kind": kind, "state": state, "sort": sort,
        "genre": genre, "year": year, "rating": rating,
        "genre_choices": genres, "year_choices": years, "rating_choices": rating_choices,
        "advanced_open": active_advanced,
        "kind_choices": Media.Kind.choices, "state_choices": Media.State.choices,
        "total_count": Media.objects.filter(kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES)).count(),
        "available_count": Media.objects.filter(kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES), state=Media.State.AVAILABLE).count(),
        "requested_count": Media.objects.filter(kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES), state=Media.State.REQUESTED).count(),
    })


def media_detail(request, pk):
    """No raw filesystem paths are shown to catalog visitors."""
    item = get_object_or_404(
        Media.objects.annotate(file_count=Count("scanned_files", distinct=True)),
        pk=pk, kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES),
    )
    item.has_cover = covers.available(item.pk)
    from .tmdb_details import current

    details = current(item)
    return render(request, "catalog/detail.html", {"item": item, "details": details})


@login_required
@require_http_methods(["GET", "POST"])
def new_request(request):
    kind = request.GET.get("kind", "movie")
    if kind not in providers.KINDS:
        kind = "movie"
    phrase = request.GET.get("q", "").strip()
    hits, error = [], None

    if request.method == "POST":
        try:
            hit = providers.verify_result(request.POST.get("selection", ""))
            with transaction.atomic():
                media, _ = Media.objects.get_or_create(
                    kind=hit["kind"],
                    external_provider=hit["external_provider"],
                    external_id=hit["external_id"],
                    defaults={"title": hit["title"], "year": hit.get("year")},
                )
                if media.state == Media.State.AVAILABLE:
                    messages.info(request, "Dieser Titel ist bereits vorhanden.")
                else:
                    obj, created = MediaRequest.objects.get_or_create(
                        media=media, user=request.user
                    )
                    if created:
                        Notification.objects.create(
                            title="media45: Neuer Medienwunsch",
                            body=f"{media.title} ({media.get_kind_display()})\nVon: {request.user.username}\nKennung: {media.external_provider}:{media.external_id}",
                            target_path=f"/admin/catalog/mediarequest/{obj.pk}/change/",
                        )
                        messages.success(request, "Dein Medienwunsch wurde gespeichert.")
                    else:
                        messages.info(request, "Du hast diesen Titel bereits angefragt.")
            return redirect("index")
        except providers.ProviderError as exc:
            error = str(exc)
    elif phrase:
        if kind not in providers.KINDS or not 2 <= len(phrase) <= 100:
            error = "Bitte einen Medientyp und einen Suchbegriff (2–100 Zeichen) angeben."
        else:
            try:
                providers.search_quota(request.user)
                hits = [
                    dict(hit, selection=providers.sign_result(hit))
                    for hit in providers.search(kind, phrase)
                ]
            except providers.ProviderError as exc:
                error = str(exc)
    return render(request, "catalog/new_request.html", {
        "hits": hits, "error": error, "kind": kind, "query": phrase,
    })


@login_required
@require_http_methods(["GET", "POST"])
def review_matches(request):
    """One candidate per scanned title group. Superusers only.

    Group selection is validated against the currently displayed page;
    no public user can approve metadata or enumerate unverified names.
    """
    if not request.user.is_active or not request.user.is_superuser:
        return HttpResponseForbidden("Zugriff nur für Administratoren.")

    phrase = request.GET.get("q", "").strip()[:100]
    root = request.GET.get("root", "")
    allowed_roots = [choice[0] for choice in MediaGroup._meta.get_field("root").choices]
    if root not in allowed_roots:
        root = ""

    linked_files = ScannedFile.objects.filter(group_id=OuterRef("pk"), media__isnull=False)
    groups = (
        MediaGroup.objects.filter(root__in=("movies", "series"), file_count__gt=0)
        .annotate(has_linked_files=Exists(linked_files))
        .filter(has_linked_files=False)
        .distinct()
    )
    deferred = request.GET.get("deferred") == "1"
    groups = groups.filter(review_deferred=deferred)
    status = request.GET.get("status", "")
    if status not in ("", "new", "empty", "candidates", "single"):
        status = ""
    if status == "new":
        groups = groups.filter(metadata_checked_at__isnull=True, candidates__isnull=True)
    elif status == "empty":
        groups = groups.filter(metadata_checked_at__isnull=False, candidates__isnull=True)
    elif status == "candidates":
        groups = groups.filter(candidates__isnull=False)
    elif status == "single":
        groups = groups.annotate(candidate_total=Count("candidates", distinct=True)).filter(candidate_total=1)
    if root:
        groups = groups.filter(root=root)
    if phrase:
        groups = groups.filter(
            Q(suggested_title__icontains=phrase) | Q(candidates__title__icontains=phrase)
        ).distinct()
    groups = groups.order_by("root", "suggested_title", "pk").prefetch_related(
        Prefetch("candidates", queryset=MetadataCandidate.objects.order_by("title", "pk"))
    )
    page = Paginator(groups, 20).get_page(request.GET.get("page"))

    if request.method == "POST":
        valid_groups = {g.pk: g for g in page.object_list}
        raw = request.POST.getlist("selection")
        try:
            if not 1 <= len(raw) <= 20:
                raise ValidationError("Bitte zwischen 1 und 20 Titel dieser Seite auswählen.")
            if len(set(raw)) != len(raw):
                raise ValidationError("Ein Treffer wurde mehrfach ausgewählt.")
            try:
                ids = [int(value) for value in raw]
            except (TypeError, ValueError):
                raise ValidationError("Ungültige Kandidatenauswahl.")
            by_id = {
                candidate.pk: group.pk
                for group in valid_groups.values()
                for candidate in group.candidates.all()
            }
            if any(cid not in by_id for cid in ids):
                raise ValidationError("Mindestens ein Treffer gehört nicht zu dieser Seite.")
            if len({by_id[cid] for cid in ids}) != len(ids):
                raise ValidationError("Bitte nur einen Treffer je Titelgruppe wählen.")
            if (MetadataCandidate.objects.filter(pk__in=ids, manually_verified=True).exists()
                    and request.POST.get('confirm_manual') != 'on'):
                raise ValidationError('Manuelle IMDb-Zuordnungen müssen ausdrücklich bestätigt werden.')
            from .matching import confirm_candidates
            approved = confirm_candidates(ids)
        except ValidationError as exc:
            for error in exc.messages:
                messages.error(request, error)
        else:
            messages.success(request, f"{len(approved)} Titel erfolgreich bestätigt.")
            target = request.path
            if request.GET:
                target += "?" + request.GET.urlencode()
            return redirect(target)

    return render(request, "catalog/review_matches.html", {
        "page_obj": page,
        "query": phrase,
        "root": root,
        "deferred": deferred,
        "status": status,
        "roots": MediaGroup._meta.get_field("root").choices,
    })


@login_required
def my_requests(request):
    """Private request history; never disclose other users' requests."""
    state = request.GET.get("state", "")
    if state not in Media.State.values:
        state = ""
    mine = MediaRequest.objects.filter(user=request.user, media__kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES)).select_related("media")
    counts = {
        "total": mine.count(),
        "requested": mine.filter(media__state=Media.State.REQUESTED).count(),
        "available": mine.filter(media__state=Media.State.AVAILABLE).count(),
    }
    if state:
        mine = mine.filter(media__state=state)
    page = Paginator(mine.order_by("-created_at", "-pk"), 20).get_page(request.GET.get("page"))
    return render(request, "catalog/my_requests.html", {
        "page_obj": page, "state": state, "state_choices": Media.State.choices,
        "counts": counts,
    })



@login_required
@require_http_methods(["GET", "POST"])
def delete_my_request(request, pk):
    """Allow users to delete only their own wish, after a confirmation page."""
    wish = get_object_or_404(
        MediaRequest.objects.select_related("media"),
        pk=pk, user=request.user,
        media__kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES),
    )
    if request.method == "POST":
        from .request_cleanup import remove_requests_and_unused_media

        title = wish.media.title
        remove_requests_and_unused_media([wish.pk])
        messages.success(request, f"Dein Wunsch für „{title}“ wurde gelöscht.")
        return redirect("my_requests")
    return render(request, "catalog/delete_my_request.html", {"wish": wish})


@login_required
@require_http_methods(["GET", "POST"])
def review_group(request, pk):
    """Review one group: search, exact ID verification, or defer. Superuser only."""
    if not request.user.is_active or not request.user.is_superuser:
        return HttpResponseForbidden("Zugriff nur für Administratoren.")
    group = get_object_or_404(MediaGroup, pk=pk, root__in=("movies", "series"))
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "defer":
            group.review_deferred = True
            group.save(update_fields=["review_deferred"])
            messages.info(request, "Titelgruppe zurückgestellt. Du findest sie unter Zurückgestellt.")
            return redirect("review_matches")
        if action == "restore":
            group.review_deferred = False
            group.save(update_fields=["review_deferred"])
            messages.success(request, "Titelgruppe wieder in der Freigabeliste.")
            return redirect("review_group", pk=group.pk)
        try:
            if action == "search":
                phrase = request.POST.get("phrase", "").strip()
                if not 2 <= len(phrase) <= 100:
                    raise ValidationError("Suchbegriff muss zwischen 2 und 100 Zeichen haben.")
                providers.search_quota(request.user)
                hits = providers.search(group.kind, phrase)
            elif action == "manual":
                from .matching import validate_identity
                identifier = request.POST.get("identifier", "").strip()
                title = request.POST.get("title", "").strip()
                year_text = request.POST.get("year", "").strip()
                if group.kind != Media.Kind.MOVIE or not validate_identity("movie", "imdb", identifier):
                    raise ValidationError("Manuelle Zuordnung ist nur für gültige Film-IMDb-IDs erlaubt.")
                if not 2 <= len(title) <= 300:
                    raise ValidationError("Bitte einen Filmtitel mit 2 bis 300 Zeichen eingeben.")
                if year_text and (len(year_text) != 4 or not year_text.isascii() or not year_text.isdecimal() or not 1888 <= int(year_text) <= 2100):
                    raise ValidationError("Das Erscheinungsjahr ist ungültig.")
                if request.POST.get("checked_imdb") != "on":
                    raise ValidationError("Bitte zuerst den Titel auf IMDb vergleichen und die Prüfung bestätigen.")
                obj, created = MetadataCandidate.objects.get_or_create(
                    group=group, kind="movie", external_provider="imdb",
                    external_id=identifier,
                    defaults={"title": title, "year": int(year_text) if year_text else None,
                              "manually_verified": True},
                )
                if not created and not obj.manually_verified:
                    messages.info(request, "Für diese IMDb-ID gibt es bereits einen automatisch gefundenen Treffer.")
                else:
                    messages.success(request, "Manuelle IMDb-Zuordnung gespeichert. Bitte unter Freigaben ausdrücklich bestätigen.")
                group.review_deferred = False
                group.save(update_fields=["review_deferred"])
                return redirect("review_group", pk=group.pk)
            elif action == "lookup":
                identifier = request.POST.get("identifier", "").strip()
                if len(identifier) > 80:
                    raise ValidationError("Kennung ist zu lang.")
                providers.search_quota(request.user)
                hits = [providers.lookup_identity(group.kind, identifier)]
            else:
                raise ValidationError("Unbekannte Aktion.")
            for hit in hits:
                MetadataCandidate.objects.update_or_create(
                    group=group, kind=hit["kind"],
                    external_provider=hit["external_provider"], external_id=hit["external_id"],
                    defaults={"title": hit["title"], "year": hit.get("year"), "manually_verified": False},
                )
            if hits:
                group.review_deferred = False
                group.save(update_fields=["review_deferred"])
                messages.success(request, f"{len(hits)} Treffer ergänzt. Bitte passende Zuordnung prüfen.")
            else:
                messages.warning(request, "Keine Treffer gefunden. Anderen Titel oder Kennung versuchen.")
        except (ValidationError, providers.ProviderError) as exc:
            messages.error(request, "; ".join(exc.messages) if isinstance(exc, ValidationError) else str(exc))
        return redirect("review_group", pk=group.pk)
    return render(request, "catalog/review_group.html", {
        "group": group,
        "candidates": group.candidates.order_by("title", "pk"),
        "linked": group.files.filter(media__isnull=False).exists(),
    })


def cover_image(request, pk):
    """Serve only locally cached WebP files; never proxy user-supplied URLs."""
    path = covers.cover_path(pk)
    if not covers.available(pk) or not Media.objects.filter(pk=pk, kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES)).exists():
        raise Http404("Kein Cover vorhanden.")
    response = FileResponse(path.open("rb"), content_type="image/webp")
    response["Cache-Control"] = "public, max-age=86400"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def credits(request):
    """Required third-party artwork attribution; no external browser image loads."""
    return render(request, "catalog/credits.html")
