"""A Google link is keyed by the account's ``sub``, not its address, so a reassigned address signs in as nobody (P152)."""

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

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile

_PROVIDER = "google-oauth2"


def _sign_in(response: dict[str, Any]) -> Any:
    request = RequestFactory().get("/accounts/complete/google-oauth2/")
    SessionMiddleware(lambda r: HttpResponse()).process_request(request)
    MessageMiddleware(lambda r: HttpResponse()).process_request(request)
    request.session.save()
    strategy = load_strategy(request)
    backend = load_backend(strategy, _PROVIDER, redirect_uri=None)
    with mock.patch("urbanlens.dashboard.services.profile.avatar.AvatarService.download", return_value=None):
        return backend.authenticate(response=response, backend=backend, strategy=strategy, request=request)


def _google(sub: str, email: str, *, verified: bool = True) -> dict[str, Any]:
    return {"sub": sub, "email": email, "email_verified": verified, "access_token": "t"}


def _owner(email: str) -> User:
    user = baker.make(User, email=email, is_active=True)
    Profile.objects.filter(user=user).update(verified_primary_email=user.profile.primary_email_normalized)
    return user


class ReassignedAddressTests(TestCase):
    """The exploit: a different Google account now holds the address an UrbanLens account signed up with."""

    def test_a_new_link_is_keyed_by_sub(self) -> None:
        user = _sign_in(_google("1001", "first@example.com"))

        self.assertIsInstance(user, User)
        self.assertEqual(UserSocialAuth.objects.get(user=user, provider=_PROVIDER).uid, "1001")

    def test_a_different_google_account_holding_the_address_does_not_sign_in_as_its_owner(self) -> None:
        owner = _sign_in(_google("1002", "alice@corp.example.com"))
        self.assertIsInstance(owner, User)

        result = _sign_in(_google("2002", "alice@corp.example.com"))

        self.assertFalse(isinstance(result, User) and result.pk == owner.pk)
        self.assertEqual(UserSocialAuth.objects.get(user=owner, provider=_PROVIDER).uid, "1002")

    def test_the_owner_still_signs_in_after_changing_their_google_address(self) -> None:
        owner = _sign_in(_google("1003", "old@example.com"))

        again = _sign_in(_google("1003", "new@example.com"))

        self.assertEqual(again.pk, owner.pk)


class LegacyAddressKeyedLinkTests(TestCase):
    """Links made before the switch are keyed by address; each is re-keyed to ``sub`` at its owner's next sign-in."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = _owner("legacy@example.com")
        self.link = UserSocialAuth.objects.create(user=self.owner, provider=_PROVIDER, uid="legacy@example.com")

    def test_the_owner_signs_in_and_the_link_is_rekeyed(self) -> None:
        user = _sign_in(_google("3001", "legacy@example.com"))

        self.assertEqual(user.pk, self.owner.pk)
        self.link.refresh_from_db()
        self.assertEqual(self.link.uid, "3001")

    def test_once_rekeyed_another_google_account_with_the_address_is_refused(self) -> None:
        _sign_in(_google("3002", "legacy@example.com"))

        result = _sign_in(_google("4002", "legacy@example.com"))

        self.assertNotIsInstance(result, User)
        self.link.refresh_from_db()
        self.assertEqual(self.link.uid, "3002")

    def test_an_unverified_address_does_not_adopt_the_link(self) -> None:
        with self.captureOnCommitCallbacks(execute=True), mock.patch("django.core.mail.EmailMultiAlternatives.send"):
            result = _sign_in(_google("3003", "legacy@example.com", verified=False))

        self.assertFalse(isinstance(result, User) and result.pk == self.owner.pk)
        self.link.refresh_from_db()
        self.assertEqual(self.link.uid, "legacy@example.com")

    def test_a_google_account_already_linked_by_sub_is_not_moved_onto_the_legacy_row(self) -> None:
        other = _owner("other@example.com")
        UserSocialAuth.objects.create(user=other, provider=_PROVIDER, uid="3004")

        user = _sign_in(_google("3004", "legacy@example.com"))

        self.assertEqual(user.pk, other.pk)
        self.link.refresh_from_db()
        self.assertEqual(self.link.uid, "legacy@example.com")

    def test_a_discord_link_whose_uid_looks_like_the_address_is_untouched(self) -> None:
        discord = UserSocialAuth.objects.create(user=self.owner, provider="discord", uid="3005@example.com")

        _sign_in(_google("3005", "3005@example.com"))

        discord.refresh_from_db()
        self.assertEqual(discord.uid, "3005@example.com")
