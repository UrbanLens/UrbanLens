"""Google sign-in: links keyed by address, and an unverified address never signs in to the account holding it."""

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


class GoogleLinkTests(TestCase):
    """Google links stay keyed by address, so an account can also sign in by email; an unverified copy of the address never signs in."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = _owner("owner@example.com")
        self.link = UserSocialAuth.objects.create(user=self.owner, provider=_PROVIDER, uid="owner@example.com")

    def test_a_new_link_is_keyed_by_address(self) -> None:
        user = _sign_in(_google("1001", "first@example.com"))

        self.assertIsInstance(user, User)
        self.assertEqual(UserSocialAuth.objects.get(user=user, provider=_PROVIDER).uid, "first@example.com")

    def test_the_owner_signs_in_with_a_verified_address(self) -> None:
        user = _sign_in(_google("2001", "owner@example.com"))

        self.assertEqual(user.pk, self.owner.pk)
        self.link.refresh_from_db()
        self.assertEqual(self.link.uid, "owner@example.com")

    def test_an_unverified_address_does_not_sign_in_as_its_owner(self) -> None:
        with self.captureOnCommitCallbacks(execute=True), mock.patch("django.core.mail.EmailMultiAlternatives.send"):
            result = _sign_in(_google("3001", "owner@example.com", verified=False))

        self.assertFalse(isinstance(result, User) and result.pk == self.owner.pk)
        self.link.refresh_from_db()
        self.assertEqual(self.link.user_id, self.owner.pk)
