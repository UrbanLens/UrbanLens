"""Tests for achievements: streak arithmetic, metric evaluation, awarding, and views."""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.urls import reverse
from hypothesis import given, settings as hypothesis_settings, strategies as st
from model_bakery import baker
import pytest

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.achievements.meta import ActivityKind, streak_metric_key
from urbanlens.dashboard.models.achievements.model import Achievement, ProfileActivityDay, ProfileStreak, UserAchievement
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.markup.model import MarkupMap
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.reviews.model import Review
from urbanlens.dashboard.services.achievements.activity import rebuild_streak, record_activity
from urbanlens.dashboard.services.achievements.evaluate import (
    evaluate_achievement_for_all,
    evaluate_profile,
    progress_for_profile,
)
from urbanlens.dashboard.services.achievements.metrics import all_metrics, compute_values, get_metric, metrics_for_triggers


class AchievementTestsBase(TestCase):
    """Shared fixture: a logged-in user with a profile."""

    def setUp(self) -> None:
        baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _achievement(self, metric: str = "pins_created", threshold: int = 3, **kwargs) -> Achievement:
        defaults = {"name": f"{metric} x{threshold}", "metric": metric, "threshold": threshold}
        defaults.update(kwargs)
        return Achievement.objects.create(**defaults)


class MetricRegistryTests(SimpleTestCase):
    """The registry is the vocabulary admins pick from - it must stay coherent."""

    def test_every_requested_metric_is_registered(self) -> None:
        """Every metric the feature promises an admin can use exists by key."""
        expected = {
            "pins_created",
            "wikis_created",
            "wiki_edits",
            "photos_uploaded",
            "markup_maps_created",
            "places_visited",
            "places_rated",
            "places_vulnerability_rated",
            "places_danger_rated",
            "trips_planned",
            "trips_attended",
            "comments_written",
            "friends",
            "people_invited",
            *(streak_metric_key(kind) for kind in ActivityKind.values),
        }
        self.assertTrue(expected.issubset({m.key for m in all_metrics()}))

    def test_metric_keys_are_unique(self) -> None:
        keys = [m.key for m in all_metrics()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_triggers_map_to_their_metrics(self) -> None:
        """A pin write must re-check pin-derived metrics and nothing unrelated."""
        affected = set(metrics_for_triggers({"pin"}))
        self.assertIn("pins_created", affected)
        self.assertIn("places_danger_rated", affected)
        self.assertNotIn("comments_written", affected)

    def test_unknown_trigger_matches_nothing(self) -> None:
        self.assertEqual(metrics_for_triggers({"not_a_real_trigger"}), [])

    def test_failing_metric_reads_as_zero(self) -> None:
        """One broken metric must not take down a whole evaluation pass."""
        from urbanlens.dashboard.services.achievements.metrics import Metric

        def explode(profile: object) -> int:
            raise RuntimeError("boom")

        metric = Metric(key="explodes", label="Explodes", unit="x", description="", compute=explode)
        self.assertEqual(metric.value_for(None), 0)


class StreakArithmeticTests(TestCase):
    """Streak counters advance, reset, and rebuild correctly."""

    def setUp(self) -> None:
        baker.make("auth.User")
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)

    def _record_days(self, days: list[datetime.date], kind: str = ActivityKind.PIN) -> ProfileStreak:
        for day in days:
            record_activity(self.profile, kind, day=day)
        return ProfileStreak.objects.get(profile=self.profile, kind=kind)

    def test_consecutive_days_extend_the_streak(self) -> None:
        start = datetime.date(2026, 3, 1)
        streak = self._record_days([start + datetime.timedelta(days=offset) for offset in range(5)])
        self.assertEqual(streak.current_length, 5)
        self.assertEqual(streak.longest_length, 5)

    def test_repeat_activity_same_day_does_not_double_count(self) -> None:
        """Fifty photos in one afternoon is one day of the streak, not fifty."""
        day = datetime.date(2026, 3, 1)
        self.assertTrue(record_activity(self.profile, ActivityKind.PHOTO, day=day))
        for _ in range(5):
            self.assertFalse(record_activity(self.profile, ActivityKind.PHOTO, day=day))
        streak = ProfileStreak.objects.get(profile=self.profile, kind=ActivityKind.PHOTO)
        self.assertEqual(streak.current_length, 1)
        self.assertEqual(ProfileActivityDay.objects.filter(profile=self.profile, kind=ActivityKind.PHOTO).count(), 1)

    def test_gap_resets_current_but_keeps_longest(self) -> None:
        start = datetime.date(2026, 3, 1)
        days = [start, start + datetime.timedelta(days=1), start + datetime.timedelta(days=2)]
        days += [start + datetime.timedelta(days=10)]
        streak = self._record_days(days)
        self.assertEqual(streak.current_length, 1)
        self.assertEqual(streak.longest_length, 3)

    def test_current_length_as_of_expires_a_stale_run(self) -> None:
        """Nothing fires on the day a user stops, so staleness is judged on read."""
        streak = ProfileStreak(current_length=7, longest_length=7, last_day=datetime.date(2026, 3, 1))
        self.assertEqual(streak.current_length_as_of(datetime.date(2026, 3, 1)), 7)
        self.assertEqual(streak.current_length_as_of(datetime.date(2026, 3, 2)), 7)
        self.assertEqual(streak.current_length_as_of(datetime.date(2026, 3, 3)), 0)

    def test_out_of_order_day_triggers_a_rebuild(self) -> None:
        """A backfilled earlier day must not be treated as extending the run."""
        start = datetime.date(2026, 3, 10)
        record_activity(self.profile, ActivityKind.PIN, day=start)
        record_activity(self.profile, ActivityKind.PIN, day=start + datetime.timedelta(days=1))
        record_activity(self.profile, ActivityKind.PIN, day=start - datetime.timedelta(days=1))

        streak = ProfileStreak.objects.get(profile=self.profile, kind=ActivityKind.PIN)
        self.assertEqual(streak.longest_length, 3)
        self.assertEqual(streak.current_length, 3)
        self.assertEqual(streak.last_day, start + datetime.timedelta(days=1))

    @given(
        gaps=st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=12),
    )
    @hypothesis_settings(max_examples=25, deadline=None)
    def test_rebuild_matches_incremental_tracking(self, gaps: list[int]) -> None:
        """However the days fall, rebuilding from the log agrees with the cache."""
        profile = Profile.objects.get(user=baker.make("auth.User"))
        day = datetime.date(2026, 1, 1)
        for gap in gaps:
            record_activity(profile, ActivityKind.COMMENT, day=day)
            day += datetime.timedelta(days=gap)

        incremental = ProfileStreak.objects.get(profile=profile, kind=ActivityKind.COMMENT)
        cached_current, cached_longest = incremental.current_length, incremental.longest_length

        rebuilt = rebuild_streak(profile, ActivityKind.COMMENT)
        self.assertEqual(rebuilt.current_length, cached_current)
        self.assertEqual(rebuilt.longest_length, cached_longest)

    def test_unknown_kind_is_ignored(self) -> None:
        self.assertFalse(record_activity(self.profile, "not_a_kind"))
        self.assertEqual(ProfileActivityDay.objects.count(), 0)


class MetricComputationTests(AchievementTestsBase):
    """Each metric counts what it claims to, and excludes what it claims to."""

    def test_pins_created_counts_own_pins(self) -> None:
        baker.make(Pin, profile=self.profile, _quantity=3)
        other = Profile.objects.get(user=baker.make("auth.User"))
        baker.make(Pin, profile=other)
        self.assertEqual(get_metric("pins_created").value_for(self.profile), 3)

    def test_photos_uploaded_excludes_external_sources(self) -> None:
        """Attaching someone else's Yelp photo is not an upload."""
        baker.make(Image, profile=self.profile, source=ImageSource.UPLOAD, _quantity=2)
        baker.make(Image, profile=self.profile, source=ImageSource.YELP)
        self.assertEqual(get_metric("photos_uploaded").value_for(self.profile), 2)

    def test_vulnerability_and_danger_are_counted_independently(self) -> None:
        baker.make(Pin, profile=self.profile, vulnerability=3, danger=0)
        baker.make(Pin, profile=self.profile, vulnerability=0, danger=2)
        baker.make(Pin, profile=self.profile, vulnerability=1, danger=1)
        baker.make(Pin, profile=self.profile, vulnerability=0, danger=0)

        self.assertEqual(get_metric("places_vulnerability_rated").value_for(self.profile), 2)
        self.assertEqual(get_metric("places_danger_rated").value_for(self.profile), 2)

    def test_places_rated_counts_star_reviews(self) -> None:
        for pin in baker.make(Pin, profile=self.profile, _quantity=2):
            Review.objects.create(profile=self.profile, pin=pin, rating=4)
        self.assertEqual(get_metric("places_rated").value_for(self.profile), 2)

    def test_comments_counted_across_pins_and_wikis(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        baker.make(Comment, profile=self.profile, pin=pin, _quantity=3)
        self.assertEqual(get_metric("comments_written").value_for(self.profile), 3)

    def test_markup_maps_counted(self) -> None:
        baker.make(MarkupMap, profile=self.profile, _quantity=2)
        self.assertEqual(get_metric("markup_maps_created").value_for(self.profile), 2)

    def test_streak_metric_reads_longest_not_current(self) -> None:
        """A broken streak keeps whatever award it earned."""
        start = datetime.date(2026, 3, 1)
        for offset in range(4):
            record_activity(self.profile, ActivityKind.LOGIN, day=start + datetime.timedelta(days=offset))
        record_activity(self.profile, ActivityKind.LOGIN, day=start + datetime.timedelta(days=30))

        self.assertEqual(get_metric(streak_metric_key(ActivityKind.LOGIN)).value_for(self.profile), 4)

    def test_compute_values_skips_unknown_keys(self) -> None:
        values = compute_values(self.profile, ["pins_created", "nonexistent_metric"])
        self.assertEqual(set(values), {"pins_created"})


class AwardingTests(AchievementTestsBase):
    """evaluate_profile grants exactly what has been qualified for, once."""

    def test_award_granted_when_threshold_reached(self) -> None:
        achievement = self._achievement(metric="pins_created", threshold=2)
        baker.make(Pin, profile=self.profile, _quantity=2)

        granted = evaluate_profile(self.profile)
        self.assertEqual([award.achievement_id for award in granted], [achievement.pk])
        self.assertEqual(granted[0].value_at_award, 2)

    def test_award_not_granted_below_threshold(self) -> None:
        self._achievement(metric="pins_created", threshold=5)
        baker.make(Pin, profile=self.profile, _quantity=4)
        self.assertEqual(evaluate_profile(self.profile), [])

    def test_award_is_not_granted_twice(self) -> None:
        self._achievement(metric="pins_created", threshold=1)
        baker.make(Pin, profile=self.profile)

        self.assertEqual(len(evaluate_profile(self.profile)), 1)
        self.assertEqual(evaluate_profile(self.profile), [])
        self.assertEqual(UserAchievement.objects.filter(profile=self.profile).count(), 1)

    def test_award_survives_the_metric_falling_back_below_threshold(self) -> None:
        """Deleting pins lowers the count but must not revoke the award."""
        self._achievement(metric="pins_created", threshold=2)
        pins = baker.make(Pin, profile=self.profile, _quantity=2)
        evaluate_profile(self.profile)

        Pin.objects.filter(pk__in=[pin.pk for pin in pins]).delete()

        self.assertEqual(UserAchievement.objects.filter(profile=self.profile).count(), 1)
        self.assertEqual(evaluate_profile(self.profile), [])

    def test_inactive_achievements_are_not_granted(self) -> None:
        self._achievement(metric="pins_created", threshold=1, is_active=False)
        baker.make(Pin, profile=self.profile)
        self.assertEqual(evaluate_profile(self.profile), [])

    def test_metric_keys_restrict_what_is_evaluated(self) -> None:
        """A comment write must not spend queries re-counting pins."""
        self._achievement(metric="pins_created", threshold=1)
        baker.make(Pin, profile=self.profile)

        self.assertEqual(evaluate_profile(self.profile, metric_keys=["comments_written"]), [])
        self.assertEqual(len(evaluate_profile(self.profile, metric_keys=["pins_created"])), 1)

    def test_tiers_of_the_same_metric_all_grant(self) -> None:
        """10/100/1000-style tiers are just several thresholds on one metric."""
        for threshold in (1, 2, 3):
            self._achievement(metric="pins_created", threshold=threshold)
        baker.make(Pin, profile=self.profile, _quantity=3)

        self.assertEqual(len(evaluate_profile(self.profile)), 3)

    def test_new_achievement_backfills_to_existing_qualifiers(self) -> None:
        """An award defined today reaches users who qualified long ago."""
        baker.make(Pin, profile=self.profile, _quantity=4)
        achievement = self._achievement(metric="pins_created", threshold=3)

        granted = evaluate_achievement_for_all(achievement)
        self.assertEqual(granted, 1)
        self.assertTrue(UserAchievement.objects.filter(profile=self.profile, achievement=achievement).exists())

    def test_backfill_skips_an_unknown_metric(self) -> None:
        achievement = Achievement.objects.create(name="Broken", metric="gone_away", threshold=1)
        self.assertEqual(evaluate_achievement_for_all(achievement), 0)

    def test_notification_raised_on_award(self) -> None:
        from urbanlens.dashboard.models.notifications.meta import NotificationType
        from urbanlens.dashboard.models.notifications.model import NotificationLog

        self._achievement(metric="pins_created", threshold=1, name="First Pin")
        baker.make(Pin, profile=self.profile)
        evaluate_profile(self.profile)

        notification = NotificationLog.objects.filter(
            profile=self.profile,
            notification_type=NotificationType.ACHIEVEMENT_EARNED,
        ).first()
        self.assertIsNotNone(notification)
        self.assertIn("First Pin", notification.title)


class SignalIntegrationTests(AchievementTestsBase):
    """The end-to-end path: a contribution lands, the award appears.

    Streak days are written synchronously inside the contribution's own
    transaction. The evaluation enqueue is deferred to ``transaction.on_commit``
    so nothing is queued against rows that may still roll back; Django's
    ``TestCase`` never commits, so award assertions drive the hook explicitly
    via ``captureOnCommitCallbacks``.
    """

    def test_creating_pins_awards_without_an_explicit_evaluation(self) -> None:
        achievement = self._achievement(metric="pins_created", threshold=2, name="Pin Pair")

        with self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile, _quantity=2)

        self.assertTrue(UserAchievement.objects.filter(profile=self.profile, achievement=achievement).exists())

    def test_pin_creation_records_a_pinning_streak_day(self) -> None:
        """Recorded even with no award defined - a streak award added later
        needs the history to reward."""
        baker.make(Pin, profile=self.profile)

        self.assertEqual(
            ProfileActivityDay.objects.filter(profile=self.profile, kind=ActivityKind.PIN).count(),
            1,
        )

    def test_external_photo_does_not_extend_the_upload_streak(self) -> None:
        """Attaching a Yelp photo creates an Image, but is not an upload."""
        baker.make(Image, profile=self.profile, source=ImageSource.YELP)
        self.assertFalse(ProfileActivityDay.objects.filter(profile=self.profile, kind=ActivityKind.PHOTO).exists())

        baker.make(Image, profile=self.profile, source=ImageSource.UPLOAD)
        self.assertTrue(ProfileActivityDay.objects.filter(profile=self.profile, kind=ActivityKind.PHOTO).exists())

    def test_editing_a_pin_does_not_add_a_second_streak_day(self) -> None:
        """Only an insert is an action taken today."""
        pin = baker.make(Pin, profile=self.profile)
        pin.description = "edited"
        pin.save()

        self.assertEqual(
            ProfileActivityDay.objects.filter(profile=self.profile, kind=ActivityKind.PIN).count(),
            1,
        )

    def test_no_award_for_a_metric_means_no_task_is_queued(self) -> None:
        """A site with no award on a metric pays nothing when it changes."""
        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile)

        queued = [call for call in enqueue.call_args_list if "achievement" in str(call)]
        self.assertEqual(queued, [])

    def test_an_award_on_the_metric_does_queue_a_task(self) -> None:
        """The counterpart to the above - gating must not disable the feature."""
        from urbanlens.dashboard.tasks import evaluate_achievements_for_profile

        self._achievement(metric="pins_created", threshold=50, name="Far Off")

        with patch("urbanlens.dashboard.services.celery.safely_enqueue_task") as enqueue, self.captureOnCommitCallbacks(execute=True):
            baker.make(Pin, profile=self.profile)

        enqueued_tasks = [call.args[0] for call in enqueue.call_args_list if call.args]
        self.assertIn(evaluate_achievements_for_profile, enqueued_tasks)

    def test_defining_an_achievement_backfills_via_signal(self) -> None:
        """Saving a new award reaches users who already qualified."""
        baker.make(Pin, profile=self.profile, _quantity=3)

        with self.captureOnCommitCallbacks(execute=True):
            achievement = self._achievement(metric="pins_created", threshold=3, name="Backfilled")

        self.assertTrue(UserAchievement.objects.filter(profile=self.profile, achievement=achievement).exists())


class AchievementModelTests(AchievementTestsBase):
    """Model-level validation and display helpers."""

    def test_unknown_metric_is_rejected(self) -> None:
        achievement = Achievement(name="Bad", metric="not_a_metric", threshold=1)
        with pytest.raises(ValidationError):
            achievement.clean()

    def test_zero_threshold_is_rejected(self) -> None:
        achievement = Achievement(name="Bad", metric="pins_created", threshold=0)
        with pytest.raises(ValidationError):
            achievement.clean()

    @given(value=st.integers(min_value=0, max_value=10_000), threshold=st.integers(min_value=1, max_value=1_000))
    @hypothesis_settings(max_examples=40, deadline=None)
    def test_progress_is_always_a_clamped_percentage(self, value: int, threshold: int) -> None:
        percent = Achievement(name="x", metric="pins_created", threshold=threshold).progress_for(value)
        self.assertGreaterEqual(percent, 0)
        self.assertLessEqual(percent, 100)

    def test_requirement_text_uses_the_metric_sentence(self) -> None:
        achievement = self._achievement(metric="photos_uploaded", threshold=25)
        self.assertEqual(achievement.requirement_text, "Upload 25 photos")

    def test_duplicate_names_get_distinct_slugs(self) -> None:
        first = self._achievement(metric="pins_created", threshold=1, name="Explorer")
        second = self._achievement(metric="pins_created", threshold=2, name="Explorer")
        self.assertNotEqual(first.slug, second.slug)


class ProgressListingTests(AchievementTestsBase):
    """progress_for_profile drives both the profile panel and the catalogue."""

    def test_owner_sees_progress_on_locked_awards(self) -> None:
        self._achievement(metric="pins_created", threshold=4)
        baker.make(Pin, profile=self.profile, _quantity=1)

        rows = progress_for_profile(self.profile, viewer=self.profile)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["earned"])
        self.assertTrue(rows[0]["show_progress"])
        self.assertEqual(rows[0]["percent"], 25)

    def test_stranger_does_not_see_progress_numbers(self) -> None:
        """Progress bars would leak exact contribution counts to any viewer."""
        self._achievement(metric="pins_created", threshold=4)
        baker.make(Pin, profile=self.profile, _quantity=3)
        stranger = Profile.objects.get(user=baker.make("auth.User"))

        rows = progress_for_profile(self.profile, viewer=stranger)
        self.assertFalse(rows[0]["show_progress"])
        self.assertEqual(rows[0]["value"], 0)

    def test_secret_awards_hidden_until_earned(self) -> None:
        secret = self._achievement(metric="pins_created", threshold=2, is_secret=True, name="Hidden")
        rows = progress_for_profile(self.profile, viewer=self.profile)
        self.assertEqual(rows, [])

        baker.make(Pin, profile=self.profile, _quantity=2)
        evaluate_profile(self.profile)

        rows = progress_for_profile(self.profile, viewer=self.profile)
        self.assertEqual([row["achievement"].pk for row in rows], [secret.pk])


class AchievementViewTests(AchievementTestsBase):
    """The profile panel and catalogue respect profile visibility."""

    def test_owner_sees_their_panel(self) -> None:
        self._achievement(metric="pins_created", threshold=1, name="First Pin")
        baker.make(Pin, profile=self.profile)
        evaluate_profile(self.profile)

        response = self.client.get(reverse("achievement.profile_panel", kwargs={"profile_slug": self.profile.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First Pin")

    def test_catalogue_page_renders(self) -> None:
        self._achievement(metric="pins_created", threshold=5, name="Pin Five")
        response = self.client.get(reverse("achievement.list", kwargs={"profile_slug": self.profile.slug}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pin Five")

    def test_hidden_profile_is_not_readable(self) -> None:
        """Achievements follow the profile's own visibility, not a looser rule."""
        other = Profile.objects.get(user=baker.make("auth.User"))
        other.profile_visibility = "private"
        other.save(update_fields=["profile_visibility"])

        response = self.client.get(reverse("achievement.profile_panel", kwargs={"profile_slug": other.slug}))
        self.assertEqual(response.status_code, 404)

    def test_anonymous_is_redirected(self) -> None:
        self.client.logout()
        response = self.client.get(reverse("achievement.profile_panel", kwargs={"profile_slug": self.profile.slug}))
        self.assertEqual(response.status_code, 302)

    def test_profile_page_still_renders_with_the_new_panel(self) -> None:
        """The panel is injected into the profile template - keep that page working."""
        response = self.client.get(reverse("profile.view"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "profile_achievements")

    def test_bare_achievements_url_redirects_to_own_catalogue(self) -> None:
        response = self.client.get(reverse("achievement.mine"))
        self.assertRedirects(
            response,
            reverse("achievement.list", kwargs={"profile_slug": self.profile.slug}),
        )


class SiteAdminAchievementViewTests(AchievementTestsBase):
    """Only site admins may define awards."""

    def setUp(self) -> None:
        super().setUp()
        self.admin_user = baker.make("auth.User", is_superuser=True, is_staff=True)
        self.admin_profile = Profile.objects.get(user=self.admin_user)

    def test_non_admin_is_refused(self) -> None:
        response = self.client.get(reverse("site_admin_achievements"))
        self.assertIn(response.status_code, (302, 403))

    def test_admin_page_renders_with_the_metric_reference(self) -> None:
        self.client.force_login(self.admin_user)
        self._achievement(metric="pins_created", threshold=5, name="Existing Award")

        response = self.client.get(reverse("site_admin_achievements"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Existing Award")
        self.assertContains(response, "Create an achievement")
        # The metric reference documents each option in the dropdown.
        self.assertContains(response, "Places rated (vulnerability)")

    def test_admin_can_create_an_achievement(self) -> None:
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("site_admin_achievements"),
            {"name": "Shutterbug", "metric": "photos_uploaded", "threshold": "10", "icon": "photo_camera", "is_active": "on", "order": "0"},
        )
        self.assertEqual(response.status_code, 200)
        achievement = Achievement.objects.get(name="Shutterbug")
        self.assertEqual(achievement.metric, "photos_uploaded")
        self.assertEqual(achievement.threshold, 10)

    def test_invalid_threshold_is_reported_not_saved(self) -> None:
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("site_admin_achievements"),
            {"name": "Bad", "metric": "photos_uploaded", "threshold": "zero"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Achievement.objects.filter(name="Bad").exists())

    def test_missing_name_is_reported(self) -> None:
        self.client.force_login(self.admin_user)
        response = self.client.post(
            reverse("site_admin_achievements"),
            {"name": "  ", "metric": "photos_uploaded", "threshold": "3"},
        )
        self.assertEqual(response.status_code, 400)

    def test_admin_can_edit_and_delete(self) -> None:
        self.client.force_login(self.admin_user)
        achievement = self._achievement(metric="pins_created", threshold=2, name="Original")

        self.client.post(
            reverse("site_admin_achievement_edit", kwargs={"achievement_id": achievement.pk}),
            {"name": "Renamed", "metric": "pins_created", "threshold": "4", "is_active": "on", "order": "1"},
        )
        achievement.refresh_from_db()
        self.assertEqual(achievement.name, "Renamed")
        self.assertEqual(achievement.threshold, 4)

        self.client.delete(reverse("site_admin_achievement_edit", kwargs={"achievement_id": achievement.pk}))
        self.assertFalse(Achievement.objects.filter(pk=achievement.pk).exists())

    def test_backfill_endpoint_grants_to_qualifiers(self) -> None:
        baker.make(Pin, profile=self.profile, _quantity=3)
        achievement = self._achievement(metric="pins_created", threshold=3, name="Trio")
        UserAchievement.objects.all().delete()

        self.client.force_login(self.admin_user)
        response = self.client.post(reverse("site_admin_achievement_backfill", kwargs={"achievement_id": achievement.pk}))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(UserAchievement.objects.filter(profile=self.profile, achievement=achievement).exists())
