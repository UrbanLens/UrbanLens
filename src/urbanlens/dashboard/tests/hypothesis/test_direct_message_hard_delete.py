"""Tests for the direct-message disappearing-message hard-delete sweep.

Regression coverage for a privacy gap: DirectMessage.is_expired_for_recipient
only ever gated *display* (the recipient saw a tombstone instead of the
content) - the row, its body/ciphertext, and any attached images stayed in
the database untouched forever, still returned by search, regardless of the
sender's "Delete My Messages After" setting. This is the sweep
(tasks.hard_delete_expired_direct_messages, driven by
DirectMessageQuerySet.due_for_hard_delete) that actually removes the row -
for both parties, including the sender - once the timer elapses.
"""

from __future__ import annotations

import datetime

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.tasks import hard_delete_expired_direct_messages


def _profile():
    return baker.make(User).profile


def _make_message(sender, recipient, *, sender_delete_after, read_at=None, body="hello", **extra) -> DirectMessage:
    return baker.make(
        DirectMessage,
        sender=sender,
        recipient=recipient,
        sender_delete_after=sender_delete_after,
        read_at=read_at,
        body=body,
        **extra,
    )


class DueForHardDeleteQuerySetTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()

    def test_never_is_excluded_even_if_read_long_ago(self) -> None:
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.NEVER,
            read_at=timezone.now() - datetime.timedelta(days=1000),
        )
        self.assertNotIn(message, DirectMessage.objects.due_for_hard_delete())

    def test_unread_is_excluded_regardless_of_retention_choice(self) -> None:
        message = _make_message(self.sender, self.recipient, sender_delete_after=MessageRetentionChoice.WHEN_READ, read_at=None)
        self.assertNotIn(message, DirectMessage.objects.due_for_hard_delete())

    def test_when_read_is_included_immediately_after_read(self) -> None:
        message = _make_message(self.sender, self.recipient, sender_delete_after=MessageRetentionChoice.WHEN_READ, read_at=timezone.now())
        self.assertIn(message, DirectMessage.objects.due_for_hard_delete())

    def test_one_day_not_yet_elapsed_is_excluded(self) -> None:
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.ONE_DAY,
            read_at=timezone.now() - datetime.timedelta(hours=1),
        )
        self.assertNotIn(message, DirectMessage.objects.due_for_hard_delete())

    def test_one_day_elapsed_is_included(self) -> None:
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.ONE_DAY,
            read_at=timezone.now() - datetime.timedelta(days=2),
        )
        self.assertIn(message, DirectMessage.objects.due_for_hard_delete())


class HardDeleteExpiredDirectMessagesTaskTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()

    def test_deletes_expired_message_row_entirely(self) -> None:
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.ONE_DAY,
            read_at=timezone.now() - datetime.timedelta(days=2),
        )
        count = hard_delete_expired_direct_messages()
        self.assertEqual(count, 1)
        self.assertFalse(DirectMessage.objects.filter(pk=message.pk).exists())

    def test_deletion_removes_it_for_the_sender_too(self) -> None:
        """Unlike delete_message_for_everyone's tombstone, the sender's own copy is also gone."""
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.WHEN_READ,
            read_at=timezone.now(),
        )
        hard_delete_expired_direct_messages()
        self.assertFalse(DirectMessage.objects.filter(pk=message.pk, sender=self.sender).exists())

    def test_leaves_not_yet_expired_messages_untouched(self) -> None:
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.THIRTY_DAYS,
            read_at=timezone.now(),
        )
        hard_delete_expired_direct_messages()
        self.assertTrue(DirectMessage.objects.filter(pk=message.pk).exists())

    def test_leaves_never_expire_messages_untouched(self) -> None:
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.NEVER,
            read_at=timezone.now() - datetime.timedelta(days=1000),
        )
        hard_delete_expired_direct_messages()
        self.assertTrue(DirectMessage.objects.filter(pk=message.pk).exists())

    def test_deletes_attached_images_too(self) -> None:
        """Image.direct_message is SET_NULL, not CASCADE - the sweep must delete them explicitly."""
        message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.WHEN_READ,
            read_at=timezone.now(),
        )
        image = baker.make(Image, profile=self.sender, direct_message=message, pin=None)
        hard_delete_expired_direct_messages()
        self.assertFalse(Image.objects.filter(pk=image.pk).exists())

    def test_returns_zero_and_no_op_when_nothing_is_due(self) -> None:
        _make_message(self.sender, self.recipient, sender_delete_after=MessageRetentionChoice.NEVER)
        self.assertEqual(hard_delete_expired_direct_messages(), 0)


class WhenReadFirstOpenTests(TestCase):
    """A "delete as soon as read" message is readable exactly once on a cold open.

    Regression test: _thread_context used to mark the thread read BEFORE
    loading the page, so is_expired_for_recipient was already True by render
    time and a recipient who opened the conversation cold only ever saw the
    "no longer available" tombstone - the content was destroyed by the act of
    trying to read it. The read mark must land after the page is loaded, so
    the first render shows the content and only later renders tombstone it.
    """

    SECRET = "the water tower ladder is on the north side"

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        self.sender.ensure_slug()
        self.recipient.ensure_slug()
        self.message = _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.WHEN_READ,
            read_at=None,
            body=self.SECRET,
        )
        self.url = reverse("messages.conversation", kwargs={"profile_slug": self.sender.slug})

    def test_first_open_shows_the_content_and_marks_it_read(self) -> None:
        self.client.force_login(self.recipient.user)
        response = self.client.get(self.url)
        self.assertContains(response, self.SECRET)
        self.message.refresh_from_db()
        self.assertIsNotNone(self.message.read_at)

    def test_second_open_tombstones_it(self) -> None:
        self.client.force_login(self.recipient.user)
        self.client.get(self.url)
        response = self.client.get(self.url)
        self.assertNotContains(response, self.SECRET)
        self.assertContains(response, "This message is no longer available")

    def test_sender_still_sees_their_own_message_after_it_expires(self) -> None:
        self.client.force_login(self.recipient.user)
        self.client.get(self.url)
        self.client.logout()
        self.client.force_login(self.sender.user)
        response = self.client.get(reverse("messages.conversation", kwargs={"profile_slug": self.recipient.slug}))
        self.assertContains(response, self.SECRET)


class SidebarPreviewTombstoneTests(TestCase):
    """The conversation-list sidebar's last-message preview honors tombstone state too.

    Regression: `_conversation_list.html` rendered `conv.last_message.body`
    directly (only branching on `is_encrypted`), so a message tombstoned in
    its own thread bubble - deleted-for-everyone, or expired via the
    "delete as soon as read" retention setting - still leaked its raw text
    into the sidebar preview line on every other page render, including the
    full messages page loaded right after the thread itself had already
    started tombstoning it.
    """

    SECRET = "the spare key is under the third flowerpot"

    def setUp(self) -> None:
        super().setUp()
        self.sender = _profile()
        self.recipient = _profile()
        self.sender.ensure_slug()
        self.recipient.ensure_slug()

    def test_expired_when_read_message_is_not_in_the_sidebar_preview(self) -> None:
        _make_message(
            self.sender,
            self.recipient,
            sender_delete_after=MessageRetentionChoice.WHEN_READ,
            read_at=timezone.now() - datetime.timedelta(minutes=1),
            body=self.SECRET,
        )
        self.client.force_login(self.recipient.user)
        response = self.client.get(reverse("messages.list"))
        self.assertNotContains(response, self.SECRET)
        self.assertContains(response, "This message is no longer available")

    def test_deleted_for_everyone_message_is_not_in_the_sidebar_preview(self) -> None:
        from urbanlens.dashboard.services.direct_messages import delete_message_for_everyone

        message = _make_message(self.sender, self.recipient, sender_delete_after=MessageRetentionChoice.NEVER, body=self.SECRET)
        delete_message_for_everyone(message, self.sender)
        self.client.force_login(self.recipient.user)
        response = self.client.get(reverse("messages.list"))
        self.assertNotContains(response, self.SECRET)
        self.assertContains(response, "Message deleted")

    def test_the_sender_still_sees_their_own_deleted_message_in_their_own_sidebar(self) -> None:
        from urbanlens.dashboard.services.direct_messages import delete_message_for_everyone

        message = _make_message(self.sender, self.recipient, sender_delete_after=MessageRetentionChoice.NEVER, body=self.SECRET)
        delete_message_for_everyone(message, self.sender)
        self.client.force_login(self.sender.user)
        response = self.client.get(reverse("messages.list"))
        self.assertContains(response, self.SECRET)
