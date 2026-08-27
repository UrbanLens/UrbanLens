"""Removing an emergency contact must cut off their already-open chat socket.

``SafetyCheckinChatConsumer`` resolves authority once, at ``connect()``. For
partners that has always been paired with a revocation path - an immediate
``partner_access_revoked`` broadcast, plus periodic re-validation as a backstop
for a broadcast lost in transit (see ``test_safety_partners``). The contact
route had neither, on the stated reasoning that a magic-link token "is either
valid or it isn't".

A contact token is in fact revoked by *deleting the row*, and
``set_checkin_contacts`` deletes every contact missing from a resubmitted list.
So a contact removed while their portal was open kept receiving the check-in's
chat indefinitely - while the HTTP fallback serving the same data correctly
refused them, since it re-resolves the token on every request.

These mirror the partner tests one-for-one, including their two delivery
concerns: the broadcast is enqueued rather than performed (hence
``broadcasts_delivered_inline``), and it is best-effort (hence a separate test
that the periodic backstop closes the socket with no broadcast at all).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from channels.db import database_sync_to_async
from django.test import override_settings
from model_bakery import baker

from urbanlens.core.tests.celery_inline import broadcasts_delivered_inline
from urbanlens.dashboard.models.safety.model import SafetyCheckinContact
from urbanlens.dashboard.tests.hypothesis.test_safety_chat import _IN_MEMORY_CHANNEL_LAYERS, SafetyCheckinChatConsumerTests, _run


@override_settings(CHANNEL_LAYERS=_IN_MEMORY_CHANNEL_LAYERS)
class ContactAccessRevocationTests(SafetyCheckinChatConsumerTests):
    """The contact route's mirror of the partner-revocation guarantees."""

    def test_removed_contact_connection_is_closed(self) -> None:
        _run(self._removed_contact_connection_is_closed())

    async def _removed_contact_connection_is_closed(self):
        from urbanlens.dashboard.services.visits.safety import set_checkin_contacts

        contact_comm = self._contact_communicator(self.contact.token)
        self.assertTrue((await contact_comm.connect())[0])

        # The owner edits the check-in and submits an empty contact list.
        with broadcasts_delivered_inline():
            await database_sync_to_async(set_checkin_contacts)(self.checkin, [])
            close_message = await contact_comm.receive_output()

        self.assertEqual(close_message["type"], "websocket.close")
        self.assertEqual(close_message.get("code"), 4404)

    def test_revocation_only_closes_the_removed_contact(self) -> None:
        """Resubmitting a contact list must not cut off the contacts kept on it."""
        _run(self._revocation_only_closes_the_removed_contact())

    async def _revocation_only_closes_the_removed_contact(self):
        from urbanlens.dashboard.services.visits.safety import set_checkin_contacts

        kept_contact = await database_sync_to_async(baker.make)(
            "dashboard.SafetyCheckinContact",
            checkin=self.checkin,
            contact_profile=None,
            email="kept@example.com",
            name="Kept",
        )
        owner_comm = self._owner_communicator()
        self.assertTrue((await owner_comm.connect())[0])
        kept_comm = self._contact_communicator(kept_contact.token)
        self.assertTrue((await kept_comm.connect())[0])
        dropped_comm = self._contact_communicator(self.contact.token)
        self.assertTrue((await dropped_comm.connect())[0])

        with broadcasts_delivered_inline():
            await database_sync_to_async(set_checkin_contacts)(self.checkin, [(None, "kept@example.com", "Kept")])
            close_message = await dropped_comm.receive_output()
        self.assertEqual(close_message.get("code"), 4404)

        await owner_comm.send_to(text_data=json.dumps({"body": "heading home"}))
        await owner_comm.receive_from()
        still_delivered = json.loads(await kept_comm.receive_from())

        self.assertEqual(still_delivered["body"], "heading home")

        await owner_comm.disconnect()
        await kept_comm.disconnect()

    def test_dropped_revocation_broadcast_is_caught_by_periodic_revalidation(self) -> None:
        """The broadcast is best-effort, so the backstop has to stand on its own."""
        _run(self._dropped_revocation_broadcast_is_caught_by_periodic_revalidation())

    async def _dropped_revocation_broadcast_is_caught_by_periodic_revalidation(self):
        with patch("urbanlens.dashboard.consumers._PARTNER_REVALIDATION_INTERVAL_SECONDS", 0.05):
            contact_comm = self._contact_communicator(self.contact.token)
            self.assertTrue((await contact_comm.connect())[0])

            # Delete the row directly - unlike set_checkin_contacts this fires no
            # broadcast at all, simulating one lost in transit.
            await database_sync_to_async(SafetyCheckinContact.objects.filter(pk=self.contact.pk).delete)()

            close_message = await contact_comm.receive_output(timeout=2)

        self.assertEqual(close_message["type"], "websocket.close")
        self.assertEqual(close_message.get("code"), 4404)

    def test_a_live_contact_is_not_closed_by_the_revalidation_loop(self) -> None:
        """The backstop must not evict contacts who are still on the list."""
        _run(self._a_live_contact_is_not_closed_by_the_revalidation_loop())

    async def _a_live_contact_is_not_closed_by_the_revalidation_loop(self):
        with patch("urbanlens.dashboard.consumers._PARTNER_REVALIDATION_INTERVAL_SECONDS", 0.05):
            contact_comm = self._contact_communicator(self.contact.token)
            self.assertTrue((await contact_comm.connect())[0])

            self.assertTrue(await contact_comm.receive_nothing(timeout=0.5), "a contact still on the list must stay connected")

        await contact_comm.disconnect()
