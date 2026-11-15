"""Validation and explicit administrator-only confirmation of metadata candidates."""
import re

from django.core.exceptions import ValidationError
from django.db import transaction

from catalog.models import Media, MetadataCandidate, ScannedFile

IMDB_RE = re.compile(r"^tt[0-9]{7,12}$")


def validate_identity(kind, provider, external_id):
    if kind in (Media.Kind.MOVIE, Media.Kind.SERIES):
        return provider == "imdb" and bool(IMDB_RE.fullmatch(external_id))
    return False


def confirm_candidate(candidate_id):
    """Explicitly confirm a server-fetched candidate. Caller enforces admin permission.

    Existing requests are preserved and resolved by their verified identity.
    Never overwrite a file already linked to a different verified media record.
    """
    with transaction.atomic():
        candidate = MetadataCandidate.objects.select_related("group").select_for_update().get(pk=candidate_id)
        group = candidate.group
        if candidate.kind != group.kind or not validate_identity(
            candidate.kind, candidate.external_provider, candidate.external_id
        ):
            raise ValidationError("Ungültige oder unpassende externe Kennung.")
        files = ScannedFile.objects.select_for_update().filter(group=group)
        if not files.exists():
            raise ValidationError("Diese Titelgruppe enthält keine Dateien.")
        if files.exclude(media__isnull=True).exclude(
            media__kind=candidate.kind,
            media__external_provider=candidate.external_provider,
            media__external_id=candidate.external_id,
        ).exists():
            raise ValidationError("Mindestens eine Datei wurde bereits einem anderen Titel zugeordnet.")
        media, created = Media.objects.get_or_create(
            kind=candidate.kind,
            external_provider=candidate.external_provider,
            external_id=candidate.external_id,
            defaults={
                "title": candidate.title,
                "year": candidate.year,
                "state": Media.State.AVAILABLE,
            },
        )
        if not created and media.state != Media.State.AVAILABLE:
            media.state = Media.State.AVAILABLE
            media.save(update_fields=["state"])
        files.update(media=media)
        return media, created


def confirm_candidates(candidate_ids):
    """Approve an explicit batch atomically, rejecting conflicting group choices.

    A bulk action must never choose between different identities for the same
    directory group. All selected candidates succeed together or none do.
    """
    ids = list(candidate_ids)
    if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
        raise ValidationError("Bitte 1 bis 100 unterschiedliche Treffer auswählen.")
    with transaction.atomic():
        candidates = list(
            MetadataCandidate.objects.select_for_update()
            .filter(pk__in=ids).order_by("group_id", "pk")
        )
        if len(candidates) != len(ids):
            raise ValidationError("Mindestens ein ausgewählter Treffer existiert nicht mehr.")
        groups = [candidate.group_id for candidate in candidates]
        if len(groups) != len(set(groups)):
            raise ValidationError(
                "Mehrere Treffer derselben Titelgruppe ausgewählt. "
                "Bitte pro Titelgruppe genau einen passenden Treffer wählen."
            )
        approved = []
        for candidate in candidates:
            media, _ = confirm_candidate(candidate.pk)
            approved.append(media)
        return approved
