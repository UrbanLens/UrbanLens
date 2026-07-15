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
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.direct_messages.meta import MessageRetentionChoice
from urbanlens.dashboard.models.direct_messages.model import DirectMessage
from urbanlens.dashboard.models.images.model import Image
from urbanlens.dashboard.tasks import hard_delete_expired_direct_messages


def _profile():
    return baker.make(User).profile


def _make_message(sender, recipient, *, sender_delete_after, read_at=None, **extra) -> DirectMessage:
    return baker.make(
        DirectMessage,
        sender=sender,
        recipient=recipient,
        sender_delete_after=sender_delete_after,
        read_at=read_at,
        body="hello",
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
