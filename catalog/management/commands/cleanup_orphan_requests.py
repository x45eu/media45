"""Review/remove orphaned request-only catalog entries.

No NAS access. Dry run is the default; --apply is deliberate.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.models import Media


class Command(BaseCommand):
    help = "Zeigt ungenutzte angefragte Titel an; --apply entfernt sie aus dem Katalog."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Gefundene Einträge tatsächlich entfernen")
        parser.add_argument("--media-id", type=int, help="Optional: nur diesen Katalogeintrag prüfen")

    def handle(self, *args, **options):
        with transaction.atomic():
            candidates = (Media.objects.select_for_update(of=("self",))
                          .filter(kind__in=(Media.Kind.MOVIE, Media.Kind.SERIES),
                                  state=Media.State.REQUESTED,
                                  requests__isnull=True,
                                  scanned_files__isnull=True)
                          .order_by("pk"))
            if options["media_id"] is not None:
                candidates = candidates.filter(pk=options["media_id"])
            records = list(candidates)
            for item in records:
                self.stdout.write(f"{item.pk}: {item.title} ({item.get_kind_display()}) "
                                  f"IMDb {item.external_id}")
            if not records:
                self.stdout.write("Keine verwaisten Anfrage-Titel gefunden.")
                return
            if not options["apply"]:
                self.stdout.write(f"{len(records)} verwaiste Anfrage-Titel gefunden. "
                                  "Nur Vorschau, nichts gelöscht. Zum Löschen --apply angeben.")
                return
            # Revalidate under lock before deleting. No approved or NAS-linked rows.
            ids = [item.pk for item in records]
            safe = Media.objects.filter(pk__in=ids,
                                        state=Media.State.REQUESTED,
                                        requests__isnull=True,
                                        scanned_files__isnull=True).distinct()
            count = safe.count()
            safe.delete()
            self.stdout.write(f"{count} verwaiste Anfrage-Titel entfernt.")
