"""DB-backed tests for small queryset methods across multiple models.

Covers: CommentQuerySet, PinMarkupQuerySet, VisitQuerySet, SocialLinkQuerySet,
NotificationQuerySet, SiteSettings.get_current(), and
ProfileNote/ProfileNickname/ProfileTrust's for_pair().

Each test creates minimal fixture data via baker and exercises the queryset
filter methods, verifying inclusion/exclusion semantics.
"""
from __future__ import annotations

from datetime import UTC, datetime, timezone
from typing import TYPE_CHECKING
import unittest

from django.contrib.auth.models import User
from model_bakery import baker

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.queryset import CommentQuerySet
from urbanlens.dashboard.models.markup.queryset import PinMarkupQuerySet
from urbanlens.dashboard.models.notifications.meta.status import Status
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.social_link.queryset import SocialLinkQuerySet as SocialLinkQuerySet
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.models.visits.queryset import VisitQuerySet

# -- CommentQuerySet ------------------------------------------------------------

class CommentQuerySetTopLevelTests(TestCase):
    """top_level() returns only comments with no parent."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude=40.0, longitude=-74.0)
        self.wiki = baker.make("dashboard.Wiki", location=self.location)
        self.top = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            wiki=self.wiki,
            parent=None,
        )
        self.reply = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            wiki=self.wiki,
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
        self.other_location = baker.make("dashboard.Location", latitude=41.1, longitude=-73.1)
        self.pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        self.other_pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.other_location)
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


class CommentQuerySetForWikiTests(TestCase):
    """for_wiki() returns top-level comments for a specific wiki."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.wiki1 = baker.make("dashboard.Wiki")
        self.wiki2 = baker.make("dashboard.Wiki")
        self.c1 = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            wiki=self.wiki1,
            parent=None,
        )
        self.c2 = baker.make(
            "dashboard.Comment",
            profile=self.user.profile,
            wiki=self.wiki2,
            parent=None,
        )

    def test_for_wiki_returns_matching_comment(self) -> None:
        from urbanlens.dashboard.models.comments.model import Comment
        qs = Comment.objects.for_wiki(self.wiki1)
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


class TripCommentQuerySetByAuthorTests(TestCase):
    """by_author() returns a profile's comments across trips, most recent first."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.other_user = baker.make("auth.User")
        self.trip = baker.make("dashboard.Trip", creator=self.user.profile)
        self.other_trip = baker.make("dashboard.Trip", creator=self.other_user.profile)

    def test_by_author_includes_own_comments_only(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripComment

        mine: TripComment = baker.make("dashboard.TripComment", trip=self.trip, author=self.user.profile)
        baker.make("dashboard.TripComment", trip=self.other_trip, author=self.other_user.profile)

        qs = TripComment.objects.by_author(self.user.profile)
        self.assertIn(mine, qs)
        self.assertEqual(qs.count(), 1)

    def test_by_author_orders_most_recent_first(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripComment

        older: TripComment = baker.make("dashboard.TripComment", trip=self.trip, author=self.user.profile)
        newer: TripComment = baker.make("dashboard.TripComment", trip=self.trip, author=self.user.profile)
        TripComment.objects.filter(pk=older.pk).update(created=datetime(2020, 1, 1, tzinfo=UTC))
        TripComment.objects.filter(pk=newer.pk).update(created=datetime(2020, 1, 2, tzinfo=UTC))

        qs = list(TripComment.objects.by_author(self.user.profile))
        self.assertEqual(qs, [newer, older])

    def test_by_author_preloads_trip_without_extra_query(self) -> None:
        from urbanlens.dashboard.models.trips.model import TripComment

        baker.make("dashboard.TripComment", trip=self.trip, author=self.user.profile)

        comment = TripComment.objects.by_author(self.user.profile).first()
        with self.assertNumQueries(0):
            _ = comment.trip.name


# -- PinMarkupQuerySet ---------------------------------------------------------

class PinMarkupQuerySetTests(TestCase):
    """for_pin() and for_profile() filter markup by parent and owner."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.location = baker.make("dashboard.Location", latitude=44.0, longitude=-70.0)
        self.pin = baker.make("dashboard.Pin", profile=self.user.profile, location=self.location)
        self.other_pin = baker.make("dashboard.Pin", profile=self.user.profile)
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
        self.other_pin = baker.make("dashboard.Pin", profile=self.user.profile)
        ts = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        self.manual_visit = baker.make(
            "dashboard.PinVisit",
            pin=self.pin,
            source=VisitSource.MANUAL,
            visited_at=ts,
        )
        self.takeout_visit = baker.make(
            "dashboard.PinVisit",
            pin=self.pin,
            source=VisitSource.HISTORY,
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


# -- SavedFilterQuerySet ---------------------------------------------------------

class SavedFilterQuerySetNameTakenForTests(TestCase):
    """name_taken_for() detects a name collision, scoped to one profile and excludable by pk."""

    def setUp(self):
        self.user = baker.make("auth.User")
        self.other_user = baker.make("auth.User")
        self.existing = baker.make("dashboard.SavedFilter", profile=self.user.profile, name="My Filter")

    def test_taken_when_another_of_the_same_profiles_filters_has_that_name(self) -> None:
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        self.assertTrue(SavedFilter.objects.name_taken_for(self.user.profile, "My Filter"))

    def test_not_taken_for_a_different_profile_with_the_same_name(self) -> None:
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        self.assertFalse(SavedFilter.objects.name_taken_for(self.other_user.profile, "My Filter"))

    def test_not_taken_for_an_unused_name(self) -> None:
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        self.assertFalse(SavedFilter.objects.name_taken_for(self.user.profile, "Unused Name"))

    def test_excluding_its_own_pk_lets_a_rename_keep_its_current_name(self) -> None:
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        self.assertFalse(SavedFilter.objects.name_taken_for(self.user.profile, "My Filter", exclude_pk=self.existing.pk))

    def test_exclude_pk_does_not_hide_a_collision_with_a_different_filter(self) -> None:
        from urbanlens.dashboard.models.saved_filter.model import SavedFilter

        other: SavedFilter = baker.make("dashboard.SavedFilter", profile=self.user.profile, name="Other Filter")
        self.assertTrue(SavedFilter.objects.name_taken_for(self.user.profile, "My Filter", exclude_pk=other.pk))


# -- ProfileNote/ProfileNickname/ProfileTrust for_pair() ------------------------

class ProfileAnnotationForPairTests(TestCase):
    """for_pair() scopes each of the three private-annotation models to one author+subject pair."""

    def setUp(self):
        self.author = baker.make("auth.User").profile
        self.subject = baker.make("auth.User").profile
        self.other_subject = baker.make("auth.User").profile
        self.other_author = baker.make("auth.User").profile

    def test_note_for_pair_includes_matching_notes_only(self) -> None:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        mine: ProfileNote = baker.make("dashboard.ProfileNote", author=self.author, subject=self.subject)
        baker.make("dashboard.ProfileNote", author=self.author, subject=self.other_subject)
        baker.make("dashboard.ProfileNote", author=self.other_author, subject=self.subject)

        qs = ProfileNote.objects.for_pair(self.author, self.subject)
        self.assertIn(mine, qs)
        self.assertEqual(qs.count(), 1)

    def test_note_for_pair_returns_every_note_for_that_pair(self) -> None:
        from urbanlens.dashboard.models.profile.note import ProfileNote

        baker.make("dashboard.ProfileNote", author=self.author, subject=self.subject, _quantity=3)

        self.assertEqual(ProfileNote.objects.for_pair(self.author, self.subject).count(), 3)

    def test_nickname_for_pair_finds_the_row(self) -> None:
        from urbanlens.dashboard.models.profile.nickname import ProfileNickname

        nickname: ProfileNickname = baker.make("dashboard.ProfileNickname", author=self.author, subject=self.subject)
        baker.make("dashboard.ProfileNickname", author=self.author, subject=self.other_subject)

        self.assertEqual(ProfileNickname.objects.for_pair(self.author, self.subject).first(), nickname)

    def test_nickname_for_pair_empty_for_a_different_pair(self) -> None:
        from urbanlens.dashboard.models.profile.nickname import ProfileNickname

        baker.make("dashboard.ProfileNickname", author=self.other_author, subject=self.subject)

        self.assertIsNone(ProfileNickname.objects.for_pair(self.author, self.subject).first())

    def test_trust_for_pair_finds_the_row(self) -> None:
        from urbanlens.dashboard.models.profile.trust import ProfileTrust

        trust: ProfileTrust = baker.make("dashboard.ProfileTrust", author=self.author, subject=self.subject, rating=4)
        baker.make("dashboard.ProfileTrust", author=self.other_author, subject=self.subject, rating=2)

        self.assertEqual(ProfileTrust.objects.for_pair(self.author, self.subject).first(), trust)

    def test_trust_for_pair_empty_for_a_different_pair(self) -> None:
        from urbanlens.dashboard.models.profile.trust import ProfileTrust

        baker.make("dashboard.ProfileTrust", author=self.author, subject=self.other_subject, rating=3)

        self.assertIsNone(ProfileTrust.objects.for_pair(self.author, self.subject).first())


# -- e2ee querysets ---------------------------------------------------------------

class MessagingKeyBundleQuerySetTests(TestCase):
    """for_profile()/for_profiles() scope MessagingKeyBundle lookups."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.other_profile = baker.make("auth.User").profile

    def test_for_profile_finds_the_bundle(self) -> None:
        from urbanlens.dashboard.models.e2ee import MessagingKeyBundle

        bundle: MessagingKeyBundle = baker.make("dashboard.MessagingKeyBundle", profile=self.profile)
        self.assertEqual(MessagingKeyBundle.objects.for_profile(self.profile).first(), bundle)

    def test_for_profile_empty_when_not_enrolled(self) -> None:
        from urbanlens.dashboard.models.e2ee import MessagingKeyBundle

        baker.make("dashboard.MessagingKeyBundle", profile=self.other_profile)
        self.assertIsNone(MessagingKeyBundle.objects.for_profile(self.profile).first())

    def test_for_profiles_accepts_profile_instances(self) -> None:
        from urbanlens.dashboard.models.e2ee import MessagingKeyBundle

        baker.make("dashboard.MessagingKeyBundle", profile=self.profile)
        baker.make("dashboard.MessagingKeyBundle", profile=self.other_profile)
        unrelated: Profile = baker.make("auth.User").profile

        qs = MessagingKeyBundle.objects.for_profiles([self.profile, self.other_profile])
        self.assertEqual(qs.count(), 2)
        self.assertFalse(qs.filter(profile=unrelated).exists())

    def test_for_profiles_accepts_raw_ids(self) -> None:
        from urbanlens.dashboard.models.e2ee import MessagingKeyBundle

        baker.make("dashboard.MessagingKeyBundle", profile=self.profile)

        self.assertEqual(MessagingKeyBundle.objects.for_profiles([self.profile.pk]).count(), 1)


class ConversationKeyQuerySetTests(TestCase):
    """between() finds a conversation's keys regardless of argument order."""

    def setUp(self):
        self.profile_a = baker.make("auth.User").profile
        self.profile_b = baker.make("auth.User").profile
        self.other_profile = baker.make("auth.User").profile

    def test_between_finds_keys_for_the_pair(self) -> None:
        from urbanlens.dashboard.models.e2ee import ConversationKey

        low, high = ConversationKey.canonical_pair(self.profile_a, self.profile_b)
        key: ConversationKey = baker.make("dashboard.ConversationKey", profile_low=low, profile_high=high, version=1)

        self.assertIn(key, ConversationKey.objects.between(self.profile_a, self.profile_b))

    def test_between_is_order_independent(self) -> None:
        from urbanlens.dashboard.models.e2ee import ConversationKey

        low, high = ConversationKey.canonical_pair(self.profile_a, self.profile_b)
        baker.make("dashboard.ConversationKey", profile_low=low, profile_high=high, version=1)

        # Same pair, arguments reversed - canonicalization inside between()
        # must find the same row either way.
        self.assertEqual(ConversationKey.objects.between(self.profile_b, self.profile_a).count(), 1)

    def test_between_excludes_a_different_pair(self) -> None:
        from urbanlens.dashboard.models.e2ee import ConversationKey

        low, high = ConversationKey.canonical_pair(self.profile_a, self.other_profile)
        baker.make("dashboard.ConversationKey", profile_low=low, profile_high=high, version=1)

        self.assertEqual(ConversationKey.objects.between(self.profile_a, self.profile_b).count(), 0)

    def test_between_orders_oldest_first(self) -> None:
        from urbanlens.dashboard.models.e2ee import ConversationKey

        low, high = ConversationKey.canonical_pair(self.profile_a, self.profile_b)
        v2: ConversationKey = baker.make("dashboard.ConversationKey", profile_low=low, profile_high=high, version=2)
        v1: ConversationKey = baker.make("dashboard.ConversationKey", profile_low=low, profile_high=high, version=1)

        self.assertEqual(list(ConversationKey.objects.between(self.profile_a, self.profile_b)), [v1, v2])


class GroupKeyQuerySetTests(TestCase):
    """for_group() scopes GroupKey lookups to one group chat."""

    def setUp(self):
        self.group = baker.make("dashboard.GroupChat")
        self.other_group = baker.make("dashboard.GroupChat")

    def test_for_group_finds_its_keys(self) -> None:
        from urbanlens.dashboard.models.e2ee import GroupKey

        key: GroupKey = baker.make("dashboard.GroupKey", group=self.group, version=1)
        baker.make("dashboard.GroupKey", group=self.other_group, version=1)

        qs = GroupKey.objects.for_group(self.group)
        self.assertIn(key, qs)
        self.assertEqual(qs.count(), 1)


# -- Direct-message pair models ---------------------------------------------------

class DirectMessagePairQuerySetTests(TestCase):
    """for_pair() scopes mutes and image permissions to one viewer+sender pair."""

    def setUp(self):
        self.viewer = baker.make("auth.User").profile
        self.sender = baker.make("auth.User").profile
        self.other = baker.make("auth.User").profile

    def test_mute_for_pair_is_directional(self) -> None:
        from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute

        baker.make("dashboard.DirectMessageMute", viewer=self.viewer, sender=self.sender)

        self.assertTrue(DirectMessageMute.objects.for_pair(self.viewer, self.sender).exists())
        # The reverse direction is a different row entirely.
        self.assertFalse(DirectMessageMute.objects.for_pair(self.sender, self.viewer).exists())

    def test_mute_for_pair_excludes_other_senders(self) -> None:
        from urbanlens.dashboard.models.direct_messages.mute import DirectMessageMute

        baker.make("dashboard.DirectMessageMute", viewer=self.viewer, sender=self.other)

        self.assertFalse(DirectMessageMute.objects.for_pair(self.viewer, self.sender).exists())

    def test_image_permission_for_pair_finds_the_row(self) -> None:
        from urbanlens.dashboard.models.direct_messages.image_permission import DirectMessageImagePermission

        row: DirectMessageImagePermission = baker.make("dashboard.DirectMessageImagePermission", viewer=self.viewer, sender=self.sender)
        baker.make("dashboard.DirectMessageImagePermission", viewer=self.viewer, sender=self.other)

        self.assertEqual(DirectMessageImagePermission.objects.for_pair(self.viewer, self.sender).first(), row)


# -- MediaRelevance ---------------------------------------------------------------

class MediaRelevanceQuerySetTests(TestCase):
    """for_gallery() scopes relevance marks to one profile+location+provider."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.location = baker.make("dashboard.Location", latitude=41.0, longitude=-73.5)
        self.other_location = baker.make("dashboard.Location", latitude=41.1, longitude=-73.6)

    def test_for_gallery_scopes_to_profile_location_and_source(self) -> None:
        from urbanlens.dashboard.models.images.relevance import MediaRelevance

        mine: MediaRelevance = baker.make("dashboard.MediaRelevance", profile=self.profile, location=self.location, source="wikimedia", item_key="a" * 40, is_relevant=True)
        baker.make("dashboard.MediaRelevance", profile=self.profile, location=self.location, source="smithsonian", item_key="b" * 40, is_relevant=True)
        baker.make("dashboard.MediaRelevance", profile=self.profile, location=self.other_location, source="wikimedia", item_key="c" * 40, is_relevant=False)

        qs = MediaRelevance.objects.for_gallery(self.profile, self.location, "wikimedia")
        self.assertIn(mine, qs)
        self.assertEqual(qs.count(), 1)


# -- ProfileEmail -----------------------------------------------------------------

class ProfileEmailQuerySetTests(TestCase):
    """verified_for() matches only verified claims on one normalized address."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile

    def test_verified_for_finds_a_verified_claim(self) -> None:
        from urbanlens.dashboard.models.profile.email import ProfileEmail

        row = ProfileEmail.objects.create(profile=self.profile, email="alt@example.com", is_verified=True)
        self.assertEqual(ProfileEmail.objects.verified_for(row.normalized_email).first(), row)

    def test_verified_for_ignores_unverified_claims(self) -> None:
        from urbanlens.dashboard.models.profile.email import ProfileEmail

        row = ProfileEmail.objects.create(profile=self.profile, email="pending@example.com", is_verified=False)
        self.assertIsNone(ProfileEmail.objects.verified_for(row.normalized_email).first())


# -- Review -----------------------------------------------------------------------

class ReviewForPairQuerySetTests(TestCase):
    """for_pair() scopes reviews to one profile+pin pair."""

    def setUp(self):
        self.profile = baker.make("auth.User").profile
        self.other_profile = baker.make("auth.User").profile
        self.pin = baker.make("dashboard.Pin", profile=self.profile)

    def test_for_pair_finds_the_row(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        review: Review = baker.make("dashboard.Review", profile=self.profile, pin=self.pin, rating=4)
        baker.make("dashboard.Review", profile=self.other_profile, pin=self.pin, rating=2)

        self.assertEqual(Review.objects.for_pair(self.profile, self.pin).first(), review)

    def test_for_pair_empty_for_an_unreviewed_pin(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        self.assertIsNone(Review.objects.for_pair(self.profile, self.pin).first())
