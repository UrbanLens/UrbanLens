"""Tests for email friend invitations processed after account verification."""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.account import _apply_pending_invitation, _process_pending_invitations
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.subscriptions import grant_subscription
from urbanlens.dashboard.models.subscriptions.model import PendingSubscriptionGrant, SubscriptionRole, UserSubscription


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

    def test_email_verification_uses_persisted_invite_token_when_email_differs(self) -> None:
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="different@example.com", is_active=False)
        invitation = FriendInvitation.objects.create(
            inviter=inviter,
            email="invited@example.com",
        )
        verification = EmailVerification.objects.create(
            user=invitee,
            pending_invite_token=invitation.token,
        )

        response = self.client.get(reverse("verify_email", args=[verification.token]))

        self.assertEqual(response.status_code, 200)
        invitee.refresh_from_db()
        self.assertTrue(invitee.is_active)
        self.assertTrue(
            Friendship.objects.filter(
                from_profile=inviter,
                to_profile=invitee.profile,
                status=FriendshipStatus.REQUESTED,
            ).exists(),
        )
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)


class PendingSubscriptionGrantRedemptionTests(TestCase):
    """Accepting an invite that carries a subscription grant applies it.

    Previously untested despite the grant/redeem code (controllers/account.py's
    _apply_pending_invitation, via PendingSubscriptionGrant.objects.for_invitation())
    having existed for a while.
    """

    def test_accepting_the_invite_grants_the_subscription(self) -> None:
        inviter = baker.make(User).profile
        admin = baker.make(User)
        invitee = baker.make(User, email="invitee@example.com", is_active=False)
        role = baker.make(SubscriptionRole)
        invitation = FriendInvitation.objects.create(inviter=inviter, email=invitee.email)
        PendingSubscriptionGrant.objects.create(invitation=invitation, role=role, granted_by=admin, duration_months="3")

        _process_pending_invitations(invitee)

        subscription = UserSubscription.objects.filter(user=invitee, role=role, revoked_at__isnull=True).first()
        self.assertIsNotNone(subscription)
        self.assertEqual(subscription.granted_by, admin)

    def test_indefinite_grant_has_no_expiry(self) -> None:
        inviter = baker.make(User).profile
        admin = baker.make(User)
        invitee = baker.make(User, email="invitee@example.com", is_active=False)
        role = baker.make(SubscriptionRole)
        invitation = FriendInvitation.objects.create(inviter=inviter, email=invitee.email)
        PendingSubscriptionGrant.objects.create(invitation=invitation, role=role, granted_by=admin, duration_months="")

        _process_pending_invitations(invitee)

        subscription = UserSubscription.objects.get(user=invitee, role=role)
        self.assertIsNone(subscription.expires_at)

    def test_no_grant_means_no_subscription(self) -> None:
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="invitee@example.com", is_active=False)
        FriendInvitation.objects.create(inviter=inviter, email=invitee.email)

        _process_pending_invitations(invitee)

        self.assertFalse(UserSubscription.objects.filter(user=invitee).exists())


class InvitationClaimIsSingleUseTests(TestCase):
    """An invitation's side effects run once, even for two racing verifications.

    ``_collect_pending_invitations`` filters on ``accepted_at__isnull=True``,
    which guards at *selection* time: a double-clicked verification link puts
    two callers past that filter holding equally-stale instances. The
    single-use guarantee therefore has to live in the write, which is why
    ``mark_accepted`` is a conditional claim rather than an unconditional
    update, and why it runs before the side effects rather than after.
    """

    def setUp(self) -> None:
        super().setUp()
        self.inviter = baker.make(User).profile
        self.invitee = baker.make(User, email="invitee@example.com", is_active=False)
        self.invitation = FriendInvitation.objects.create(inviter=self.inviter, email=self.invitee.email)

    def test_only_the_first_claim_wins(self) -> None:
        self.assertTrue(self.invitation.mark_accepted())
        self.assertFalse(self.invitation.mark_accepted(), "an already-accepted invitation was claimed a second time")

    def test_the_first_claim_does_not_move_the_recorded_time(self) -> None:
        self.invitation.mark_accepted()
        first = FriendInvitation.objects.get(pk=self.invitation.pk).accepted_at
        self.invitation.mark_accepted()
        self.assertEqual(FriendInvitation.objects.get(pk=self.invitation.pk).accepted_at, first)

    def test_a_second_application_of_a_stale_instance_runs_no_side_effects(self) -> None:
        """The double-clicked-link shape: the same open-looking instance, applied twice.

        Asserted against the side effects being *attempted*, not against their
        result. Today's side effects happen to be individually idempotent -
        `Friendship.request` refuses a duplicate, `grant_subscription`
        recomputes an absolute expiry rather than stacking - so counting rows
        would pass with or without the claim. What the claim guarantees is
        that the second caller never gets there at all, which is what protects
        a side effect added later that does *not* have that property.
        """
        profile = self.invitee.profile
        stale = FriendInvitation.objects.get(pk=self.invitation.pk)

        with patch.object(Friendship, "request", wraps=Friendship.request) as requested:
            _apply_pending_invitation(stale, profile)
            _apply_pending_invitation(stale, profile)

        self.assertEqual(requested.call_count, 1, "the invitation's side effects ran a second time on an already-accepted invite")

    def test_a_grant_is_not_re_redeemed_on_a_second_application(self) -> None:
        admin = baker.make(User)
        role = baker.make(SubscriptionRole)
        PendingSubscriptionGrant.objects.create(invitation=self.invitation, role=role, granted_by=admin, duration_months="3")
        profile = self.invitee.profile
        stale = FriendInvitation.objects.get(pk=self.invitation.pk)

        with patch("urbanlens.dashboard.models.subscriptions.grant_subscription", wraps=grant_subscription) as granted:
            _apply_pending_invitation(stale, profile)
            _apply_pending_invitation(stale, profile)

        self.assertEqual(granted.call_count, 1)
        self.assertEqual(UserSubscription.objects.filter(user=self.invitee, role=role, revoked_at__isnull=True).count(), 1)
