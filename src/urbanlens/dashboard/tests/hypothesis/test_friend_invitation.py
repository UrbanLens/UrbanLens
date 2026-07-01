"""Tests for email friend invitations processed after account verification."""
from __future__ import annotations

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.account import _process_pending_invitations
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog


class PendingFriendInvitationTests(TestCase):
    """Pending email invitations should create friend requests and notifications."""

    def test_process_pending_invitations_creates_friend_request_and_notification(self) -> None:
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="invitee@example.com", is_active=False)
        invitation = FriendInvitation.objects.create(
            inviter=inviter,
            email=invitee.email,
        )

        _process_pending_invitations(invitee)

        friendship = Friendship.objects.filter(
            from_profile=inviter,
            to_profile=invitee.profile,
            status=FriendshipStatus.REQUESTED,
        ).first()
        self.assertIsNotNone(friendship)

        notification = NotificationLog.objects.filter(
            profile=invitee.profile,
            notification_type=NotificationType.FRIEND_REQUEST,
            source_profile=inviter,
        ).first()
        self.assertIsNotNone(notification)

        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)

    def test_process_pending_invitations_uses_invite_token(self) -> None:
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="different@example.com", is_active=False)
        invitation = FriendInvitation.objects.create(
            inviter=inviter,
            email="invited@example.com",
        )

        _process_pending_invitations(invitee, invite_token=str(invitation.token))

        self.assertTrue(
            Friendship.objects.filter(
                from_profile=inviter,
                to_profile=invitee.profile,
                status=FriendshipStatus.REQUESTED,
            ).exists(),
        )
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=invitee.profile,
                notification_type=NotificationType.FRIEND_REQUEST,
                source_profile=inviter,
            ).exists(),
        )


class InviteSignupEmailVerificationTests(TestCase):
    """Invite-token signup should skip email verification only for the invited address."""

    def _signup_payload(self, email: str) -> dict[str, str]:
        return {
            "username": "newinvitee",
            "email": email,
            "password1": "ComplexPass123!",
            "password2": "ComplexPass123!",
        }

    def test_signup_with_matching_invite_email_activates_without_verification(self) -> None:
        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="invitee@example.com")

        response = self.client.post(
            f"/signup/?invite={invitation.token}",
            data=self._signup_payload("INVITEE@example.com"),
        )

        self.assertEqual(response.status_code, 302)
        invitee = User.objects.get(username="newinvitee")
        self.assertTrue(invitee.is_active)
        self.assertFalse(hasattr(invitee, "email_verification"))
        self.assertTrue(
            Friendship.objects.filter(
                from_profile=inviter,
                to_profile=invitee.profile,
                status=FriendshipStatus.REQUESTED,
            ).exists(),
        )
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)

    def test_signup_with_nonmatching_invite_email_still_requires_verification(self) -> None:
        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="invitee@example.com")

        response = self.client.post(
            f"/signup/?invite={invitation.token}",
            data=self._signup_payload("different@example.com"),
        )

        self.assertEqual(response.status_code, 302)
        invitee = User.objects.get(username="newinvitee")
        self.assertFalse(invitee.is_active)
        self.assertTrue(hasattr(invitee, "email_verification"))
        self.assertFalse(
            Friendship.objects.filter(
                from_profile=inviter,
                to_profile=invitee.profile,
                status=FriendshipStatus.REQUESTED,
            ).exists(),
        )
        invitation.refresh_from_db()
        self.assertIsNone(invitation.accepted_at)
