"""Regression guard: declining an *accepted* partner row must revoke live access.

``SafetyCheckinChatConsumer`` checks "may this profile watch this check-in?" once,
at ``connect()`` time, and then keeps the socket open. That is why
``services.safety.remove_checkin_partner`` (the owner-initiated removal) ends with
``_broadcast_partner_access_revoked`` - without it, a removed partner keeps
receiving chat, status and live-location frames for as long as their tab stays
open.

``decline_checkin_partner_invite`` deletes exactly the same row and, until this
guard existed, did *not* broadcast. That was harmless while the only caller was
the web overview page (which renders a Decline button solely for ``INVITED``
rows, and an invitee has no socket to revoke). The external API's decline
endpoint uses the same owner-supplied queryset the controller does -
``filter(checkin__uuid=..., profile=caller)``, with no status filter, because a
status filter would make a repeat decline 404 for the wrong reason - so an
already-``ACCEPTED`` partner can now reach it as a "resign". At that point the
missing broadcast is a partner who resigned but keeps streaming someone's live
position, which is the single most sensitive read in this application.
"""

from __future__ import annotations

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.models.safety.model import SafetyCheckinPartner, SafetyCheckinPartnerStatus
from urbanlens.dashboard.services.safety import create_checkin, decline_checkin_partner_invite


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
            The created partner row.
        """
        return SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=self.partner_profile, invited_by=self.owner, status=status)

    def test_declining_an_accepted_row_revokes_any_open_connection(self) -> None:
        """Resigning is a removal, and a removal has to reach the live socket."""
        partner = self._partner(SafetyCheckinPartnerStatus.ACCEPTED)

        with mock.patch("urbanlens.dashboard.services.safety._broadcast_partner_access_revoked") as revoke:
            decline_checkin_partner_invite(partner)

        revoke.assert_called_once_with(self.checkin, self.partner_profile.pk)
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists())

    def test_declining_a_pending_invite_broadcasts_nothing(self) -> None:
        """An invitee never had access, so there is nothing to revoke.

        Worth pinning down rather than treating as harmless: the revocation frame
        goes to the whole check-in group, and the consumer only acts on it when the
        payload's ``profile_id`` matches its own. Broadcasting for someone who was
        never connected is wasted work on a group the owner is sitting in, and it
        would make the frame useless as a signal that access actually changed.
        """
        partner = self._partner(SafetyCheckinPartnerStatus.INVITED)

        with mock.patch("urbanlens.dashboard.services.safety._broadcast_partner_access_revoked") as revoke:
            decline_checkin_partner_invite(partner)

        revoke.assert_not_called()
        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=partner.pk).exists())
