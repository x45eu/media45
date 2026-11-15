from django.conf import settings
from pathlib import Path
from django.test import SimpleTestCase
from catalog.management.commands.scan_media import guess_title


class ScannerGuessTests(SimpleTestCase):
    def test_movie_folder(self):
        self.assertEqual(guess_title(Path("Interstellar (2014)/movie.mkv"), "movies"), "Interstellar")

    def test_series_folder(self):
        self.assertEqual(guess_title(Path("Breaking Bad/Season 01/S01E01.mkv"), "series"), "Breaking Bad")


from catalog.grouping import group_info


class GroupingTests(SimpleTestCase):
    def test_series_episodes_same_group(self):
        first = group_info("series", "Breaking Bad/Season 1/S01E01.mkv")
        second = group_info("series", "Breaking Bad/Season 2/S02E02.mkv")
        self.assertEqual(first, second)
        self.assertEqual(first[2], "Breaking Bad")

    def test_single_file_movies_differ(self):
        self.assertNotEqual(
            group_info("movies", "Movie One.mkv")[0],
            group_info("movies", "Movie Two.mkv")[0],
        )


from catalog.matching import validate_identity


class MetadataIdentityTests(SimpleTestCase):
    def test_imdb(self):
        self.assertTrue(validate_identity("movie", "imdb", "tt0816692"))
        self.assertFalse(validate_identity("movie", "imdb", "ttfake"))
        self.assertFalse(validate_identity("series", "tmdb", "tt0816692"))



from unittest.mock import patch
from catalog.providers import movie_search, series_search


class AccountFreeProviderTests(SimpleTestCase):
    @patch("catalog.providers.wikidata_get")
    def test_wikidata_reads_imdb_claim(self, request):
        request.side_effect = [
            {"search": [{"id": "Q1"}]},
            {"search": []},
            {"entities": {"Q1": {
                "labels": {"de": {"value": "Testfilm"}},
                "descriptions": {"de": {"value": "Film"}},
                "claims": {
                    "P345": [{"mainsnak": {"datavalue": {"value": "tt0816692"}}}],
                    "P577": [{"mainsnak": {"datavalue": {"value": {"time": "+2014-01-01T00:00:00Z"}}}}],
                },
            }}},
        ]
        hits = movie_search("Testfilm")
        self.assertEqual(hits[0]["external_id"], "tt0816692")
        self.assertEqual(hits[0]["year"], 2014)

    @patch("catalog.providers.get_json")
    def test_tvmaze_skips_missing_imdb(self, request):
        request.return_value = [
            {"show": {"name": "Unbekannt", "externals": {"imdb": None}}},
            {"show": {"name": "Testserie", "premiered": "2012-01-01", "externals": {"imdb": "tt1234567"}}},
        ]
        hits = series_search("Testserie")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["external_id"], "tt1234567")


from django.test import TestCase
from django.core.exceptions import ValidationError
from catalog.matching import confirm_candidates
from catalog.models import Media, MediaGroup, MetadataCandidate, ScannedFile, MediaRequest
from django.utils import timezone


class BulkConfirmationTests(TestCase):
    def make_candidate(self, title, imdb, group_key):
        group = MediaGroup.objects.create(
            root="movies", group_key=group_key, kind="movie",
            suggested_title=title, file_count=1,
        )
        ScannedFile.objects.create(
            root="movies", relative_path=f"{group_key}.mkv",
            group=group, media_kind="movie", suggested_title=title,
            file_size=1, modified_ns=1, last_seen_at=timezone.now(),
        )
        return MetadataCandidate.objects.create(
            group=group, kind="movie", title=title,
            external_provider="imdb", external_id=imdb,
        )

    def test_multiple_distinct_groups(self):
        first = self.make_candidate("Test A", "tt1234567", "a")
        second = self.make_candidate("Test B", "tt1234568", "b")
        result = confirm_candidates([first.pk, second.pk])
        self.assertEqual(len(result), 2)
        self.assertEqual(Media.objects.filter(state=Media.State.AVAILABLE).count(), 2)

    def test_conflicting_choices_rejected_without_changes(self):
        first = self.make_candidate("Test A", "tt1234567", "a")
        second = MetadataCandidate.objects.create(
            group=first.group, kind="movie", title="Test B",
            external_provider="imdb", external_id="tt1234568",
        )
        with self.assertRaises(ValidationError):
            confirm_candidates([first.pk, second.pk])
        self.assertEqual(Media.objects.count(), 0)

    def test_rollback_if_later_candidate_invalid(self):
        first = self.make_candidate("Test A", "tt1234567", "a")
        invalid = self.make_candidate("Test B", "not-an-imdb-id", "b")
        with self.assertRaises(ValidationError):
            confirm_candidates([first.pk, invalid.pk])
        self.assertEqual(Media.objects.count(), 0)
        self.assertEqual(ScannedFile.objects.filter(media__isnull=False).count(), 0)


from unittest.mock import patch
from django.core.management import call_command
from io import StringIO
from catalog.models import MediaGroup, MetadataCandidate


class BulkMatchingCommandTests(TestCase):
    @patch("catalog.management.commands.match_media.time.sleep")
    @patch("catalog.management.commands.match_media.search")
    def test_full_scan_remembers_empty_results_and_resumes(self, provider, sleep):
        MediaGroup.objects.create(
            root="movies", group_key="movie-1", kind="movie",
            suggested_title="Film eins", file_count=1,
        )
        MediaGroup.objects.create(
            root="movies", group_key="movie-2", kind="movie",
            suggested_title="Film zwei", file_count=1,
        )
        provider.side_effect = [
            [{"kind": "movie", "title": "Film eins", "year": 2020,
              "external_provider": "imdb", "external_id": "tt1234567"}],
            [],
        ]
        call_command("match_media", "--all", stdout=StringIO())
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(MetadataCandidate.objects.count(), 1)
        self.assertEqual(MediaGroup.objects.filter(metadata_checked_at__isnull=False).count(), 2)
        call_command("match_media", "--all", stdout=StringIO())
        self.assertEqual(provider.call_count, 2)

from datetime import timedelta



from unittest.mock import patch as mock_patch
from django.core.management.base import CommandError
from catalog.management.commands.auto_media import Command as AutoMediaCommand
from catalog.models import ProviderThrottle


class AutomaticSchedulerTests(TestCase):
    @mock_patch("catalog.management.commands.auto_media.call_command")
    def test_first_cycle_runs_inventory_grouping_and_matching(self, caller):
        AutoMediaCommand()._cycle(10)
        self.assertEqual(
            [item.args[0] for item in caller.call_args_list],
            ["scan_media", "group_media", "match_media"],
        )
        self.assertTrue(ProviderThrottle.objects.filter(provider="media45_automated_scan").exists())

    @mock_patch("catalog.management.commands.auto_media.call_command")
    def test_followup_cycle_only_matches(self, caller):
        command = AutoMediaCommand()
        command._cycle(10)
        caller.reset_mock()
        command._cycle(10)
        self.assertEqual([item.args[0] for item in caller.call_args_list], ["match_media"])

    @mock_patch("catalog.management.commands.auto_media.call_command")
    def test_failed_scan_does_not_mark_complete(self, caller):
        caller.side_effect = CommandError("NAS fehlt")
        with self.assertRaises(CommandError):
            AutoMediaCommand()._cycle(10)
        record = ProviderThrottle.objects.get(provider="media45_automated_scan")
        self.assertGreater(timezone.now() - record.last_call, timedelta(hours=24))


from django.urls import reverse
from django.test import Client


class CatalogViewTests(TestCase):
    def setUp(self):
        self.movie = Media.objects.create(
            kind="movie", title="Alpha Movie", year=2020,
            external_provider="imdb", external_id="tt1234567", state="available",
        )

    def test_filter_and_search(self):
        result = self.client.get(reverse("index"), {"q": "Alpha", "kind": "movie", "state": "available"})
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "Alpha Movie")

    def test_detail_does_not_show_internal_filename(self):
        ScannedFile.objects.create(
            root="movies", relative_path="private/path/alpha.mkv",
            media_kind="movie", suggested_title="Alpha Movie",
            file_size=123, modified_ns=123, last_seen_at=timezone.now(),
            media=self.movie,
        )
        response = self.client.get(reverse("media_detail", args=[self.movie.pk]))
        self.assertContains(response, "Alpha Movie")
        self.assertContains(response, "Verknüpfte Dateien")
        self.assertNotContains(response, "private/path/alpha.mkv")

    def test_invalid_filter_values_do_not_fail(self):
        response = self.client.get(reverse("index"), {"kind": "invalid", "state": "x", "page": "garbage"})
        self.assertEqual(response.status_code, 200)

    def test_missing_detail_is_404(self):
        response = self.client.get(reverse("media_detail", args=[999999]))
        self.assertEqual(response.status_code, 404)


from django.contrib.auth import get_user_model


class GroupedReviewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            username="review-admin", email="review@example.invalid", password="very-long-admin-test-password"
        )
        self.regular = User.objects.create_user(
            username="review-user", password="very-long-user-test-password"
        )
        from catalog.models import TotpDevice
        TotpDevice.objects.create(user=self.admin, secret="JBSWY3DPEHPK3PXP")
        self.first = self._group("group-a", "Film A", "tt1234567")
        self.second = self._group("group-b", "Film B", "tt1234568")

    def _group(self, key, title, imdb):
        group = MediaGroup.objects.create(
            root="movies", group_key=key, kind="movie",
            suggested_title=title, file_count=1,
        )
        ScannedFile.objects.create(
            root="movies", relative_path=f"{key}.mkv",
            media_kind="movie", suggested_title=title,
            file_size=1, modified_ns=1, last_seen_at=timezone.now(),
            group=group,
        )
        return MetadataCandidate.objects.create(
            group=group, kind="movie", title=title,
            external_provider="imdb", external_id=imdb,
        )

    def test_regular_user_cannot_view_or_post(self):
        self.client.force_login(self.regular)
        url = reverse("review_matches")
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"selection": [self.first.pk]}).status_code, 403)
        self.assertFalse(Media.objects.exists())

    def test_anonymous_redirects_to_login(self):
        self.assertEqual(self.client.get(reverse("review_matches")).status_code, 302)

    def test_admin_sees_one_card_per_group(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        response = self.client.get(reverse("review_matches"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Film A")
        self.assertContains(response, "Film B")

    def test_admin_can_confirm_multiple_groups(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        response = self.client.post(reverse("review_matches"), {
            "selection": [str(self.first.pk), str(self.second.pk)]
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Media.objects.filter(state="available").count(), 2)
        self.assertEqual(ScannedFile.objects.filter(media__isnull=False).count(), 2)

    def test_conflicting_selection_rolls_back(self):
        alternative = MetadataCandidate.objects.create(
            group=self.first.group, kind="movie", title="Andere Film A",
            external_provider="imdb", external_id="tt1234569",
        )
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        response = self.client.post(reverse("review_matches"), {
            "selection": [str(self.first.pk), str(alternative.pk), str(self.second.pk)]
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Media.objects.count(), 0)

    def test_selection_must_be_on_filtered_page(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        response = self.client.post(reverse("review_matches") + "?q=Film+A", {
            "selection": [str(self.second.pk)]
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Media.objects.exists())

    def test_confirmed_group_no_longer_displayed(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        self.client.post(reverse("review_matches"), {"selection": [str(self.first.pk)]})
        response = self.client.get(reverse("review_matches"))
        self.assertNotContains(response, "Film A")
        self.assertContains(response, "Film B")


class MyRequestsAndPasswordTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user(username="alice-private", password="old-passphrase-for-alice-2026")
        self.bob = User.objects.create_user(username="bob-private", password="old-passphrase-for-bob-2026")
        self.a = Media.objects.create(kind="movie", title="Alice private movie", year=2021,
            external_provider="imdb", external_id="tt7654321", state="requested")
        self.b = Media.objects.create(kind="movie", title="Bob private movie", year=2022,
            external_provider="imdb", external_id="tt7654322", state="available")
        MediaRequest.objects.create(media=self.a, user=self.alice)
        MediaRequest.objects.create(media=self.b, user=self.bob)

    def test_unauthenticated_requests_redirect(self):
        self.assertEqual(self.client.get(reverse("my_requests")).status_code, 302)

    def test_only_own_requests_visible(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("my_requests"))
        self.assertContains(response, "Alice private movie")
        self.assertNotContains(response, "Bob private movie")

    def test_current_media_status_is_displayed(self):
        self.client.force_login(self.alice)
        self.a.state = "available"
        self.a.save(update_fields=["state"])
        self.assertContains(self.client.get(reverse("my_requests"), {"state":"available"}), "Vorhanden")
        self.assertNotContains(self.client.get(reverse("my_requests"), {"state":"requested"}), "Alice private movie")

    def test_password_change_checks_old_password(self):
        self.client.force_login(self.alice)
        response = self.client.post(reverse("password_change"), {
            "old_password":"incorrect", "new_password1":"entirely-new-passphrase-2026",
            "new_password2":"entirely-new-passphrase-2026",
        })
        self.assertEqual(response.status_code, 200)
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("old-passphrase-for-alice-2026"))

    def test_password_change_keeps_session(self):
        self.client.force_login(self.alice)
        response = self.client.post(reverse("password_change"), {
            "old_password":"old-passphrase-for-alice-2026",
            "new_password1":"entirely-new-passphrase-2026",
            "new_password2":"entirely-new-passphrase-2026",
        })
        self.assertRedirects(response, reverse("password_change_done"))
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("entirely-new-passphrase-2026"))
        self.assertEqual(self.client.get(reverse("my_requests")).status_code, 200)


from catalog.providers import ProviderError, lookup_identity


class ReviewCorrectionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser(username="reviewfix", email="fix@example.invalid", password="long-test-passphrase-2026")
        self.user = User.objects.create_user(username="reviewordinary", password="long-test-passphrase-2026")
        from catalog.models import TotpDevice
        TotpDevice.objects.create(user=self.admin, secret="JBSWY3DPEHPK3PXP")
        self.group = MediaGroup.objects.create(root="movies", group_key="fix", kind="movie",
                                               suggested_title="Falsch geschriebener Film", file_count=1)

    def test_user_cannot_defer_or_search(self):
        self.client.force_login(self.user)
        u = reverse("review_group", args=[self.group.pk])
        self.assertEqual(self.client.get(u).status_code, 403)
        self.assertEqual(self.client.post(u, {"action": "defer"}).status_code, 403)

    def test_defer_and_restore(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        u = reverse("review_group", args=[self.group.pk])
        self.assertEqual(self.client.post(u, {"action": "defer"}).status_code, 302)
        self.group.refresh_from_db()
        self.assertTrue(self.group.review_deferred)
        self.assertEqual(self.client.post(u, {"action": "restore"}).status_code, 302)
        self.group.refresh_from_db()
        self.assertFalse(self.group.review_deferred)

    @patch("catalog.views.providers.search")
    def test_alternative_title_adds_unconfirmed_candidate(self, finder):
        finder.return_value = [{"kind": "movie", "title": "Korrigierter Film", "year": 2022,
                                "external_provider": "imdb", "external_id": "tt1234567"}]
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        self.client.post(reverse("review_group", args=[self.group.pk]),
                         {"action": "search", "phrase": "Korrigierter Film"})
        self.assertEqual(MetadataCandidate.objects.filter(group=self.group).count(), 1)
        self.assertFalse(Media.objects.exists())

    @patch("catalog.views.providers.lookup_identity")
    def test_exact_id_is_verified_before_saving(self, finder):
        finder.return_value = {"kind": "movie", "title": "ID Match", "year": None,
                               "external_provider": "imdb", "external_id": "tt1234567"}
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        self.client.post(reverse("review_group", args=[self.group.pk]),
                         {"action": "lookup", "identifier": "tt1234567"})
        finder.assert_called_once_with("movie", "tt1234567")
        self.assertEqual(self.group.candidates.count(), 1)

    def test_bad_identifier_not_saved(self):
        self.client.force_login(self.admin)
        session = self.client.session
        session['media45_mfa_verified_user'] = self.admin.pk
        session.save()
        self.client.post(reverse("review_group", args=[self.group.pk]),
                         {"action": "lookup", "identifier": "not-valid"})
        self.assertFalse(self.group.candidates.exists())


class ProviderHttpRetryTests(SimpleTestCase):
    @patch("catalog.providers.time.sleep")
    @patch("catalog.providers.requests.get")
    def test_429_retried_then_reports_status(self, get, sleep):
        import requests
        from catalog.providers import get_json
        response = requests.Response()
        response.status_code = 429
        response.url = "https://www.wikidata.org/w/api.php"
        get.return_value = response
        with self.assertRaisesMessage(ProviderError, "HTTP 429"):
            get_json("https://www.wikidata.org/w/api.php")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)


from catalog.models import TotpDevice, TotpRecoveryCode
from catalog import mfa
import pyotp


class TotpSecurityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="mfa-admin", email="mfa@example.invalid", password="long-mfa-password-2026"
        )

    def test_admin_redirected_to_enrollment_without_device(self):
        self.client.force_login(self.user)
        response = self.client.get("/admin/")
        self.assertRedirects(response, reverse("mfa_setup"), fetch_redirect_response=False)

    def test_setup_needs_current_password(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("mfa_setup"), {"action": "begin", "password": "wrong"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(mfa.PENDING_SECRET, self.client.session)
        self.assertFalse(TotpDevice.objects.filter(user=self.user).exists())

    def test_enrollment_and_one_time_recovery(self):
        self.client.force_login(self.user)
        self.client.post(reverse("mfa_setup"), {"action": "begin", "password": "long-mfa-password-2026"})
        pending = self.client.session[mfa.PENDING_SECRET]
        otp = pyotp.TOTP(pending).now()
        response = self.client.post(reverse("mfa_setup"), {"action": "confirm", "code": otp})
        self.assertRedirects(response, reverse("mfa_recovery"), fetch_redirect_response=False)
        self.assertEqual(TotpRecoveryCode.objects.filter(device__user=self.user).count(), 10)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        once = self.client.get(reverse("mfa_recovery"))
        self.assertEqual(once.status_code, 200)
        self.assertIn("Notfallcodes", once.content.decode())
        self.assertRedirects(self.client.get(reverse("mfa_recovery")), reverse("index"))

    def test_replayed_totp_cannot_be_reused(self):
        TotpDevice.objects.create(user=self.user, secret=pyotp.random_base32())
        code = pyotp.TOTP(self.user.media45_totp.secret).now()
        self.assertTrue(mfa.verify_second_factor(self.user, code))
        self.assertFalse(mfa.verify_second_factor(self.user, code))

    def test_unverified_device_blocks_admin_and_review(self):
        TotpDevice.objects.create(user=self.user, secret=pyotp.random_base32())
        self.client.force_login(self.user)
        self.assertRedirects(self.client.get("/admin/"), reverse("mfa_verify"), fetch_redirect_response=False)
        self.assertRedirects(self.client.get(reverse("review_matches")), reverse("mfa_verify"), fetch_redirect_response=False)

    def test_recovery_code_single_use(self):
        device = TotpDevice.objects.create(user=self.user, secret=pyotp.random_base32())
        code = mfa.make_recovery_codes(device)[0]
        self.assertTrue(mfa.verify_second_factor(self.user, code))
        self.assertFalse(mfa.verify_second_factor(self.user, code))

    def test_no_device_regular_user_not_redirected(self):
        user = get_user_model().objects.create_user(
            username="normal-user-no-totp", password="normal-user-passphrase-2026"
        )
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("index")).status_code, 200)


class ManualImdbFallbackTests(TestCase):
    def setUp(self):
        from catalog.models import TotpDevice
        User = get_user_model()
        self.admin = User.objects.create_superuser(
            username="manual-imdb-admin", email="manual@example.invalid",
            password="long-test-password-for-manual-imdb",
        )
        TotpDevice.objects.create(user=self.admin, secret="JBSWY3DPEHPK3PXP")
        self.group = MediaGroup.objects.create(
            root="movies", group_key="manual-test", kind="movie",
            suggested_title="Beverly Hills Cop 2", file_count=1,
        )
        ScannedFile.objects.create(
            root="movies", relative_path="Beverly Hills Cop 2.mkv", group=self.group,
            media_kind="movie", suggested_title="Beverly Hills Cop 2",
            file_size=1, modified_ns=1, last_seen_at=timezone.now(),
        )
        self.client.force_login(self.admin)
        session = self.client.session
        session["media45_mfa_verified_user"] = self.admin.pk
        session.save()

    def test_manual_candidate_requires_explicit_check(self):
        url = reverse("review_group", args=[self.group.pk])
        self.client.post(url, {"action":"manual", "identifier":"tt1234567",
                               "title":"Beverly Hills Cop II"})
        self.assertFalse(self.group.candidates.exists())

    def test_manual_candidate_marked_and_requires_second_ack(self):
        url = reverse("review_group", args=[self.group.pk])
        response = self.client.post(url, {"action":"manual", "identifier":"tt1234567",
                                         "title":"Beverly Hills Cop II", "year":"1987",
                                         "checked_imdb":"on"})
        self.assertEqual(response.status_code, 302)
        candidate = self.group.candidates.get()
        self.assertTrue(candidate.manually_verified)
        self.assertFalse(Media.objects.exists())
        url = reverse("review_matches")
        rejected = self.client.post(url, {"selection":[str(candidate.pk)]})
        self.assertEqual(rejected.status_code, 200)
        self.assertFalse(Media.objects.exists())
        accepted = self.client.post(url, {"selection":[str(candidate.pk)],"confirm_manual":"on"})
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(Media.objects.filter(state="available").count(), 1)

    def test_invalid_imdb_id_not_saved(self):
        self.client.post(reverse("review_group", args=[self.group.pk]),
                         {"action":"manual", "identifier":"not-a-valid-id",
                          "title":"A film", "checked_imdb":"on"})
        self.assertFalse(self.group.candidates.exists())


class TotpSettingsTests(TestCase):
    def setUp(self):
        from catalog.models import TotpDevice
        self.user = get_user_model().objects.create_superuser(
            username="mfa-settings-admin",
            email="settings@example.invalid",
            password="settings-long-password-2026",
        )
        self.device = TotpDevice.objects.create(user=self.user, secret=pyotp.random_base32())
        self.client.force_login(self.user)
        session = self.client.session
        session[mfa.SESSION_MARKER] = self.user.pk
        session.save()

    def test_navigation_shows_enabled_status(self):
        response = self.client.get(reverse("mfa_settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TOTP ist aktiviert")
        self.assertContains(response, "Notfallcodes")

    def test_setup_with_enabled_device_goes_to_settings(self):
        self.assertRedirects(
            self.client.get(reverse("mfa_setup")),
            reverse("mfa_settings"),
        )

    def test_wrong_password_does_not_rotate_codes(self):
        code = mfa.make_recovery_codes(self.device)[0]
        self.client.post(reverse("mfa_settings"), {
            "password": "wrong",
            "code": pyotp.TOTP(self.device.secret).now(),
        })
        self.assertTrue(mfa.verify_second_factor(self.user, code))

    def test_rotating_codes_invalidates_previous_and_preserves_device(self):
        old_code = mfa.make_recovery_codes(self.device)[0]
        response = self.client.post(reverse("mfa_settings"), {
            "password": "settings-long-password-2026",
            "code": pyotp.TOTP(self.device.secret).now(),
        })
        # Do not follow the redirect: the recovery page shows codes only once.
        self.assertRedirects(response, reverse("mfa_recovery"), fetch_redirect_response=False)
        self.assertEqual(self.device.recovery_codes.count(), 10)
        self.assertFalse(mfa.verify_second_factor(self.user, old_code))
        self.device.refresh_from_db()
        self.assertTrue(pyotp.TOTP(self.device.secret))
        self.assertEqual(self.client.get(reverse("mfa_recovery")).status_code, 200)


class TotpNavigationRegressionTests(TestCase):
    def setUp(self):
        from catalog.models import TotpDevice
        self.admin = get_user_model().objects.create_superuser(
            username="mfa-nav-admin", email="mfa-nav@example.invalid",
            password="long-test-password-for-mfa-navigation",
        )
        TotpDevice.objects.create(user=self.admin, secret=pyotp.random_base32())
        self.client.force_login(self.admin)
        session = self.client.session
        session[mfa.SESSION_MARKER] = self.admin.pk
        session.save()

    def test_settings_link_and_page(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, 'href="/account/mfa/settings/"')
        self.assertContains(self.client.get(reverse("mfa_settings")), "TOTP ist aktiviert")

    def test_old_verify_link_redirects_to_settings(self):
        self.assertRedirects(self.client.get(reverse("mfa_verify")), reverse("mfa_settings"))

    def test_unverified_settings_redirects_to_challenge(self):
        session = self.client.session
        session.pop(mfa.SESSION_MARKER, None)
        session.save()
        self.assertRedirects(self.client.get(reverse("mfa_settings")), reverse("mfa_verify"), fetch_redirect_response=False)


from catalog.models import LoginGuard
from catalog.login_guard import ADDRESS_LIMIT, PAIR_LIMIT, blocked, guard_keys


class LoginThrottlingTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="login-guard-user", password="strong-enough-login-guard-password",
        )
        self.admin = User.objects.create_superuser(
            username="login-guard-admin", email="login@example.invalid",
            password="strong-enough-admin-guard-password",
        )

    def test_failed_login_counts_and_blocks_valid_password(self):
        url = reverse("login")
        for attempt in range(PAIR_LIMIT):
            self.assertEqual(self.client.post(url, {
                "username": self.user.username, "password": "incorrect",
            }).status_code, 200)
        result = self.client.post(url, {
            "username": self.user.username,
            "password": "strong-enough-login-guard-password",
        })
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "Zu viele Anmeldeversuche")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_success_clears_pair_counter(self):
        url = reverse("login")
        self.client.post(url, {"username": self.user.username, "password": "incorrect"})
        response = self.client.post(url, {
            "username": self.user.username,
            "password": "strong-enough-login-guard-password",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LoginGuard.objects.filter(
            key=guard_keys(response.wsgi_request, self.user.username)[0]).exists())

    def test_admin_login_uses_same_limit(self):
        url = "/admin/login/"
        for attempt in range(PAIR_LIMIT):
            self.client.post(url, {"username": self.admin.username, "password": "wrong"})
        response = self.client.post(url, {
            "username": self.admin.username,
            "password": "strong-enough-admin-guard-password",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Zu viele Anmeldeversuche")

    def test_unrelated_user_can_still_log_in(self):
        url = reverse("login")
        for attempt in range(PAIR_LIMIT):
            self.client.post(url, {
                "username": self.user.username,
                "password": "wrong",
            })
        self.assertEqual(self.client.post(url, {
            "username": self.admin.username,
            "password": "strong-enough-admin-guard-password",
        }).status_code, 302)

    def test_no_plaintext_account_identifiers_in_buckets(self):
        self.client.post(reverse("login"), {
            "username": self.user.username, "password": "incorrect",
        })
        self.assertTrue(LoginGuard.objects.exists())
        for entry in LoginGuard.objects.all():
            self.assertEqual(len(entry.key), 64)
            self.assertNotIn(self.user.username, entry.key)

    def test_spoofed_forwarded_for_does_not_change_bucket(self):
        from django.test import RequestFactory
        factory = RequestFactory()
        a = factory.post("/login/", REMOTE_ADDR="127.0.0.1",
                         HTTP_X_FORWARDED_FOR="192.0.2.1")
        b = factory.post("/login/", REMOTE_ADDR="127.0.0.1",
                         HTTP_X_FORWARDED_FOR="192.0.2.2")
        self.assertEqual(guard_keys(a, "anyone"), guard_keys(b, "anyone"))


from types import SimpleNamespace
from catalog.models import PasskeyCredential
from catalog import passkeys
import json


class PasskeyFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="webauthn-admin", email="webauthn@example.invalid",
            password="webauthn-test-password-strong-2026",
        )
        self.other = get_user_model().objects.create_superuser(
            username="another-webauthn-admin", email="other@example.invalid",
            password="another-webauthn-test-password-2026",
        )
        TotpDevice.objects.create(user=self.user, secret=pyotp.random_base32())
        TotpDevice.objects.create(user=self.other, secret=pyotp.random_base32())
        self.client.force_login(self.user)

    def verify_session(self):
        session = self.client.session
        session[mfa.SESSION_MARKER] = self.user.pk
        session.save()

    def test_unverified_admin_cannot_register_or_manage(self):
        self.assertRedirects(
            self.client.get(reverse("passkeys_manage")),
            reverse("mfa_verify"), fetch_redirect_response=False
        )
        response = self.client.post(reverse("passkeys_register_options"), {
            "name": "Test Key", "password": "webauthn-test-password-strong-2026"
        })
        self.assertEqual(response.status_code, 302)

    def test_registration_requires_password(self):
        self.verify_session()
        response = self.client.post(reverse("passkeys_register_options"), {
            "name": "Test Key", "password": "incorrect"
        })
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PasskeyCredential.objects.exists())

    def test_registration_challenge_is_session_bound_and_one_time(self):
        self.verify_session()
        response = self.client.post(reverse("passkeys_register_options"), {
            "name": "USB-C Key", "password": "webauthn-test-password-strong-2026"
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("challenge", response.json())
        self.assertIn(passkeys.REGISTER_SESSION, self.client.session)
        request = response.wsgi_request
        challenge = passkeys.consume_challenge(request, passkeys.REGISTER_SESSION)
        self.assertTrue(challenge)
        with self.assertRaises(ValidationError):
            passkeys.consume_challenge(request, passkeys.REGISTER_SESSION)

    @patch("catalog.passkey_views.webauthn.verify_registration_response")
    def test_registration_stores_only_public_material(self, verifier):
        self.verify_session()
        self.client.post(reverse("passkeys_register_options"), {
            "name": "YubiKey A", "password": "webauthn-test-password-strong-2026"
        })
        verifier.return_value = SimpleNamespace(
            credential_id=b"credential-unique-001", credential_public_key=b"cose-public",
            sign_count=0,
        )
        response = self.client.post(
            reverse("passkeys_register_finish"),
            data=json.dumps({"id": "ignored", "type": "public-key"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        device = PasskeyCredential.objects.get(user=self.user)
        self.assertEqual(device.name, "YubiKey A")
        self.assertEqual(bytes(device.public_key), b"cose-public")
        self.assertFalse(hasattr(device, "private_key"))
        self.assertEqual(verifier.call_args.kwargs["expected_origin"], settings.ORIGIN)

    def test_assertion_refuses_other_accounts_key(self):
        PasskeyCredential.objects.create(user=self.other, name="Other", credential_id=b"foreign-key",
                                         public_key=b"public", sign_count=0)
        options = self.client.post(reverse("passkeys_auth_options"))
        self.assertEqual(options.status_code, 400)
        self.assertFalse(PasskeyCredential.objects.filter(user=self.user).exists())

    @patch("catalog.passkey_views.webauthn.verify_authentication_response")
    def test_assertion_updates_counter_and_marks_mfa(self, verifier):
        PasskeyCredential.objects.create(user=self.user, name="Own", credential_id=b"my-key",
                                         public_key=b"public", sign_count=5)
        options = self.client.post(reverse("passkeys_auth_options"))
        self.assertEqual(options.status_code, 200)
        verifier.return_value = SimpleNamespace(new_sign_count=6)
        response = self.client.post(
            reverse("passkeys_auth_finish"),
            data=json.dumps({"id": passkeys.b64(b"my-key"), "type": "public-key"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.session[mfa.SESSION_MARKER], self.user.pk)
        self.assertEqual(PasskeyCredential.objects.get(user=self.user).sign_count, 6)
        self.assertEqual(verifier.call_args.kwargs["credential_current_sign_count"], 5)

    def test_cannot_remove_other_users_key(self):
        self.verify_session()
        foreign = PasskeyCredential.objects.create(
            user=self.other, name="Foreign", credential_id=b"other-key", public_key=b"public"
        )
        response = self.client.post(reverse("passkeys_remove", args=[foreign.pk]), {
            "password": "webauthn-test-password-strong-2026", "code": "123456",
        })
        self.assertEqual(response.status_code, 404)
        self.assertTrue(PasskeyCredential.objects.filter(pk=foreign.pk).exists())


    @patch("catalog.passkey_views.webauthn.verify_authentication_response")
    def test_invalid_signature_does_not_complete_mfa(self, verifier):
        from webauthn.helpers.exceptions import InvalidAuthenticationResponse
        PasskeyCredential.objects.create(
            user=self.user, name="Key", credential_id=b"invalid-signature-key",
            public_key=b"public", sign_count=0,
        )
        self.assertEqual(self.client.post(reverse("passkeys_auth_options")).status_code, 200)
        verifier.side_effect = InvalidAuthenticationResponse("Invalid signature")
        response = self.client.post(
            reverse("passkeys_auth_finish"),
            data=json.dumps({"id": passkeys.b64(b"invalid-signature-key")}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(mfa.SESSION_MARKER, self.client.session)

    def test_registration_challenge_not_accepted_for_login(self):
        self.verify_session()
        self.client.post(reverse("passkeys_register_options"), {
            "name": "Some key", "password": "webauthn-test-password-strong-2026"
        })
        session = self.client.session
        session.pop(mfa.SESSION_MARKER, None)
        session.save()
        response = self.client.post(
            reverse("passkeys_auth_finish"),
            data=json.dumps({"id": passkeys.b64(b"missing")}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(mfa.SESSION_MARKER, self.client.session)


class PasswordlessPasskeyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser(
            username="passkey-login-admin", email="passkey-login@example.invalid",
            password="passwordless-backup-password-2026",
        )
        self.other = get_user_model().objects.create_user(
            username="passkey-login-other", password="passwordless-other-password-2026",
        )
        TotpDevice.objects.create(user=self.user, secret=pyotp.random_base32())
        self.cred = PasskeyCredential.objects.create(
            user=self.user, name="USB Key", credential_id=b"passwordless-key",
            public_key=b"stored-public-key", sign_count=4,
        )
        self.client.logout()

    def options(self, username="passkey-login-admin"):
        return self.client.post(reverse("passwordless_options"), {"username": username})

    def finish(self, credential_id=b"passwordless-key", handle=None):
        response = {"authenticatorData": "", "clientDataJSON": "", "signature": "",
                    "userHandle": handle}
        return self.client.post(
            reverse("passwordless_finish"),
            data=json.dumps({"id": passkeys.b64(credential_id),
                             "type": "public-key", "response": response}),
            content_type="application/json",
        )

    def test_unauthenticated_user_can_get_challenge(self):
        response = self.options()
        self.assertEqual(response.status_code, 200)
        self.assertIn("challenge", response.json())
        self.assertIn(passkeys.LOGIN_SESSION, self.client.session)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_unknown_account_does_not_create_session(self):
        self.assertEqual(self.options("no-such-passkey-user").status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_finishing_without_challenge_denied(self):
        self.assertEqual(self.finish().status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    @patch("catalog.passkey_views.webauthn.verify_authentication_response")
    def test_valid_passwordless_login_marks_mfa_and_updates_counter(self, verify):
        verify.return_value = SimpleNamespace(new_sign_count=5)
        self.assertEqual(self.options().status_code, 200)
        response = self.finish()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)
        self.assertEqual(self.client.session[mfa.SESSION_MARKER], self.user.pk)
        self.assertEqual(PasskeyCredential.objects.get(pk=self.cred.pk).sign_count, 5)
        self.assertEqual(verify.call_args.kwargs["credential_current_sign_count"], 4)
        self.assertEqual(verify.call_args.kwargs["expected_rp_id"], settings.HOST)
        self.assertEqual(verify.call_args.kwargs["expected_origin"], settings.ORIGIN)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    @patch("catalog.passkey_views.webauthn.verify_authentication_response")
    def test_passkey_cannot_be_replayed(self, verify):
        verify.return_value = SimpleNamespace(new_sign_count=5)
        self.options()
        self.assertEqual(self.finish().status_code, 200)
        self.client.logout()
        self.assertEqual(self.finish().status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    @patch("catalog.passkey_views.webauthn.verify_authentication_response")
    def test_wrong_credential_id_does_not_log_in(self, verify):
        self.options()
        self.assertEqual(self.finish(b"foreign-credential").status_code, 400)
        verify.assert_not_called()
        self.assertNotIn("_auth_user_id", self.client.session)

    @patch("catalog.passkey_views.webauthn.verify_authentication_response")
    def test_mismatched_user_handle_rejected(self, verify):
        self.options()
        self.assertEqual(self.finish(handle=passkeys.b64(b"99999")).status_code, 400)
        verify.assert_not_called()
        self.assertNotIn("_auth_user_id", self.client.session)

    @patch("catalog.passkey_views.webauthn.verify_authentication_response")
    def test_bad_signature_does_not_log_in(self, verify):
        from webauthn.helpers.exceptions import InvalidAuthenticationResponse
        self.options()
        verify.side_effect = InvalidAuthenticationResponse("invalid")
        self.assertEqual(self.finish().status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_mfa_assertion_challenge_does_not_work_as_passwordless(self):
        self.assertEqual(self.finish().status_code, 400)

    def test_inactive_user_cannot_start_login(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.options().status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_account_bound_to_its_selected_credential(self):
        PasskeyCredential.objects.create(
            user=self.other, name="Other", credential_id=b"other-key",
            public_key=b"other-public",
        )
        self.options()
        self.assertEqual(self.finish(b"other-key").status_code, 400)
        self.assertNotIn("_auth_user_id", self.client.session)


from tempfile import TemporaryDirectory
from pathlib import Path
from django.test import override_settings
from catalog import covers
from unittest.mock import MagicMock
import io
from PIL import Image


class CoverCacheTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.override = override_settings(COVER_DIR=Path(self.temp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.movie = Media.objects.create(
            kind="movie", title="Verified Cover Film",
            external_provider="imdb", external_id="tt1234567",
            state="available",
        )

    def test_without_cover_uses_original_placeholder(self):
        page = self.client.get(reverse("index"))
        self.assertContains(page, "Verified Cover Film")
        self.assertContains(page, 'art-mark')
        self.assertNotContains(page, 'class="cover-image"')
        self.assertEqual(self.client.get(reverse("media_cover", args=[self.movie.pk])).status_code, 404)

    def test_local_image_is_served_without_external_url(self):
        path = covers.cover_path(self.movie.pk)
        path.write_bytes(b"RIFFfake-webp")
        page = self.client.get(reverse("index"))
        self.assertContains(page, reverse("media_cover", args=[self.movie.pk]))
        self.assertNotContains(page, "upload.wikimedia.org")
        result = self.client.get(reverse("media_cover", args=[self.movie.pk]))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result["Content-Type"], "image/webp")
        self.assertEqual(b"".join(result.streaming_content), b"RIFFfake-webp")

    def test_unknown_media_id_cannot_serve_orphan_file(self):
        covers.cover_path(99999).write_bytes(b"private")
        self.assertEqual(self.client.get(reverse("media_cover", args=[99999])).status_code, 404)

    def test_imdb_id_must_match_exactly(self):
        item = self.movie
        search_results = {"query": {"search": [{"title": "Q123"}]}}
        entities = {"entities": {"Q123": {"claims": {
            "P345": [{"mainsnak": {"datavalue": {"value": "tt9999999"}}}],
            "P18": [{"mainsnak": {"datavalue": {"value": "wrong-poster.jpg"}}}],
        }}}}
        with patch("catalog.covers.get_json", side_effect=[search_results, entities]) as query:
            with self.assertRaises(covers.CoverUnavailable):
                covers.wikimedia_image_for(item)
        self.assertEqual(query.call_count, 2)

    def test_only_approved_image_hosts_and_https(self):
        self.assertTrue(covers.safe_image_url("https://upload.wikimedia.org/test.png"))
        self.assertTrue(covers.safe_image_url("https://thumb.wikimedia.org/test.png"))
        self.assertTrue(covers.safe_image_url("https://ia600107.us.archive.org/test.jpg"))
        self.assertFalse(covers.safe_image_url("http://upload.wikimedia.org/test.png"))
        self.assertFalse(covers.safe_image_url("https://upload.wikimedia.org.evil.test/test.png"))
        self.assertFalse(covers.safe_image_url("https://127.0.0.1/test.png"))

    @patch("catalog.covers.requests.get")
    def test_image_download_converts_to_webp(self, get):
        image = Image.new("RGB", (20, 30), color="navy")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        payload = stream.getvalue()
        response = MagicMock(status_code=200)
        response.iter_content.return_value = [payload]
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        get.return_value = response
        result = covers.fetch_image("https://upload.wikimedia.org/test.png")
        self.assertTrue(result.startswith(b"RIFF"))
        self.assertIn(b"WEBP", result[:16])

    @patch("catalog.covers.requests.get")
    def test_image_redirect_to_unapproved_host_refused(self, get):
        response = MagicMock(status_code=302, headers={"Location": "https://example.org/file.jpg"})
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        get.return_value = response
        with self.assertRaises(covers.CoverUnavailable):
            covers.fetch_image("https://upload.wikimedia.org/test.jpg")
        self.assertEqual(get.call_count, 1)


    def test_imdb_claim_prefers_real_film_poster_property(self):
        search_results = {"query": {"search": [{"title": "Q123"}]}}
        entities = {"entities": {"Q123": {"claims": {
            "P345": [{"mainsnak": {"datavalue": {"value": "tt1234567"}}}],
            "P3383": [{"mainsnak": {"datavalue": {"value": "Movie poster.jpg"}}}],
            "P18": [{"mainsnak": {"datavalue": {"value": "Photo.jpg"}}}],
        }}}}
        with patch("catalog.covers.get_json", side_effect=[search_results, entities]):
            url = covers.wikimedia_image_for(self.movie)
        self.assertIn("Special:Redirect/file/Movie_poster.jpg", url)

    def test_title_fallback_still_checks_imdb_id(self):
        search_results = {"query": {"search": []}}
        title = {"search": [{"id": "Q123"}]}
        entity = {"entities": {"Q123": {"claims": {
            "P345": [{"mainsnak": {"datavalue": {"value": "tt1234567"}}}],
            "P18": [{"mainsnak": {"datavalue": {"value": "Photo.jpg"}}}],
        }}}}
        with patch("catalog.covers.get_json", side_effect=[search_results, title, title, entity]):
            url = covers.wikimedia_image_for(self.movie)
        self.assertIn("Photo.jpg", url)

    def test_rejects_non_https_and_credential_urls(self):
        self.assertFalse(covers.safe_image_url("https://fake@upload.wikimedia.org/photo.jpg"))
        self.assertFalse(covers.safe_image_url("https://upload.wikimedia.org:8443/photo.jpg"))
        self.assertTrue(covers.safe_image_url("https://commons.wikimedia.org/wiki/Special:Redirect/file/A.jpg"))

    @override_settings(TMDB_READ_ACCESS_TOKEN="fake-token")
    @patch("catalog.covers.requests.get")
    def test_tmdb_movie_by_exact_imdb_id(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            "movie_results": [{"id": 123, "poster_path": "/abcdefgh123.jpg"}],
            "tv_results": [],
        }
        url = covers.tmdb_image_for(self.movie)
        self.assertEqual(url, "https://image.tmdb.org/t/p/w500/abcdefgh123.jpg")
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://api.themoviedb.org/3/find/tt1234567")
        self.assertEqual(kwargs["params"]["external_source"], "imdb_id")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fake-token")

    @override_settings(TMDB_READ_ACCESS_TOKEN="fake-token")
    @patch("catalog.covers.requests.get")
    def test_tmdb_tv_uses_tv_results(self, get):
        self.movie.kind = "series"
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            "movie_results": [{"poster_path": "/wrongmovie.jpg"}],
            "tv_results": [{"poster_path": "/tvposter123.png"}],
        }
        self.assertEqual(covers.tmdb_image_for(self.movie),
                         "https://image.tmdb.org/t/p/w500/tvposter123.png")

    @override_settings(TMDB_READ_ACCESS_TOKEN="fake-token")
    @patch("catalog.covers.requests.get")
    def test_tmdb_missing_ambiguous_or_external_poster_rejected(self, get):
        get.return_value.status_code = 200
        for results in ([], [{"poster_path": "/one123456.jpg"}, {"poster_path": "/two123456.jpg"}],
                        [{"poster_path": "https://evil.example/image.jpg"}],
                        [{"poster_path": None}]):
            get.return_value.json.return_value = {"movie_results": results}
            with self.assertRaises(covers.CoverUnavailable):
                covers.tmdb_image_for(self.movie)

    @override_settings(TMDB_READ_ACCESS_TOKEN="")
    @patch("catalog.covers.requests.get")
    def test_tmdb_missing_token_never_calls_api(self, get):
        with self.assertRaises(covers.CoverUnavailable):
            covers.tmdb_image_for(self.movie)
        get.assert_not_called()

    def test_expired_tmdb_cover_is_not_served(self):
        import os
        import time
        covers.cover_path(self.movie.pk).write_bytes(b"RIFFtest")
        stamp = covers.tmdb_stamp(self.movie.pk)
        stamp.touch()
        self.assertTrue(covers.available(self.movie.pk))
        old = time.time() - covers.TMDB_TTL_SECONDS - 5
        os.utime(stamp, (old, old))
        self.assertFalse(covers.available(self.movie.pk))
        self.assertEqual(self.client.get(reverse("media_cover", args=[self.movie.pk])).status_code, 404)

    @override_settings(TMDB_READ_ACCESS_TOKEN="fake-token")
    @patch("catalog.covers.fetch_image", return_value=b"RIFFnew")
    @patch("catalog.covers.tmdb_image_for", return_value="https://image.tmdb.org/t/p/w500/abcde123.jpg")
    def test_tmdb_refresh_replaces_old_wikimedia_cover(self, find, fetch):
        covers.cover_path(self.movie.pk).write_bytes(b"old Wikimedia")
        covers.acquire(self.movie)
        self.assertEqual(covers.cover_path(self.movie.pk).read_bytes(), b"RIFFnew")
        self.assertTrue(covers.tmdb_stamp(self.movie.pk).exists())
        find.assert_called_once_with(self.movie)
        fetch.assert_called_once()

    def test_credits_displays_tmdb_disclaimer_and_local_logo(self):
        page = self.client.get(reverse("credits"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "This product uses the TMDB API but is not endorsed or certified by TMDB.")
        self.assertContains(page, "tmdb-logo.")
        self.assertContains(page, 'alt="TMDB"')


class TmdbRecognitionTests(TestCase):
    @patch("catalog.providers.tmdb_get")
    def test_movie_search_requires_independently_fetched_imdb_id(self, fetch):
        from catalog.providers import tmdb_search
        fetch.side_effect = [
            {"results": [
                {"id": 11, "title": "Film A", "release_date": "2001-01-01"},
                {"id": 12, "title": "Film B", "release_date": "2002-01-01"},
                {"id": 13, "title": "Film C", "release_date": "2003-01-01"},
            ]},
            {"imdb_id": "tt1234567"},
            {"imdb_id": None},
            {"imdb_id": "invalid"},
        ]
        result = tmdb_search("movie", "Film")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["external_id"], "tt1234567")
        self.assertEqual(result[0]["external_provider"], "imdb")
        self.assertEqual(result[0]["year"], 2001)
        self.assertEqual(fetch.call_args_list[0].args[0], "/search/movie")

    @patch("catalog.providers.tmdb_get")
    def test_series_search_uses_tv_external_ids_and_respects_year(self, fetch):
        from catalog.providers import tmdb_search
        fetch.side_effect = [
            {"results": [{"id": 1396, "name": "Serie A", "first_air_date": "2008-01-01"}]},
            {"imdb_id": "tt0903747"},
        ]
        result = tmdb_search("series", "Serie A (2008)")
        self.assertEqual(result[0]["external_id"], "tt0903747")
        self.assertEqual(result[0]["kind"], "series")
        self.assertEqual(fetch.call_args_list[0].args[0], "/search/tv")
        self.assertEqual(fetch.call_args_list[0].args[1]["first_air_date_year"], 2008)
        self.assertEqual(fetch.call_args_list[1].args[0], "/tv/1396/external_ids")

    @patch("catalog.providers.tmdb_get")
    def test_lookup_rejects_wrong_media_type(self, fetch):
        from catalog.providers import tmdb_lookup, ProviderError
        fetch.return_value = {"movie_results": [{"title": "Film"}], "tv_results": []}
        with self.assertRaises(ProviderError):
            tmdb_lookup("series", "tt1234567")

    @patch("catalog.providers.tmdb_get")
    def test_lookup_by_imdb_id_preserves_known_identity(self, fetch):
        from catalog.providers import tmdb_lookup
        fetch.return_value = {"movie_results": [{"title": "Deutscher Titel", "release_date": "1999-01-01"}]}
        result = tmdb_lookup("movie", "tt1234567")
        self.assertEqual(result["external_provider"], "imdb")
        self.assertEqual(result["external_id"], "tt1234567")
        self.assertEqual(result["year"], 1999)

    @override_settings(TMDB_READ_ACCESS_TOKEN="")
    @patch("catalog.providers.requests.get")
    def test_tmdb_missing_token_does_not_call_network(self, get):
        from catalog.providers import tmdb_get, ProviderError
        with self.assertRaises(ProviderError):
            tmdb_get("/search/movie", {"query": "Test"})
        get.assert_not_called()

    @override_settings(TMDB_READ_ACCESS_TOKEN="dummy-test-token")
    @patch("catalog.providers.requests.get")
    def test_tmdb_auth_header_never_uses_url_query(self, get):
        from catalog.providers import tmdb_get
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"results": []}
        tmdb_get("/search/movie", {"query": "Film"})
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://api.themoviedb.org/3/search/movie")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer dummy-test-token")
        self.assertNotIn("api_key", kwargs["params"])

    @patch("catalog.management.commands.match_media.time.sleep")
    @patch("catalog.management.commands.match_media.search")
    def test_retry_empty_only_rechecks_unmatched_unlinked_groups(self, search, sleep):
        from django.core.management import call_command
        from io import StringIO
        empty = MediaGroup.objects.create(root="movies", group_key="empty-tmdb",
            kind="movie", suggested_title="Unbekannter Film", file_count=1,
            metadata_checked_at=timezone.now(), metadata_result_count=0)
        MediaGroup.objects.create(root="movies", group_key="fresh-tmdb",
            kind="movie", suggested_title="Noch nicht geprüft", file_count=1)
        deferred = MediaGroup.objects.create(root="movies", group_key="deferred-tmdb",
            kind="movie", suggested_title="Zurückgestellt", file_count=1,
            metadata_checked_at=timezone.now(), review_deferred=True)
        search.return_value = [{"kind": "movie", "title": "TMDB Film", "year": 2020,
                                "external_provider": "imdb", "external_id": "tt1234567"}]
        call_command("match_media", "--retry-empty", "--limit", "10", stdout=StringIO())
        search.assert_called_once_with("movie", "Unbekannter Film")
        self.assertEqual(MetadataCandidate.objects.filter(group=empty).count(), 1)
        self.assertEqual(MetadataCandidate.objects.filter(group=deferred).count(), 0)

    def test_review_status_filters_are_superuser_only(self):
        from django.contrib.auth import get_user_model
        from catalog.models import TotpDevice
        user = get_user_model().objects.create_superuser(
            username="tmdb-review-admin", email="review-tmdb@example.invalid",
            password="a-long-test-password-for-tmdb",
        )
        TotpDevice.objects.create(user=user, secret="JBSWY3DPEHPK3PXP")
        grp = MediaGroup.objects.create(root="movies", group_key="review-empty-tmdb",
            kind="movie", suggested_title="Unbekanntes Werk", file_count=1,
            metadata_checked_at=timezone.now())
        self.client.force_login(user)
        session = self.client.session
        session["media45_mfa_verified_user"] = user.pk
        session.save()
        result = self.client.get(reverse("review_matches"), {"status": "empty"})
        self.assertEqual(result.status_code, 200)
        self.assertContains(result, "Unbekanntes Werk")
        self.assertContains(result, "Ohne Treffer")
        result = self.client.get(reverse("review_matches"), {"status": "new"})
        self.assertNotContains(result, "Unbekanntes Werk")


class TmdbTitleVariantTests(SimpleTestCase):
    def test_star_trek_names_and_abbreviations(self):
        from catalog.providers import tmdb_title_variants
        self.assertIn("Star Trek The Next Generation", tmdb_title_variants("Startrek TNG"))
        self.assertIn("Star Trek Voyager", tmdb_title_variants("Startrek Voyager"))
        self.assertIn("Star Trek Enterprise", tmdb_title_variants("Startrek Enterprise"))

    def test_transliterations_and_typos(self):
        from catalog.providers import tmdb_title_variants
        self.assertIn("Vier Fäuste gegen Rio", tmdb_title_variants("Vier Faeuste gegen Rio"))
        self.assertIn("Out Come the Wolves", tmdb_title_variants("Out Come The Wolfes"))

    def test_spaces_and_punctuation(self):
        from catalog.providers import tmdb_title_variants
        self.assertIn("Underworld Evolution", tmdb_title_variants("Underworld - Evolution"))
        self.assertIn("Star Trek Voyager", tmdb_title_variants("StarTrek_Voyager"))

    @patch("catalog.providers.tmdb_get")
    def test_search_attempts_alternative_only_after_no_verified_hits(self, fetch):
        from catalog.providers import tmdb_search
        fetch.side_effect = [
            {"results": []},  # Startrek TNG
            {"results": [{"id": 777, "name": "Star Trek: The Next Generation",
                           "first_air_date": "1987-09-28"}]},
            {"imdb_id": "tt0092455"},
        ]
        hits = tmdb_search("series", "Startrek TNG")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["external_id"], "tt0092455")
        self.assertEqual(fetch.call_args_list[0].args[1]["query"], "Startrek TNG")
        self.assertEqual(fetch.call_args_list[1].args[1]["query"], "Star Trek The Next Generation")
        self.assertEqual(fetch.call_count, 3)

    @patch("catalog.providers.tmdb_get")
    def test_search_does_not_run_variants_if_exact_search_has_result(self, fetch):
        from catalog.providers import tmdb_search
        fetch.side_effect = [
            {"results": [{"id": 123, "title": "Taxi", "release_date": "1998-01-01"}]},
            {"imdb_id": "tt0152930"},
        ]
        hits = tmdb_search("movie", "Taxi")
        self.assertEqual(hits[0]["external_id"], "tt0152930")
        self.assertEqual(fetch.call_count, 2)

    @patch("catalog.providers.tmdb_get")
    def test_wrong_id_does_not_create_unverified_candidate(self, fetch):
        from catalog.providers import tmdb_search
        fetch.return_value = {"results": [{"id": 77, "title": "Wrong"}]}
        def reply(endpoint, params=None):
            if endpoint.startswith("/search/"):
                return {"results": [{"id": 77, "title": "Wrong"}]}
            return {"imdb_id": "not-an-imdb-id"}
        fetch.side_effect = reply
        self.assertEqual(tmdb_search("movie", "Vier Faeuste gegen Rio"), [])




class FilmAndSeriesScopeTests(SimpleTestCase):
    def test_supported_catalog_and_scanner_types(self):
        from catalog.models import Media, ScannedFile
        from catalog.management.commands.scan_media import ROOTS
        from catalog.providers import KINDS

        self.assertEqual(set(Media.Kind.values), {"movie", "series"})
        self.assertEqual(set(ScannedFile.Root.values), {"movies", "series"})
        self.assertEqual(set(ROOTS), {"movies", "series"})
        self.assertEqual(set(KINDS), {"movie", "series"})


class CoverBatchCommandTests(TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from django.test import override_settings
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(COVER_DIR=Path(self.tmp.name))
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.media = [
            Media.objects.create(
                kind="movie", title=f"Poster {n}",
                external_provider="imdb", external_id=f"tt{n:07d}",
                state=Media.State.AVAILABLE,
            ) for n in range(1, 5)
        ]

    @patch("catalog.management.commands.fetch_covers.acquire")
    def test_status_is_offline_and_counts_available(self, acquire):
        from catalog import covers
        from io import StringIO
        from django.core.management import call_command
        covers.cover_path(self.media[0].pk).write_bytes(b"RIFF")
        output = StringIO()
        call_command("fetch_covers", "--status", stdout=output)
        self.assertIn("1/4 vorhanden, 3 fehlen", output.getvalue())
        acquire.assert_not_called()

    @patch("catalog.management.commands.fetch_covers.time.sleep")
    @patch("catalog.management.commands.fetch_covers.acquire")
    def test_limit_counts_uncached_attempts(self, acquire, sleep):
        from catalog import covers
        from io import StringIO
        from django.core.management import call_command
        covers.cover_path(self.media[0].pk).write_bytes(b"RIFF")
        covers.cover_path(self.media[1].pk).write_bytes(b"RIFF")
        output = StringIO()
        call_command("fetch_covers", "--limit", "1", stdout=output)
        acquire.assert_called_once_with(self.media[2])
        self.assertIn("1 geladen", output.getvalue())
        self.assertIn("2 bereits vorhanden", output.getvalue())

    @patch("catalog.management.commands.fetch_covers.time.sleep")
    @patch("catalog.management.commands.fetch_covers.acquire")
    def test_refresh_only_attempts_requested_limit(self, acquire, sleep):
        from catalog import covers
        from io import StringIO
        from django.core.management import call_command
        covers.cover_path(self.media[0].pk).write_bytes(b"RIFF")
        call_command("fetch_covers", "--refresh", "--limit", "2", stdout=StringIO())
        self.assertEqual([args.args[0].pk for args in acquire.call_args_list],
                         [self.media[0].pk, self.media[1].pk])


class OrphanRequestCleanupTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.user = get_user_model().objects.create_user(
            username="cleanup-test-user", password="a-very-long-test-password-2026"
        )
        self.media = Media.objects.create(
            title="Only a test request", kind=Media.Kind.MOVIE,
            external_provider="imdb", external_id="tt8765432",
            state=Media.State.REQUESTED,
        )
        self.request = MediaRequest.objects.create(media=self.media, user=self.user)

    def test_delete_last_request_cleans_unlinked_requested_media(self):
        from catalog.request_cleanup import remove_requests_and_unused_media
        from catalog.models import Notification
        Notification.objects.create(
            title="Test", body="Test",
            target_path=f"/admin/catalog/mediarequest/{self.request.pk}/change/",
        )
        removed_requests, removed_media = remove_requests_and_unused_media([self.request.pk])
        self.assertEqual((removed_requests, removed_media), (1, 1))
        self.assertFalse(Media.objects.filter(pk=self.media.pk).exists())
        self.assertFalse(Notification.objects.exists())

    def test_keep_media_if_another_request_exists(self):
        from catalog.request_cleanup import remove_requests_and_unused_media
        from django.contrib.auth import get_user_model
        another = get_user_model().objects.create_user(
            username="other-cleanup-user", password="another-long-test-password-2026"
        )
        other_request = MediaRequest.objects.create(media=self.media, user=another)
        self.assertEqual(remove_requests_and_unused_media([self.request.pk]), (1, 0))
        self.assertTrue(Media.objects.filter(pk=self.media.pk).exists())
        self.assertTrue(MediaRequest.objects.filter(pk=other_request.pk).exists())

    def test_keep_media_if_linked_to_nas_inventory(self):
        from catalog.request_cleanup import remove_requests_and_unused_media
        ScannedFile.objects.create(
            root="movies", relative_path="protected/movie.mkv", media_kind="movie",
            suggested_title="Only a test request", file_size=1, modified_ns=1,
            last_seen_at=timezone.now(), media=self.media,
        )
        self.assertEqual(remove_requests_and_unused_media([self.request.pk]), (1, 0))
        self.assertTrue(Media.objects.filter(pk=self.media.pk).exists())

    def test_keep_approved_media(self):
        from catalog.request_cleanup import remove_requests_and_unused_media
        self.media.state = Media.State.AVAILABLE
        self.media.save(update_fields=["state"])
        self.assertEqual(remove_requests_and_unused_media([self.request.pk]), (1, 0))
        self.assertTrue(Media.objects.filter(pk=self.media.pk).exists())

    def test_command_is_dry_run_until_explicit_apply(self):
        from django.core.management import call_command
        from io import StringIO
        self.request.delete()
        output = StringIO()
        call_command("cleanup_orphan_requests", stdout=output)
        self.assertIn("Nur Vorschau", output.getvalue())
        self.assertTrue(Media.objects.filter(pk=self.media.pk).exists())
        call_command("cleanup_orphan_requests", "--apply", "--media-id",
                     str(self.media.pk), stdout=StringIO())
        self.assertFalse(Media.objects.filter(pk=self.media.pk).exists())


class DeleteOwnWishViewTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.alice = User.objects.create_user(
            username="delete-wish-alice", password="a-long-alice-password-for-test"
        )
        self.bob = User.objects.create_user(
            username="delete-wish-bob", password="a-long-bob-password-for-test"
        )
        self.film = Media.objects.create(
            kind="movie", title="Film zum Abbestellen", year=2024,
            external_provider="imdb", external_id="tt9999888",
            state=Media.State.REQUESTED,
        )
        self.wish = MediaRequest.objects.create(media=self.film, user=self.alice)
        self.url = reverse("delete_my_request", args=[self.wish.pk])

    def test_anonymous_user_must_log_in(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url).status_code, 302)

    def test_other_user_cannot_view_or_delete_wish(self):
        self.client.force_login(self.bob)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertTrue(MediaRequest.objects.filter(pk=self.wish.pk).exists())

    def test_get_is_only_confirmation_and_list_has_delete_link(self):
        self.client.force_login(self.alice)
        self.assertContains(self.client.get(reverse("my_requests")), self.url)
        self.assertContains(self.client.get(self.url), "Anfrage endgültig löschen")
        self.assertTrue(MediaRequest.objects.filter(pk=self.wish.pk).exists())

    def test_post_deletes_own_wish_and_unused_requested_media(self):
        self.client.force_login(self.alice)
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("my_requests"))
        self.assertFalse(MediaRequest.objects.filter(pk=self.wish.pk).exists())
        self.assertFalse(Media.objects.filter(pk=self.film.pk).exists())

    def test_post_preserves_shared_wish_and_media(self):
        bob_wish = MediaRequest.objects.create(media=self.film, user=self.bob)
        self.client.force_login(self.alice)
        self.assertRedirects(self.client.post(self.url), reverse("my_requests"))
        self.assertTrue(Media.objects.filter(pk=self.film.pk).exists())
        self.assertTrue(MediaRequest.objects.filter(pk=bob_wish.pk).exists())

    def test_post_preserves_available_media(self):
        self.film.state = Media.State.AVAILABLE
        self.film.save(update_fields=["state"])
        self.client.force_login(self.alice)
        self.assertRedirects(self.client.post(self.url), reverse("my_requests"))
        self.assertTrue(Media.objects.filter(pk=self.film.pk).exists())


class TMDBDetailCacheTests(TestCase):
    def setUp(self):
        from catalog.models import Media
        self.movie = Media.objects.create(
            kind="movie", title="Details Example", year=2020,
            external_provider="imdb", external_id="tt1234567",
            state=Media.State.AVAILABLE,
        )

    @patch("catalog.tmdb_details.tmdb_get")
    def test_fetch_saves_german_metadata_without_modifying_media(self, provider):
        from catalog.tmdb_details import fetch, current
        from catalog.models import MediaDetails
        provider.side_effect = [
            {"movie_results": [{"id": 42}]},
            {
                "id": 42, "overview": "Eine deutsche Beschreibung.",
                "genres": [{"id": 1, "name": "Drama"}, {"id": 2, "name": "Action"}],
                "runtime": 109, "vote_average": 8.24, "vote_count": 100,
            },
        ]
        cache = fetch(self.movie)
        self.assertEqual(cache.imdb_id, "tt1234567")
        self.assertEqual(cache.tmdb_id, 42)
        self.assertEqual(cache.runtime_minutes, 109)
        self.assertEqual(cache.genres, ["Drama", "Action"])
        self.assertEqual(str(cache.rating), "8.2")
        self.assertEqual(MediaDetails.objects.count(), 1)
        self.assertEqual(current(self.movie).synopsis, "Eine deutsche Beschreibung.")
        self.movie.refresh_from_db()
        self.assertEqual((self.movie.year, self.movie.title, self.movie.external_id),
                         (2020, "Details Example", "tt1234567"))
        self.assertEqual(provider.call_args_list[0].args[0], "/find/tt1234567")
        self.assertEqual(provider.call_args_list[1].args[0], "/movie/42")

    @patch("catalog.tmdb_details.tmdb_get")
    def test_ambiguous_find_rejected_without_storing(self, provider):
        from catalog.tmdb_details import fetch, DetailsUnavailable
        from catalog.models import MediaDetails
        provider.return_value = {"movie_results": [{"id": 1}, {"id": 2}]}
        with self.assertRaises(DetailsUnavailable):
            fetch(self.movie)
        self.assertFalse(MediaDetails.objects.exists())
        provider.assert_called_once()

    @patch("catalog.tmdb_details.tmdb_get")
    def test_mismatched_detail_response_rejected(self, provider):
        from catalog.tmdb_details import fetch, DetailsUnavailable
        provider.side_effect = [
            {"movie_results": [{"id": 42}]},
            {"id": 33, "overview": "Wrong film"},
        ]
        with self.assertRaises(DetailsUnavailable):
            fetch(self.movie)
        from catalog.models import MediaDetails
        self.assertFalse(MediaDetails.objects.filter(media=self.movie).exists())

    @patch("catalog.tmdb_details.tmdb_get")
    def test_series_uses_tv_and_episode_runtime(self, provider):
        from catalog.tmdb_details import fetch
        self.movie.kind = "series"
        self.movie.save(update_fields=["kind"])
        provider.side_effect = [
            {"tv_results": [{"id": 73}]},
            {"id": 73, "overview": "", "genres": [],
             "episode_run_time": [43, 48], "vote_count": 0, "vote_average": 0},
        ]
        details = fetch(self.movie)
        self.assertEqual(details.runtime_minutes, 43)
        self.assertIsNone(details.rating)
        self.assertEqual(provider.call_args_list[1].args[0], "/tv/73")

    @patch("catalog.tmdb_details.tmdb_get")
    def test_cache_hidden_when_identity_changes_or_expires(self, provider):
        from catalog.tmdb_details import fetch, current, TTL
        from catalog.models import MediaDetails
        from django.utils import timezone
        cache = MediaDetails.objects.create(
            media=self.movie, imdb_id=self.movie.external_id, tmdb_id=123,
            synopsis="Geschützt", fetched_at=timezone.now() - TTL - timedelta(days=1),
        )
        self.assertIsNone(current(self.movie))
        cache.fetched_at = timezone.now()
        cache.save(update_fields=["fetched_at"])
        self.movie.external_id = "tt7654321"
        self.movie.save(update_fields=["external_id"])
        self.assertIsNone(current(self.movie))
        self.client.get(reverse("media_detail", args=[self.movie.pk]))
        provider.assert_not_called()

    @patch("catalog.tmdb_details.tmdb_get")
    def test_page_shows_local_cache_without_network(self, provider):
        from catalog.models import MediaDetails
        MediaDetails.objects.create(
            media=self.movie, imdb_id=self.movie.external_id, tmdb_id=55,
            synopsis="Nur lokale Handlung", genres=["Abenteuer"],
            runtime_minutes=111, rating="7.5", fetched_at=timezone.now(),
        )
        response = self.client.get(reverse("media_detail", args=[self.movie.pk]))
        self.assertContains(response, "Nur lokale Handlung")
        self.assertContains(response, "Abenteuer")
        self.assertContains(response, "111 Minuten")
        self.assertContains(response, "7,5")
        provider.assert_not_called()

    @patch("catalog.management.commands.fetch_details.fetch")
    def test_management_command_status_is_offline_and_batches_are_resumable(self, fetch_details):
        from django.core.management import call_command
        from io import StringIO
        from catalog.models import MediaDetails
        MediaDetails.objects.create(
            media=self.movie, imdb_id=self.movie.external_id, tmdb_id=55,
            fetched_at=timezone.now(),
        )
        out = StringIO()
        call_command("fetch_details", "--status", stdout=out)
        self.assertIn("1/1 aktuell", out.getvalue())
        fetch_details.assert_not_called()
        call_command("fetch_details", "--limit", "2", stdout=StringIO())
        fetch_details.assert_not_called()

    @patch("catalog.management.commands.fetch_details.time.sleep")
    @patch("catalog.management.commands.fetch_details.fetch")
    def test_command_attempts_missing_cache(self, fetch_details, sleep):
        from django.core.management import call_command
        from io import StringIO
        call_command("fetch_details", "--limit", "1", stdout=StringIO())
        fetch_details.assert_called_once()
        self.assertEqual(fetch_details.call_args.args[0].pk, self.movie.pk)


class DetailPresentationTests(TestCase):
    def setUp(self):
        self.media = Media.objects.create(
            kind=Media.Kind.MOVIE, title="Privater Beispieltitel",
            year=2024, external_provider="imdb", external_id="tt9876543",
            state=Media.State.AVAILABLE,
        )

    def test_detail_page_shows_cached_tmdb_values_without_remote_assets(self):
        from catalog.models import MediaDetails
        MediaDetails.objects.create(
            media=self.media, imdb_id="tt9876543", tmdb_id=123,
            synopsis="Lokale Beschreibung für diesen Film.",
            genres=["Drama", "Thriller"],
            runtime_minutes=104, rating="7.6", fetched_at=timezone.now(),
        )
        with patch("catalog.tmdb_details.tmdb_get") as provider:
            response = self.client.get(reverse("media_detail", args=[self.media.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Lokale Beschreibung für diesen Film.")
        self.assertContains(response, "Drama")
        self.assertContains(response, "Thriller")
        self.assertContains(response, "104 Minuten")
        self.assertContains(response, "TMDB-Bewertung")
        self.assertContains(response, "Katalogkennung")
        self.assertContains(response, "Cover werden ausschließlich von media45 lokal ausgeliefert.")
        self.assertNotContains(response, "image.tmdb.org")
        provider.assert_not_called()

    def test_detail_without_metadata_has_clear_fallback(self):
        response = self.client.get(reverse("media_detail", args=[self.media.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "noch keine Inhaltsbeschreibung")
        self.assertContains(response, "Noch keine lokal gespeicherten TMDB-Details.")
        self.assertContains(response, "Privater Beispieltitel")


class PWAInstallTests(TestCase):
    def test_manifest_is_installable_and_contains_no_private_data(self):
        response = self.client.get(reverse("pwa_manifest"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertTrue(response["Content-Type"].startswith("application/manifest+json"))
        data = response.json()
        self.assertEqual(data["start_url"], "/")
        self.assertEqual(data["display"], "standalone")
        self.assertEqual({x["sizes"] for x in data["icons"] if x["type"] == "image/png"},
                         {"192x192", "512x512"})
        self.assertNotIn("https://", response.content.decode())
        self.assertNotIn("token", response.content.decode().lower())

    def test_worker_never_caches_catalog_responses(self):
        response = self.client.get(reverse("pwa_service_worker"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertIn("no-store", response["Cache-Control"])
        script = response.content.decode()
        self.assertNotIn('addEventListener("fetch"', script)
        self.assertNotIn("cache.put(", script)
        self.assertNotIn("cache.addAll(", script)

    def test_page_includes_install_manifest_and_script(self):
        response = self.client.get(reverse("index"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rel="manifest"')
        self.assertContains(response, 'rel="apple-touch-icon"')
        self.assertContains(response, "service-worker.js")
        self.assertContains(response, 'id="install-media45"')
        self.assertContains(response, 'id="pwa-install-hint"')
        self.assertContains(response, 'id="dismiss-media45-install"')
        self.assertContains(response, '(display-mode: standalone)')
        self.assertContains(response, 'media45-install-dismissed')
        markup = response.content.decode()
        self.assertLess(markup.index("</header>"), markup.index('id="install-media45"'))



class MobileNavigationTests(TestCase):
    def test_anonymous_mobile_menu_shows_only_public_links(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, '<details class="mobile-menu">')
        self.assertContains(response, 'aria-label="Mobile Hauptnavigation"')
        self.assertContains(response, "<summary")
        self.assertContains(response, "Menü</summary>")
        self.assertContains(response, 'class="desktop-nav"')
        self.assertNotContains(response, 'href="/admin/"')
        self.assertNotContains(response, 'href="/account/passkeys/"')
        self.assertNotContains(response, "Meine Wünsche")
        self.assertContains(response, 'href="/login/"')

    def test_authenticated_mobile_menu_exposes_account_links_and_post_logout(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(
            username="mobile-nav-user", password="a-long-enough-test-password-2026",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("index"))
        self.assertContains(response, "Mobile Hauptnavigation")
        self.assertContains(response, "Meine Wünsche")
        self.assertContains(response, "Passkeys")
        self.assertContains(response, 'action="/logout/" method="post"', count=2)
        self.assertNotContains(response, 'href="/admin/"')
        self.assertNotContains(response, "Freigaben")

    def test_staff_only_links_are_conditionally_visible(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(
            username="mobile-nav-admin", password="a-long-enough-test-password-2026",
            is_staff=True, is_superuser=True,
        )
        self.client.force_login(user)
        # Staff users may be redirected to mandatory 2FA setup; inspect the
        # template directly with an authenticated request context instead.
        from django.template.loader import render_to_string
        from django.test import RequestFactory
        request = RequestFactory().get("/")
        request.user = user
        markup = render_to_string("catalog/base.html", {"user": user}, request=request)
        self.assertIn('href="/admin/"', markup)
        self.assertIn("Freigaben", markup)
        self.assertIn('aria-label="Mobile Hauptnavigation"', markup)


class CatalogAdvancedFilterTests(TestCase):
    def setUp(self):
        from catalog.models import MediaDetails
        self.drama = Media.objects.create(
            kind="movie", title="Dramatest", year=2020, state="available",
            external_provider="imdb", external_id="tt1200001",
        )
        self.scifi = Media.objects.create(
            kind="series", title="Sciencefictiontest", year=2024, state="available",
            external_provider="imdb", external_id="tt1200002",
        )
        self.unrated = Media.objects.create(
            kind="movie", title="Ohne Bewertung", year=2024, state="requested",
            external_provider="imdb", external_id="tt1200003",
        )
        MediaDetails.objects.create(
            media=self.drama, imdb_id="tt1200001", tmdb_id=1001,
            genres=["Drama"], synopsis="Lokale Beschreibung", rating="8.4",
            fetched_at=timezone.now(),
        )
        MediaDetails.objects.create(
            media=self.scifi, imdb_id="tt1200002", tmdb_id=1002,
            genres=["Science Fiction", "Drama"], rating="7.2",
            fetched_at=timezone.now(),
        )

    def test_genre_filter_reads_only_local_metadata(self):
        with patch("catalog.providers.tmdb_get") as provider:
            response = self.client.get(reverse("index"), {"genre": "Science Fiction"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([m.pk for m in response.context["page_obj"]], [self.scifi.pk])
        self.assertContains(response, 'value="Science Fiction" selected')
        provider.assert_not_called()

    def test_year_rating_and_kind_filters_compose(self):
        response = self.client.get(reverse("index"), {
            "year": "2020", "rating": "8", "genre": "Drama", "kind": "movie",
            "state": "available",
        })
        self.assertEqual([m.pk for m in response.context["page_obj"]], [self.drama.pk])
        self.assertContains(response, '<details class="advanced-filters" open>')
        response = self.client.get(reverse("index"), {
            "year": "2024", "rating": "8", "genre": "Drama",
        })
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_rating_sort_keeps_unrated_titles_at_bottom(self):
        response = self.client.get(reverse("index"), {"sort": "rating"})
        titles = [m.title for m in response.context["page_obj"]]
        self.assertEqual(titles, ["Dramatest", "Sciencefictiontest", "Ohne Bewertung"])

    def test_stale_or_mismatched_metadata_does_not_filter(self):
        from catalog.models import MediaDetails
        from datetime import timedelta
        cache = MediaDetails.objects.get(media=self.drama)
        cache.fetched_at = timezone.now() - timedelta(days=91)
        cache.save(update_fields=["fetched_at"])
        response = self.client.get(reverse("index"), {"genre": "Drama", "rating": "8"})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)
        cache.fetched_at = timezone.now()
        cache.imdb_id = "tt9999000"
        cache.save(update_fields=["fetched_at", "imdb_id"])
        response = self.client.get(reverse("index"), {"genre": "Drama", "rating": "8"})
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_invalid_filter_values_are_ignored(self):
        response = self.client.get(reverse("index"), {
            "genre": "not-a-real-genre", "year": "999999999999",
            "rating": "999", "sort": "unsupported",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 3)
        self.assertEqual(response.context["sort"], "title")
        self.assertEqual(response.context["genre"], "")
        self.assertIsNone(response.context["year"])
