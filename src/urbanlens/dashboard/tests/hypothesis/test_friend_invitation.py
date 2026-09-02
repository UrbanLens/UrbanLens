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

    def test_process_pending_invitations_matches_gmail_variant(self) -> None:
        """A pending invite to one Gmail spelling must still be found when the
        invitee registers under a dot/+ variant of the same address - see
        FriendInvitation.email_normalized. Previously this orphaned the
        invitation: a case-insensitive-exact match doesn't strip Gmail dots.
        """
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="john.doe.3@gmail.com", is_active=False)
        invitation = FriendInvitation.objects.create(
            inviter=inviter,
            email="johndoe3@gmail.com",
        )

        _process_pending_invitations(invitee)

        friendship = Friendship.objects.filter(
            from_profile=inviter,
            to_profile=invitee.profile,
            status=FriendshipStatus.REQUESTED,
        ).first()
        self.assertIsNotNone(friendship)

        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)

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


class EmailNormalizedFieldTests(TestCase):
    """FriendInvitation.email_normalized self-populates on every save."""

    def test_gmail_variant_is_dot_and_plus_stripped(self) -> None:
        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="Jake.Smith+x@gmail.com")

        self.assertEqual(invitation.email_normalized, "jakesmith@gmail.com")

    def test_non_gmail_address_is_only_lowercased(self) -> None:
        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="Jake.Smith@Example.com")

        self.assertEqual(invitation.email_normalized, "jake.smith@example.com")


class EmailNormalizedBackfillMigrationTests(TestCase):
    """The 0048 migration's data backfill, exercised directly against a real row.

    Regression coverage for the migration function itself (app label/field-name
    typos, the only()/iterator()/bulk_update plumbing) that a plain model-level
    test of save() can't reach, since save() already keeps email_normalized
    populated on every row created through the ORM during the test run.
    """

    def test_backfill_normalizes_a_row_left_blank(self) -> None:
        import importlib

        from django.apps import apps as live_apps

        migration = importlib.import_module("urbanlens.dashboard.migrations.0049_friendinvitation_email_normalized")

        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="Jake.Smith+x@gmail.com")
        # Simulate a pre-existing row as it looked right after the AddField
        # ran and before the backfill did - blank, same as the field's own
        # default.
        FriendInvitation.objects.filter(pk=invitation.pk).update(email_normalized="")

        migration.backfill_friendinvitation_email_normalized(live_apps, None)

        invitation.refresh_from_db()
        self.assertEqual(invitation.email_normalized, "jakesmith@gmail.com")


class MarkAcceptedClaimTests(TestCase):
    """``mark_accepted`` must be a write-time conditional claim, not a blind update."""

    def test_mark_accepted_returns_true_once_then_false(self) -> None:
        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="invitee@example.com")

        self.assertTrue(invitation.mark_accepted())
        self.assertTrue(invitation.is_accepted())
        self.assertFalse(invitation.mark_accepted())

        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)

    def test_stale_instance_cannot_reclaim(self) -> None:
        """A second in-memory copy (as held by a concurrent request) loses the claim."""
        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="invitee@example.com")
        stale = FriendInvitation.objects.get(pk=invitation.pk)

        self.assertTrue(invitation.mark_accepted())
        self.assertFalse(stale.mark_accepted())


class ApplyPendingInvitationReplayTests(TestCase):
    """An already-accepted invitation must not fire side effects a second time."""

    def test_already_accepted_invite_performs_no_side_effects(self) -> None:
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="invitee@example.com")
        # A concurrent request selected the invitation while it was still open,
        # so its in-memory copy predates the other request's claim.
        invitation = FriendInvitation.objects.create(inviter=inviter, email=invitee.email)
        stale = FriendInvitation.objects.get(pk=invitation.pk)
        self.assertTrue(invitation.mark_accepted())

        with (
            patch("urbanlens.dashboard.models.friendship.model.Friendship.request") as request_mock,
            patch("urbanlens.dashboard.controllers.friendship.notify_friend_request") as notify_mock,
            patch("urbanlens.dashboard.models.subscriptions.grant_subscription") as grant_mock,
        ):
            _apply_pending_invitation(stale, invitee.profile)

        request_mock.assert_not_called()
        notify_mock.assert_not_called()
        grant_mock.assert_not_called()
