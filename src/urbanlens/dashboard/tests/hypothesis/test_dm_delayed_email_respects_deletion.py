"""Unsending a message must also stop the email that carries its text."""

from __future__ import annotations

from django.core import mail
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.messaging.direct_messages import delete_message_for_everyone, delete_message_for_self
from urbanlens.dashboard.services.social.friendship import block_profile
from urbanlens.dashboard.tasks import send_direct_message_email_if_unread


def _profile(email: str) -> Profile:
    return baker.make("auth.User", email=email).profile


class DelayedDirectMessageEmailTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile("sender@example.com")
        self.recipient = _profile("recipient@example.com")
        Friendship.objects.create(from_profile=self.sender, to_profile=self.recipient, status=FriendshipStatus.ACCEPTED)
        self.message = baker.make(
            DirectMessage, sender=self.sender, recipient=self.recipient, body="meet me at the old mill at nine"
        )
        mail.outbox.clear()

    def test_an_unsent_message_is_not_emailed(self) -> None:
        delete_message_for_everyone(self.message, self.sender)

        send_direct_message_email_if_unread(self.message.pk)

        self.assertEqual(mail.outbox, [], "the delayed email delivered a message the sender had already unsent")

    def test_a_message_the_recipient_deleted_is_not_emailed(self) -> None:
        """Their own copy is gone from the app; mailing it back undoes that."""
        delete_message_for_self(self.message, self.recipient)

        send_direct_message_email_if_unread(self.message.pk)

        self.assertEqual(mail.outbox, [], "the delayed email delivered a message the recipient had deleted")

    def test_a_message_from_a_since_blocked_sender_is_not_emailed(self) -> None:
        """Blocking is enforced when sending; the delayed email outlives the send.

        A block placed inside the 120-second window - which is exactly when someone reaches for it, right after
        the message that prompted it - otherwise still delivers that message's text to the blocker's inbox, out
        of band and permanent, after the app has stopped showing it."""
        block_profile(self.recipient, self.sender)

        send_direct_message_email_if_unread(self.message.pk)

        self.assertEqual(
            mail.outbox, [], "the delayed email delivered a message from a sender the recipient had blocked"
        )

    def test_an_ordinary_unread_message_is_still_emailed(self) -> None:
        """The guard must not break the feature it is guarding."""
        send_direct_message_email_if_unread(self.message.pk)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("old mill", mail.outbox[0].body)
