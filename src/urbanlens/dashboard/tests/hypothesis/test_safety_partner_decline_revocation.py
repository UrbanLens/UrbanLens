"""Regression guard: declining an *accepted* partner row must revoke live access."""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner, SafetyCheckinPartnerStatus
from urbanlens.dashboard.services.visits.safety import create_checkin, decline_checkin_partner_invite


class DeclineRevokesLiveAccessTests(TestCase):
    """What ``decline_checkin_partner_invite`` broadcasts, and when."""

    def setUp(self) -> None:
        """Create an owner with one live check-in, plus a partner profile."""
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.owner = Profile.objects.get(user=baker.make(User, username="explorer"))
        self.partner_profile = Profile.objects.get(user=baker.make(User, username="watcher"))
        self.checkin = create_checkin(
            profile=self.owner,
            title="Quarry trip",
            checkin_by=timezone.now() + datetime.timedelta(hours=6),
            grace_period=datetime.timedelta(hours=1),
            plan_details="North rim, back by dark",
            contact_message="Please call me",
            contacts=[(None, "friend@example.com", "Friend")],
        )

    def _partner(self, status: str) -> SafetyCheckinPartner:
        """Attach the partner profile to the fixture check-in with *status*.

        Args:
            status: One of ``SafetyCheckinPartnerStatus``.

        Returns:
            The created partner row."""
        return SafetyCheckinPartner.objects.create(
            checkin=self.checkin, profile=self.partner_profile, invited_by=self.owner, status=status
        )

    def test_declining_an_accepted_row_revokes_any_open_connection(self) -> None:
        """Resigning is a removal, and a removal has to reach the live socket."""
        partner = self._partner(SafetyCheckinPartnerStatus.ACCEPTED)

        with mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_partner_access_revoked") as revoke:
            decline_checkin_partner_invite(partner)

        revoke.assert_called_once_with(self.checkin, self.partner_profile.pk)
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists())

    def test_declining_a_pending_invite_broadcasts_nothing(self) -> None:
        """An invitee never had access, so there is nothing to revoke.

        Worth pinning down rather than treating as harmless: the revocation frame goes to the whole check-in
        group, and the consumer only acts on it when the payload's ``profile_id`` matches its own."""
        partner = self._partner(SafetyCheckinPartnerStatus.INVITED)

        with mock.patch("urbanlens.dashboard.services.visits.safety._broadcast_partner_access_revoked") as revoke:
            decline_checkin_partner_invite(partner)

        revoke.assert_not_called()
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists())
