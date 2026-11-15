"""Manual, bounded artwork batches. Never reads or writes NAS files."""
import time

import requests

from django.core.management.base import BaseCommand

from catalog.covers import CoverUnavailable, acquire, available
from catalog.models import Media


class Command(BaseCommand):
    help = "Cache covers for verified Media objects only. No NAS access."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=12)
        parser.add_argument("--all", action="store_true")
        parser.add_argument("--refresh", action="store_true")
        parser.add_argument("--status", action="store_true", help="Nur lokalen Coverbestand zählen, kein Netzwerkzugriff")
        parser.add_argument("--media-id", type=int)
        parser.add_argument("--start-id", type=int, default=1)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 500:
            self.stderr.write("Limit muss zwischen 1 und 500 liegen.")
            return
        if options["start_id"] < 1:
            self.stderr.write("--start-id muss mindestens 1 sein.")
            return
        items = Media.objects.filter(
            pk__gte=options["start_id"], kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES),
        ).order_by("pk")
        if options["media_id"] is not None:
            items = items.filter(pk=options["media_id"])

        # Status mode neither modifies files nor contacts an API.
        if options["status"]:
            total = present = 0
            for item in items.iterator(chunk_size=100):
                total += 1
                present += int(available(item.pk))
            self.stdout.write(
                f"Coverbestand: {present}/{total} vorhanden, {total - present} fehlen oder sind abgelaufen."
            )
            return

        found = missed = skipped = selected = 0
        invalid_token = False
        # The limit applies to actual download attempts, NOT already-cached
        # entries. This makes small resumable batches useful after initial runs.
        for item in items.iterator(chunk_size=100):
            cached = available(item.pk)
            if cached and not options["refresh"]:
                skipped += 1
                continue
            if not options["all"] and selected >= limit:
                break
            selected += 1
            try:
                acquire(item)
                found += 1
                self.stdout.write(f"Cover gespeichert: {item.pk} – {item.title}")
            except CoverUnavailable as exc:
                if "TMDB_READ_ACCESS_TOKEN fehlt" in str(exc) or "TMDB-Token ungültig" in str(exc):
                    invalid_token = True
                missed += 1
                self.stdout.write(f"Kein Cover: {item.pk} – {item.title}: {exc}")
            except (OSError, requests.RequestException) as exc:
                missed += 1
                self.stderr.write(f"Cover vorübergehend nicht abrufbar: {item.pk}: {type(exc).__name__}")
            if invalid_token:
                self.stderr.write("TMDB-Konfiguration prüfen; weitere Anfragen abgebrochen.")
                break
            time.sleep(1.2)
        self.stdout.write(f"Fertig: {found} geladen, {missed} ohne Cover, {skipped} bereits vorhanden.")
