"""Google sign-in: links keyed by the account's stable id, address-keyed legacy links migrate, and an unverified address never signs in."""

from __future__ import annotations

from typing import Any
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponse
from django.test import RequestFactory
from model_bakery import baker
from social_core.exceptions import AuthResponseError
from social_django.models import UserSocialAuth
from social_django.utils import load_backend, load_strategy

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile

_PROVIDER = "google-oauth2"


def _backend() -> tuple[Any, Any, Any]:
    request = RequestFactory().get("/accounts/complete/google-oauth2/")
    SessionMiddleware(lambda r: HttpResponse()).process_request(request)
    MessageMiddleware(lambda r: HttpResponse()).process_request(request)
    request.session.save()
    strategy = load_strategy(request)
    return request, strategy, load_backend(strategy, _PROVIDER, redirect_uri=None)


def _sign_in(response: dict[str, Any]) -> Any:
    request, strategy, backend = _backend()
    with mock.patch("urbanlens.dashboard.services.profile.avatar.AvatarService.download", return_value=None):
        return backend.authenticate(response=response, backend=backend, strategy=strategy, request=request)


def _google(sub: str, email: str, *, verified: bool = True) -> dict[str, Any]:
    return {"sub": sub, "email": email, "email_verified": verified, "access_token": "t"}


def _owner(email: str) -> User:
    user = baker.make(User, email=email, is_active=True)
    Profile.objects.filter(user=user).update(verified_primary_email=user.profile.primary_email_normalized)
    return user


class GoogleLinkTests(TestCase):
    """social-auth-core 6 keys Google links by ``sub`` and migrates an address-keyed legacy row on a verified sign-in."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = _owner("owner@example.com")
        self.link = UserSocialAuth.objects.create(user=self.owner, provider=_PROVIDER, uid="owner@example.com")

    def test_a_new_link_is_keyed_by_the_google_account_id(self) -> None:
        user = _sign_in(_google("1001", "first@example.com"))

        self.assertIsInstance(user, User)
        self.assertEqual(UserSocialAuth.objects.get(user=user, provider=_PROVIDER).uid, "1001")

    def test_the_owner_migrates_from_an_address_keyed_link_with_a_verified_address(self) -> None:
        user = _sign_in(_google("2001", "owner@example.com"))

        self.assertEqual(user.pk, self.owner.pk)
        self.link.refresh_from_db()
        self.assertEqual(self.link.uid, "2001")

    def test_an_unverified_address_is_refused_before_the_pipeline_runs(self) -> None:
        """The refusal is the backend's: ``user_data`` raises on anything but ``email_verified is True``, so no link is looked up."""
        _request, _strategy, backend = _backend()

        for claim in (False, None, "true"):
            payload = {"sub": "3001", "email": "owner@example.com", "email_verified": claim}
            with (
                self.subTest(email_verified=claim),
                mock.patch.object(backend, "get_json", return_value=payload),
                self.assertRaises(AuthResponseError),
            ):
                backend.user_data("t")

        self.link.refresh_from_db()
        self.assertEqual((self.link.user_id, self.link.uid), (self.owner.pk, "owner@example.com"))
