"""Tests for external visit participants and deferred email invites."""

from __future__ import annotations

import datetime
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.email_log import EmailSendLog, EmailType
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import VisibilityChoice
from urbanlens.dashboard.models.site_settings.model import SiteSettings
from urbanlens.dashboard.models.visit_suggestions.model import VisitSuggestion
from urbanlens.dashboard.models.visits.model import PinVisit, VisitSource
from urbanlens.dashboard.models.visits.participant import ExternalVisitParticipant
from urbanlens.dashboard.services.security.email_safety import hash_email, record_email_sent
from urbanlens.dashboard.services.visits.visit_invites import process_pending_visit_invites, sync_external_participants
from urbanlens.dashboard.tasks import deliver_friend_invitation, deliver_visit_invite


class _VisitInviteTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner_user = baker.make(User, username="visit-owner", email="owner@example.com")
        self.owner = self.owner_user.profile
        self.location = baker.make(
            Location, latitude="42.200000", longitude="-73.800000", official_name="Grain Elevator"
        )
        self.pin = Pin.objects.create(profile=self.owner, location=self.location)
        self.visit = PinVisit.objects.create(
            pin=self.pin,
            visited_at=datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.UTC),
            source=VisitSource.MANUAL,
        )
        self.factory = RequestFactory()

    def _post_request(self, data: dict):
        request = self.factory.post("/", data)
        request.user = self.owner_user
        return request

    def _sync(self, data: dict, visit: PinVisit | None = None) -> None:
        with (
            tasks_run_inline(deliver_friend_invitation, deliver_visit_invite),
            self.captureOnCommitCallbacks(execute=True),
        ):
            sync_external_participants(self._post_request(data), visit or self.visit)


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
                self._post_request(
                    {"external_name_1": "Sam", "external_email_1": "sam@example.com", "external_invite_1": "on"}
                ),
                self.visit,
            )

        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertEqual(participant.email_hash, hash_email("sam@example.com"))
        self.assertNotIn("sam@example.com", participant.email_hash)

    def test_invalid_email_treated_as_no_email(self):
        sync_external_participants(
            self._post_request(
                {"external_name_1": "Sam", "external_email_1": "not-an-email", "external_invite_1": "on"}
            ),
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
    """An email that already belongs to a member is offered the friendship and the visit, to answer separately."""

    def setUp(self) -> None:
        super().setUp()
        self.member_user = baker.make(User, username="already-here", email="member@example.com", is_active=True)
        self.member = self.member_user.profile
        self.member.verified_primary_email = self.member.primary_email_normalized
        self.member.save(update_fields=["verified_primary_email"])

    def _tag_member_by_email(self, *, invite: bool = True) -> None:
        data = {"external_name_1": "Casey", "external_email_1": "member@example.com"}
        if invite:
            data["external_invite_1"] = "on"
        self._sync(data)

    def test_matched_profile_set(self):
        self._tag_member_by_email()
        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertEqual(participant.matched_profile, self.member)

    def test_friend_invitation_and_suggestion_sent(self):
        self._tag_member_by_email()
        self.assertTrue(FriendInvitation.objects.filter(inviter=self.owner, invitee=self.member).exists())
        self.assertTrue(
            VisitSuggestion.objects.filter(
                suggested_to=self.member, suggested_by=self.owner, origin_visit=self.visit
            ).exists()
        )

    def test_unchecked_invite_records_without_contacting(self):
        self._tag_member_by_email(invite=False)
        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertIsNone(participant.matched_profile)
        self.assertFalse(FriendInvitation.objects.filter(inviter=self.owner).exists())
        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=self.member).exists())


class UnknownEmailInviteTests(_VisitInviteTestCase):
    """Unknown addresses get one invitation email, subject to safety rules."""

    def _tag_unknown(self, email: str = "stranger@example.com", visit: PinVisit | None = None) -> None:
        self._sync({"external_name_1": "Stranger", "external_email_1": email, "external_invite_1": "on"}, visit)

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_invite_email_sent_and_logged(self, mock_send):
        self._tag_unknown()

        participant = ExternalVisitParticipant.objects.get(visit=self.visit)
        self.assertTrue(participant.invite_sent)
        self.assertTrue(participant.suggestion_requested)
        mock_send.assert_called_once()
        log = EmailSendLog.objects.get(sender=self.owner)
        self.assertEqual(log.email_type, EmailType.JOIN_INVITE)
        self.assertTrue(log.delivered)
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
        other_visit = PinVisit.objects.create(
            pin=self.pin,
            visited_at=datetime.datetime(2026, 7, 2, 12, 0, tzinfo=datetime.UTC),
            source=VisitSource.MANUAL,
        )
        self._tag_unknown(visit=other_visit)

        self.assertEqual(mock_send.call_count, 1)
        second = ExternalVisitParticipant.objects.get(visit=other_visit)
        self.assertTrue(second.invite_sent)
        # The hash row still enables deferred delivery later.
        self.assertEqual(second.email_hash, hash_email("stranger@example.com"))

    @patch("django.core.mail.EmailMultiAlternatives.send")
    def test_reinviting_a_gmail_variant_replaces_the_pending_invitation(self, mock_send):
        """Same dedup guarantee as the friend-invite-by-email flow (see test_friend_invite_privacy.py) - this is a separate code path into the same FriendInvitation table, and must not let a Gmail dot/+ variant leave two open rows behind."""
        other_visit = PinVisit.objects.create(
            pin=self.pin,
            visited_at=datetime.datetime(2026, 7, 2, 12, 0, tzinfo=datetime.UTC),
            source=VisitSource.MANUAL,
        )
        self._tag_unknown("johndoe3@gmail.com")
        self._tag_unknown("John.Doe.3+invite@gmail.com", visit=other_visit)

        self.assertEqual(
            FriendInvitation.objects.filter(inviter=self.owner, accepted_at__isnull=True).count(),
            1,
        )

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

    def _pending_participant(
        self, email: str = "future@example.com", *, suggestion: bool = True
    ) -> ExternalVisitParticipant:
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


class OwnerCannotTellWhetherAParticipantHasAnAccountTests(_VisitInviteTestCase):
    """Tagging an address must look and cost the same to the owner whether or not it belongs to an account."""

    def setUp(self) -> None:
        super().setUp()
        self.member_user = baker.make(User, username="hidden-member", email="member@example.com", is_active=True)
        self.member = self.member_user.profile
        self.member.verified_primary_email = self.member.primary_email_normalized
        self.member.friend_request_visibility = VisibilityChoice.ANYONE
        self.member.save(update_fields=["verified_primary_email", "friend_request_visibility"])
        self.client.force_login(self.owner_user)

    def _log_visit(self, email: str, day: int) -> PinVisit:
        with (
            patch("django.core.mail.EmailMultiAlternatives.send"),
            tasks_run_inline(deliver_friend_invitation, deliver_visit_invite),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.client.post(
                reverse("pin.visits", kwargs={"pin_slug": self.pin.slug}),
                {
                    "visited_date": f"2026-07-{day:02d}",
                    "external_name_1": "Casey",
                    "external_email_1": email,
                    "external_invite_1": "on",
                },
            )
        return PinVisit.objects.filter(pin=self.pin).latest("created")

    def _external_markup(self, html: str) -> list[str]:
        return re.findall(r'<span class="visit-external-name".*?</span>', html, re.DOTALL)

    def test_history_shows_both_participants_identically(self):
        self._log_visit("member@example.com", 10)
        self._log_visit("stranger@example.com", 11)

        html = self.client.get(reverse("pin.visits", kwargs={"pin_slug": self.pin.slug})).content.decode()

        self.assertNotIn("hidden-member", html)
        rows = self._external_markup(html)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], rows[1])

    def test_edit_form_does_not_name_the_account(self):
        visit = self._log_visit("member@example.com", 12)

        html = self.client.get(
            reverse("pin.visit.edit", kwargs={"pin_slug": self.pin.slug, "visit_id": visit.pk})
        ).content.decode()

        self.assertNotIn("hidden-member", html)

    def test_no_friendship_row_exists_before_the_member_answers(self):
        visit = self._log_visit("member@example.com", 13)

        self.assertFalse(Friendship.objects.filter(from_profile=self.owner, to_profile=self.member).exists())
        invitation = FriendInvitation.objects.get(inviter=self.owner, invitee=self.member)
        self.assertIsNone(invitation.accepted_at)
        self.assertTrue(VisitSuggestion.objects.filter(suggested_to=self.member, origin_visit=visit).exists())

    def test_budget_is_charged_the_same_either_way(self):
        self._log_visit("member@example.com", 14)
        charged_for_member = EmailSendLog.objects.filter(sender=self.owner).count()
        self._log_visit("stranger@example.com", 15)

        self.assertEqual(charged_for_member, 1)
        self.assertEqual(EmailSendLog.objects.filter(sender=self.owner).count(), 2)

    def test_the_request_itself_neither_matches_nor_sends(self):
        with patch("django.core.mail.EmailMultiAlternatives.send") as mock_send:
            sync_external_participants(
                self._post_request(
                    {"external_name_1": "Casey", "external_email_1": "member@example.com", "external_invite_1": "on"}
                ),
                self.visit,
            )

        mock_send.assert_not_called()
        self.assertIsNone(ExternalVisitParticipant.objects.get(visit=self.visit).matched_profile)
        self.assertFalse(VisitSuggestion.objects.filter(suggested_to=self.member).exists())

    def test_signing_up_later_does_not_send_a_friend_request_by_itself(self):
        ExternalVisitParticipant.objects.create(
            visit=self.visit,
            display_name="Later",
            email_hash=hash_email("later@example.com"),
            suggestion_requested=True,
        )
        newcomer = baker.make(User, username="later", email="later@example.com", is_active=True)

        process_pending_visit_invites(newcomer)

        self.assertFalse(Friendship.objects.filter(from_profile=self.owner, to_profile=newcomer.profile).exists())
        self.assertTrue(VisitSuggestion.objects.filter(suggested_to=newcomer.profile, origin_visit=self.visit).exists())


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
