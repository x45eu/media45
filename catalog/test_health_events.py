"""Error-marker tests: explicit exceptions, privacy, recovery, no migrations."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from catalog.health_events import (SCAN_FAILURE, MATCH_FAILURE, DETAILS_FAILURE,
                                   record_failure, clear_failure)
from catalog.models import ProviderThrottle
from catalog.management.commands.auto_media import Command as MediaCommand, STATE_KEY as SCAN_KEY
from catalog.management.commands.auto_details import Command as DetailsCommand, STATE_KEY as DETAILS_KEY
from catalog.status_views import assess_freshness, assess_failure


class HealthMarkerTests(TestCase):
    def test_only_known_keys_and_no_details_persist(self):
        with self.assertRaises(ValueError):
            record_failure("secret=/mnt/private/file")
        record_failure(SCAN_FAILURE)
        record = ProviderThrottle.objects.get(provider=SCAN_FAILURE)
        self.assertIsNotNone(record.last_call)
        self.assertEqual(record.provider, SCAN_FAILURE)
        clear_failure(SCAN_FAILURE)
        self.assertFalse(ProviderThrottle.objects.filter(provider=SCAN_FAILURE).exists())

    def test_failure_overrides_green(self):
        now = timezone.now()
        fresh = assess_freshness(now, now)
        self.assertEqual(assess_failure(fresh, now - timedelta(minutes=3), now)["level"], "error")

    def test_future_failure_timestamp_not_reported_as_real_error(self):
        now = timezone.now()
        self.assertEqual(assess_failure(assess_freshness(now, now), now + timedelta(minutes=2), now)["level"], "unknown")

    @patch("catalog.management.commands.auto_media.call_command")
    def test_scan_exception_records_failure_then_success_clears(self, cmd):
        scheduler = MediaCommand()
        cmd.side_effect = RuntimeError("private path /mnt/media/filme")
        with self.assertRaises(RuntimeError):
            scheduler._cycle(10)
        self.assertTrue(ProviderThrottle.objects.filter(provider=SCAN_FAILURE).exists())
        cmd.side_effect = None
        scheduler._cycle(10)
        self.assertFalse(ProviderThrottle.objects.filter(provider=SCAN_FAILURE).exists())

    @patch("catalog.management.commands.auto_media.call_command")
    def test_match_exception_uses_separate_key(self, cmd):
        ProviderThrottle.objects.create(provider=SCAN_KEY, last_call=timezone.now())
        cmd.side_effect = RuntimeError("sensitive message")
        with self.assertRaises(RuntimeError):
            MediaCommand()._cycle(10)
        self.assertTrue(ProviderThrottle.objects.filter(provider=MATCH_FAILURE).exists())
        self.assertFalse(ProviderThrottle.objects.filter(provider=SCAN_FAILURE).exists())

    @override_settings(TMDB_READ_ACCESS_TOKEN="dummy-token")
    @patch("catalog.management.commands.auto_details.call_command")
    def test_tmdb_exception_recorded_and_cleared(self, cmd):
        cmd.side_effect = RuntimeError("token=not-to-store")
        with self.assertRaises(RuntimeError):
            DetailsCommand()._run_if_due(1, timedelta(hours=24))
        self.assertTrue(ProviderThrottle.objects.filter(provider=DETAILS_FAILURE).exists())
        cmd.side_effect = None
        DetailsCommand()._run_if_due(1, timedelta(hours=24))
        self.assertFalse(ProviderThrottle.objects.filter(provider=DETAILS_FAILURE).exists())

    @override_settings(TMDB_READ_ACCESS_TOKEN="")
    def test_missing_token_records_failure_without_leaking_value(self):
        DetailsCommand()._run_if_due(1, timedelta(hours=24))
        self.assertTrue(ProviderThrottle.objects.filter(provider=DETAILS_FAILURE).exists())
        self.assertFalse(ProviderThrottle.objects.filter(provider=DETAILS_KEY).exists())
