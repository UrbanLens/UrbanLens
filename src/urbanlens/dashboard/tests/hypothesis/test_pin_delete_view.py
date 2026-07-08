"""Tests for PinDeleteView - delete a pin owned by the current user."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_edit import PinDeleteView
from urbanlens.dashboard.models.pin.model import Pin


class PinDeleteViewTests(TestCase):
    """DELETE /map/pin/<slug>/delete/ removes the pin and returns HX-Redirect."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.owner = baker.make(User)
        self.profile = self.owner.profile
        self.pin = baker.make(Pin, profile=self.profile, slug="my-pin")

    def _delete(self, user: User, pin_slug: str) -> object:
        request = self.factory.delete(f"/map/pin/{pin_slug}/delete/")
        request.user = user
        return PinDeleteView.as_view()(request, pin_slug=pin_slug)

    def test_owner_delete_returns_200(self) -> None:
        response = self._delete(self.owner, self.pin.slug)
        self.assertEqual(response.status_code, 200)

    def test_owner_delete_sets_hx_redirect_header(self) -> None:
        response = self._delete(self.owner, self.pin.slug)
        self.assertIn("HX-Redirect", response)

    def test_hx_redirect_points_to_map(self) -> None:
        response = self._delete(self.owner, self.pin.slug)
        self.assertIn("/map", response["HX-Redirect"])

    def test_owner_delete_removes_pin_from_db(self) -> None:
        pin_pk = self.pin.pk
        self._delete(self.owner, self.pin.slug)
        self.assertFalse(Pin.objects.filter(pk=pin_pk).exists())

    def test_other_user_delete_returns_404(self) -> None:
        # The lookup is scoped to the requester's own profile, so another
        # user's pin is indistinguishable from a nonexistent one - this
        # avoids leaking pin existence to non-owners.
        other = baker.make(User)
        response = self._delete(other, self.pin.slug)
        self.assertEqual(response.status_code, 404)

    def test_other_user_delete_does_not_remove_pin(self) -> None:
        other = baker.make(User)
        self._delete(other, self.pin.slug)
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_nonexistent_pin_returns_404(self) -> None:
        response = self._delete(self.owner, "no-such-slug-xyz")
        self.assertEqual(response.status_code, 404)

    def test_delete_body_is_empty(self) -> None:
        response = self._delete(self.owner, self.pin.slug)
        self.assertEqual(response.content, b"")


class PinDeleteViewMultiplePinsTests(TestCase):
    """Deleting one pin does not affect the owner's other pins."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.owner = baker.make(User)
        self.profile = self.owner.profile
        self.pin_a = baker.make(Pin, profile=self.profile)
        self.pin_b = baker.make(Pin, profile=self.profile)

    def test_deleting_one_pin_leaves_other_intact(self) -> None:
        request = self.factory.delete(f"/map/pin/{self.pin_a.slug}/delete/")
        request.user = self.owner
        PinDeleteView.as_view()(request, pin_slug=self.pin_a.slug)
        self.assertTrue(Pin.objects.filter(pk=self.pin_b.pk).exists())
