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

from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
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
        partner = SafetyCheckinPartner.objects.create(
            checkin=_checkin(self.blocker),
            profile=self.blocked,
            invited_by=self.blocker,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )

        block_profile(self.blocker, self.blocked)

        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(), "a blocked profile still has partner access to the blocker's check-in")

    def test_blocking_also_ends_your_own_access_to_theirs(self) -> None:
        """The other direction - blocking is mutual disengagement."""
        partner = SafetyCheckinPartner.objects.create(
            checkin=_checkin(self.blocked),
            profile=self.blocker,
            invited_by=self.blocked,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )

        block_profile(self.blocker, self.blocked)

        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(), "the blocker kept watching a profile they blocked")

    def test_an_outstanding_invitation_is_revoked_too(self) -> None:
        partner = SafetyCheckinPartner.objects.create(
            checkin=_checkin(self.blocker),
            profile=self.blocked,
            invited_by=self.blocker,
            status=SafetyCheckinPartnerStatus.INVITED,
        )

        block_profile(self.blocker, self.blocked)

        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(), "an outstanding invitation survived the block")

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

        self.assertTrue(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists(), "blocking one profile removed an unrelated partner")
