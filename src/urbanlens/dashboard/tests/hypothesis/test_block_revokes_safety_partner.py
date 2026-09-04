"""Blocking someone must end any safety-partner access between the two profiles.

An accepted `SafetyCheckinPartner` sees the owner's live location, their check-in
chat, and their escalation status. Blocking is the strongest "stop" this app
offers, and it left those rows - and any open WebSocket - untouched: the blocked
partner kept watching.

`remove_checkin_partner` is already the right mechanism. It deletes the row and
calls `_broadcast_partner_access_revoked`, which closes live connections whose
permission was only checked at connect() time. Nothing called it from the
blocking flow.

Both directions are revoked, which is what the filed entry asked for. Blocking is
a mutual disengagement: the blocker plainly does not want the blocked profile
watching them, and continuing to watch someone you have blocked is the same
relationship viewed from the other side.

Invited-but-not-yet-accepted rows go too - an outstanding invitation is an offer
of exactly the access being revoked.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.pin_share.meta import PinShareStatus
from urbanlens.dashboard.models.pin_share.model import PinShare
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner, SafetyCheckinPartnerStatus
from urbanlens.dashboard.services.social.friendship import block_profile


def _profile() -> Profile:
    return baker.make("auth.User").profile


def _checkin(profile: Profile):
    return baker.make(
        "dashboard.SafetyCheckin",
        profile=profile,
        title="Test hike",
        checkin_by=timezone.now() + datetime.timedelta(hours=2),
        grace_period=datetime.timedelta(hours=1),
    )


class BlockRevokesSafetyPartnerTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.blocker = _profile()
        self.blocked = _profile()

    def test_blocking_removes_the_blocked_profile_from_your_checkin(self) -> None:
        checkin = _checkin(self.blocker)
        partner = SafetyCheckinPartner.objects.create(
            checkin=checkin,
            profile=self.blocked,
            invited_by=self.blocker,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )

        with mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_partner_access_revoked") as revoke:
            block_profile(self.blocker, self.blocked)

        self.assertFalse(
            SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(),
            "a blocked profile still has partner access to the blocker's check-in",
        )
        # A bare `.delete()` would satisfy the assertion above too - this proves the
        # row went through `remove_checkin_partner`, which also closes any live
        # WebSocket, rather than a delete that silently skips that revocation.
        revoke.assert_called_once_with(checkin, self.blocked.pk)

    def test_blocking_also_ends_your_own_access_to_theirs(self) -> None:
        """The other direction - blocking is mutual disengagement."""
        checkin = _checkin(self.blocked)
        partner = SafetyCheckinPartner.objects.create(
            checkin=checkin,
            profile=self.blocker,
            invited_by=self.blocked,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )

        with mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_partner_access_revoked") as revoke:
            block_profile(self.blocker, self.blocked)

        self.assertFalse(
            SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(),
            "the blocker kept watching a profile they blocked",
        )
        revoke.assert_called_once_with(checkin, self.blocker.pk)

    def test_an_outstanding_invitation_is_revoked_too(self) -> None:
        partner = SafetyCheckinPartner.objects.create(
            checkin=_checkin(self.blocker),
            profile=self.blocked,
            invited_by=self.blocker,
            status=SafetyCheckinPartnerStatus.INVITED,
        )

        block_profile(self.blocker, self.blocked)

        self.assertFalse(
            SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(), "an outstanding invitation survived the block"
        )

    def test_an_unrelated_partner_is_untouched(self) -> None:
        """The revocation must be scoped to the two profiles involved."""
        bystander = _profile()
        partner = SafetyCheckinPartner.objects.create(
            checkin=_checkin(self.blocker),
            profile=bystander,
            invited_by=self.blocker,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )

        block_profile(self.blocker, self.blocked)

        self.assertTrue(
            SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(),
            "blocking one profile removed an unrelated partner",
        )

    def test_a_partnership_on_someone_elses_checkin_is_untouched(self) -> None:
        """The scoping is by *pair*, not by "either profile appears somewhere".

        Both rows below involve one of the two blocking profiles, but paired
        with a third party rather than each other - a filter that matched on
        either profile alone (instead of the specific actor/target pair) would
        wrongly sweep these up too.
        """
        third_party = _profile()
        blocked_elsewhere = SafetyCheckinPartner.objects.create(
            checkin=_checkin(third_party),
            profile=self.blocked,
            invited_by=third_party,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )
        blocker_elsewhere = SafetyCheckinPartner.objects.create(
            checkin=_checkin(third_party),
            profile=self.blocker,
            invited_by=third_party,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )

        block_profile(self.blocker, self.blocked)

        self.assertTrue(
            SafetyCheckinPartner.objects.filter(pk=blocked_elsewhere.pk).exists(),
            "blocking removed the blocked profile's partnership on an unrelated check-in",
        )
        self.assertTrue(
            SafetyCheckinPartner.objects.filter(pk=blocker_elsewhere.pk).exists(),
            "blocking removed the blocker's own partnership on an unrelated check-in",
        )


class BlockWithdrawsPendingPinShareTests(TestCase):
    """A pending share is an offer; blocking withdraws it.

    This codebase already draws the line: ``DirectMessageShare.revoke`` undoes a
    share "only if the recipient hasn't acted on it yet", and leaves accepted
    ones "completely alone - there is nothing to revoke once the recipient has
    acted". Accepting a pin share runs ``create_pin_from_share``, so the
    recipient ends up owning their own Pin - taking the share row back would
    give nothing back.

    A *pending* share is the other case, and the accept path does not re-check
    blocking: without this, a profile could block someone and have them accept
    the standing offer afterwards, ending up with a copy of a place the blocker
    had just withdrawn from them.
    """

    def setUp(self) -> None:
        super().setUp()
        self.blocker = _profile()
        self.blocked = _profile()

    def _share(self, sender: Profile, recipient: Profile, status: str) -> PinShare:
        return baker.make(PinShare, from_profile=sender, to_profile=recipient, status=status)

    def test_a_pending_share_to_the_blocked_profile_is_withdrawn(self) -> None:
        share = self._share(self.blocker, self.blocked, PinShareStatus.PENDING)

        block_profile(self.blocker, self.blocked)

        share.refresh_from_db()
        self.assertEqual(share.status, PinShareStatus.REJECTED, "a blocked profile can still accept the standing offer")

    def test_a_pending_share_from_the_blocked_profile_is_withdrawn_too(self) -> None:
        share = self._share(self.blocked, self.blocker, PinShareStatus.PENDING)

        block_profile(self.blocker, self.blocked)

        share.refresh_from_db()
        self.assertEqual(share.status, PinShareStatus.REJECTED)

    def test_an_accepted_share_is_left_alone(self) -> None:
        """The recipient already owns their own pin; there is nothing to take back."""
        share = self._share(self.blocker, self.blocked, PinShareStatus.ACCEPTED)

        block_profile(self.blocker, self.blocked)

        share.refresh_from_db()
        self.assertEqual(share.status, PinShareStatus.ACCEPTED)

    def test_an_unrelated_pending_share_is_untouched(self) -> None:
        bystander = _profile()
        share = self._share(self.blocker, bystander, PinShareStatus.PENDING)

        block_profile(self.blocker, self.blocked)

        share.refresh_from_db()
        self.assertEqual(share.status, PinShareStatus.PENDING)

    def test_a_pending_share_between_the_blocked_profile_and_a_bystander_is_untouched(self) -> None:
        """Same scoping concern from the other profile's side of the pair.

        A filter matching on either party alone (rather than the specific
        actor/target pair) would wrongly withdraw this - it never involves
        the blocker at all.
        """
        bystander = _profile()
        share = self._share(self.blocked, bystander, PinShareStatus.PENDING)

        block_profile(self.blocker, self.blocked)

        share.refresh_from_db()
        self.assertEqual(share.status, PinShareStatus.PENDING)
