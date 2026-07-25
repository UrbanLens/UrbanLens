"""Tests for safety check-in partners: invite validation, the owner/partner permission
check, the detail view's partner fallback, and the chat consumer's widened access.
"""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import SafetyCheckinChatConsumer
from urbanlens.dashboard.models.friendship.meta import FriendshipStatus
from urbanlens.dashboard.models.friendship.model import Friendship
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinPartner, SafetyCheckinPartnerStatus
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.safety import invite_checkin_partner, is_owner_or_accepted_partner

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile

_IN_MEMORY_CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}


def _profile(**kwargs) -> Profile:
    return baker.make("auth.User", **kwargs).profile


def _checkin(profile: Profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Test hike",
        "checkin_by": timezone.now() + datetime.timedelta(hours=2),
        "grace_period": datetime.timedelta(hours=1),
    }
    defaults.update(kwargs)
    return baker.make("dashboard.SafetyCheckin", **defaults)


class InviteCheckinPartnerTests(TestCase):
    """invite_checkin_partner's rejection branches, plus the success path."""

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner)

    def test_unknown_username_raises(self):
        with self.assertRaisesMessage(ValueError, "No user found"):
            invite_checkin_partner(self.checkin, inviter=self.owner, username="nobody-by-this-name")

    def test_self_invite_raises(self):
        with self.assertRaisesMessage(ValueError, "your own check-in"):
            invite_checkin_partner(self.checkin, inviter=self.owner, username=self.owner.username)

    def test_blocked_invitee_raises(self):
        invitee = _profile()
        Friendship.objects.create(from_profile=invitee, to_profile=self.owner, status=FriendshipStatus.BLOCKED)

        with self.assertRaisesMessage(ValueError, "isn't accepting invitations"):
            invite_checkin_partner(self.checkin, inviter=self.owner, username=invitee.username)

    def test_duplicate_invite_raises(self):
        invitee = _profile()
        invite_checkin_partner(self.checkin, inviter=self.owner, username=invitee.username)

        with self.assertRaisesMessage(ValueError, "already been invited"):
            invite_checkin_partner(self.checkin, inviter=self.owner, username=invitee.username)

    def test_over_cap_raises(self):
        settings = SiteSettings.get_current()
        settings.max_safety_checkin_partners = 1
        settings.save(update_fields=["max_safety_checkin_partners"])
        first = _profile()
        second = _profile()
        invite_checkin_partner(self.checkin, inviter=self.owner, username=first.username)

        with self.assertRaisesMessage(ValueError, "at most 1 partners"):
            invite_checkin_partner(self.checkin, inviter=self.owner, username=second.username)

    def test_successful_invite_creates_invited_partner(self):
        invitee = _profile()

        partner = invite_checkin_partner(self.checkin, inviter=self.owner, username=invitee.username)

        self.assertEqual(partner.status, SafetyCheckinPartnerStatus.INVITED)
        self.assertEqual(partner.profile_id, invitee.pk)
        self.assertEqual(partner.invited_by_id, self.owner.pk)


class IsOwnerOrAcceptedPartnerTests(TestCase):
    """is_owner_or_accepted_partner: the owner and unrelated-profile boundary cases.

    Kept in a class of its own, separate from the @given property test below -
    per this repo's CLAUDE.md, Hypothesis example-shrinking and this TestCase's
    per-test transaction rollback don't always compose cleanly, so a plain
    fixture-sharing test placed alongside a `@given` method in the same class
    can see leftover data from it.
    """

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner)
        self.other = _profile()

    def test_owner_is_always_authorized(self):
        self.assertTrue(is_owner_or_accepted_partner(self.checkin, self.owner))

    def test_unrelated_profile_is_never_authorized(self):
        self.assertFalse(is_owner_or_accepted_partner(self.checkin, self.other))


class IsOwnerOrAcceptedPartnerHypothesisTests(TestCase):
    """is_owner_or_accepted_partner is true iff a non-owner partner is ACCEPTED -
    never for an INVITED-but-not-yet-accepted partner. Isolated in its own class,
    see IsOwnerOrAcceptedPartnerTests's docstring for why.
    """

    @settings(max_examples=10, deadline=None)
    @given(st.sampled_from([SafetyCheckinPartnerStatus.INVITED, SafetyCheckinPartnerStatus.ACCEPTED]))
    def test_partner_authorized_iff_accepted(self, status):
        owner = _profile()
        checkin = _checkin(owner)
        other = _profile()
        SafetyCheckinPartner.objects.create(checkin=checkin, profile=other, invited_by=owner, status=status)

        authorized = is_owner_or_accepted_partner(checkin, other)

        self.assertEqual(authorized, status == SafetyCheckinPartnerStatus.ACCEPTED)


class SafetyCheckinDetailPartnerFallbackTests(TestCase):
    """SafetyCheckinDetailView's non-owner fallbacks: accepted partner vs. still-community-only."""

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner, plan_details="Secret plan details")

    def test_unrelated_profile_still_falls_through_to_community_view_and_404s(self):
        """Regression guard: a random authenticated non-owner, non-partner hitting the
        uuid route must still 404 via the existing community-view fallback, unchanged
        by the new partner branch being tried first.
        """
        stranger = _profile()
        self.client.force_login(stranger.user)

        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(self.checkin.uuid)}))

        self.assertEqual(response.status_code, 404)

    def test_accepted_partner_sees_full_detail_page(self):
        partner_profile = _profile()
        SafetyCheckinPartner.objects.create(
            checkin=self.checkin,
            profile=partner_profile,
            invited_by=self.owner,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )
        self.client.force_login(partner_profile.user)

        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(self.checkin.uuid)}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Secret plan details")
        self.assertTrue(response.context["viewer_is_partner"])

    def test_invited_but_not_accepted_partner_does_not_see_full_detail_page(self):
        partner_profile = _profile()
        SafetyCheckinPartner.objects.create(
            checkin=self.checkin,
            profile=partner_profile,
            invited_by=self.owner,
            status=SafetyCheckinPartnerStatus.INVITED,
        )
        self.client.force_login(partner_profile.user)

        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(self.checkin.uuid)}))

        self.assertEqual(response.status_code, 404)


def _run(coro):
    """Run *coro* via async_to_sync - see test_safety_chat.py's identical helper for why
    a bare asyncio.run() won't work with database_sync_to_async's thread-sensitive mode.
    """

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


@override_settings(CHANNEL_LAYERS=_IN_MEMORY_CHANNEL_LAYERS)
class SafetyCheckinChatConsumerPartnerTests(TransactionTestCase):
    """SafetyCheckinChatConsumer's widened session-route permission check."""

    def setUp(self):
        self.owner_user = baker.make("auth.User")
        self.owner_profile = self.owner_user.profile
        self.checkin = _checkin(self.owner_profile)

    def _session_communicator(self, user) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(SafetyCheckinChatConsumer.as_asgi(), f"/ws/safety/checkin/{self.checkin.uuid}/chat/")
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": str(self.checkin.uuid), "token": None}}
        comm.scope["user"] = user
        return comm

    def test_invited_but_not_accepted_partner_is_rejected(self):
        _run(self._invited_but_not_accepted_partner_is_rejected())

    async def _invited_but_not_accepted_partner_is_rejected(self):
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _make_invited_partner():
            partner_user = baker.make("auth.User")
            SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=partner_user.profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.INVITED,
            )
            return partner_user

        partner_user = await _make_invited_partner()
        comm = self._session_communicator(partner_user)
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)

    def test_accepted_partner_can_connect_and_receive_owner_messages(self):
        _run(self._accepted_partner_can_connect_and_receive_owner_messages())

    async def _accepted_partner_can_connect_and_receive_owner_messages(self):
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _make_accepted_partner():
            partner_user = baker.make("auth.User")
            SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=partner_user.profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.ACCEPTED,
            )
            return partner_user

        partner_user = await _make_accepted_partner()

        owner_comm = self._session_communicator(self.owner_user)
        connected, _ = await owner_comm.connect()
        self.assertTrue(connected)

        partner_comm = self._session_communicator(partner_user)
        connected, _ = await partner_comm.connect()
        self.assertTrue(connected)

        await owner_comm.send_to(text_data=json.dumps({"body": "Heading up the trail now"}))

        owner_echo = json.loads(await owner_comm.receive_from())
        partner_recv = json.loads(await partner_comm.receive_from())
        self.assertEqual(owner_echo["body"], "Heading up the trail now")
        self.assertEqual(partner_recv["body"], "Heading up the trail now")

        await owner_comm.disconnect()
        await partner_comm.disconnect()

    def test_unauthenticated_session_route_is_still_rejected(self):
        _run(self._unauthenticated_session_route_is_still_rejected())

    async def _unauthenticated_session_route_is_still_rejected(self):
        comm = self._session_communicator(AnonymousUser())
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)
