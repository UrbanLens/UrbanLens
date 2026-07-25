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

from django.utils import timezone
from hypothesis import given, settings, strategies as st
from model_bakery import baker
import nacl.public
import nacl.secret

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
from urbanlens.dashboard.services.safety import _seal_archive_payload, archive_checkin, schedule_checkin_archival

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
        recovered_json = nacl.secret.SecretBox(symmetric_key).decrypt(base64.b64decode(ciphertext_b64), base64.b64decode(nonce_b64))
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

    def test_opt_out_still_resolves_after_its_linked_contact_is_scrubbed(self):
        _enroll(self.owner)
        contact_profile = _profile()
        contact = SafetyCheckinContact.objects.create(checkin=self.checkin, contact_profile=contact_profile, email=None)
        opt_out = SafetyContactOptOut.objects.create(contact_profile=contact_profile, scope=SafetyContactOptOutScope.CHECKIN, checkin=self.checkin)

        archive_checkin(self.checkin)

        contact.refresh_from_db()
        opt_out.refresh_from_db()
        self.assertEqual(contact.contact_profile_id, contact_profile.pk)
        self.assertEqual(opt_out.contact_profile_id, contact_profile.pk)
        self.assertEqual(opt_out.checkin_id, self.checkin.pk)


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
            SafetyCheckinPartner.objects.create(checkin=checkin, profile=partner_profile, invited_by=owner, status=SafetyCheckinPartnerStatus.ACCEPTED)
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
        SafetyCheckinPartner.objects.create(checkin=checkin, profile=partner_profile, invited_by=owner)  # default status: invited

        schedule_checkin_archival(checkin)

        checkin.refresh_from_db()
        self.assertEqual(checkin.archive_scheduled_at, checkin.resolved_at)
