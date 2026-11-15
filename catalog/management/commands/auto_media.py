"""Periodic read-only inventory and limited metadata discovery.

Start only after an existing manual match_media --all run has finished.
Uses a dedicated scanner container, never web/worker media mounts.
"""
import logging
import time
from datetime import timedelta

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

from catalog.models import ProviderThrottle

log = logging.getLogger(__name__)
LOCK_ID = 45142026
STATE_KEY = "media45_automated_scan"
SCAN_INTERVAL = timedelta(hours=24)
MATCH_INTERVAL_SECONDS = 15 * 60


class Command(BaseCommand):
    help = "Startet täglich den NAS-Dateiscan und regelmäßig kleine Metadatenpakete."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--match-limit", type=int, default=10)
        parser.add_argument("--poll-seconds", type=int, default=900)

    def handle(self, *args, **opts):
        if not 1 <= opts["match_limit"] <= 20:
            raise CommandError("--match-limit muss zwischen 1 und 20 liegen.")
        if not 300 <= opts["poll_seconds"] <= 86400:
            raise CommandError("--poll-seconds muss zwischen 300 und 86400 liegen.")

        # PostgreSQL advisory lock: two scheduler containers must not run the
        # inventory/grouping pipeline concurrently. Keep the session open.
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            acquired = cursor.fetchone()[0]
        if not acquired:
            raise CommandError("Ein anderer media45-Scheduler läuft bereits.")

        try:
            while True:
                self._cycle(opts["match_limit"])
                if not opts["loop"]:
                    break
                time.sleep(opts["poll_seconds"])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])

    def _cycle(self, limit):
        now = timezone.now()
        record, _ = ProviderThrottle.objects.get_or_create(
            provider=STATE_KEY,
            defaults={"last_call": now - SCAN_INTERVAL - timedelta(seconds=1)},
        )
        if now - record.last_call >= SCAN_INTERVAL:
            # Mark the schedule only after scan AND grouping succeed.
            # Missing NAS mounts cause scan_media to fail and are never
            # treated as empty libraries.
            self.stdout.write("media45: NAS-Inventur wird gestartet.")
            call_command("scan_media", "--root", "all", stdout=self.stdout, stderr=self.stderr)
            call_command("group_media", stdout=self.stdout, stderr=self.stderr)
            record.last_call = timezone.now()
            record.save(update_fields=["last_call"])
        # Existing metadata confirmations are never touched.
        # match_media already remembers successful and zero-result searches.
        self.stdout.write(f"media45: Metadatenpaket ({limit} Gruppen) wird geprüft.")
        call_command("match_media", limit=limit, stdout=self.stdout, stderr=self.stderr)
