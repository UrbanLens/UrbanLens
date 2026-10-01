"""Tests for email friend invitations processed after account verification."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.account import _process_pending_invitations
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.subscriptions.model import PendingSubscriptionGrant, SubscriptionRole, UserSubscription


class PendingFriendInvitationTests(TestCase):
    """Verifying a new account shows it the invitations sent to its address, and answers none of them.

    Creating a friend request here would change the inviter's pending entry and so tell them the address just
    registered.
    """

    def _assert_bound_not_requested(self, invitation: FriendInvitation, invitee: User, inviter) -> None:
        invitation.refresh_from_db()
        self.assertEqual(invitation.invitee_id, invitee.profile.pk)
        self.assertIsNone(invitation.accepted_at)
        self.assertFalse(Friendship.objects.filter(from_profile=inviter, to_profile=invitee.profile).exists())
        notification = NotificationLog.objects.get(
            profile=invitee.profile, notification_type=NotificationType.FRIEND_REQUEST, source_profile=inviter
        )
        self.assertEqual(notification.url, reverse("friend.invitation", kwargs={"token": invitation.token}))

    def test_process_pending_invitations_binds_and_notifies_without_requesting(self) -> None:
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="invitee@example.com", is_active=False)
        invitation = FriendInvitation.objects.create(inviter=inviter, email=invitee.email)

        _process_pending_invitations(invitee)

        self._assert_bound_not_requested(invitation, invitee, inviter)

    def test_process_pending_invitations_matches_gmail_variant(self) -> None:
        """A pending invite to one Gmail spelling must still be found when the invitee registers under a dot/+ variant of the same address - see FriendInvitation.email_normalized."""
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="john.doe.3@gmail.com", is_active=False)
        invitation = FriendInvitation.objects.create(inviter=inviter, email="johndoe3@gmail.com")

        _process_pending_invitations(invitee)

        self._assert_bound_not_requested(invitation, invitee, inviter)

    def test_the_new_account_then_accepts_on_the_invitation_page(self) -> None:
        inviter = baker.make(User).profile
        invitee = baker.make(User, email="invitee@example.com", is_active=True)
        invitation = FriendInvitation.objects.create(inviter=inviter, email=invitee.email)
        _process_pending_invitations(invitee)

        self.client.force_login(invitee)
        self.client.post(reverse("friend.invitation.answer", kwargs={"token": invitation.token}), {"answer": "accept"})

        friendship = Friendship.objects.all().between(inviter, invitee.profile)
        self.assertEqual(friendship.status, FriendshipStatus.ACCEPTED)
        invitation.refresh_from_db()
        self.assertIsNotNone(invitation.accepted_at)


class PendingSubscriptionGrantRedemptionTests(TestCase):
    """Accepting an invite that carries a subscription grant applies it."""

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

    Regression coverage for the migration function itself (app label/field-name typos, the
    only()/iterator()/bulk_update plumbing) that a plain model-level test of save() can't reach, since save()
    already keeps email_normalized populated on every row created through the ORM during the test run."""

    def test_backfill_normalizes_a_row_left_blank(self) -> None:
        import importlib

        from django.apps import apps as live_apps

        migration = importlib.import_module("urbanlens.dashboard.migrations.0032_v0_8_0")

        inviter = baker.make(User).profile
        invitation = FriendInvitation.objects.create(inviter=inviter, email="Jake.Smith+x@gmail.com")
        # Simulate a pre-existing row as it looked right after the AddField
        # ran and before the backfill did - blank, same as the field's own
        # default.
        FriendInvitation.objects.filter(pk=invitation.pk).update(email_normalized="")

        migration._0049_backfill_friendinvitation_email_normalized(live_apps, None)

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


class BindingReplayTests(TestCase):
    """A second verification of the same address must not redeem a grant or notify twice."""

    def test_processing_twice_redeems_and_notifies_once(self) -> None:
        inviter = baker.make(User).profile
        admin = baker.make(User)
        invitee = baker.make(User, email="invitee@example.com", is_active=False)
        role = baker.make(SubscriptionRole)
        invitation = FriendInvitation.objects.create(inviter=inviter, email=invitee.email)
        PendingSubscriptionGrant.objects.create(invitation=invitation, role=role, granted_by=admin, duration_months="3")

        _process_pending_invitations(invitee)
        _process_pending_invitations(invitee)

        self.assertEqual(UserSubscription.objects.filter(user=invitee, role=role).count(), 1)
        self.assertEqual(
            NotificationLog.objects.filter(
                profile=invitee.profile, notification_type=NotificationType.FRIEND_REQUEST
            ).count(),
            1,
        )


class OnlyTheInvitedAddressCanAnswerTests(TestCase):
    """Holding the link is not holding the address: only an account that verified it may answer."""

    def setUp(self) -> None:
        super().setUp()
        self.inviter = baker.make(User, email="inviter@example.com").profile
        self.invitation = FriendInvitation.objects.create(inviter=self.inviter, email="invited@example.com")

    def _verified(self, username: str, email: str) -> User:
        user = baker.make(User, username=username, email=email, is_active=True)
        user.profile.verified_primary_email = user.profile.primary_email_normalized
        user.profile.save(update_fields=["verified_primary_email"])
        return user

    def _answer(self, answer: str = "accept"):
        return self.client.post(
            reverse("friend.invitation.answer", kwargs={"token": self.invitation.token}), {"answer": answer}
        )

    def test_another_signed_in_account_cannot_accept_it(self) -> None:
        stranger = self._verified("stranger", "stranger@example.com")
        self.client.force_login(stranger)

        self._answer()

        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.invitee_id)
        self.assertIsNone(self.invitation.accepted_at)
        self.assertIsNone(Friendship.objects.all().between(self.inviter, stranger.profile))

    def test_another_account_is_not_shown_the_answer_buttons(self) -> None:
        self.client.force_login(self._verified("stranger", "stranger@example.com"))

        body = self.client.get(reverse("friend.invitation", kwargs={"token": self.invitation.token})).content.decode()

        self.assertNotIn('value="accept"', body)

    def test_signing_up_through_the_link_with_another_address_binds_nothing(self) -> None:
        admin = baker.make(User)
        role = baker.make(SubscriptionRole)
        PendingSubscriptionGrant.objects.create(
            invitation=self.invitation, role=role, granted_by=admin, duration_months="3"
        )
        newcomer = baker.make(User, email="someone-else@example.com", is_active=False)
        verification = EmailVerification.objects.create(user=newcomer, pending_invite_token=self.invitation.token)

        self.client.get(reverse("verify_email", args=[verification.token]))

        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.invitee_id)
        self.assertFalse(UserSubscription.objects.filter(user=newcomer).exists())
        self.assertTrue(PendingSubscriptionGrant.objects.filter(invitation=self.invitation).exists())

    def test_the_account_that_verified_the_address_can_claim_it_on_the_page(self) -> None:
        owner = self._verified("owner", "invited@example.com")
        self.client.force_login(owner)

        self.client.get(reverse("friend.invitation", kwargs={"token": self.invitation.token}))
        self._answer()

        self.assertEqual(
            Friendship.objects.all().between(self.inviter, owner.profile).status, FriendshipStatus.ACCEPTED
        )

    def test_an_accept_cannot_override_a_decline_that_already_committed(self) -> None:
        from urbanlens.dashboard.services.social.friend_invitations import FriendInvitationError, accept, decline

        owner = self._verified("owner", "invited@example.com")
        FriendInvitation.objects.filter(pk=self.invitation.pk).update(invitee=owner.profile)
        stale = FriendInvitation.objects.get(pk=self.invitation.pk)
        decline(FriendInvitation.objects.get(pk=self.invitation.pk), owner.profile)

        with self.assertRaises(FriendInvitationError):
            accept(stale, owner.profile)

        self.assertIsNone(Friendship.objects.all().between(self.inviter, owner.profile))
