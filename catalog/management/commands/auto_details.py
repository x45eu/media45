"""Optional daily refresh of local TMDB metadata without NAS access.

The operator enables this command in a separate, NAS-free Compose service.
No updates are made to verified identities, media files, or cover images.
"""
import time
from datetime import timedelta

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from catalog.models import ProviderThrottle
from catalog.health_events import DETAILS_FAILURE, record_failure, clear_failure


LOCK_ID = 45142027
STATE_KEY = "media45_tmdb_details_refresh"


class Command(BaseCommand):
    help = "Prueft taeglich hoechstens ein kleines Paket fehlender oder abgelaufener TMDB-Details."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Dauerbetrieb fuer Docker Compose")
        parser.add_argument("--limit", type=int, default=30, help="Maximale Titel pro Durchlauf (1-50)")
        parser.add_argument("--poll-seconds", type=int, default=3600, help="Pruefintervall im Dauerbetrieb")
        parser.add_argument("--interval-hours", type=int, default=24, help="Minimaler Abstand zwischen Abfragen")

    def handle(self, *args, **options):
        limit = options["limit"]
        poll = options["poll_seconds"]
        hours = options["interval_hours"]
        if not 1 <= limit <= 50:
            raise CommandError("--limit muss zwischen 1 und 50 liegen.")
        if not 300 <= poll <= 86400:
            raise CommandError("--poll-seconds muss zwischen 300 und 86400 liegen.")
        if not 1 <= hours <= 168:
            raise CommandError("--interval-hours muss zwischen 1 und 168 liegen.")

        while True:
            self._run_if_due(limit, timedelta(hours=hours))
            if not options["loop"]:
                return
            time.sleep(poll)

    def _run_if_due(self, limit, interval):
        """Use a PostgreSQL advisory lock and a persistent last-run timestamp."""
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            locked = cursor.fetchone()[0]
        if not locked:
            self.stdout.write("TMDB: Ein anderer Metadaten-Durchlauf ist bereits aktiv.")
            return False
        try:
            if not (getattr(settings, "TMDB_READ_ACCESS_TOKEN", "") or "").strip():
                record_failure(DETAILS_FAILURE)
                self.stderr.write("TMDB: Kein Token konfiguriert; Aktualisierung uebersprungen.")
                return False
            now = timezone.now()
            record, _ = ProviderThrottle.objects.get_or_create(
                provider=STATE_KEY,
                defaults={"last_call": now - interval - timedelta(seconds=1)},
            )
            if now - record.last_call < interval:
                self.stdout.write("TMDB: Noch nicht faellig; keine API-Abfragen.")
                return False
            # fetch_details itself skips valid, identity-bound caches, limits
            # requests and never reads NAS paths or downloads cover images.
            self.stdout.write(f"TMDB: Starte lokalen Details-Abgleich (max. {limit} Titel).")
            try:
                call_command("fetch_details", limit=limit, stdout=self.stdout, stderr=self.stderr)
            except Exception:
                record_failure(DETAILS_FAILURE)
                raise
            record.last_call = timezone.now()
            record.save(update_fields=["last_call"])
            clear_failure(DETAILS_FAILURE)
            return True
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
