"""Every notification category with a real producer now supports Email delivery.

Eight categories (friend_request, friend_accepted, added_to_trip,
comment_reply, comment_liked, pin_shared, visit_suggested) used to treat
"Email" and "Notification" as indistinguishable - both just showed the
in-app row, since none of them had any email-sending code at all. Each now
gates the in-app row on SITE/BOTH and a real email (via
services.notifications.notification_delivery.send_notification_email) on
EMAIL/BOTH, matching the two categories (message, the safety check-in types)
that already worked this way. achievement_earned's own coverage lives in
test_achievements.py, next to its other notification tests.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.comments.model import Comment
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.trips.model import Trip
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.services.notifications.comment_notifications import notify_reaction, notify_reply
from urbanlens.dashboard.services.sharing.pin_sharing import create_pin_share
from urbanlens.dashboard.services.social.friendship import (
    accept_friend_request,
    notify_friend_request,
    request_or_accept_friendship,
)
from urbanlens.dashboard.services.trips.trip_membership import notify_added_to_trip
from urbanlens.dashboard.services.visits.visits import create_visit_suggestion


def _profile(email: str = "") -> Profile:
    return baker.make(User, email=email).profile


def _befriend(a: Profile, b: Profile) -> None:
    Friendship.objects.create(
        from_profile=a,
        to_profile=b,
        status=FriendshipStatus.ACCEPTED,
        relationship_type=FriendshipType.FRIEND,
        permissions=Permission.VIEW_PROFILE,
    )


def _set_pref(profile: Profile, field: str, value: str) -> None:
    prefs, _ = NotificationPreference.objects.get_or_create(profile=profile)
    setattr(prefs, field, value)
    prefs.save(update_fields=[field])


class FriendRequestEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile("recipient@example.com")
        _set_pref(self.recipient, "friend_request", DeliveryPreference.EMAIL)
        mail.outbox.clear()

    def test_email_only_sends_an_email_and_no_site_row(self) -> None:
        notify_friend_request(self.sender, self.recipient)

        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.recipient, notification_type=NotificationType.FRIEND_REQUEST
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["recipient@example.com"])
        self.assertIn("friend request", mail.outbox[0].subject.lower())


class FriendAcceptedEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.requester = _profile("requester@example.com")
        self.acceptor = _profile("acceptor@example.com")
        mail.outbox.clear()

    def test_email_only_sends_an_email_and_no_site_row(self) -> None:
        """accept_friend_request: the original requester is who gets notified."""
        _set_pref(self.requester, "friend_accepted", DeliveryPreference.EMAIL)
        Friendship.request(from_profile=self.requester, to_profile=self.acceptor.pk)
        mail.outbox.clear()

        accept_friend_request(self.acceptor, self.requester)

        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.requester, notification_type=NotificationType.FRIEND_ACCEPTED
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["requester@example.com"])

    def test_the_auto_accept_branch_also_emails(self) -> None:
        """request_or_accept_friendship's crossed-request path shares the same helper now.

        self.acceptor sent the original request; self.requester "requesting"
        them back crosses it and auto-accepts - so self.acceptor (the
        original sender) is who gets told it was accepted.
        """
        _set_pref(self.acceptor, "friend_accepted", DeliveryPreference.EMAIL)
        Friendship.request(from_profile=self.acceptor, to_profile=self.requester.pk)
        mail.outbox.clear()

        request_or_accept_friendship(self.requester, self.acceptor)

        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.acceptor, notification_type=NotificationType.FRIEND_ACCEPTED
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["acceptor@example.com"])


class AddedToTripEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.inviter = _profile()
        self.invitee = _profile("invitee@example.com")
        self.trip = baker.make(Trip, creator=self.inviter, name="Ridge Line")
        _set_pref(self.invitee, "added_to_trip", DeliveryPreference.EMAIL)
        mail.outbox.clear()

    def test_email_only_sends_an_email_and_no_site_row(self) -> None:
        notify_added_to_trip(self.inviter, self.invitee, self.trip)

        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.invitee, notification_type=NotificationType.ADDED_TO_TRIP
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["invitee@example.com"])
        self.assertIn("Ridge Line", mail.outbox[0].body)


class CommentReplyAndReactionEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.author = _profile("author@example.com")
        self.replier = _profile()
        self.pin = baker.make(Pin, profile=self.author)
        self.comment = baker.make(Comment, pin=self.pin, wiki=None, profile=self.author, text="original")
        _set_pref(self.author, "comment_reply", DeliveryPreference.EMAIL)
        _set_pref(self.author, "comment_liked", DeliveryPreference.EMAIL)
        # get_or_create's create path (the first _set_pref call, no row yet)
        # caches the new NotificationPreference on self.author's reverse O2O
        # descriptor; its get path (the second call, row already exists)
        # returns a separate instance that never touches that cache - so
        # self.author.notification_preferences (and self.comment.profile,
        # the same object) would still show comment_liked at its pre-edit
        # default without this refresh, even though both fields are already
        # correctly persisted in the database by this point.
        self.author.refresh_from_db()
        mail.outbox.clear()

    def test_reply_email_only_sends_an_email_and_no_site_row(self) -> None:
        notify_reply(self.replier, self.comment)

        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.author, notification_type=NotificationType.COMMENT_REPLY
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["author@example.com"])

    def test_reaction_email_only_sends_an_email_and_no_site_row(self) -> None:
        notify_reaction(self.replier, self.comment)

        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.author, notification_type=NotificationType.COMMENT_LIKED
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["author@example.com"])


class PinSharedEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile("recipient@example.com")
        _befriend(self.sender, self.recipient)
        self.pin = baker.make(Pin, profile=self.sender)
        _set_pref(self.recipient, "pin_shared", DeliveryPreference.EMAIL)
        mail.outbox.clear()

    def test_email_only_sends_an_email_and_no_site_row(self) -> None:
        share = create_pin_share(self.sender, self.recipient, self.pin)

        self.assertIsNone(share.notification)
        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.recipient, notification_type=NotificationType.PIN_SHARED
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["recipient@example.com"])


class VisitSuggestedEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.suggester = _profile()
        self.recipient = _profile("recipient@example.com")
        self.location = baker.make(Location, latitude="40.0", longitude="-74.0", official_name="Old Mill")
        self.visited_at = timezone.make_aware(datetime.datetime(2026, 6, 1, 14, 0))
        origin_pin = baker.make(Pin, profile=self.suggester, location=self.location)
        self.origin_visit = baker.make(PinVisit, pin=origin_pin, visited_at=self.visited_at, source=VisitSource.MANUAL)
        _set_pref(self.recipient, "visit_suggested", DeliveryPreference.EMAIL)
        mail.outbox.clear()

    def test_email_only_sends_an_email_and_no_site_row(self) -> None:
        suggestion = create_visit_suggestion(
            suggested_to=self.recipient,
            suggested_by=self.suggester,
            visited_at=self.visited_at,
            location=self.location,
            latitude=40.0,
            longitude=-74.0,
            candidate_profiles=[],
            origin_visit=self.origin_visit,
        )

        self.assertIsNotNone(suggestion)
        self.assertIsNone(suggestion.notification)
        self.assertFalse(
            NotificationLog.objects.filter(
                profile=self.recipient, notification_type=NotificationType.VISIT_SUGGESTED
            ).exists()
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["recipient@example.com"])
