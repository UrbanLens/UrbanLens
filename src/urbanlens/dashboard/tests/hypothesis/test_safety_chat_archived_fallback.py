"""Posting to an archived check-in through the no-JS fallback must not 500.

`post_chat_message` raises two failures, and they are **siblings** rather than
parent and child - `SafetyValidationError` and `CheckinArchivedError` both
derive from `ValueError` directly. The external API catches both, deliberately
distinguishing them (409 vs 400: the body was fine, the check-in's plaintext is
already sealed into its encrypted archive, so a client should retire the
conversation rather than ask the user to retype). The HTML fallback caught only
`SafetyValidationError`, so the archived case escaped as a 500.

Where it lands matters: this view is the path used when JavaScript is off or
the WebSocket is down, on a *safety* feature - the surface that runs precisely
when something is already degraded.

Found by sweeping for functions whose callers catch different exception sets,
which is the same shape as the decompression-bomb handler that had reached one
of its two call sites (PROBLEMS.md, 2026-08-16).
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinStatus
from urbanlens.dashboard.services.visits.safety import CheckinArchivedError, SafetyValidationError


class ArchivedCheckinChatFallbackTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)  # absorbs the bootstrap site-admin promotion
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.checkin = baker.make(
            SafetyCheckin,
            profile=self.profile,
            status=SafetyCheckinStatus.CHECKED_IN,
            title="night shoot",
            checkin_by=timezone.now() - timedelta(hours=2),
            grace_period=timedelta(hours=1),
            notify_community_wiki=False,
        )
        self.url = reverse("safety.checkin.messages", kwargs={"checkin_uuid": self.checkin.uuid})

    def test_the_two_failures_are_siblings_not_parent_and_child(self) -> None:
        """The whole reason one handler could not cover both."""
        self.assertFalse(issubclass(CheckinArchivedError, SafetyValidationError))
        self.assertTrue(issubclass(CheckinArchivedError, ValueError))
        self.assertTrue(issubclass(SafetyValidationError, ValueError))

    def _archive(self) -> None:
        """Archive the check-in for real, rather than faking the closed state.

        `archive_checkin` seals the plaintext to the owner's E2EE public key, so
        the bundle needs a genuine 32-byte key - a placeholder string makes
        NaCl reject it and the archive silently never happens.
        """
        import base64
        import os

        from urbanlens.dashboard.models.e2ee.key_bundle import MessagingKeyBundle
        from urbanlens.dashboard.services.visits.safety import archive_checkin

        MessagingKeyBundle.objects.create(
            profile=self.profile,
            public_key=base64.b64encode(os.urandom(32)).decode(),
            recovery_wrapped_secret=base64.b64encode(os.urandom(72)).decode(),
        )
        archive_checkin(self.checkin)
        self.checkin.refresh_from_db()
        self.assertTrue(hasattr(self.checkin, "archive"), "precondition: the check-in must actually be archived")

    def test_posting_to_an_archived_checkin_answers_409_not_500(self) -> None:
        self._archive()

        response = self.client.post(self.url, {"body": "are you out?"})

        self.assertEqual(response.status_code, 409)

    def test_the_refusal_explains_itself_in_sender_safe_text(self) -> None:
        """The chat panel surfaces 400 and 409 bodies directly, so they must be safe."""
        self._archive()

        body = self.client.post(self.url, {"body": "are you out?"}).content.decode()

        self.assertTrue(body.strip())
        self.assertNotIn("<", body)

    def test_an_unarchived_checkin_still_accepts_messages(self) -> None:
        """Anti-vacuity: the new branch must not intercept the ordinary path."""
        response = self.client.post(self.url, {"body": "on my way back"})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.checkin.messages.exists())
