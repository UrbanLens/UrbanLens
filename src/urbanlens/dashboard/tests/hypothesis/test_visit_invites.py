"""Tests for external visit participants and deferred email invites.

Covers:
- sync_external_participants - parsing the indexed form fields, hashed email
  storage, removal, and the invite/no-invite choice
- immediate delivery when the email already belongs to a member (friend
  request + visit suggestion)
- join-invite email for unknown addresses (send log, dedup, rate caps)
- process_pending_visit_invites - deferred delivery once the address is
  verified on an account
- the per-participant "send suggestion" toggle in the visit-create view
"""

from __future__ import annotations

import datetime
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.email_log import EmailSendLog, EmailType
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.models.visits.participant import ExternalVisitParticipant
from urbanlens.dashboard.services.email_safety import hash_email, record_email_sent
from urbanlens.dashboard.services.visit_invites import process_pending_visit_invites, sync_external_participants


class _VisitInviteTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner_user = baker.make(User, username="visit-owner", email="owner@example.com")
        self.owner = self.owner_user.profile
        self.location = baker.make(Location, latitude="42.200000", longitude="-73.800000", official_name="Grain Elevator")
        self.pin = Pin.objects.create(profile=self.owner, location=self.location)
        self.visit = PinVisit.objects.create(pin=self.pin, visited_at=datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.UTC), source=VisitSource.MANUAL)
        self.factory = RequestFactory()

    def _post_request(self, data: dict):
        request = self.factory.post("/", data)
        request.user = self.owner_user
        return request


class SyncExternalParticipantsTests(_VisitInviteTestCase):
    """Form parsing and row lifecycle."""

    def test_creates_participant_without_email(self):
        sync_external_participants(self._post_request({"external_name_1": "Uncle Bob"}), self.visit)

        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertEqual(participant.display_name, "Uncle Bob")
        self.assertEqual(participant.email_hash, "")
        self.assertFalse(participant.invite_sent)

    def test_blank_names_ignored(self):
        sync_external_participants(self._post_request({"external_name_1": "   "}), self.visit)
        self.assertFalse(ExternalVisitParticipant.objects.filter(visit=self.visit).exists())

    def test_email_stored_as_hash_only(self):
        with patch("django.core.mail.EmailMultiAlternatives.send"):
            sync_external_participants(
                self._post_request({"external_name_1": "Sam", "external_email_1": "sam@example.com", "external_invite_1": "on"}),
                self.visit,
            )

        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertEqual(participant.email_hash, hash_email("sam@example.com"))
        self.assertNotIn("sam@example.com", participant.email_hash)

    def test_invalid_email_treated_as_no_email(self):
        sync_external_participants(
            self._post_request({"external_name_1": "Sam", "external_email_1": "not-an-email", "external_invite_1": "on"}),
            self.visit,
        )
        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertEqual(participant.email_hash, "")
        self.assertFalse(participant.suggestion_requested)

    def test_remove_deletes_row(self):
        participant = ExternalVisitParticipant.objects.create(visit=self.visit, display_name="Sam")
        sync_external_participants(self._post_request({"external_remove": str(participant.pk)}), self.visit)
        self.assertFalse(ExternalVisitParticipant.objects.filter(pk=participant.pk).exists())


class ExistingMemberMatchTests(_VisitInviteTestCase):
    """An email that already belongs to a member is delivered to immediately."""

    def setUp(self) -> None:
        super().setUp()
        self.member_user = baker.make(User, username="already-here", email="member@example.com", is_active=True)
        self.member = self.member_user.profile

    def _tag_member_by_email(self, *, invite: bool = True) -> None:
        data = {"external_name_1": "Casey", "external_email_1": "member@example.com"}
        if invite:
            data["external_invite_1"] = "on"
        sync_external_participants(self._post_request(data), self.visit)

    def test_matched_profile_set(self):
        self._tag_member_by_email()
        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertEqual(participant.matched_profile, self.member)

    def test_friend_request_and_suggestion_sent(self):
        self._tag_member_by_email()
        self.assertTrue(Friendship.objects.filter(from_profile=self.owner, to_profile=self.member).exists())
        self.assertTrue(VisitSuggestion.objects.filter(suggested_to=self.member, suggested_by=self.owner, origin_visit=self.visit).exists())

    def test_unchecked_invite_records_without_contacting(self):
        self._tag_member_by_email(invite=False)
        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertEqual(participant.matched_profile, self.member)
        self.assertFalse(Friendship.objects.filter(from_profile=self.owner, to_profile=self.member).exists())
        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=self.member).exists())


class UnknownEmailInviteTests(_VisitInviteTestCase):
    """Unknown addresses get one join-the-site email, subject to safety rules."""

    def _tag_unknown(self, email: str = "stranger@example.com") -> None:
        sync_external_participants(
            self._post_request({"external_name_1": "Stranger", "external_email_1": email, "external_invite_1": "on"}),
            self.visit,
        )

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_invite_email_sent_and_logged(self, mock_send):
        self._tag_unknown()

        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertTrue(participant.invite_sent)
        self.assertTrue(participant.suggestion_requested)
        mock_send.assert_called_once()
        log = EmailSendLog.objects.get(sender=self.owner)
        self.assertEqual(log.email_type, EmailType.VISIT_INVITE)
        self.assertTrue(FriendInvitation.objects.filter(inviter=self.owner, email="stranger@example.com").exists())

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_no_email_when_invite_unchecked(self, mock_send):
        sync_external_participants(
            self._post_request({"external_name_1": "Stranger", "external_email_1": "stranger@example.com"}),
            self.visit,
        )
        mock_send.assert_not_called()
        self.assertFalse(EmailSendLog.objects.filter(sender=self.owner).exists())

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_second_invite_to_same_address_suppressed(self, mock_send):
        self._tag_unknown()
        other_visit = PinVisit.objects.create(pin=self.pin, visited_at=datetime.datetime(2026, 7, 2, 12, 0, tzinfo=datetime.UTC), source=VisitSource.MANUAL)
        sync_external_participants(
            self._post_request({"external_name_1": "Stranger", "external_email_1": "stranger@example.com", "external_invite_1": "on"}),
            other_visit,
        )

        self.assertEqual(mock_send.call_count, 1)
        second = ExternalVisitParticipant.objects.get(visit=other_visit)
        self.assertFalse(second.invite_sent)
        # The hash row still enables deferred delivery later.
        self.assertEqual(second.email_hash, hash_email("stranger@example.com"))

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_rate_limit_suppresses_email(self, mock_send):
        settings = SiteSettings.get_current()
        settings.email_limit_per_hour = 1
        settings.save()
        record_email_sent(self.owner, "elsewhere@example.com", EmailType.JOIN_INVITE)

        self._tag_unknown()

        mock_send.assert_not_called()
        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertFalse(participant.invite_sent)


class DeferredDeliveryTests(_VisitInviteTestCase):
    """Registering (or verifying a secondary email) later delivers the invite."""

    def _pending_participant(self, email: str = "future@example.com", *, suggestion: bool = True) -> ExternalVisitParticipant:
        return ExternalVisitParticipant.objects.create(
            visit=self.visit,
            display_name="Future Friend",
            email_hash=hash_email(email),
            suggestion_requested=suggestion,
        )

    def test_registration_resolves_participant_and_delivers(self):
        participant = self._pending_participant()
        newcomer = baker.make(User, username="newcomer", email="future@example.com", is_active=True)

        resolved = process_pending_visit_invites(newcomer)

        self.assertEqual(resolved, 1)
        participant.refresh_from_db()
        self.assertEqual(participant.matched_profile, newcomer.profile)
        self.assertTrue(Friendship.objects.filter(from_profile=self.owner, to_profile=newcomer.profile).exists())
        self.assertTrue(VisitSuggestion.objects.filter(suggested_to=newcomer.profile, origin_visit=self.visit).exists())

    def test_gmail_variant_matches(self):
        self._pending_participant(email="jakesmith@gmail.com")
        newcomer = baker.make(User, username="jake", email="Jake.Smith@gmail.com", is_active=True)

        self.assertEqual(process_pending_visit_invites(newcomer), 1)

    def test_secondary_email_matches_via_explicit_address(self):
        self._pending_participant(email="alt@example.com")
        newcomer = baker.make(User, username="alt-user", email="primary@example.com", is_active=True)

        self.assertEqual(process_pending_visit_invites(newcomer, email="alt@example.com"), 1)

    def test_no_suggestion_when_not_requested(self):
        self._pending_participant(suggestion=False)
        newcomer = baker.make(User, username="quiet", email="future@example.com", is_active=True)

        process_pending_visit_invites(newcomer)

        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=newcomer.profile).exists())
        self.assertFalse(Friendship.objects.filter(from_profile=self.owner, to_profile=newcomer.profile).exists())

    def test_already_matched_rows_ignored(self):
        other = baker.make(User, username="taken", email="future@example.com", is_active=True)
        participant = self._pending_participant()
        participant.matched_profile = other.profile
        participant.save(update_fields=["matched_profile"])

        self.assertEqual(process_pending_visit_invites(other), 0)


class SuggestionToggleViewTests(_VisitInviteTestCase):
    """The visit-create view honours each participant's suggestion checkbox."""

    def setUp(self) -> None:
        super().setUp()
        self.friend_a = baker.make(User, username="friend-a").profile
        self.friend_b = baker.make(User, username="friend-b").profile
        Friendship.objects.create(from_profile=self.owner, to_profile=self.friend_a, status=FriendshipStatus.ACCEPTED)
        Friendship.objects.create(from_profile=self.owner, to_profile=self.friend_b, status=FriendshipStatus.ACCEPTED)
        self.client.force_login(self.owner_user)

    def test_only_toggled_participants_get_suggestions(self):
        response = self.client.post(
            reverse("pin.visits", kwargs={"pin_slug": self.pin.slug}),
            {
                "visited_date": "2026-07-03",
                "participant_ids": [str(self.friend_a.pk), str(self.friend_b.pk)],
                "suggest_participant_ids": [str(self.friend_a.pk)],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(VisitSuggestion.objects.filter(suggested_to=self.friend_a).exists())
        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=self.friend_b).exists())
        visit = PinVisit.objects.filter(pin=self.pin).latest("created")
        self.assertEqual(set(visit.participants.values_list("pk", flat=True)), {self.friend_a.pk, self.friend_b.pk})
