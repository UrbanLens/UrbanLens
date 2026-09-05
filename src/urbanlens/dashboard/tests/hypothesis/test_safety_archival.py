"""Tests for the safety check-in archival/encryption pipeline: the PyNaCl seal/secretbox
wire format, archive_checkin's idempotency and no-bundle-yet handling, SafetyContactOptOut
FK integrity after a contact is scrubbed, and schedule_checkin_archival's immediate-vs-
grace-window logic.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
from typing import TYPE_CHECKING
from unittest import mock

from django.urls import reverse
from django.utils import timezone
from model_bakery import baker
import nacl.public
import nacl.secret

from hypothesis import given, settings, strategies as st
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.e2ee.key_bundle import MessagingKeyBundle
from urbanlens.dashboard.models.safety.model import (
    SafetyCheckin,
    SafetyCheckinArchive,
    SafetyCheckinContact,
    SafetyCheckinPartner,
    SafetyCheckinPartnerStatus,
    SafetyCheckinStatus,
    SafetyContactOptOut,
    SafetyContactOptOutScope,
)
from urbanlens.dashboard.services.notifications.notifications import NotificationEvent
from urbanlens.dashboard.services.visits.safety import (
    MAX_ARCHIVE_ATTEMPTS,
    _seal_archive_payload,
    archive_checkin,
    schedule_checkin_archival,
)

if TYPE_CHECKING:
    from urbanlens.dashboard.models.profile.model import Profile


def _profile(**kwargs) -> Profile:
    return baker.make("auth.User", **kwargs).profile


def _checkin(profile: Profile, **kwargs) -> SafetyCheckin:
    defaults = {
        "profile": profile,
        "title": "Test hike",
        "plan_details": "Hike to the summit and back",
        "checkin_by": timezone.now() - datetime.timedelta(hours=1),
        "grace_period": datetime.timedelta(hours=1),
        "status": SafetyCheckinStatus.FOUND_SAFE,
        "resolved_at": timezone.now(),
        "resolved_by_label": "you",
    }
    defaults.update(kwargs)
    return baker.make("dashboard.SafetyCheckin", **defaults)


def _enroll(profile: Profile, *, public_key: bytes | None = None) -> MessagingKeyBundle:
    """Mirrors test_e2ee.py's own _enroll helper - a bundle with a syntactically valid
    (but not necessarily "real") X25519 public key is enough for anything that only
    needs a bundle to *exist*; tests that verify the sealed content round-trips use a
    real generated keypair instead (see SealArchivePayloadInteropTests).
    """
    return MessagingKeyBundle.objects.create(
        profile=profile,
        public_key=base64.b64encode(public_key or os.urandom(32)).decode(),
        recovery_wrapped_secret=base64.b64encode(os.urandom(72)).decode(),
    )


class SealArchivePayloadInteropTests(TestCase):
    """_seal_archive_payload's output opens with plain PyNaCl exactly like the owner's
    browser will need to - the same class of assertion test_e2ee_interop.py already
    makes for the DM primitives, exercised here against this feature's actual
    production function rather than a reimplementation of the primitive.
    """

    def test_seal_then_open_round_trips_the_payload(self):
        keypair = nacl.public.PrivateKey.generate()
        public_key_b64 = base64.b64encode(keypair.public_key.encode()).decode()
        payload = {
            "title": "Weekend hike",
            "plan_details": "Up and back before dark",
            "contacts": [{"display_name": "A Friend", "email": "friend@example.com"}],
            "resolved_by_label": "you",
        }

        ciphertext_b64, nonce_b64, sealed_key_b64 = _seal_archive_payload(payload, public_key_b64)

        symmetric_key = nacl.public.SealedBox(keypair).decrypt(base64.b64decode(sealed_key_b64))
        recovered_json = nacl.secret.SecretBox(symmetric_key).decrypt(
            base64.b64decode(ciphertext_b64), base64.b64decode(nonce_b64)
        )
        self.assertEqual(json.loads(recovered_json), payload)

    def test_a_different_keypair_cannot_open_the_sealed_key(self):
        keypair = nacl.public.PrivateKey.generate()
        attacker = nacl.public.PrivateKey.generate()
        public_key_b64 = base64.b64encode(keypair.public_key.encode()).decode()

        _ciphertext_b64, _nonce_b64, sealed_key_b64 = _seal_archive_payload({"a": 1}, public_key_b64)

        with self.assertRaises(Exception):
            nacl.public.SealedBox(attacker).decrypt(base64.b64decode(sealed_key_b64))


class ArchiveCheckinTests(TestCase):
    """archive_checkin's no-bundle-yet, idempotency, and scrub behavior."""

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner)

    def test_no_op_when_owner_has_no_key_bundle_yet(self):
        archive_checkin(self.checkin)

        self.checkin.refresh_from_db()
        self.assertFalse(hasattr(self.checkin, "archive"))
        self.assertEqual(self.checkin.title, "Test hike")

    def test_archives_and_scrubs_once_a_bundle_exists(self):
        _enroll(self.owner)

        archive_checkin(self.checkin)

        self.checkin.refresh_from_db()
        self.assertTrue(SafetyCheckinArchive.objects.filter(checkin=self.checkin).exists())
        self.assertEqual(self.checkin.title, "")
        self.assertEqual(self.checkin.plan_details, "")
        self.assertEqual(self.checkin.resolved_by_label, "")

    def test_is_idempotent(self):
        _enroll(self.owner)
        archive_checkin(self.checkin)

        archive_checkin(self.checkin)

        self.assertEqual(SafetyCheckinArchive.objects.filter(checkin=self.checkin).count(), 1)

    def test_email_only_contact_email_is_replaced_not_nulled(self):
        """Regression guard: nulling email on a contact with no linked profile would
        violate SafetyCheckinContact's exactly-one-of(contact_profile, email) CheckConstraint.
        """
        _enroll(self.owner)
        contact = SafetyCheckinContact.objects.create(checkin=self.checkin, email="watcher@example.com", name="Watcher")

        archive_checkin(self.checkin)

        contact.refresh_from_db()
        self.assertIsNotNone(contact.email)
        self.assertNotEqual(contact.email, "watcher@example.com")
        self.assertEqual(contact.name, "")

    def test_scrubs_destination_location_trip_and_markup_map_links(self):
        """Regression guard: these FKs must not survive archival - a DB-level reader
        could otherwise join straight through the archived checkin row to plaintext
        destination/trip/route data, defeating the "only the owner can decrypt this"
        promise. The linked rows themselves are untouched (severed, not deleted) -
        they're independently owned/shared resources with their own lifecycle.
        """
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.markup.model import MarkupMap
        from urbanlens.dashboard.models.trips.model import Trip

        _enroll(self.owner)
        location = Location.objects.create(latitude="40.000000", longitude="-74.000000", official_name="Old Mill")
        trip = baker.make("dashboard.Trip", name="Weekend trip")
        markup_map = MarkupMap.objects.create(profile=self.owner, title="Route")
        reference_map = MarkupMap.objects.create(profile=self.owner, title="Reference")
        self.checkin.destination_location = location
        self.checkin.trip = trip
        self.checkin.markup_map = markup_map
        self.checkin.save(update_fields=["destination_location", "trip", "markup_map"])
        self.checkin.markup_maps.add(reference_map)

        archive_checkin(self.checkin)

        self.checkin.refresh_from_db()
        self.assertIsNone(self.checkin.destination_location_id)
        self.assertIsNone(self.checkin.trip_id)
        self.assertIsNone(self.checkin.markup_map_id)
        self.assertEqual(list(self.checkin.markup_maps.all()), [])
        self.assertTrue(Location.objects.filter(pk=location.pk).exists())
        self.assertTrue(Trip.objects.filter(pk=trip.pk).exists())
        self.assertTrue(MarkupMap.objects.filter(pk__in=[markup_map.pk, reference_map.pk]).count() == 2)

    def test_payload_captures_location_trip_and_map_content_before_scrubbing(self):
        """The owner must not lose the destination/trip/route content just because the
        checkin's own FKs to them are severed - it has to survive inside what they can
        decrypt. Round-trips through a real keypair, like SealArchivePayloadInteropTests.
        """
        from urbanlens.dashboard.models.location.model import Location
        from urbanlens.dashboard.models.markup.model import MarkupMap
        from urbanlens.dashboard.models.trips.model import Trip

        keypair = nacl.public.PrivateKey.generate()
        _enroll(self.owner, public_key=keypair.public_key.encode())
        location = Location.objects.create(latitude="40.000000", longitude="-74.000000", official_name="Old Mill")
        trip = baker.make("dashboard.Trip", name="Weekend trip")
        markup_map = MarkupMap.objects.create(profile=self.owner, title="Route")
        self.checkin.destination_location = location
        self.checkin.trip = trip
        self.checkin.markup_map = markup_map
        self.checkin.save(update_fields=["destination_location", "trip", "markup_map"])

        archive_checkin(self.checkin)

        archive = SafetyCheckinArchive.objects.get(checkin=self.checkin)
        symmetric_key = nacl.public.SealedBox(keypair).decrypt(base64.b64decode(archive.sealed_key))
        payload = json.loads(
            nacl.secret.SecretBox(symmetric_key).decrypt(
                base64.b64decode(archive.ciphertext), base64.b64decode(archive.nonce)
            )
        )
        self.assertEqual(payload["destination_location"], {"name": "Old Mill", "address": location.address})
        self.assertEqual(payload["trip"], {"name": "Weekend trip"})
        self.assertEqual([m["title"] for m in payload["maps"]], ["Route"])

    def test_opt_out_still_resolves_after_its_linked_contact_is_scrubbed(self):
        _enroll(self.owner)
        contact_profile = _profile()
        contact = SafetyCheckinContact.objects.create(checkin=self.checkin, contact_profile=contact_profile, email=None)
        opt_out = SafetyContactOptOut.objects.create(
            contact_profile=contact_profile, scope=SafetyContactOptOutScope.CHECKIN, checkin=self.checkin
        )

        archive_checkin(self.checkin)

        contact.refresh_from_db()
        opt_out.refresh_from_db()
        self.assertEqual(contact.contact_profile_id, contact_profile.pk)
        self.assertEqual(opt_out.contact_profile_id, contact_profile.pk)
        self.assertEqual(opt_out.checkin_id, self.checkin.pk)


class ArchiveCheckinFailureCapTests(TestCase):
    """archive_checkin's give-up-after-MAX_ARCHIVE_ATTEMPTS backstop for a checkin whose
    archival keeps failing (docs/PROBLEMS.md: a corrupted MessagingKeyBundle.public_key
    otherwise fails the same way on every 5-minute sweep forever, with no cap or alert).
    """

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner)
        # Wrong-length key: fails nacl.public.PublicKey the same deterministic way a
        # genuinely corrupted key would, on every attempt.
        _enroll(self.owner, public_key=b"too-short-to-be-a-real-x25519-key")

    def test_a_failed_attempt_raises_and_records_one_failure(self):
        with self.assertRaises(Exception):
            archive_checkin(self.checkin)

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.archive_failure_count, 1)
        self.assertIsNone(self.checkin.archive_failed_at)

    def test_gives_up_after_max_attempts_and_is_excluded_from_future_sweeps(self):
        self.checkin.archive_scheduled_at = timezone.now() - datetime.timedelta(minutes=1)
        self.checkin.save(update_fields=["archive_scheduled_at"])

        for _ in range(MAX_ARCHIVE_ATTEMPTS):
            with self.assertRaises(Exception):
                archive_checkin(self.checkin)

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.archive_failure_count, MAX_ARCHIVE_ATTEMPTS)
        self.assertIsNotNone(self.checkin.archive_failed_at)
        self.assertNotIn(self.checkin.pk, list(SafetyCheckin.objects.due_for_archival().values_list("pk", flat=True)))

    def test_does_not_alert_before_the_cap_is_reached(self):
        with mock.patch("urbanlens.dashboard.services.notifications.notifications.notify") as mock_notify:
            for _ in range(MAX_ARCHIVE_ATTEMPTS - 1):
                with self.assertRaises(Exception):
                    archive_checkin(self.checkin)

        mock_notify.assert_not_called()

    def test_alerts_the_admin_exactly_once_when_giving_up(self):
        with mock.patch("urbanlens.dashboard.services.notifications.notifications.notify") as mock_notify:
            for _ in range(MAX_ARCHIVE_ATTEMPTS):
                with self.assertRaises(Exception):
                    archive_checkin(self.checkin)

        mock_notify.assert_called_once()
        args, _kwargs = mock_notify.call_args
        self.assertEqual(args[0], NotificationEvent.SAFETY_CHECKIN_ARCHIVAL_FAILED)

    def test_further_calls_past_the_cap_do_not_keep_incrementing(self):
        for _ in range(MAX_ARCHIVE_ATTEMPTS):
            with self.assertRaises(Exception):
                archive_checkin(self.checkin)

        with (
            mock.patch("urbanlens.dashboard.services.notifications.notifications.notify") as mock_notify,
            self.assertRaises(Exception),
        ):
            archive_checkin(self.checkin)

        self.checkin.refresh_from_db()
        self.assertEqual(self.checkin.archive_failure_count, MAX_ARCHIVE_ATTEMPTS + 1)
        mock_notify.assert_not_called()


class ChatBlockedAfterArchivalTests(TestCase):
    """create_chat_message must reject new messages once a checkin is archived -
    otherwise plaintext could keep accumulating in SafetyCheckinMessage.body with no
    re-archival trigger to ever scrub it again.
    """

    def setUp(self):
        self.owner = _profile()
        self.checkin = _checkin(self.owner)
        _enroll(self.owner)
        archive_checkin(self.checkin)
        self.checkin.refresh_from_db()

    def test_owner_message_is_rejected_after_archival(self):
        from urbanlens.dashboard.services.visits.safety import create_chat_message

        with self.assertRaisesMessage(ValueError, "concluded"):
            create_chat_message(self.checkin, user=self.owner.user, contact=None, body="One more thing")

        self.assertEqual(self.checkin.messages.count(), 0)

    def test_accepting_a_pending_partner_invite_after_archival_writes_no_plaintext_message(self):
        """Regression guard: a partner invite can still be legitimately accepted after its
        checkin has already resolved and archived (e.g. the owner self-checked-in with no
        other viewers yet, archived immediately, and the invitee accepts hours later) -
        the ACCEPTED status flip must still happen, but no system chat message may be
        written into the already-archived (never-to-be-scrubbed-again) record.
        """
        from urbanlens.dashboard.services.visits.safety import accept_checkin_partner_invite

        invitee = _profile()
        partner = SafetyCheckinPartner.objects.create(checkin=self.checkin, profile=invitee, invited_by=self.owner)

        accept_checkin_partner_invite(partner)

        partner.refresh_from_db()
        self.assertEqual(partner.status, SafetyCheckinPartnerStatus.ACCEPTED)
        self.assertEqual(self.checkin.messages.count(), 0)

    def test_mark_found_safe_after_archival_writes_no_plaintext_message(self):
        """Regression guard: a stale contact token or partner mark-safe link remains a
        live, POST-able endpoint after resolution/archival (the UI only hides the
        button) - hitting it again must be a clean no-op, not a fresh plaintext message
        into a record that already claims to have no more plaintext left.
        """
        from urbanlens.dashboard.services.visits.safety import mark_found_safe

        contact = SafetyCheckinContact.objects.create(checkin=self.checkin, email="watcher@example.com", name="Watcher")

        mark_found_safe(contact)

        self.assertEqual(self.checkin.messages.count(), 0)

    def test_mark_found_safe_by_partner_after_archival_writes_no_plaintext_message(self):
        from urbanlens.dashboard.services.visits.safety import mark_found_safe_by_partner

        partner_profile = _profile()
        SafetyCheckinPartner.objects.create(
            checkin=self.checkin,
            profile=partner_profile,
            invited_by=self.owner,
            status=SafetyCheckinPartnerStatus.ACCEPTED,
        )

        mark_found_safe_by_partner(self.checkin, partner_profile)

        self.assertEqual(self.checkin.messages.count(), 0)


class ArchivePayloadMapDedupTests(TestCase):
    """_build_archive_payload must not double-count a map that ended up in both
    `markup_map` and `markup_maps` - the attach endpoint's own exclusion of the primary
    map is UI-only (SafetyCheckinMapPickerView), not re-enforced server-side, so the
    payload builder is the actual backstop.
    """

    def test_a_map_that_is_both_primary_and_attached_is_only_listed_once(self):
        from urbanlens.dashboard.models.markup.model import MarkupMap
        from urbanlens.dashboard.services.visits.safety import _build_archive_payload

        owner = _profile()
        checkin = _checkin(owner)
        shared_map = MarkupMap.objects.create(profile=owner, title="Route")
        checkin.markup_map = shared_map
        checkin.save(update_fields=["markup_map"])
        checkin.markup_maps.add(shared_map)

        payload = _build_archive_payload(checkin)

        self.assertEqual([m["title"] for m in payload["maps"]], ["Route"])


class MapAttachViewRejectsPrimaryMapTests(TestCase):
    """SafetyCheckinMapAttachView must not let the primary route map also be attached
    as a secondary reference map - the picker (SafetyCheckinMapPickerView) already
    excludes it from candidates shown in the UI, but that alone doesn't stop a direct
    POST with the primary map's own uuid.
    """

    def test_attaching_the_checkins_own_primary_map_is_rejected(self):
        from urbanlens.dashboard.models.markup.model import MarkupMap

        owner = _profile()
        checkin = _checkin(owner)
        primary_map = MarkupMap.objects.create(profile=owner, title="Route")
        checkin.markup_map = primary_map
        checkin.save(update_fields=["markup_map"])
        self.client.force_login(owner.user)

        response = self.client.post(
            reverse("safety.checkin.maps.attach", kwargs={"checkin_slug": str(checkin.uuid)}),
            {"map_uuid": str(primary_map.uuid)},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(checkin.markup_maps.all()), [])


class ScheduleCheckinArchivalTests(TestCase):
    """schedule_checkin_archival picks immediate vs. +1h archival correctly across
    every combination of accepted-partner/contact presence.
    """

    @settings(max_examples=10, deadline=None)
    @given(has_partner=st.booleans(), has_contact=st.booleans())
    def test_archive_timing_matches_viewer_presence(self, has_partner, has_contact):
        owner = _profile()
        checkin = _checkin(owner)
        if has_partner:
            partner_profile = _profile()
            SafetyCheckinPartner.objects.create(
                checkin=checkin, profile=partner_profile, invited_by=owner, status=SafetyCheckinPartnerStatus.ACCEPTED
            )
        if has_contact:
            SafetyCheckinContact.objects.create(checkin=checkin, email="contact@example.com")

        schedule_checkin_archival(checkin)

        checkin.refresh_from_db()
        if has_partner or has_contact:
            self.assertGreater(checkin.archive_scheduled_at, checkin.resolved_at)
        else:
            self.assertEqual(checkin.archive_scheduled_at, checkin.resolved_at)

    def test_invited_but_not_accepted_partner_does_not_count_as_a_viewer(self):
        owner = _profile()
        checkin = _checkin(owner)
        partner_profile = _profile()
        SafetyCheckinPartner.objects.create(
            checkin=checkin, profile=partner_profile, invited_by=owner
        )  # default status: invited

        schedule_checkin_archival(checkin)

        checkin.refresh_from_db()
        self.assertEqual(checkin.archive_scheduled_at, checkin.resolved_at)


class ArchivedCheckinDetailViewTests(TestCase):
    """The detail view's rendered HTML after archival: shows the locked notice, never the
    pre-scrub plaintext, and offers an unlock affordance only to the true owner - never a
    partner, even one who could see everything while the check-in was still active.
    """

    def setUp(self):
        self.owner = _profile()
        _enroll(self.owner)
        self.checkin = _checkin(self.owner, plan_details="Secret plan: go to the old mill")
        archive_checkin(self.checkin)
        self.checkin.refresh_from_db()

    def test_owner_sees_locked_notice_with_unlock_button_and_no_plaintext(self):
        self.client.force_login(self.owner.user)

        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(self.checkin.uuid)}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This check-in has concluded and its details are now private.")
        self.assertContains(response, 'id="safety-archive-unlock-btn"')
        self.assertNotContains(response, "Secret plan: go to the old mill")

    def test_accepted_partner_sees_locked_notice_without_unlock_button(self):
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
        self.assertContains(response, "This check-in has concluded and its details are now private.")
        self.assertNotContains(response, 'id="safety-archive-unlock-btn"')
        self.assertNotContains(response, "Secret plan: go to the old mill")


class ArchiveCountdownRenderingTests(TestCase):
    """The detail page shows a countdown once resolved-with-viewers, and switches to the
    locked notice only once actually archived - never both, never neither.
    """

    def test_countdown_shown_when_other_viewers_exist_and_not_yet_archived(self):
        owner = _profile()
        checkin = _checkin(owner)
        partner_profile = _profile()
        SafetyCheckinPartner.objects.create(
            checkin=checkin, profile=partner_profile, invited_by=owner, status=SafetyCheckinPartnerStatus.ACCEPTED
        )
        schedule_checkin_archival(checkin)
        checkin.refresh_from_db()

        self.client.force_login(owner.user)
        response = self.client.get(reverse("safety.checkin.detail", kwargs={"checkin_slug": str(checkin.uuid)}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "will be encrypted")
        self.assertNotContains(response, "This check-in has concluded and its details are now private.")
