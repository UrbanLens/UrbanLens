"""Emails raised by a person's own action are sent by a worker after commit, not during their request."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.services.profile.account_deletion import request_deletion
from urbanlens.dashboard.services.visits.safety import invite_checkin_partner
from urbanlens.dashboard.tasks import send_email_task
from urbanlens.dashboard.tests.hypothesis.test_safety_partners import _checkin

_SEND = "urbanlens.dashboard.services.notifications.notification_delivery.EmailMultiAlternatives.send"


class QueuedAfterCommitMixin:
    def assert_queued_then_sent(self, action, to: str) -> None:
        with mock.patch(_SEND) as send, tasks_run_inline(send_email_task) as enqueue:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                action()
            send.assert_not_called()
            enqueue.assert_not_called()
            self.assertTrue(callbacks)
        mail.outbox.clear()
        with tasks_run_inline(send_email_task), self.captureOnCommitCallbacks(execute=True):
            action()
        self.assertEqual([message.to for message in mail.outbox], [[to]])
        self.assertTrue(mail.outbox[0].alternatives, "the HTML body was lost on the way to the worker")


class AccountDeletionEmailTests(QueuedAfterCommitMixin, TestCase):
    def test_the_deletion_notice_is_sent_after_commit(self) -> None:
        profile = baker.make(User, email="owner@example.com").profile
        self.assert_queued_then_sent(lambda: request_deletion(profile), "owner@example.com")


class SafetyPartnerInviteEmailTests(QueuedAfterCommitMixin, TestCase):
    def test_the_partner_invite_is_sent_after_commit(self) -> None:
        owner = baker.make(User).profile
        partner = baker.make(User, email="partner@example.com").profile
        Profile.objects.filter(pk=partner.pk).update(profile_visibility=VisibilityChoice.ANYONE)
        partner.refresh_from_db()

        def invite() -> None:
            invite_checkin_partner(_checkin(owner), inviter=owner, username=partner.username)

        self.assert_queued_then_sent(invite, "partner@example.com")
