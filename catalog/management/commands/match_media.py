"""Resumable, rate-limited metadata candidate discovery for film and series libraries."""
import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Exists, OuterRef
from django.utils import timezone

from catalog.models import MediaGroup, MetadataCandidate
from catalog.providers import ProviderError, search


class Command(BaseCommand):
    help = "Fragt TMDB kontrolliert ab; Bestätigung bleibt manuell."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=3)
        parser.add_argument("--all", action="store_true", help="Alle noch nicht abgefragten Gruppen bearbeiten")
        parser.add_argument("--group-id", type=int)
        parser.add_argument("--root", choices=("movies", "series"))
        parser.add_argument("--refresh", action="store_true", help="Bereits geprüfte Gruppen erneut abfragen")
        parser.add_argument("--retry-empty", action="store_true", help="Nur bereits geprüfte Gruppen ohne Kandidaten erneut prüfen")

    def handle(self, *args, **options):
        limit = options["limit"]
        if options["all"] and options["group_id"]:
            raise CommandError("--all und --group-id dürfen nicht kombiniert werden.")
        if options["all"] and options["refresh"]:
            raise CommandError("--all und --refresh nicht kombinieren: erneute Abfragen bitte gezielt starten.")
        if options["retry_empty"] and options["refresh"]:
            raise CommandError("--retry-empty und --refresh nicht kombinieren.")
        if not 1 <= limit <= 20:
            raise CommandError("--limit muss zwischen 1 und 20 liegen.")
        already = MetadataCandidate.objects.filter(group_id=OuterRef("pk"))
        from catalog.models import ScannedFile
        linked = ScannedFile.objects.filter(group_id=OuterRef("pk"), media__isnull=False)
        qs = MediaGroup.objects.filter(root__in=("movies", "series"), file_count__gt=0).annotate(
            has_candidates=Exists(already), has_linked=Exists(linked),
        ).filter(has_linked=False, review_deferred=False)
        if options["group_id"]:
            qs = qs.filter(pk=options["group_id"])
            if not qs.exists():
                raise CommandError("Titelgruppe nicht gefunden.")
        elif options["retry_empty"]:
            qs = qs.filter(metadata_checked_at__isnull=False, has_candidates=False)
        elif not options["refresh"]:
            # Empty searches are remembered; retry them explicitly with --retry-empty.
            qs = qs.filter(metadata_checked_at__isnull=True, has_candidates=False)
        if options["root"]:
            qs = qs.filter(root=options["root"])
        total = qs.count() if options["all"] else min(qs.count(), limit)
        groups = qs.order_by("pk") if options["all"] else qs.order_by("pk")[:limit]
        self.stdout.write(f"{total} Titelgruppen für Metadatenabgleich ausgewählt.")
        ok = 0
        found = 0
        empty = 0
        last_call = None
        for group in groups.iterator(chunk_size=50):
            # All providers are public services; avoid hammering them and make
            # the job resumable by persisting each successful group's status.
            if last_call is not None:
                time.sleep(max(0, 1.6 - (time.monotonic() - last_call)))
            phrase = group.suggested_title
            # A title from the inventory is only a search hint; never auto-approve.
            last_call = time.monotonic()
            try:
                candidates = search(group.kind, phrase[:100])
            except ProviderError as exc:
                self.stderr.write(f"Gruppe {group.pk} ({group.suggested_title}): {exc}")
                self.stderr.write(self.style.WARNING(
                    "Abgebrochen, um den Metadatendienst zu schonen. "
                    "Erfolgreiche Gruppen bleiben gespeichert; Befehl später erneut starten."
                ))
                break
            for hit in candidates:
                if hit["kind"] != group.kind:
                    continue
                _, created = MetadataCandidate.objects.update_or_create(
                    group=group, kind=hit["kind"],
                    external_provider=hit["external_provider"], external_id=hit["external_id"],
                    defaults={"title": hit["title"], "year": hit.get("year")},
                )
                found += int(created)
            group.metadata_checked_at = timezone.now()
            group.metadata_result_count = sum(1 for hit in candidates if hit["kind"] == group.kind)
            group.save(update_fields=["metadata_checked_at", "metadata_result_count"])
            ok += 1
            empty += not bool(group.metadata_result_count)
            self.stdout.write(
                f"[{ok}/{total}] Gruppe {group.pk}: {group.metadata_result_count} Treffer "
                f"({group.suggested_title})"
            )
        self.stdout.write(self.style.SUCCESS(
            f"{ok}/{total} Gruppen bearbeitet, {found} neue Vorschläge, "
            f"{empty} ohne Treffer. Keine automatische Freigabe."
        ))
