"""A notification email is sent by a worker after the triggering write commits, never during the request.

SMTP has a 10 s timeout, so a slow mail server used to hold a friend request, share, comment reply or trip add
open for that long; and an email sent before commit could describe a write that then rolled back.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.db import transaction
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.notifications.meta import DeliveryPreference
from urbanlens.dashboard.models.notifications.model import NotificationPreference
from urbanlens.dashboard.services.social.friendship import notify_friend_request
from urbanlens.dashboard.tasks import send_notification_email_task

_SEND = "urbanlens.dashboard.services.notifications.notification_delivery.EmailMultiAlternatives.send"


class NotificationEmailOffRequestPathTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.sender = baker.make(User).profile
        self.recipient = baker.make(User, email="recipient@example.com").profile
        prefs, _ = NotificationPreference.objects.get_or_create(profile=self.recipient)
        prefs.friend_request = DeliveryPreference.EMAIL
        prefs.save(update_fields=["friend_request"])
        mail.outbox.clear()

    def test_nothing_is_sent_before_the_write_commits(self) -> None:
        with mock.patch(_SEND) as send, tasks_run_inline(send_notification_email_task) as enqueue:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                notify_friend_request(self.sender, self.recipient)
            send.assert_not_called()
            enqueue.assert_not_called()
            self.assertEqual(len(callbacks), 1)

    def test_the_worker_sends_it_after_commit(self) -> None:
        with tasks_run_inline(send_notification_email_task), self.captureOnCommitCallbacks(execute=True):
            notify_friend_request(self.sender, self.recipient)

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["recipient@example.com"])

    def test_a_rolled_back_write_sends_nothing(self) -> None:
        def notify_then_fail() -> None:
            with transaction.atomic():
                notify_friend_request(self.sender, self.recipient)
                raise RuntimeError

        with tasks_run_inline(send_notification_email_task) as enqueue:
            with self.captureOnCommitCallbacks(execute=True), self.assertRaises(RuntimeError):
                notify_then_fail()
            enqueue.assert_not_called()
        self.assertEqual(mail.outbox, [])

    def test_the_address_is_read_when_the_worker_runs(self) -> None:
        with tasks_run_inline(send_notification_email_task), self.captureOnCommitCallbacks(execute=True):
            notify_friend_request(self.sender, self.recipient)
            User.objects.filter(pk=self.recipient.user_id).update(email="")

        self.assertEqual(mail.outbox, [])
