"""Create reviewable title groups from existing scanner inventory.

Runs against PostgreSQL; does not access media files or query public services.
"""
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from catalog.grouping import group_info
from catalog.models import MediaGroup, ScannedFile


class Command(BaseCommand):
    help = "Gruppiert inventarisierte Dateien ohne externe Metadatenabfragen."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        buckets = defaultdict(list)
        titles = {}
        for item in ScannedFile.objects.filter(root__in=("movies", "series")).only("id", "root", "relative_path").iterator(chunk_size=1000):
            key, kind, title = group_info(item.root, item.relative_path)
            identity = (item.root, key)
            buckets[identity].append(item.pk)
            titles.setdefault(identity, (kind, title))
        self.stdout.write(
            f"{len(buckets)} Titelgruppen aus {sum(map(len, buckets.values()))} Dateien ermittelt."
        )
        if options["dry_run"]:
            self.stdout.write("Dry-Run: keine Datenbankänderungen.")
            return
        with transaction.atomic():
            # Keep existing group IDs and their candidate suggestions on regrouping.
            # Existing verified Media / MediaRequest rows remain untouched.
            active_ids = []
            for (root, key), file_ids in buckets.items():
                kind, title = titles[(root, key)]
                group, _ = MediaGroup.objects.update_or_create(
                    root=root, group_key=key,
                    defaults={"kind": kind, "suggested_title": title, "file_count": len(file_ids)},
                )
                active_ids.append(group.pk)
                ScannedFile.objects.filter(pk__in=file_ids).update(group=group)
            stale = MediaGroup.objects.exclude(pk__in=active_ids)
            ScannedFile.objects.filter(group__in=stale).update(group=None)
            stale.delete()
        self.stdout.write(self.style.SUCCESS(
            f"{len(buckets)} Titelgruppen gespeichert. Bitte im Django-Admin prüfen."
        ))
