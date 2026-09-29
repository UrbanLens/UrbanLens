"""``delivery_preference`` and ``deliver_notification``: one preference branch for every notification producer."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.core import mail
from model_bakery import baker

from urbanlens.core.tests.celery_inline import notification_emails_sent
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType, Permission
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference, NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog, NotificationPreference
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.notifications.notification_delivery import deliver_notification, delivery_preference


def _profile(email: str = "") -> Profile:
    return baker.make(User, email=email).profile


class DeliveryPreferenceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.profile = _profile()

    def test_missing_preferences_row_gets_the_default(self) -> None:
        self.assertFalse(NotificationPreference.objects.filter(profile=self.profile).exists())

        self.assertEqual(delivery_preference(self.profile, "friend_request"), DeliveryPreference.SITE)
        self.assertEqual(
            delivery_preference(self.profile, "wiki_safety_checkin", default=DeliveryPreference.BOTH),
            DeliveryPreference.BOTH,
        )

    def test_stored_choice_is_returned_as_a_member(self) -> None:
        NotificationPreference.objects.create(profile=self.profile, friend_request=DeliveryPreference.EMAIL)
        self.profile.refresh_from_db()

        preference = delivery_preference(self.profile, "friend_request")

        self.assertIs(preference, DeliveryPreference.EMAIL)
        self.assertTrue(preference.includes_email)
        self.assertFalse(preference.includes_site)

    def test_unknown_stored_value_falls_back_to_the_default(self) -> None:
        NotificationPreference.objects.create(profile=self.profile)
        NotificationPreference.objects.filter(profile=self.profile).update(friend_request="bogus")
        self.profile.refresh_from_db()

        self.assertEqual(delivery_preference(self.profile, "friend_request"), DeliveryPreference.SITE)

    def test_a_misspelt_field_raises_instead_of_reading_as_the_default(self) -> None:
        NotificationPreference.objects.create(profile=self.profile)
        self.profile.refresh_from_db()

        with self.assertRaises(AttributeError):
            delivery_preference(self.profile, "friend_requets")


class DeliverNotificationTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.recipient = _profile("recipient@example.com")
        self.source = _profile()
        mail.outbox.clear()

    def _deliver(self, preference: DeliveryPreference, **extra) -> NotificationLog | None:
        with notification_emails_sent():
            return deliver_notification(
                self.recipient,
                preference,
                title="New friend request",
                message="someone wants to be your friend.",
                notification_type=NotificationType.FRIEND_REQUEST,
                source_profile=self.source,
                **extra,
            )

    def test_none_delivers_nothing(self) -> None:
        self.assertIsNone(self._deliver(DeliveryPreference.NONE, url="/friends/"))

        self.assertFalse(NotificationLog.objects.filter(profile=self.recipient).exists())
        self.assertEqual(mail.outbox, [])

    def test_site_writes_the_row_and_sends_no_email(self) -> None:
        notification = self._deliver(DeliveryPreference.SITE, url="/friends/")

        self.assertIsNotNone(notification)
        row = NotificationLog.objects.get(profile=self.recipient)
        self.assertEqual(row, notification)
        self.assertEqual(
            (row.title, row.message, row.url, row.notification_type, row.source_profile_id),
            (
                "New friend request",
                "someone wants to be your friend.",
                "/friends/",
                NotificationType.FRIEND_REQUEST,
                self.source.pk,
            ),
        )
        self.assertEqual(mail.outbox, [])

    def test_email_sends_the_email_and_writes_no_row(self) -> None:
        self.assertIsNone(self._deliver(DeliveryPreference.EMAIL, url="/friends/"))

        self.assertFalse(NotificationLog.objects.filter(profile=self.recipient).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "New friend request")
        self.assertEqual(mail.outbox[0].to, ["recipient@example.com"])
        self.assertIn("someone wants to be your friend.", mail.outbox[0].body)
        self.assertIn("/friends/", mail.outbox[0].body)

    def test_both_writes_the_row_and_sends_the_email(self) -> None:
        notification = self._deliver(DeliveryPreference.BOTH, url="/friends/")

        self.assertEqual(NotificationLog.objects.get(profile=self.recipient), notification)
        self.assertEqual(len(mail.outbox), 1)

    def test_email_url_overrides_only_the_email_link(self) -> None:
        self._deliver(DeliveryPreference.BOTH, email_url="/notifications/")

        self.assertEqual(NotificationLog.objects.get(profile=self.recipient).url, "")
        self.assertIn("/notifications/", mail.outbox[0].body)

    def test_a_muted_source_writes_no_row(self) -> None:
        friendship = Friendship.objects.create(
            from_profile=self.source,
            to_profile=self.recipient,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        friendship.mute(self.recipient)

        self.assertIsNone(self._deliver(DeliveryPreference.SITE, url="/friends/"))
        self.assertFalse(NotificationLog.objects.filter(profile=self.recipient).exists())

    def test_missing_preferences_row_delivers_on_site(self) -> None:
        preference = delivery_preference(self.recipient, "friend_request")

        self.assertIsNotNone(self._deliver(preference, url="/friends/"))
        self.assertEqual(mail.outbox, [])


class TripInviteInMessageHonoursEmailTests(TestCase):
    """The chat-message trip invite reads ``added_to_trip`` the same way every other trip invite does."""

    def test_email_only_recipient_is_emailed_rather_than_given_a_bell_row(self) -> None:
        from urbanlens.dashboard.models.trips.model import Trip, TripMembership
        from urbanlens.dashboard.services.messaging.direct_message_shares import invite_to_trip_in_message

        sender = _profile()
        recipient = _profile("invitee@example.com")
        Friendship.objects.create(
            from_profile=sender,
            to_profile=recipient,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
            permissions=Permission.VIEW_PROFILE,
        )
        NotificationPreference.objects.create(profile=recipient, added_to_trip=DeliveryPreference.EMAIL)
        recipient.refresh_from_db()
        trip = Trip.objects.create(name="My Trip", creator=sender)
        TripMembership.objects.create(trip=trip, profile=sender)
        mail.outbox.clear()

        with notification_emails_sent():
            invite_to_trip_in_message(sender, recipient, trip, "join us")

        self.assertFalse(
            NotificationLog.objects.filter(
                profile=recipient, notification_type=NotificationType.ADDED_TO_TRIP
            ).exists(),
        )
        self.assertEqual([message.subject for message in mail.outbox], ["Trip invitation"])
