"""Explicit, resumable TMDB metadata batches; never touches NAS files."""
import time

from django.core.management.base import BaseCommand, CommandError

from catalog.models import Media, MediaDetails
from catalog.tmdb_details import DetailsUnavailable, current, fetch


class Command(BaseCommand):
    help = "Lädt deutsche TMDB-Detailinformationen für bestätigte IMDb-Katalogtitel."

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true", help="Nur lokalen Datenbestand zählen (offline)")
        parser.add_argument("--limit", type=int, default=12)
        parser.add_argument("--all", action="store_true", help="Alle fehlenden oder abgelaufenen Einträge bearbeiten")
        parser.add_argument("--refresh", action="store_true", help="Auch aktuelle Cache-Einträge neu laden")
        parser.add_argument("--media-id", type=int)
        parser.add_argument("--start-id", type=int, default=1)

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 500:
            raise CommandError("--limit muss zwischen 1 und 500 liegen.")
        if options["start_id"] < 1 or (options["media_id"] is not None and options["media_id"] < 1):
            raise CommandError("Die Medien-ID muss mindestens 1 sein.")
        items = (Media.objects.filter(
            kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES),
            pk__gte=options["start_id"],
        ).select_related("details").order_by("pk"))
        if options["media_id"] is not None:
            items = items.filter(pk=options["media_id"])

        if options["status"]:
            total = ready = 0
            for item in items.iterator(chunk_size=100):
                total += 1
                ready += int(current(item) is not None)
            self.stdout.write(
                f"TMDB-Details: {ready}/{total} aktuell, {total - ready} fehlen oder sind abgelaufen."
            )
            return

        done = missing = skipped = selected = 0
        for item in items.iterator(chunk_size=100):
            if not options["refresh"] and current(item) is not None:
                skipped += 1
                continue
            if not options["all"] and selected >= limit:
                break
            selected += 1
            try:
                fetch(item)
                done += 1
                self.stdout.write(f"Details gespeichert: {item.pk} – {item.title}")
            except DetailsUnavailable as exc:
                missing += 1
                self.stderr.write(f"Keine Details: {item.pk} – {item.title}: {exc}")
                if ("TMDB_READ_ACCESS_TOKEN fehlt" in str(exc)
                        or "TMDB-Token ungültig" in str(exc)
                        or "TMDB begrenzt" in str(exc)):
                    self.stderr.write("TMDB-Zugang prüfen oder später erneut versuchen.")
                    break
            time.sleep(1.1)
        self.stdout.write(f"Fertig: {done} aktualisiert, {missing} ohne Details, {skipped} bereits aktuell.")
