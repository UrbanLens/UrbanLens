"""Tests for safety check-in live location: sharing toggle, position updates, and the
chat consumer's location-group scoping (session route only, never the contact route).
"""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from hypothesis import given, settings, strategies as st
from model_bakery import baker

from urbanlens.core.tests.celery_inline import broadcasts_delivered_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.consumers import SafetyCheckinChatConsumer
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinContact,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
)
from urbanlens.dashboard.services.visits.safety import set_live_location_sharing, update_live_location

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


class UpdateLiveLocationTests(TestCase):
    """update_live_location's rejection branches, plus set_live_location_sharing's clearing behavior."""

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner)

    def test_raises_when_sharing_disabled(self):
        with self.assertRaisesMessage(ValueError, "not enabled"):
            update_live_location(self.checkin, latitude=1.0, longitude=2.0, accuracy=None)

    def test_raises_when_checkin_already_resolved(self):
        set_live_location_sharing(self.checkin, enabled=True)
        self.checkin.status = SafetyCheckinStatus.CHECKED_IN
        self.checkin.resolved_at = timezone.now()
        self.checkin.save(update_fields=["status", "resolved_at", "updated"])

        with self.assertRaisesMessage(ValueError, "already concluded"):
            update_live_location(self.checkin, latitude=1.0, longitude=2.0, accuracy=None)

    def test_update_succeeds_while_sharing_is_enabled(self):
        set_live_location_sharing(self.checkin, enabled=True)

        update_live_location(self.checkin, latitude=40.0, longitude=-74.0, accuracy=12.5)

        self.checkin.refresh_from_db()
        self.assertEqual(float(self.checkin.live_latitude), 40.0)
        self.assertEqual(float(self.checkin.live_longitude), -74.0)
        self.assertEqual(self.checkin.live_location_accuracy, 12.5)
        self.assertIsNotNone(self.checkin.live_location_updated_at)

    def test_concurrent_toggle_off_is_not_masked_by_a_stale_in_memory_flag(self):
        """Regression guard: update_live_location must re-check sharing-enabled against
        the DB at write time, not the caller's possibly-stale in-memory `checkin` - a
        toggle-off landing between the caller's own check and this call must never
        persist a real position onto a row now flagged "sharing disabled".
        """
        set_live_location_sharing(self.checkin, enabled=True)
        # Simulate another request disabling sharing concurrently, without refreshing
        # this in-memory `self.checkin` - it still reads live_location_sharing_enabled=True.
        SafetyCheckin.objects.filter(pk=self.checkin.pk).update(live_location_sharing_enabled=False)

        with self.assertRaisesMessage(ValueError, "not enabled"):
            update_live_location(self.checkin, latitude=40.0, longitude=-74.0, accuracy=12.5)

        self.checkin.refresh_from_db()
        self.assertIsNone(self.checkin.live_latitude)

    def test_disabling_sharing_clears_the_last_known_position(self):
        set_live_location_sharing(self.checkin, enabled=True)
        update_live_location(self.checkin, latitude=40.0, longitude=-74.0, accuracy=12.5)

        set_live_location_sharing(self.checkin, enabled=False)

        self.checkin.refresh_from_db()
        self.assertFalse(self.checkin.live_location_sharing_enabled)
        self.assertIsNone(self.checkin.live_latitude)
        self.assertIsNone(self.checkin.live_longitude)
        self.assertIsNone(self.checkin.live_location_accuracy)
        self.assertIsNone(self.checkin.live_location_updated_at)


class UpdateLiveLocationPrecisionTests(TestCase):
    """update_live_location round-trips lat/lng through the DecimalField(9,6) column
    without silent truncation, for arbitrary real-world coordinates.
    """

    @settings(max_examples=25, deadline=None)
    @given(
        latitude=st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False),
        longitude=st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False),
        accuracy=st.one_of(st.none(), st.floats(min_value=0, max_value=100_000, allow_nan=False, allow_infinity=False)),
    )
    def test_round_trips_within_decimal_field_precision(self, latitude, longitude, accuracy):
        owner = _profile()
        checkin = _checkin(owner)
        set_live_location_sharing(checkin, enabled=True)

        update_live_location(checkin, latitude=latitude, longitude=longitude, accuracy=accuracy)

        checkin.refresh_from_db()
        # places=5, not 6: DecimalField(max_digits=9, decimal_places=6) can round the 6th
        # decimal place either up or down, so a value sitting exactly on that rounding
        # boundary (e.g. 1.0703125) can differ from the input by up to 5e-7 - exactly the
        # places=6 threshold - making that stricter tolerance flaky by construction, not a
        # sign of truncation (which would produce an error orders of magnitude larger).
        self.assertAlmostEqual(float(checkin.live_latitude), latitude, places=5)
        self.assertAlmostEqual(float(checkin.live_longitude), longitude, places=5)
        if accuracy is None:
            self.assertIsNone(checkin.live_location_accuracy)
        else:
            self.assertAlmostEqual(checkin.live_location_accuracy, accuracy, places=6)


def _run(coro):
    """Run *coro* via async_to_sync - see test_safety_chat.py's identical helper for why
    a bare asyncio.run() won't work with database_sync_to_async's thread-sensitive mode.
    """

    async def _wrap():
        return await coro

    return async_to_sync(_wrap)()


@override_settings(CHANNEL_LAYERS=_IN_MEMORY_CHANNEL_LAYERS)
class SafetyCheckinLocationGroupScopingTests(TransactionTestCase):
    """The chat consumer's location group: session route only, never the contact route -
    even for a profile that is simultaneously a contact *and* an accepted partner on the
    very same check-in.
    """

    def setUp(self):
        self.owner_user = baker.make("auth.User")
        self.owner_profile = self.owner_user.profile
        self.checkin = _checkin(self.owner_profile)

    def _session_communicator(self, user) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(
            SafetyCheckinChatConsumer.as_asgi(), f"/ws/safety/checkin/{self.checkin.uuid}/chat/"
        )
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": str(self.checkin.uuid), "token": None}}
        comm.scope["user"] = user
        return comm

    def _contact_communicator(self, token) -> WebsocketCommunicator:
        comm = WebsocketCommunicator(SafetyCheckinChatConsumer.as_asgi(), f"/ws/safety/contact/{token}/chat/")
        comm.scope["url_route"] = {"kwargs": {"checkin_uuid": None, "token": str(token)}}
        comm.scope["user"] = AnonymousUser()
        return comm

    def test_contact_route_never_receives_location_even_as_an_accepted_partner(self):
        _run(self._contact_route_never_receives_location_even_as_an_accepted_partner())

    async def _contact_route_never_receives_location_even_as_an_accepted_partner(self):
        @database_sync_to_async
        def _setup():
            dual_user = baker.make("auth.User")
            dual_profile = dual_user.profile
            contact = SafetyCheckinContact.objects.create(
                checkin=self.checkin, contact_profile=dual_profile, email=None
            )
            SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=dual_profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.ACCEPTED,
            )
            set_live_location_sharing(self.checkin, enabled=True)
            return contact, dual_user

        contact, dual_user = await _setup()

        contact_comm = self._contact_communicator(contact.token)
        connected, _ = await contact_comm.connect()
        self.assertTrue(connected)

        session_comm = self._session_communicator(dual_user)
        connected, _ = await session_comm.connect()
        self.assertTrue(connected)

        # Location updates reach a socket via a group_send the service enqueues
        # rather than performs (see core.tests.celery_inline).
        with broadcasts_delivered_inline():
            await database_sync_to_async(update_live_location)(self.checkin, latitude=1.0, longitude=2.0, accuracy=None)
            session_msg = json.loads(await session_comm.receive_from())
        self.assertEqual(session_msg["type"], "location_update")
        self.assertEqual(session_msg["latitude"], 1.0)

        # The contact-route connection for this exact same profile must see nothing -
        # the location group is scoped to the connection route, not the profile.
        self.assertTrue(await contact_comm.receive_nothing(timeout=0.3))

        await contact_comm.disconnect()
        await session_comm.disconnect()

    def test_session_route_partner_receives_location_updates(self):
        _run(self._session_route_partner_receives_location_updates())

    async def _session_route_partner_receives_location_updates(self):
        @database_sync_to_async
        def _setup():
            partner_user = baker.make("auth.User")
            SafetyCheckinPartner.objects.create(
                checkin=self.checkin,
                profile=partner_user.profile,
                invited_by=self.owner_profile,
                status=SafetyCheckinPartnerStatus.ACCEPTED,
            )
            set_live_location_sharing(self.checkin, enabled=True)
            return partner_user

        partner_user = await _setup()

        partner_comm = self._session_communicator(partner_user)
        connected, _ = await partner_comm.connect()
        self.assertTrue(connected)

        with broadcasts_delivered_inline():
            await database_sync_to_async(update_live_location)(
                self.checkin, latitude=40.0, longitude=-74.0, accuracy=5.0
            )
            msg = json.loads(await partner_comm.receive_from())
        self.assertEqual(msg["type"], "location_update")
        self.assertEqual(msg["latitude"], 40.0)
        self.assertEqual(msg["longitude"], -74.0)
        self.assertEqual(msg["accuracy"], 5.0)

        await partner_comm.disconnect()
