"""An SSO sign-in cannot put an account on an address it has not proved, or that another account holds (G3-31)."""

from __future__ import annotations

from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory
from model_bakery import baker
from social_django.models import UserSocialAuth
from social_django.utils import load_backend, load_strategy

from urbanlens.core.tests.celery_inline import tasks_run_inline
from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account import EmailVerification
from urbanlens.dashboard.models.profile.email import ProfileEmail
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.email_normalization import find_user_by_email, find_verified_user_by_email


def _request():
    request = RequestFactory().get("/accounts/complete/discord/")
    SessionMiddleware(lambda r: HttpResponse()).process_request(request)
    MessageMiddleware(lambda r: HttpResponse()).process_request(request)
    request.session.save()
    return request


def _sign_in(provider: str, response: dict[str, Any]) -> Any:
    """Run the configured social-auth pipeline for ``response``, as the completion view does.

    Returns:
        The signed-in user, or the response a pipeline step answered with.
    """
    request = _request()
    strategy = load_strategy(request)
    backend = load_backend(strategy, provider, redirect_uri=None)
    with mock.patch("urbanlens.dashboard.services.profile.avatar.AvatarService.download", return_value=None):
        return backend.authenticate(response=response, backend=backend, strategy=strategy, request=request)


def _verified_owner(email: str) -> User:
    user = baker.make(User, email=email, is_active=True)
    Profile.objects.filter(user=user).update(verified_primary_email=user.profile.primary_email_normalized)
    return user


def _discord(uid: str, email: str, *, verified: bool) -> dict[str, Any]:
    return {"id": uid, "username": f"handle{uid}", "email": email, "verified": verified, "access_token": "t"}


class SsoSquattingTests(TestCase):
    """The exploit: sign in with a provider account whose address you never proved."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.owner = _verified_owner("victim@example.com")

    def test_an_unverified_provider_address_does_not_land_on_the_new_account(self) -> None:
        with self.captureOnCommitCallbacks(execute=True), mock.patch("django.core.mail.EmailMultiAlternatives.send"):
            user = _sign_in("discord", _discord("111", "victim@example.com", verified=False))

        self.assertIsInstance(user, User)
        self.assertNotEqual(user.pk, self.owner.pk)
        user.refresh_from_db()
        self.assertEqual(user.email, "")
        self.assertEqual(find_user_by_email("victim@example.com").pk, self.owner.pk)
        self.assertEqual(Profile.objects.filter(primary_email_normalized="victim@example.com").count(), 1)

    def test_an_unverified_provider_address_is_claimed_through_the_confirmation_flow(self) -> None:
        """It is still the signer's to prove: a pending claim, and the holder gets the in-use notice."""
        from urbanlens.dashboard.tasks import deliver_email_claim

        # The patch is outermost so it is still in place when the on-commit delivery runs.
        with (
            mock.patch("urbanlens.dashboard.services.auth.email_claims.send_address_in_use_notice") as notice,
            tasks_run_inline(deliver_email_claim),
            self.captureOnCommitCallbacks(execute=True),
        ):
            user = _sign_in("discord", _discord("112", "victim@example.com", verified=False))

        claim = ProfileEmail.objects.get(profile__user=user)
        self.assertFalse(claim.is_verified)
        self.assertTrue(claim.promote_on_verify)
        notice.assert_called_once_with("victim@example.com")

    def test_a_held_and_a_free_unverified_address_look_the_same_to_the_signer(self) -> None:
        """The enumeration policy: nothing the signer sees differs by whether the address is registered."""
        outcomes = []
        for uid, email in (("113", "victim@example.com"), ("114", "nobody@example.com")):
            with (
                self.captureOnCommitCallbacks(execute=True),
                mock.patch("django.core.mail.EmailMultiAlternatives.send"),
            ):
                user = _sign_in("discord", _discord(uid, email, verified=False))
            user.refresh_from_db()
            claim = ProfileEmail.objects.get(profile__user=user)
            outcomes.append((type(user), user.email, user.is_active, claim.is_verified, claim.promote_on_verify))

        self.assertEqual(outcomes[0], outcomes[1])

    def test_a_verified_provider_address_another_account_holds_creates_no_account(self) -> None:
        """The signer proved the mailbox, so telling them it is registered tells its owner."""
        users_before = User.objects.count()

        result = _sign_in("discord", _discord("115", "victim@example.com", verified=True))

        self.assertNotIsInstance(result, User)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(User.objects.count(), users_before)
        self.assertFalse(UserSocialAuth.objects.filter(uid="115").exists())

    def test_a_pending_signup_holds_the_address_against_a_verified_sso_account_too(self) -> None:
        pending = baker.make(User, email="pending@example.com", is_active=False)
        EmailVerification.objects.create(user=pending)

        result = _sign_in("discord", _discord("116", "pending@example.com", verified=True))

        self.assertNotIsInstance(result, User)
        self.assertTrue(User.objects.filter(pk=pending.pk).exists())

    def test_an_abandoned_signup_does_not_hold_the_address(self) -> None:
        """The same rule complete_signup applies: an expired, never-verified signup never proved it."""
        import datetime

        from django.utils import timezone

        abandoned = baker.make(User, email="late@example.com", is_active=False)
        verification = EmailVerification.objects.create(user=abandoned)
        EmailVerification.objects.filter(pk=verification.pk).update(
            created=timezone.now() - datetime.timedelta(days=30)
        )

        user = _sign_in("discord", _discord("117", "late@example.com", verified=True))

        self.assertIsInstance(user, User)
        self.assertFalse(User.objects.filter(pk=abandoned.pk).exists())
        self.assertEqual(find_verified_user_by_email("late@example.com").pk, user.pk)

    def test_a_verified_free_provider_address_becomes_the_verified_primary(self) -> None:
        user = _sign_in("discord", _discord("118", "fresh@example.com", verified=True))

        self.assertIsInstance(user, User)
        self.assertEqual(find_verified_user_by_email("fresh@example.com").pk, user.pk)

    def test_a_returning_accounts_provider_address_change_does_not_move_its_email(self) -> None:
        user = _sign_in("discord", _discord("119", "mine@example.com", verified=True))

        again = _sign_in("discord", _discord("119", "victim@example.com", verified=True))

        self.assertEqual(again.pk, user.pk)
        again.refresh_from_db()
        self.assertEqual(again.email, "mine@example.com")
        self.assertEqual(find_user_by_email("victim@example.com").pk, self.owner.pk)


class VerifiedPrimaryUniquenessTests(TestCase):
    """At most one account holds a given address as its proved primary."""

    def test_two_accounts_cannot_both_hold_one_verified_primary(self) -> None:
        from django.db import IntegrityError, transaction

        first = _verified_owner("one@example.com")
        second = baker.make(User, email="one@example.com", is_active=True)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Profile.objects.filter(user=second).update(verified_primary_email="one@example.com")
        self.assertEqual(find_verified_user_by_email("one@example.com").pk, first.pk)

    def test_changing_the_primary_drops_the_stale_verified_address(self) -> None:
        user = _verified_owner("old@example.com")

        user.email = "new@example.com"
        user.save(update_fields=["email"])

        user.profile.refresh_from_db()
        self.assertEqual(user.profile.verified_primary_email, "")
        # The released address can now be proved by someone else.
        _verified_owner("old@example.com")


class CalendarAttendeeMatchTests(TestCase):
    """A calendar attendee is matched to an account only by an address that account proved."""

    def test_an_unverified_primary_is_not_matched_as_a_friend(self) -> None:
        from urbanlens.dashboard.models.friendship.meta import FriendshipStatus, FriendshipType
        from urbanlens.dashboard.models.friendship.model import Friendship
        from urbanlens.dashboard.services.trips.calendar_sync import match_event_attendees

        baker.make(User)
        importer = baker.make(User, email="importer@example.com").profile
        squatter = baker.make(User, email="colleague@example.com", is_active=True).profile
        Friendship.objects.create(
            from_profile=importer,
            to_profile=squatter,
            status=FriendshipStatus.ACCEPTED,
            relationship_type=FriendshipType.FRIEND,
        )

        friends, others = match_event_attendees(importer, {"attendees": [{"email": "colleague@example.com"}]})

        self.assertEqual(friends, [])
        self.assertEqual(others, ["colleague@example.com"])

        Profile.objects.filter(pk=squatter.pk).update(verified_primary_email="colleague@example.com")
        friends, _others = match_event_attendees(importer, {"attendees": [{"email": "colleague@example.com"}]})
        self.assertEqual([p.pk for p in friends], [squatter.pk])
