"""Inviting someone to a trip by email.

The inviter must not learn whether the address belongs to an account, and the invitee answers "join the trip?"
and "become friends?" independently of each other and of signing up.
"""

from __future__ import annotations

from datetime import timedelta
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import EmailVerification
from urbanlens.dashboard.models.email_log import EmailSendLog, EmailType
from urbanlens.dashboard.models.friendship.invitation import FriendInvitation
from urbanlens.dashboard.models.friendship.model import Friendship, FriendshipStatus
from urbanlens.dashboard.models.notifications.meta import NotificationType
from urbanlens.dashboard.models.notifications.model import NotificationLog
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.models.profile.model import Profile, VisibilityChoice
from urbanlens.dashboard.models.site_settings import SiteSettings
from urbanlens.dashboard.models.trips.invitation import TripInvitation, TripInvitationResponse
from urbanlens.dashboard.models.trips.model import Trip, TripMembership
from urbanlens.dashboard.services.trips.trip_errors import TripNotFoundError, TripValidationError
from urbanlens.dashboard.services.trips.trip_invitations import (
    bind_invitations_to_account,
    friendship_offer_open,
    invite_to_trip_by_email,
    respond_to_friendship,
    respond_to_trip,
)
from urbanlens.dashboard.tasks import deliver_trip_invitation

_CSRF_TOKEN_RE = re.compile(rb"(?<![A-Za-z0-9])[A-Za-z0-9]{64}(?![A-Za-z0-9])")
_UUID_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

UNREGISTERED = "nobody.here@mailbox.org"


def _make_trip(creator: Profile, name: str = "Rust Belt Ramble") -> Trip:
    trip = Trip.objects.create(name=name, creator=creator)
    TripMembership.objects.get_or_create(
        trip=trip, profile=creator, defaults={"rsvp": "yes", "status": TripMembership.STATUS_JOINED}
    )
    return trip


def _user(username: str, email: str) -> User:
    """An active account whose primary address is verified and which accepts friend requests from anyone."""
    user = baker.make(User, username=username, email=email, is_active=True)
    profile = user.profile
    profile.friend_request_visibility = VisibilityChoice.ANYONE
    profile.verified_primary_email = profile.primary_email_normalized
    profile.save(update_fields=["friend_request_visibility", "verified_primary_email"])
    return user


def _befriend(a: Profile, b: Profile) -> None:
    Friendship.objects.create(from_profile=a, to_profile=b, status=FriendshipStatus.ACCEPTED)


def _url(path: str) -> str:
    return f"https://urbanlens.test{path}"


class _InvitationTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.inviter_user = _user("inviter", "inviter@mailbox.org")
        self.inviter = self.inviter_user.profile
        self.invitee_user = _user("invitee", "invitee@mailbox.org")
        self.invitee = self.invitee_user.profile

    def invite(self, trip: Trip, email: str, *, deliver: bool = True) -> TripInvitation:
        if not deliver:
            return invite_to_trip_by_email(trip, self.inviter, email, invitation_url_builder=_url)
        with tasks_run_inline(deliver_trip_invitation), self.captureOnCommitCallbacks(execute=True):
            return invite_to_trip_by_email(trip, self.inviter, email, invitation_url_builder=_url)


class InviterCannotTellRegisteredFromUnregisteredTests(_InvitationTestCase):
    """Every case the inviter might want to probe renders exactly like an address with no account."""

    def _panel_after_inviting(self, email: str) -> bytes:
        trip = _make_trip(self.inviter)
        self.client.force_login(self.inviter_user)
        with tasks_run_inline(deliver_trip_invitation), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("trips.members", args=[trip.slug]), {"username": email}, HTTP_HX_REQUEST="true"
            )
        self.assertEqual(response.status_code, 200, response.content[:300])
        after = self.client.get(reverse("trips.members", args=[trip.slug]))
        body = response.content + b"\n----\n" + after.content + b"\n----\n" + response.get("HX-Trigger", "").encode()
        body = body.replace(trip.slug.encode(), b"SLUG").replace(email.encode(), b"EMAIL")
        return _UUID_RE.sub(b"UUID", _CSRF_TOKEN_RE.sub(b"CSRF", body))

    def _cases(self) -> dict[str, str]:
        friend = _user("friendly", "friendly@mailbox.org").profile
        _befriend(self.inviter, friend)
        closed = _user("closed", "closed@mailbox.org").profile
        closed.friend_request_visibility = VisibilityChoice.NO_ONE
        closed.save(update_fields=["friend_request_visibility"])
        blocker = _user("blocker", "blocker@mailbox.org").profile
        Friendship.objects.create(from_profile=blocker, to_profile=self.inviter, status=FriendshipStatus.BLOCKED)
        secondary_owner = _user("second", "second-primary@mailbox.org").profile
        ProfileEmail.objects.create(
            profile=secondary_owner, email="second-alias@mailbox.org", is_verified=True, verified_at=timezone.now()
        )
        return {
            "a stranger's account": self.invitee_user.email,
            "a friend's account": "friendly@mailbox.org",
            "an account refusing friend requests": "closed@mailbox.org",
            "an account that blocked the inviter": "blocker@mailbox.org",
            "an account's verified secondary address": "second-alias@mailbox.org",
        }

    def test_the_members_panel_and_toast_are_identical_for_every_kind_of_address(self) -> None:
        settings = SiteSettings.get_current()
        settings.email_limit_per_hour = 0
        settings.save()
        cases = self._cases()
        baseline = self._panel_after_inviting(UNREGISTERED)
        for label, email in cases.items():
            with self.subTest(label):
                self.assertEqual(self._panel_after_inviting(email), baseline)

    def test_the_address_is_listed_as_invited_and_no_account_is_named(self) -> None:
        trip = _make_trip(self.inviter)
        self.invite(trip, self.invitee_user.email)
        self.client.force_login(self.inviter_user)
        body = self.client.get(reverse("trips.members", args=[trip.slug])).content.decode()
        self.assertIn(self.invitee_user.email, body)
        self.assertNotIn("@invitee", body)
        self.assertNotIn(f"trip-member-{self.invitee.pk}", body)

    def test_no_friendship_or_friend_invitation_appears_for_the_inviter_to_see(self) -> None:
        trip = _make_trip(self.inviter)
        self.invite(trip, self.invitee_user.email)
        self.invite(trip, UNREGISTERED)
        self.assertFalse(Friendship.objects.filter(from_profile=self.inviter).exists())
        self.assertFalse(FriendInvitation.objects.filter(inviter=self.inviter).exists())

    def test_the_invitation_token_is_never_shown_to_the_inviter(self) -> None:
        trip = _make_trip(self.inviter)
        invitation = self.invite(trip, UNREGISTERED)
        self.client.force_login(self.inviter_user)
        body = self.client.get(reverse("trips.members", args=[trip.slug])).content.decode()
        self.assertNotIn(str(invitation.token), body)

    def test_delivery_is_left_to_a_task_for_both_kinds_of_address(self) -> None:
        trip = _make_trip(self.inviter)
        with (
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            invite_to_trip_by_email(trip, self.inviter, self.invitee_user.email, invitation_url_builder=_url)
            invite_to_trip_by_email(trip, self.inviter, UNREGISTERED, invitation_url_builder=_url)
        self.assertEqual(
            [call.args[0] for call in enqueue.call_args_list], [deliver_trip_invitation, deliver_trip_invitation]
        )
        self.assertEqual(mail.outbox, [])
        self.assertFalse(NotificationLog.objects.filter(profile=self.invitee).exists())
        self.assertFalse(TripInvitation.objects.filter(invitee__isnull=False).exists())

    def test_the_email_budget_is_charged_the_same_either_way(self) -> None:
        trip = _make_trip(self.inviter)
        self.invite(trip, self.invitee_user.email)
        registered = EmailSendLog.objects.filter(sender=self.inviter).count()
        self.invite(trip, UNREGISTERED)
        self.assertEqual(registered, 1)
        self.assertEqual(EmailSendLog.objects.filter(sender=self.inviter).count(), 2)

    def test_other_members_do_not_see_the_inviters_invitations(self) -> None:
        trip = _make_trip(self.inviter)
        TripMembership.objects.create(trip=trip, profile=self.invitee, status=TripMembership.STATUS_JOINED)
        self.invite(trip, UNREGISTERED)
        self.client.force_login(self.invitee_user)
        body = self.client.get(reverse("trips.members", args=[trip.slug])).content.decode()
        self.assertNotIn(UNREGISTERED, body)


class RefusalsDoNotDependOnTheAddressTests(_InvitationTestCase):
    def _post(self, trip: Trip, email: str):
        self.client.force_login(self.inviter_user)
        return self.client.post(reverse("trips.members", args=[trip.slug]), {"email": email})

    def test_a_member_without_permission_is_refused_either_way(self) -> None:
        trip = _make_trip(_user("owner", "owner@mailbox.org").profile)
        trip.allow_add_members = Trip.PERM_NONE
        trip.save(update_fields=["allow_add_members"])
        TripMembership.objects.create(trip=trip, profile=self.inviter, status=TripMembership.STATUS_JOINED)
        self.assertEqual(self._post(trip, self.invitee_user.email).status_code, 403)
        self.assertEqual(self._post(trip, UNREGISTERED).status_code, 403)
        self.assertFalse(TripInvitation.objects.exists())

    def test_a_malformed_address_is_refused(self) -> None:
        response = self._post(_make_trip(self.inviter), "not an address@")
        self.assertEqual(response.status_code, 400)

    def test_the_inviters_own_addresses_are_refused(self) -> None:
        ProfileEmail.objects.create(
            profile=self.inviter, email="inviter-alias@mailbox.org", is_verified=True, verified_at=timezone.now()
        )
        trip = _make_trip(self.inviter)
        for address in ("inviter@mailbox.org", "Inviter@Mailbox.org", "inviter-alias@mailbox.org"):
            with self.subTest(address):
                self.assertEqual(self._post(trip, address).status_code, 400)
        self.assertFalse(TripInvitation.objects.exists())

    def test_open_invitations_count_toward_the_member_cap_whoever_they_address(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_trip_members = 2
        settings.save()
        trip = _make_trip(self.inviter)
        self.invite(trip, "first@mailbox.org")
        registered = self._post(trip, self.invitee_user.email)
        unregistered = self._post(trip, UNREGISTERED)
        self.assertEqual((registered.status_code, registered.content), (unregistered.status_code, unregistered.content))
        self.assertEqual(registered.status_code, 400)

    def test_the_email_budget_is_checked_before_the_address_is_looked_up(self) -> None:
        trip = _make_trip(self.inviter)
        with patch(
            "urbanlens.dashboard.services.trips.trip_invitations.email_rate_limit_error", return_value="Slow down."
        ):
            registered = self._post(trip, self.invitee_user.email)
            unregistered = self._post(trip, UNREGISTERED)
        self.assertEqual((registered.status_code, registered.content), (unregistered.status_code, unregistered.content))
        self.assertEqual(registered.status_code, 429)


class DeliveryTests(_InvitationTestCase):
    def test_an_account_gets_a_notification_linking_to_its_invitation_and_no_email(self) -> None:
        trip = _make_trip(self.inviter)
        invitation = self.invite(trip, self.invitee_user.email)
        invitation.refresh_from_db()
        self.assertEqual(invitation.invitee_id, self.invitee.pk)
        note = NotificationLog.objects.get(profile=self.invitee, notification_type=NotificationType.ADDED_TO_TRIP)
        self.assertEqual(note.url, reverse("trips.invitation", kwargs={"token": invitation.token}))
        self.assertEqual(mail.outbox, [])
        self.assertFalse(TripMembership.objects.filter(trip=trip, profile=self.invitee).exists())

    def test_an_unregistered_address_gets_one_email_with_the_invitation_link(self) -> None:
        trip = _make_trip(self.inviter)
        invitation = self.invite(trip, UNREGISTERED)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [UNREGISTERED])
        self.assertIn(trip.name, message.subject)
        self.assertIn(_url(reverse("trips.invitation", kwargs={"token": invitation.token})), message.body)
        self.assertIn("ignore this email", message.body)
        self.assertTrue(EmailSendLog.objects.filter(sender=self.inviter, email_type=EmailType.TRIP_INVITE).exists())

    def test_inviting_the_same_address_again_sends_nothing_more(self) -> None:
        trip = _make_trip(self.inviter)
        first = self.invite(trip, UNREGISTERED)
        second = self.invite(trip, UNREGISTERED.upper())
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_reserved_domain_is_never_handed_to_the_mail_relay_or_charged(self) -> None:
        self.invite(_make_trip(self.inviter), "someone@e2e.invalid")
        self.assertEqual(mail.outbox, [])
        self.assertFalse(EmailSendLog.objects.filter(sender=self.inviter).exists())

    def test_a_reserved_domain_is_uncharged_whether_or_not_an_account_holds_it(self) -> None:
        _user("reserved", "reserved@e2e.invalid")
        with patch(
            "urbanlens.dashboard.services.trips.trip_invitations.email_rate_limit_error", return_value="Slow down."
        ) as budget:
            self.invite(_make_trip(self.inviter, "One"), "reserved@e2e.invalid")
            self.invite(_make_trip(self.inviter, "Two"), "nobody@e2e.invalid")
        budget.assert_not_called()

    def test_a_block_suppresses_the_notification(self) -> None:
        Friendship.objects.create(from_profile=self.invitee, to_profile=self.inviter, status=FriendshipStatus.BLOCKED)
        self.invite(_make_trip(self.inviter), self.invitee_user.email)
        self.assertFalse(NotificationLog.objects.filter(profile=self.invitee).exists())

    def test_an_existing_member_is_not_notified(self) -> None:
        trip = _make_trip(self.inviter)
        TripMembership.objects.create(trip=trip, profile=self.invitee, status=TripMembership.STATUS_JOINED)
        self.invite(trip, self.invitee_user.email)
        self.assertFalse(NotificationLog.objects.filter(profile=self.invitee).exists())


class InviteeAnswersEachQuestionIndependentlyTests(_InvitationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.trip = _make_trip(self.inviter)
        self.invitation = self.invite(self.trip, self.invitee_user.email)
        self.invitation.refresh_from_db()
        self.client.force_login(self.invitee_user)

    def _answer(self, question: str, answer: str):
        return self.client.post(
            reverse(f"trips.invitation.{question}", kwargs={"token": self.invitation.token}), {"answer": answer}
        )

    def _friends(self) -> bool:
        friendship = Friendship.objects.all().between(self.inviter, self.invitee)
        return friendship is not None and friendship.status == FriendshipStatus.ACCEPTED

    def _joined(self) -> bool:
        return TripMembership.objects.filter(
            trip=self.trip, profile=self.invitee, status=TripMembership.STATUS_JOINED
        ).exists()

    def test_the_page_asks_both_questions(self) -> None:
        body = self.client.get(reverse("trips.invitation", kwargs={"token": self.invitation.token})).content.decode()
        self.assertIn("Join the trip?", body)
        self.assertIn("Become friends with", body)

    def test_joining_the_trip_does_not_make_friends(self) -> None:
        response = self._answer("trip", "accept")
        self.assertRedirects(response, reverse("trips.detail", args=[self.trip.slug]), fetch_redirect_response=False)
        self.assertTrue(self._joined())
        self.assertFalse(self._friends())
        self.assertFalse(
            Friendship.objects.filter(from_profile=self.inviter, status=FriendshipStatus.REQUESTED).exists()
        )
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.friend_response, TripInvitationResponse.PENDING)

    def test_becoming_friends_does_not_join_the_trip(self) -> None:
        self._answer("friend", "accept")
        self.assertTrue(self._friends())
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=self.invitee).exists())
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.trip_response, TripInvitationResponse.PENDING)

    def test_declining_the_trip_leaves_the_friend_request_open(self) -> None:
        self._answer("trip", "decline")
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.trip_response, TripInvitationResponse.DECLINED)
        self.assertTrue(friendship_offer_open(self.invitation, self.invitee))
        self._answer("friend", "accept")
        self.assertTrue(self._friends())
        self.assertFalse(self._joined())

    def test_declining_friendship_leaves_the_trip_open(self) -> None:
        self._answer("friend", "decline")
        self.assertFalse(self._friends())
        self._answer("trip", "accept")
        self.assertTrue(self._joined())
        self.assertFalse(self._friends())

    def test_a_question_cannot_be_answered_twice(self) -> None:
        respond_to_trip(self.invitation, self.invitee, accept=False)
        with self.assertRaises(TripValidationError):
            respond_to_trip(self.invitation, self.invitee, accept=True)
        self.assertFalse(self._joined())

    def test_another_account_cannot_answer_or_see_it(self) -> None:
        other = _user("other", "other@mailbox.org")
        self.client.force_login(other)
        page = self.client.get(reverse("trips.invitation", kwargs={"token": self.invitation.token}))
        self.assertEqual(page.status_code, 403)
        self.assertNotIn(self.trip.name, page.content.decode())
        self.assertEqual(self._answer("trip", "accept").status_code, 403)
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=other.profile).exists())

    def test_the_inviter_cannot_answer_for_the_invitee(self) -> None:
        with self.assertRaises(TripNotFoundError):
            respond_to_trip(self.invitation, self.inviter, accept=True)
        with self.assertRaises(TripNotFoundError):
            respond_to_friendship(self.invitation, self.inviter, accept=True)

    def test_the_friend_question_is_not_asked_of_an_existing_friend(self) -> None:
        _befriend(self.inviter, self.invitee)
        self.assertFalse(friendship_offer_open(self.invitation, self.invitee))
        body = self.client.get(reverse("trips.invitation", kwargs={"token": self.invitation.token})).content.decode()
        self.assertNotIn("Become friends with", body)
        self.assertIn("Join the trip?", body)

    def test_the_friend_question_honours_the_invitees_friend_request_setting(self) -> None:
        self.invitee.friend_request_visibility = VisibilityChoice.NO_ONE
        self.invitee.save(update_fields=["friend_request_visibility"])
        self.assertFalse(friendship_offer_open(self.invitation, self.invitee))
        self._answer("friend", "accept")
        self.assertFalse(self._friends())

    def test_a_block_closes_both_questions(self) -> None:
        Friendship.objects.create(from_profile=self.inviter, to_profile=self.invitee, status=FriendshipStatus.BLOCKED)
        self.assertEqual(self._answer("trip", "accept").status_code, 404)
        self.assertFalse(self._joined())
        self.assertFalse(friendship_offer_open(self.invitation, self.invitee))

    def test_a_full_trip_refuses_the_join(self) -> None:
        settings = SiteSettings.get_current()
        settings.max_trip_members = 1
        settings.save()
        self._answer("trip", "accept")
        self.assertFalse(self._joined())
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.trip_response, TripInvitationResponse.PENDING)

    def test_signing_out_bounces_an_answer_to_login(self) -> None:
        self.client.logout()
        response = self._answer("trip", "accept")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.assertFalse(self._joined())


class UnregisteredInviteeTests(_InvitationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.trip = _make_trip(self.inviter)
        self.invitation = self.invite(self.trip, UNREGISTERED)
        self.page = reverse("trips.invitation", kwargs={"token": self.invitation.token})

    def test_the_page_opens_without_an_account_and_offers_only_sign_up_and_sign_in(self) -> None:
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(f"?invite={self.invitation.token}", body)
        self.assertNotIn('value="decline"', body)

    def test_an_answer_without_an_account_goes_to_sign_in_and_changes_nothing(self) -> None:
        for question in ("trip", "friend"):
            response = self.client.post(
                reverse(f"trips.invitation.{question}", kwargs={"token": self.invitation.token}),
                {"answer": "decline"},
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("login"), response["Location"])
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.trip_response, TripInvitationResponse.PENDING)
        self.assertEqual(self.invitation.friend_response, TripInvitationResponse.PENDING)

    def test_verifying_a_new_account_binds_the_invitation_but_joins_and_befriends_nothing(self) -> None:
        newcomer = baker.make(User, username="newcomer", email=UNREGISTERED, is_active=False)
        verification = EmailVerification.objects.create(user=newcomer)
        self.client.get(reverse("verify_email", args=[verification.token]))

        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.invitee_id, newcomer.profile.pk)
        self.assertEqual(self.invitation.trip_response, TripInvitationResponse.PENDING)
        self.assertEqual(self.invitation.friend_response, TripInvitationResponse.PENDING)
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=newcomer.profile).exists())
        self.assertFalse(Friendship.objects.all().between(self.inviter, newcomer.profile))
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=newcomer.profile, notification_type=NotificationType.ADDED_TO_TRIP
            ).exists()
        )

    def test_a_new_account_then_answers_each_question_itself(self) -> None:
        newcomer = _user("newcomer", UNREGISTERED)
        bind_invitations_to_account(newcomer)
        self.invitation.refresh_from_db()
        respond_to_friendship(self.invitation, newcomer.profile, accept=True)
        respond_to_trip(self.invitation, newcomer.profile, accept=False)
        self.assertEqual(
            Friendship.objects.all().between(self.inviter, newcomer.profile).status, FriendshipStatus.ACCEPTED
        )
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=newcomer.profile).exists())

    def test_an_expired_invitation_is_not_found(self) -> None:
        TripInvitation.objects.filter(pk=self.invitation.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self.client.get(self.page).status_code, 404)

    def test_an_unknown_token_is_not_found(self) -> None:
        response = self.client.get(
            reverse("trips.invitation", kwargs={"token": "00000000-0000-4000-8000-000000000000"})
        )
        self.assertEqual(response.status_code, 404)


class InviterManagesInvitationsTests(_InvitationTestCase):
    def test_the_inviter_can_withdraw_an_open_invitation(self) -> None:
        trip = _make_trip(self.inviter)
        invitation = self.invite(trip, UNREGISTERED)
        self.client.force_login(self.inviter_user)
        response = self.client.post(reverse("trips.invitation.cancel", args=[trip.slug, invitation.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripInvitation.objects.filter(pk=invitation.pk).exists())
        self.assertNotIn(UNREGISTERED, response.content.decode())

    def test_nobody_else_can_withdraw_it(self) -> None:
        trip = _make_trip(self.inviter)
        TripMembership.objects.create(trip=trip, profile=self.invitee, status=TripMembership.STATUS_JOINED)
        invitation = self.invite(trip, UNREGISTERED)
        self.client.force_login(self.invitee_user)
        response = self.client.post(reverse("trips.invitation.cancel", args=[trip.slug, invitation.uuid]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(TripInvitation.objects.filter(pk=invitation.pk).exists())

    def test_an_invitation_leaves_the_list_once_both_questions_are_answered(self) -> None:
        trip = _make_trip(self.inviter)
        invitation = self.invite(trip, self.invitee_user.email)
        invitation.refresh_from_db()
        respond_to_trip(invitation, self.invitee, accept=True)
        self.client.force_login(self.inviter_user)
        body = self.client.get(reverse("trips.members", args=[trip.slug])).content.decode()
        self.assertIn("Joined", body)
        self.assertIn(f"trip-member-{self.invitee.pk}", body)
        respond_to_friendship(invitation, self.invitee, accept=False)
        body = self.client.get(reverse("trips.members", args=[trip.slug])).content.decode()
        self.assertNotIn("data-trip-invitation", body)


class CreateTripWithEmailInvitationsTests(_InvitationTestCase):
    def test_each_address_is_invited(self) -> None:
        self.client.force_login(self.inviter_user)
        with tasks_run_inline(deliver_trip_invitation), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("trips.create"),
                {"name": "Mill Run", "invite_emails": f"{UNREGISTERED}, {self.invitee_user.email}"},
            )
        self.assertEqual(response.status_code, 200)
        trip = Trip.objects.get(name="Mill Run")
        self.assertEqual(TripInvitation.objects.filter(trip=trip).count(), 2)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(
            NotificationLog.objects.filter(
                profile=self.invitee, notification_type=NotificationType.ADDED_TO_TRIP
            ).exists()
        )
        self.assertFalse(TripMembership.objects.filter(trip=trip, profile=self.invitee).exists())

    def test_a_malformed_address_refuses_before_the_trip_is_made(self) -> None:
        self.client.force_login(self.inviter_user)
        response = self.client.post(
            reverse("trips.create"), {"name": "Mill Run", "invite_emails": "fine@mailbox.org, broken@"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("broken@", response.content.decode())
        self.assertFalse(Trip.objects.filter(name="Mill Run").exists())


class ExternalApiInvitationTests(_InvitationTestCase):
    def setUp(self) -> None:
        super().setUp()
        from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
        from urbanlens.dashboard.services.auth.api_keys import generate_api_key

        api_key, raw = generate_api_key(self.inviter_user, "Mobile")
        ApiKey.objects.filter(pk=api_key.pk).update(
            scopes=[ApiKeyScope.TRIPS_READ.value, ApiKeyScope.TRIPS_WRITE.value]
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}

    def _invite(self, trip: Trip, email: str):
        url = reverse("external_api:trips.invitations", args=[trip.slug])
        with tasks_run_inline(deliver_trip_invitation), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, {"email": email}, content_type="application/json", **self.auth)

    def _shape(self, response, email: str) -> tuple[int, list[str], str]:
        body = response.json()
        return response.status_code, sorted(body), body.get("email", "").replace(email, "EMAIL")

    def test_the_answer_is_the_same_for_an_account_and_an_unregistered_address(self) -> None:
        registered = self._invite(_make_trip(self.inviter), self.invitee_user.email)
        unregistered = self._invite(_make_trip(self.inviter), UNREGISTERED)
        self.assertEqual(registered.status_code, 202)
        self.assertEqual(self._shape(registered, self.invitee_user.email), self._shape(unregistered, UNREGISTERED))
        self.assertNotIn("token", registered.json())
        self.assertNotIn("invitee", registered.json())

    def test_a_member_lists_only_their_own_invitations(self) -> None:
        trip = _make_trip(self.invitee)
        trip.allow_add_members = Trip.PERM_EVERYONE
        trip.save(update_fields=["allow_add_members"])
        TripMembership.objects.create(trip=trip, profile=self.inviter, status=TripMembership.STATUS_JOINED)
        self._invite(trip, UNREGISTERED)
        with tasks_run_inline(deliver_trip_invitation), self.captureOnCommitCallbacks(execute=True):
            invite_to_trip_by_email(trip, self.invitee, "someone-else@mailbox.org", invitation_url_builder=_url)
        response = self.client.get(reverse("external_api:trips.invitations", args=[trip.slug]), **self.auth)
        emails = [row["email"] for row in response.json()["results"]]
        self.assertEqual(emails, [UNREGISTERED])

    def test_delete_withdraws_the_invitation(self) -> None:
        trip = _make_trip(self.inviter)
        created = self._invite(trip, UNREGISTERED).json()
        url = reverse("external_api:trips.invitations.detail", args=[trip.slug, created["uuid"]])
        self.assertEqual(self.client.delete(url, **self.auth).status_code, 204)
        self.assertEqual(self.client.delete(url, **self.auth).status_code, 404)
        self.assertFalse(TripInvitation.objects.exists())


class ReviewFindingsTests(_InvitationTestCase):
    """Attacks found in review, each reproduced before its fix."""

    def test_the_request_does_the_same_database_work_for_any_address(self) -> None:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries_for(email: str) -> int:
            trip = _make_trip(self.inviter)
            with (
                patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task"),
                CaptureQueriesContext(connection) as ctx,
            ):
                invite_to_trip_by_email(trip, self.inviter, email, invitation_url_builder=_url)
            return len(ctx.captured_queries)

        settings = SiteSettings.get_current()
        settings.email_limit_per_hour = 0
        settings.save()
        ProfileEmail.objects.create(
            profile=_user("alias", "alias-primary@mailbox.org").profile, email="alias@mailbox.org", is_verified=True
        )
        baseline = queries_for(UNREGISTERED)
        for email in (self.invitee_user.email, "alias@mailbox.org"):
            with self.subTest(email):
                self.assertEqual(queries_for(email), baseline)

    def test_an_unverified_primary_address_does_not_capture_the_invitation(self) -> None:
        squatter = _user("squatter", "squatter@mailbox.org")
        squatter.email = "victim@mailbox.org"
        squatter.save(update_fields=["email"])
        invitation = self.invite(_make_trip(self.inviter), "victim@mailbox.org")
        invitation.refresh_from_db()
        self.assertIsNone(invitation.invitee_id)
        self.assertFalse(NotificationLog.objects.filter(profile=squatter.profile).exists())
        self.assertEqual([message.to for message in mail.outbox], [["victim@mailbox.org"]])

    def test_a_verified_primary_address_is_notified_in_app(self) -> None:
        self.invitee.verified_primary_email = self.invitee.primary_email_normalized
        self.invitee.save(update_fields=["verified_primary_email"])
        invitation = self.invite(_make_trip(self.inviter), self.invitee_user.email)
        invitation.refresh_from_db()
        self.assertEqual(invitation.invitee_id, self.invitee.pk)
        self.assertEqual(mail.outbox, [])

    def test_a_removed_members_invitation_can_no_longer_be_accepted(self) -> None:
        owner = _user("owner", "owner@mailbox.org").profile
        trip = _make_trip(owner)
        trip.allow_add_members = Trip.PERM_EVERYONE
        trip.save(update_fields=["allow_add_members"])
        TripMembership.objects.create(trip=trip, profile=self.inviter, status=TripMembership.STATUS_JOINED)
        invitation = self.invite(trip, "alt@mailbox.org")
        from urbanlens.dashboard.services.trips.trip_membership import remove_member

        remove_member(trip, owner, self.inviter)
        self.assertFalse(TripInvitation.objects.filter(pk=invitation.pk).exists())

    def test_an_invitation_whose_inviter_lost_the_right_to_add_cannot_be_accepted(self) -> None:
        owner = _user("owner", "owner@mailbox.org").profile
        trip = _make_trip(owner)
        trip.allow_add_members = Trip.PERM_EVERYONE
        trip.save(update_fields=["allow_add_members"])
        TripMembership.objects.create(trip=trip, profile=self.inviter, status=TripMembership.STATUS_JOINED)
        invitation = self.invite(trip, "alt@mailbox.org")
        trip.allow_add_members = Trip.PERM_NONE
        trip.save(update_fields=["allow_add_members"])
        alt = _user("alt", "alt@mailbox.org")
        with self.assertRaises(TripValidationError):
            respond_to_trip(invitation, alt.profile, accept=True)
        self.assertFalse(TripMembership.objects.filter(trip=trip, profile=alt.profile).exists())

    def test_the_creator_sees_and_can_withdraw_every_open_invitation(self) -> None:
        owner_user = _user("owner", "owner@mailbox.org")
        trip = _make_trip(owner_user.profile)
        trip.allow_add_members = Trip.PERM_EVERYONE
        trip.save(update_fields=["allow_add_members"])
        TripMembership.objects.create(trip=trip, profile=self.inviter, status=TripMembership.STATUS_JOINED)
        invitation = self.invite(trip, UNREGISTERED)
        self.client.force_login(owner_user)
        body = self.client.get(reverse("trips.members", args=[trip.slug])).content.decode()
        self.assertIn(UNREGISTERED, body)
        response = self.client.post(reverse("trips.invitation.cancel", args=[trip.slug, invitation.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripInvitation.objects.filter(pk=invitation.pk).exists())

    def test_a_second_member_inviting_the_same_address_gets_their_own_invitation(self) -> None:
        trip = _make_trip(self.inviter)
        trip.allow_add_members = Trip.PERM_EVERYONE
        trip.save(update_fields=["allow_add_members"])
        TripMembership.objects.create(trip=trip, profile=self.invitee, status=TripMembership.STATUS_JOINED)
        first = self.invite(trip, "secret.person@gmail.com")
        with tasks_run_inline(deliver_trip_invitation), self.captureOnCommitCallbacks(execute=True):
            second = invite_to_trip_by_email(
                trip, self.invitee, "secretperson+x@gmail.com", invitation_url_builder=_url
            )
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(second.inviter_id, self.invitee.pk)
        self.assertEqual(second.email, "secretperson+x@gmail.com")

    def test_the_inviter_can_withdraw_the_friend_offer_after_the_trip_is_answered(self) -> None:
        trip = _make_trip(self.inviter)
        invitation = self.invite(trip, UNREGISTERED)
        TripInvitation.objects.filter(pk=invitation.pk).update(trip_response=TripInvitationResponse.DECLINED)
        self.client.force_login(self.inviter_user)
        body = self.client.get(reverse("trips.members", args=[trip.slug])).content.decode()
        self.assertIn(UNREGISTERED, body)
        response = self.client.post(reverse("trips.invitation.cancel", args=[trip.slug, invitation.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(TripInvitation.objects.filter(pk=invitation.pk).exists())


class OnlyTheInvitedAddressCanAnswerTests(_InvitationTestCase):
    """Holding the link is not holding the address: only an account that verified it may answer."""

    def setUp(self) -> None:
        super().setUp()
        self.trip = _make_trip(self.inviter)
        self.invitation = self.invite(self.trip, UNREGISTERED)
        self.stranger = _user("stranger", "stranger@mailbox.org")

    def test_another_signed_in_account_cannot_join_the_trip(self) -> None:
        self.client.force_login(self.stranger)

        response = self.client.post(
            reverse("trips.invitation.trip", kwargs={"token": self.invitation.token}), {"answer": "accept"}
        )

        self.assertNotEqual(response.status_code, 302)
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.invitee_id)
        self.assertFalse(TripMembership.objects.filter(trip=self.trip, profile=self.stranger.profile).exists())

    def test_another_signed_in_account_cannot_befriend_the_inviter(self) -> None:
        self.client.force_login(self.stranger)

        self.client.post(
            reverse("trips.invitation.friend", kwargs={"token": self.invitation.token}), {"answer": "accept"}
        )

        self.assertIsNone(Friendship.objects.all().between(self.inviter, self.stranger.profile))

    def test_signing_up_through_the_link_with_another_address_binds_nothing(self) -> None:
        newcomer = baker.make(User, username="newcomer", email="different@mailbox.org", is_active=False)
        verification = EmailVerification.objects.create(user=newcomer, pending_invite_token=self.invitation.token)

        self.client.get(reverse("verify_email", args=[verification.token]))

        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.invitee_id)

    def test_the_account_that_verified_the_address_can_claim_it_on_the_page(self) -> None:
        owner = _user("owner", UNREGISTERED)
        self.client.force_login(owner)

        self.client.post(
            reverse("trips.invitation.trip", kwargs={"token": self.invitation.token}), {"answer": "accept"}
        )

        self.assertTrue(TripMembership.objects.filter(trip=self.trip, profile=owner.profile).exists())

    def test_a_friend_accept_cannot_override_a_decline_that_already_committed(self) -> None:
        owner = _user("owner", UNREGISTERED)
        TripInvitation.objects.filter(pk=self.invitation.pk).update(invitee=owner.profile)
        stale = TripInvitation.objects.get(pk=self.invitation.pk)
        respond_to_friendship(TripInvitation.objects.get(pk=self.invitation.pk), owner.profile, accept=False)

        with self.assertRaises(TripValidationError):
            respond_to_friendship(stale, owner.profile, accept=True)

        self.assertIsNone(Friendship.objects.all().between(self.inviter, owner.profile))
