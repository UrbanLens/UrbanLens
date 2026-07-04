"""DB-backed tests for small queryset methods across multiple models.

Covers: CommentQuerySet, PinMarkupQuerySet, VisitQuerySet, SocialLinkQuerySet,
NotificationQuerySet, and SiteSettings.get_current().

Each test creates minimal fixture data via baker and exercises the queryset
filter methods, verifying inclusion/exclusion semantics.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

from django.contrib.auth.models import User
from urbanlens.core.tests.testcase import TestCase
from model_bakery import baker

from urbanlens.dashboard.models.comments.queryset import CommentQuerySet
from urbanlens.dashboard.models.markup.queryset import PinMarkupQuerySet
from urbanlens.dashboard.models.notifications.meta.status import Status
from urbanlens.dashboard.models.social_link.queryset import QuerySet as SocialLinkQuerySet
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.models.visits.queryset import VisitQuerySet


# -- CommentQuerySet ------------------------------------------------------------

class CommentQuerySetTopLevelTests(TestCase):
    """top_level() returns only comments with no parent."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.top = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            location=self.location,
            parent=None,
        )
        self.reply = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            location=self.location,
            parent=self.top,
        )

    def test_top_level_includes_root_comment(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        qs = Comment.objects.top_level()
        self.assertIn(self.top, qs)

    def test_top_level_excludes_replies(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        qs = Comment.objects.top_level()
        self.assertNotIn(self.reply, qs)

    def test_reply_survives_parent_delete_and_becomes_top_level(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment

        reply_pk = self.reply.pk
        self.top.delete()

        qs = Comment.objects.top_level()
        reply = Comment.objects.get(pk=reply_pk)
        self.assertIsNone(reply.parent)
        self.assertIn(reply, qs)


class CommentQuerySetForPinTests(TestCase):
    """for_pin() returns top-level comments for a specific pin."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude=41.0, longitude=-73.0)
        self.pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        self.other_pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        self.comment = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            pin=self.pin,
            parent=None,
        )
        self.other_comment = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            pin=self.other_pin,
            parent=None,
        )

    def test_for_pin_includes_matching_comment(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        qs = Comment.objects.for_pin(self.pin)
        self.assertIn(self.comment, qs)

    def test_for_pin_excludes_other_pin_comment(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        qs = Comment.objects.for_pin(self.pin)
        self.assertNotIn(self.other_comment, qs)


class CommentQuerySetForLocationTests(TestCase):
    """for_location() returns top-level comments for a specific location."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.loc1 = baker.make("dashboard.Location", latitude=42.0, longitude=-72.0)
        self.loc2 = baker.make("dashboard.Location", latitude=43.0, longitude=-71.0)
        self.c1 = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            location=self.loc1,
            parent=None,
        )
        self.c2 = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            location=self.loc2,
            parent=None,
        )

    def test_for_location_returns_matching_comment(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        qs = Comment.objects.for_location(self.loc1)
        self.assertIn(self.c1, qs)
        self.assertNotIn(self.c2, qs)


class TripCommentDeleteTests(TestCase):
    """Deleting a trip comment parent preserves its replies."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.trip = baker.make("dashboard.Trip", creator=self.user.profile)
        self.top = baker.make(
            "dashboard.TripComment",
            trip=self.trip,
            author=self.user.profile,
            parent=None,
        )
        self.reply = baker.make(
            "dashboard.TripComment",
            trip=self.trip,
            author=self.user.profile,
            parent=self.top,
        )

    def test_reply_survives_parent_delete_and_becomes_top_level(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripComment

        reply_pk = self.reply.pk
        self.top.delete()

        reply = TripComment.objects.get(pk=reply_pk)
        self.assertIsNone(reply.parent)
        self.assertIn(reply, self.trip.comments.filter(parent__isnull=True))


# -- PinMarkupQuerySet ---------------------------------------------------------

class PinMarkupQuerySetTests(TestCase):
    """for_pin() and for_profile() filter markup by parent and owner."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude=44.0, longitude=-70.0)
        self.pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        self.other_pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        self.markup = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=self.user.profile,
        )
        self.other_markup = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.other_pin,
            profile=self.user.profile,
        )

    def test_for_pin_returns_markup_for_that_pin(self) -> None:
        from urbanlens.dashboard.models.markup.model import PinMarkup
        qs = PinMarkup.objects.for_pin(self.pin)
        self.assertIn(self.markup, qs)
        self.assertNotIn(self.other_markup, qs)

    def test_for_profile_returns_markup_for_that_profile(self) -> None:
        from urbanlens.dashboard.models.markup.model import PinMarkup
        other_user: User = baker.make(User)
        other_markup: PinMarkup = baker.make(
            "dashboard.PinMarkup",
            parent_pin=self.pin,
            profile=other_user.profile,
        )
        qs = PinMarkup.objects.for_profile(self.user.profile)
        self.assertIn(self.markup, qs)
        self.assertNotIn(other_markup, qs)


# -- VisitQuerySet -------------------------------------------------------------

class VisitQuerySetTests(TestCase):
    """for_pin(), manual(), and from_takeout() filter visits correctly."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude=45.0, longitude=-69.0)
        self.pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        self.other_pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        ts = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.manual_visit = baker.make(
            "dashboard.PinVisit",
            pin=self.pin,
            source=VisitSource.MANUAL,
            visited_at=ts,
        )
        self.takeout_visit = baker.make(
            "dashboard.PinVisit",
            pin=self.pin,
            source=VisitSource.GOOGLE_TAKEOUT,
            visited_at=ts,
        )
        self.other_pin_visit = baker.make(
            "dashboard.PinVisit",
            pin=self.other_pin,
            source=VisitSource.MANUAL,
            visited_at=ts,
        )

    def test_for_pin_returns_only_that_pins_visits(self) -> None:
        qs = PinVisit.objects.for_pin(self.pin.pk)
        self.assertIn(self.manual_visit, qs)
        self.assertIn(self.takeout_visit, qs)
        self.assertNotIn(self.other_pin_visit, qs)

    def test_manual_returns_only_manual_visits(self) -> None:
        qs = PinVisit.objects.filter(pin=self.pin).manual()
        self.assertIn(self.manual_visit, qs)
        self.assertNotIn(self.takeout_visit, qs)

    def test_from_takeout_returns_only_takeout_visits(self) -> None:
        qs = PinVisit.objects.filter(pin=self.pin).from_takeout()
        self.assertIn(self.takeout_visit, qs)
        self.assertNotIn(self.manual_visit, qs)


# -- SocialLinkQuerySet --------------------------------------------------------

class SocialLinkQuerySetTests(TestCase):
    """for_profile() with Profile or int, and platform() filter correctly."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.other_user = baker.make("auth.User")
        self.ig_link = baker.make(
            "dashboard.SocialLink",
            profile=self.user.profile,
            platform="instagram",
            handle="user_ig",
        )
        self.tw_link = baker.make(
            "dashboard.SocialLink",
            profile=self.user.profile,
            platform="twitter",
            handle="user_tw",
        )
        self.other_link = baker.make(
            "dashboard.SocialLink",
            profile=self.other_user.profile,
            platform="instagram",
            handle="other_ig",
        )

    def test_for_profile_with_profile_instance(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink
        qs = SocialLink.objects.for_profile(self.user.profile)
        self.assertIn(self.ig_link, qs)
        self.assertIn(self.tw_link, qs)
        self.assertNotIn(self.other_link, qs)

    def test_for_profile_with_int_pk(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink
        qs = SocialLink.objects.for_profile(self.user.profile.pk)
        self.assertIn(self.ig_link, qs)
        self.assertNotIn(self.other_link, qs)

    def test_platform_filters_by_platform_string(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink
        qs = SocialLink.objects.for_profile(self.user.profile).platform("instagram")
        self.assertIn(self.ig_link, qs)
        self.assertNotIn(self.tw_link, qs)

    def test_platform_empty_when_no_match(self) -> None:
        from urbanlens.dashboard.models.social_link.model import SocialLink
        qs = SocialLink.objects.for_profile(self.user.profile).platform("nonexistent")
        self.assertFalse(qs.exists())


# -- NotificationQuerySet -------------------------------------------------------

class NotificationQuerySetTests(TestCase):
    """unread(), for_profile(), and mark_read() behave correctly."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.other_user = baker.make("auth.User")
        self.unread_notif = baker.make(
            "dashboard.NotificationLog",
            profile=self.user.profile,
            status=Status.UNREAD,
        )
        self.read_notif = baker.make(
            "dashboard.NotificationLog",
            profile=self.user.profile,
            status=Status.READ,
        )
        self.other_notif = baker.make(
            "dashboard.NotificationLog",
            profile=self.other_user.profile,
            status=Status.UNREAD,
        )

    def test_for_profile_returns_own_notifications(self) -> None:
        from urbanlens.dashboard.models.notifications.model import NotificationLog
        qs = NotificationLog.objects.for_profile(self.user.profile)
        self.assertIn(self.unread_notif, qs)
        self.assertIn(self.read_notif, qs)
        self.assertNotIn(self.other_notif, qs)

    def test_unread_returns_unread_notifications(self) -> None:
        from urbanlens.dashboard.models.notifications.model import NotificationLog
        qs = NotificationLog.objects.for_profile(self.user.profile).unread()
        self.assertIn(self.unread_notif, qs)
        self.assertNotIn(self.read_notif, qs)

    def test_mark_read_updates_status_to_read(self) -> None:
        from urbanlens.dashboard.models.notifications.model import NotificationLog
        count = NotificationLog.objects.for_profile(self.user.profile).unread().mark_read()
        self.assertGreaterEqual(count, 1)
        self.unread_notif.refresh_from_db()
        self.assertEqual(self.unread_notif.status, Status.READ)

    def test_mark_read_returns_updated_count(self) -> None:
        from urbanlens.dashboard.models.notifications.model import NotificationLog
        count = NotificationLog.objects.for_profile(self.user.profile).unread().mark_read()
        self.assertEqual(count, 1)


# -- SiteSettings singleton -----------------------------------------------------

class SiteSettingsSingletonTests(TestCase):
    """SiteSettings.get_current() creates and returns a singleton record."""

    def setUp(self):
        SiteSettings.objects.filter(pk=1).delete()

    def test_get_current_creates_record_when_missing(self) -> None:
        self.assertFalse(SiteSettings.objects.filter(pk=1).exists())
        result = SiteSettings.get_current()
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, 1)

    def test_get_current_returns_existing_record_on_second_call(self) -> None:
        first = SiteSettings.get_current()
        second = SiteSettings.get_current()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SiteSettings.objects.filter(pk=1).count(), 1)

    def test_get_current_str_is_site_settings(self) -> None:
        result = SiteSettings.get_current()
        self.assertEqual(str(result), "Site Settings")

    def test_get_current_has_sensible_defaults(self) -> None:
        result = SiteSettings.get_current()
        self.assertGreater(result.max_trip_members, 0)
        self.assertGreater(result.max_bbox_area_km2, 0)


# -- TDD: Status enum values are swapped (bug demonstration) -------------------

class NotificationStatusBugTests(TestCase):
    """Status enum member names must match their stored database values."""

    def test_unread_value_should_be_unread(self) -> None:
        """Status.UNREAD.value should be 'unread', not 'read'."""
        self.assertEqual(Status.UNREAD.value, "unread")

    def test_read_value_should_be_read(self) -> None:
        """Status.READ.value should be 'read', not 'unread'."""
        self.assertEqual(Status.READ.value, "read")
