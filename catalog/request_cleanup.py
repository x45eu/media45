"""Careful removal of orphaned request-only media entries.

Never remove approved or in-progress catalog titles, linked NAS inventory,
or a media item that still has another user's request.
"""
from django.db import transaction

from .models import Media, MediaRequest, Notification


def remove_requests_and_unused_media(request_ids):
    """Delete selected requests and only safe, now-unused requested media.

    Admin single- and bulk-delete share this transaction and safety check.
    """
    ids = sorted({int(pk) for pk in request_ids})
    if not ids:
        return 0, 0

    with transaction.atomic():
        # Serialize against new wishes for the same catalog entries.
        media_ids = list(MediaRequest.objects.filter(pk__in=ids)
                         .values_list("media_id", flat=True).distinct())
        list(Media.objects.select_for_update().filter(pk__in=media_ids).order_by("pk"))
        requests = list(MediaRequest.objects.filter(pk__in=ids).values_list("pk", flat=True))
        if not requests:
            return 0, 0
        Notification.objects.filter(target_path__in=[
            f"/admin/catalog/mediarequest/{pk}/change/" for pk in requests
        ]).delete()
        deleted_count, _ = MediaRequest.objects.filter(pk__in=requests).delete()
        # Only remove purely wish-created, unapproved/unlinked leftovers.
        candidates = (Media.objects.filter(pk__in=media_ids, state=Media.State.REQUESTED,
                        requests__isnull=True, scanned_files__isnull=True).distinct())
        removed_count = candidates.count()
        candidates.delete()
        return len(requests), removed_count
