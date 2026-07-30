"""Tests for safety check-in partners: invite validation, the owner/partner permission
check, the detail view's partner fallback, and the chat consumer's widened access.
"""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

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
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.safety.model import SafetyCheckin, SafetyCheckinPartner, SafetyCheckinPartnerStatus
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.services.safety import accept_checkin_partner_invite, invite_checkin_partner, is_owner_or_accepted_partner, remove_checkin_partner

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


class AcceptCheckinPartnerInviteTests(TestCase):
    """accept_checkin_partner_invite's idempotency and its concurrent-removal guard."""

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner)
        self.invitee = _profile()
        self.partner = SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=self.invitee, invited_by=self.owner)

    def test_repeat_accept_is_a_no_op(self):
        accept_checkin_partner_invite(self.partner)
        NotificationLog.objects.all().delete()

        accept_checkin_partner_invite(self.partner)

        self.assertFalse(NotificationLog.objects.exists())

    def test_accept_after_concurrent_removal_is_a_no_op(self):
        """Regression guard: accepting a stale in-memory ``partner`` whose row the owner
        already removed concurrently must not resurrect it or send a phantom "partner
        accepted" notification - save() on a deleted row would otherwise silently
        no-op the UPDATE while the rest of the function ran anyway.
        """
        SafetyCheckinPartner.objects.filter(pk=self.partner.pk).delete()

        accept_checkin_partner_invite(self.partner)

        self.assertFalse(SafetyCheckinPartner.objects.filter(pk=self.partner.pk).exists())
        self.assertFalse(NotificationLog.objects.filter(profile=self.owner).exists())


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

    def test_removed_partner_connection_is_force_closed(self):
        """Regression guard: permission is otherwise only checked once, at connect()
        time - without a live revocation, a removed partner's already-open socket
        would keep receiving chat/status/location/archive events indefinitely.
        """
        _run(self._removed_partner_connection_is_force_closed())

    async def _removed_partner_connection_is_force_closed(self):
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _make_accepted_partner():
            partner_user = baker.make("auth.User")
            partner = SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=partner_user.profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.ACCEPTED,
            )
            return partner_user, partner

        partner_user, partner = await _make_accepted_partner()
        partner_comm = self._session_communicator(partner_user)
        connected, _ = await partner_comm.connect()
        self.assertTrue(connected)

        await database_sync_to_async(remove_checkin_partner)(partner)

        close_message = await partner_comm.receive_output()
        self.assertEqual(close_message["type"], "websocket.close")
        self.assertEqual(close_message.get("code"), 4404)

    def test_removed_partner_is_closed_on_every_simultaneous_connection(self):
        """Regression guard: a removed partner logged in from two tabs/devices at once
        must have BOTH connections closed - the group_send that drives revocation
        reaches every channel_name registered for the group, not just one of them.
        """
        _run(self._removed_partner_is_closed_on_every_simultaneous_connection())

    async def _removed_partner_is_closed_on_every_simultaneous_connection(self):
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _make_accepted_partner():
            partner_user = baker.make("auth.User")
            partner = SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=partner_user.profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.ACCEPTED,
            )
            return partner_user, partner

        partner_user, partner = await _make_accepted_partner()
        tab_one = self._session_communicator(partner_user)
        tab_two = self._session_communicator(partner_user)
        connected_one, _ = await tab_one.connect()
        connected_two, _ = await tab_two.connect()
        self.assertTrue(connected_one)
        self.assertTrue(connected_two)

        await database_sync_to_async(remove_checkin_partner)(partner)

        close_one = await tab_one.receive_output()
        close_two = await tab_two.receive_output()
        self.assertEqual(close_one["type"], "websocket.close")
        self.assertEqual(close_one.get("code"), 4404)
        self.assertEqual(close_two["type"], "websocket.close")
        self.assertEqual(close_two.get("code"), 4404)

    def test_write_access_is_revoked_immediately_even_before_the_close_arrives(self):
        """Regression guard for _create_message's in-band recheck: a removed partner's
        connection may not have processed its close frame yet (or the revocation
        broadcast may never arrive at all, see the periodic-revalidation test below) -
        either way, an attempted send in that window must be rejected, not accepted.
        """
        _run(self._write_access_is_revoked_immediately_even_before_the_close_arrives())

    async def _write_access_is_revoked_immediately_even_before_the_close_arrives(self):
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _make_accepted_partner():
            partner_user = baker.make("auth.User")
            partner = SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=partner_user.profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.ACCEPTED,
            )
            return partner_user, partner

        partner_user, partner = await _make_accepted_partner()
        partner_comm = self._session_communicator(partner_user)
        connected, _ = await partner_comm.connect()
        self.assertTrue(connected)

        # Remove the row directly via the ORM - no partner_access_revoked broadcast at
        # all, so the connection is still open and would otherwise still look authorized.
        await database_sync_to_async(SafetyCheckinPartner.objects.filter(pk=partner.pk).delete)()

        await partner_comm.send_to(text_data=json.dumps({"body": "Still here?"}))
        response = json.loads(await partner_comm.receive_from())
        self.assertEqual(response["type"], "error")

        @database_sync_to_async
        def _message_count():
            return SafetyCheckin.objects.get(pk=self.checkin.pk).messages.count()

        self.assertEqual(await _message_count(), 0)

    def test_dropped_revocation_broadcast_is_caught_by_periodic_revalidation(self):
        """Regression guard: partner_access_revoked (the group_send remove_checkin_partner
        fires) is best-effort, like every other broadcast in this module - if it's ever
        lost (a channel-layer hiccup), the periodic re-validation backstop must still
        close the connection on its own, rather than leaving it open indefinitely.
        """
        _run(self._dropped_revocation_broadcast_is_caught_by_periodic_revalidation())

    async def _dropped_revocation_broadcast_is_caught_by_periodic_revalidation(self):
        from channels.db import database_sync_to_async

        @database_sync_to_async
        def _make_accepted_partner():
            partner_user = baker.make("auth.User")
            partner = SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=partner_user.profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.ACCEPTED,
            )
            return partner_user, partner

        partner_user, partner = await _make_accepted_partner()
        with patch("urbanlens.dashboard.consumers._PARTNER_REVALIDATION_INTERVAL_SECONDS", 0.05):
            partner_comm = self._session_communicator(partner_user)
            connected, _ = await partner_comm.connect()
            self.assertTrue(connected)

            # Remove the row directly via the ORM - unlike remove_checkin_partner, this
            # fires no partner_access_revoked broadcast at all, simulating one that was
            # lost in transit.
            await database_sync_to_async(SafetyCheckinPartner.objects.filter(pk=partner.pk).delete)()

            close_message = await partner_comm.receive_output(timeout=2)
        self.assertEqual(close_message["type"], "websocket.close")
        self.assertEqual(close_message.get("code"), 4404)

    def test_unauthenticated_session_route_is_still_rejected(self):
        _run(self._unauthenticated_session_route_is_still_rejected())

    async def _unauthenticated_session_route_is_still_rejected(self):
        comm = self._session_communicator(AnonymousUser())
        connected, close_code = await comm.connect()
        self.assertFalse(connected)
        self.assertEqual(close_code, 4404)
