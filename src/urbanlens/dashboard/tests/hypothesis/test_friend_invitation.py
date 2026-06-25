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
