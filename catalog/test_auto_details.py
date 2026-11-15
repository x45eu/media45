"""Tests for the optional, isolated local metadata scheduler."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from catalog.management.commands.auto_details import STATE_KEY
from catalog.models import ProviderThrottle


@override_settings(TMDB_READ_ACCESS_TOKEN="local-test-token")
class AutoDetailsTests(TestCase):
    @patch("catalog.management.commands.auto_details.call_command")
    def test_first_run_refreshes_once_and_remembers_timestamp(self, refresh):
        call_command("auto_details", limit=12, stdout=StringIO())
        refresh.assert_called_once()
        self.assertEqual(refresh.call_args.args, ("fetch_details",))
        self.assertEqual(refresh.call_args.kwargs["limit"], 12)
        self.assertTrue(ProviderThrottle.objects.filter(provider=STATE_KEY).exists())
        call_command("auto_details", limit=12, stdout=StringIO())
        refresh.assert_called_once()

    @patch("catalog.management.commands.auto_details.call_command")
    def test_expired_schedule_runs_again(self, refresh):
        ProviderThrottle.objects.create(
            provider=STATE_KEY, last_call=timezone.now() - timedelta(hours=25),
        )
        call_command("auto_details", stdout=StringIO())
        refresh.assert_called_once()

    @override_settings(TMDB_READ_ACCESS_TOKEN="")
    @patch("catalog.management.commands.auto_details.call_command")
    def test_no_token_never_calls_provider_or_records_success(self, refresh):
        call_command("auto_details", stdout=StringIO(), stderr=StringIO())
        refresh.assert_not_called()
        self.assertFalse(ProviderThrottle.objects.filter(provider=STATE_KEY).exists())

    def test_rejects_unbounded_requests(self):
        with self.assertRaises(CommandError):
            call_command("auto_details", limit=999, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("auto_details", interval_hours=0, stdout=StringIO())
