"""Privacy and access-control tests for the local status dashboard."""
from datetime import timedelta
from tempfile import TemporaryDirectory
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.management.commands.auto_details import STATE_KEY as DETAILS_KEY
from catalog.models import Media, MediaDetails, ProviderThrottle, TotpDevice


class SystemStatusTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.admin = users.objects.create_superuser(username="status-admin", email="admin@example.invalid", password="test-admin-password-long-enough")
        self.regular = users.objects.create_user(username="status-user", password="test-user-password-long-enough")
        TotpDevice.objects.create(user=self.admin, secret="JBSWY3DPEHPK3PXP")
        self.media = Media.objects.create(kind="movie", title="Example film", external_provider="imdb", external_id="tt1234567", state="available")

    def test_anonymous_redirect(self):
        self.assertEqual(self.client.get(reverse("system_status")).status_code, 302)

    def test_regular_user_forbidden(self):
        self.client.force_login(self.regular)
        self.assertEqual(self.client.get(reverse("system_status")).status_code, 403)

    def test_mfa_required_for_superuser(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("system_status")).status_code, 302)

    def test_verified_admin_sees_only_aggregate_status(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session["media45_mfa_verified_user"] = self.admin.pk
        session.save()
        with TemporaryDirectory() as cover_dir, override_settings(COVER_DIR=cover_dir):
            Path(cover_dir, f"{self.media.pk}.webp").write_bytes(b"test")
            MediaDetails.objects.create(media=self.media, imdb_id=self.media.external_id, tmdb_id=123, fetched_at=timezone.now(), synopsis="private synopsis")
            ProviderThrottle.objects.create(provider=DETAILS_KEY, last_call=timezone.now() - timedelta(hours=1))
            response = self.client.get(reverse("system_status"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Systemstatus")
        self.assertContains(response, "TMDB-Metadaten")
        self.assertNotContains(response, "private synopsis")
        self.assertNotContains(response, "tt1234567")
        self.assertEqual(response["Cache-Control"], "max-age=0, no-cache, no-store, must-revalidate, private")

    def test_post_rejected(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session["media45_mfa_verified_user"] = self.admin.pk
        session.save()
        self.assertEqual(self.client.post(reverse("system_status")).status_code, 405)


class AdminWorkSummaryTests(TestCase):
    def test_empty_work_queue_is_zero(self):
        from catalog.status_views import admin_work_summary
        summary = admin_work_summary()
        self.assertEqual(set(summary), {
            "groups_with_suggestions", "groups_unsearched", "groups_no_results",
            "groups_deferred", "files_without_media",
        })
        self.assertTrue(all(v == 0 for v in summary.values()))

    def test_counts_active_deferred_and_linked_separately(self):
        from catalog.models import MediaGroup, MetadataCandidate, ScannedFile
        from catalog.status_views import admin_work_summary

        def group(n, **kw):
            return MediaGroup.objects.create(
                root="movies", group_key=f"test-{n}", kind="movie",
                suggested_title=f"Title {n}", file_count=1, **kw,
            )

        suggested = group(1)
        MetadataCandidate.objects.create(
            group=suggested, kind="movie", title="Secret Candidate",
            external_provider="imdb", external_id="tt8765432",
        )
        group(2)
        group(3, metadata_checked_at=timezone.now())
        group(4, review_deferred=True)
        linked_group = group(5)
        ScannedFile.objects.create(
            root="movies", relative_path="secret/file.mkv", media_kind="movie",
            suggested_title="Secret filename", file_size=100, modified_ns=1,
            last_seen_at=timezone.now(), group=linked_group, media=Media.objects.create(
                kind="movie", title="Linked", external_provider="imdb",
                external_id="tt7654321",
            ),
        )
        ScannedFile.objects.create(
            root="movies", relative_path="secret/unlinked.mkv", media_kind="movie",
            suggested_title="Secret file", file_size=100, modified_ns=1,
            last_seen_at=timezone.now(), group=suggested,
        )
        summary = admin_work_summary()
        self.assertEqual(summary, {
            "groups_with_suggestions": 1,
            "groups_unsearched": 1,
            "groups_no_results": 1,
            "groups_deferred": 1,
            "files_without_media": 1,
        })

    def test_admin_page_contains_only_counts(self):
        from catalog.models import MediaGroup, MetadataCandidate, ScannedFile
        admin = get_user_model().objects.create_superuser(
            username="work-admin", email="work@example.invalid",
            password="long-password-for-test",
        )
        TotpDevice.objects.create(user=admin, secret="JBSWY3DPEHPK3PXP")
        group = MediaGroup.objects.create(
            root="movies", group_key="private-work", kind="movie",
            suggested_title="Sensitive Work Title", file_count=1,
        )
        MetadataCandidate.objects.create(
            group=group, kind="movie", title="Sensitive Candidate Title",
            external_provider="imdb", external_id="tt8989898",
        )
        ScannedFile.objects.create(
            root="movies", relative_path="PRIVATE/nas-video.mkv",
            media_kind="movie", suggested_title="Sensitive NAS Title",
            file_size=100, modified_ns=1, last_seen_at=timezone.now(), group=group,
        )
        self.client.force_login(admin)
        session = self.client.session
        session["media45_mfa_verified_user"] = admin.pk
        session.save()
        with TemporaryDirectory() as cover_dir, override_settings(COVER_DIR=cover_dir):
            response = self.client.get(reverse("system_status"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Offene Verwaltungsaufgaben")
        for secret in ("Sensitive Work Title", "Sensitive Candidate Title",
                       "PRIVATE/nas-video.mkv", "tt8989898"):
            self.assertNotContains(response, secret)


class StatusAssessmentTests(TestCase):
    def test_no_last_run_is_unknown(self):
        from catalog.status_views import assess_freshness
        self.assertEqual(assess_freshness(None, timezone.now())["level"], "unknown")

    def test_recent_run_is_not_a_liveness_claim(self):
        from catalog.status_views import assess_freshness
        now = timezone.now()
        self.assertEqual(assess_freshness(now - timedelta(hours=24), now)["level"], "ok")

    def test_overdue_run_warns(self):
        from catalog.status_views import assess_freshness
        now = timezone.now()
        self.assertEqual(assess_freshness(now - timedelta(hours=27), now)["level"], "warning")

    def test_future_timestamp_not_green(self):
        from catalog.status_views import assess_freshness
        now = timezone.now()
        self.assertEqual(assess_freshness(now + timedelta(minutes=1), now)["level"], "unknown")

    def test_empty_catalog_unknown(self):
        from catalog.status_views import assess_completeness
        self.assertEqual(assess_completeness(0, 0)["level"], "unknown")

    def test_partial_catalog_warning(self):
        from catalog.status_views import assess_completeness
        self.assertEqual(assess_completeness(9, 10)["level"], "warning")

    def test_complete_catalog_ok(self):
        from catalog.status_views import assess_completeness
        self.assertEqual(assess_completeness(10, 10)["level"], "ok")
