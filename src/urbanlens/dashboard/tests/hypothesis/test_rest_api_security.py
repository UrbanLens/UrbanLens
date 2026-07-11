"""Tests for the /rest/ API surface: authentication, per-user scoping, and minimal exposure.

The REST API exists solely for the app's own frontend, which uses exactly
three operations:

- ``PATCH /rest/pins/<uuid>/``  (map popup quick-edit, pin dragging)
- ``DELETE /rest/pins/<uuid>/`` (pin delete with undo stash)
- ``PATCH /rest/reviews/create_or_update/<pin id>/`` (star-rating widget)

Everything else (pin create/list/retrieve, profile CRUD, review router
routes) has been deliberately removed; these tests pin that down so the
surface cannot silently grow back.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import NoReverseMatch, reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.reviews.model import Review


class RestApiRequiresAuthenticationTests(TestCase):
    """Anonymous requests must be rejected by every /rest/ endpoint."""

    def test_anonymous_cannot_patch_a_pin(self) -> None:
        pin = baker.make(Pin, profile=baker.make(User).profile)
        response = self.client.patch(
            reverse("pins-detail", args=[pin.uuid]),
            data={"name": "hijacked"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_delete_a_pin(self) -> None:
        pin = baker.make(Pin, profile=baker.make(User).profile)
        response = self.client.delete(reverse("pins-detail", args=[pin.uuid]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Pin.objects.filter(pk=pin.pk).exists())

    def test_anonymous_cannot_rate_a_pin(self) -> None:
        pin = baker.make(Pin, profile=baker.make(User).profile)
        response = self.client.patch(
            reverse("review-create-or-update", args=[pin.pk]),
            data={"rating": 3},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)


class RemovedRestRoutesTests(TestCase):
    """Routes the app does not use must not exist at all."""

    def test_profile_routes_do_not_exist(self) -> None:
        with self.assertRaises(NoReverseMatch):
            reverse("profiles-list")
        with self.assertRaises(NoReverseMatch):
            reverse("profiles-detail", args=["00000000-0000-0000-0000-000000000000"])

    def test_review_router_routes_do_not_exist(self) -> None:
        with self.assertRaises(NoReverseMatch):
            reverse("reviews-list")
        with self.assertRaises(NoReverseMatch):
            reverse("reviews-detail", args=[1])

    def test_pin_list_and_create_routes_do_not_exist(self) -> None:
        with self.assertRaises(NoReverseMatch):
            reverse("pins-list")

    def test_pin_detail_only_allows_patch_and_delete(self) -> None:
        user = baker.make(User)
        self.client.force_login(user)
        pin = baker.make(Pin, profile=user.profile)
        url = reverse("pins-detail", args=[pin.uuid])
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(
            self.client.put(url, data={"name": "x"}, content_type="application/json").status_code,
            405,
        )


class PinScopingTests(TestCase):
    """Pins must only ever be editable/deletable by their owner."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)

    def test_patch_own_pin_succeeds(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        response = self.client.patch(
            reverse("pins-detail", args=[pin.uuid]),
            data={"name": "Renamed"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        pin.refresh_from_db()
        self.assertEqual(pin.name, "Renamed")

    def test_cannot_patch_other_users_pin(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make(User).profile, name="Theirs")
        response = self.client.patch(
            reverse("pins-detail", args=[other_pin.uuid]),
            data={"name": "hijacked"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 404)
        other_pin.refresh_from_db()
        self.assertEqual(other_pin.name, "Theirs")

    def test_cannot_reassign_pin_to_another_profile(self) -> None:
        other_profile = baker.make(User).profile
        pin = baker.make(Pin, profile=self.profile)
        response = self.client.patch(
            reverse("pins-detail", args=[pin.uuid]),
            data={"profile": other_profile.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        pin.refresh_from_db()
        self.assertEqual(pin.profile, self.profile)

    def test_patch_with_coordinates_moves_pin(self) -> None:
        """A coordinate PATCH must actually repoint the pin (coords live on Location)."""
        origin, _ = Location.objects.get_nearby_or_create(42.65, -73.75)
        pin = baker.make(Pin, profile=self.profile, location=origin)
        response = self.client.patch(
            reverse("pins-detail", args=[pin.uuid]),
            data={"latitude": "41.500000", "longitude": "-72.500000"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        pin.refresh_from_db()
        self.assertAlmostEqual(float(pin.location.latitude), 41.5, places=4)
        self.assertAlmostEqual(float(pin.location.longitude), -72.5, places=4)

    def test_patch_with_invalid_coordinates_is_rejected(self) -> None:
        origin, _ = Location.objects.get_nearby_or_create(42.65, -73.75)
        pin = baker.make(Pin, profile=self.profile, location=origin)
        for lat, lng in (("north", "west"), ("nan", "inf"), ("95", "0"), ("0", "999")):
            response = self.client.patch(
                reverse("pins-detail", args=[pin.uuid]),
                data={"latitude": lat, "longitude": lng},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 400, (lat, lng, response.content))
        pin.refresh_from_db()
        self.assertAlmostEqual(float(pin.location.latitude), 42.65, places=4)

    def test_delete_own_pin_succeeds(self) -> None:
        pin = baker.make(Pin, profile=self.profile)
        response = self.client.delete(reverse("pins-detail", args=[pin.uuid]))
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Pin.objects.filter(pk=pin.pk).exists())

    def test_cannot_delete_other_users_pin(self) -> None:
        other_pin = baker.make(Pin, profile=baker.make(User).profile)
        response = self.client.delete(reverse("pins-detail", args=[other_pin.uuid]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Pin.objects.filter(pk=other_pin.pk).exists())


class ReviewUpsertTests(TestCase):
    """The rating endpoint must upsert on (profile, pin) without server errors."""

    def setUp(self) -> None:
        self.user = baker.make(User)
        self.profile = self.user.profile
        self.client.force_login(self.user)
        self.pin = baker.make(Pin, profile=self.profile)

    def test_create_or_update_route_upserts(self) -> None:
        url = reverse("review-create-or-update", args=[self.pin.pk])
        first = self.client.patch(url, data={"rating": 3}, content_type="application/json")
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.patch(url, data={"rating": 1}, content_type="application/json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(Review.objects.get(profile=self.profile, pin=self.pin).rating, 1)

    def test_create_or_update_without_rating_is_rejected(self) -> None:
        url = reverse("review-create-or-update", args=[self.pin.pk])
        response = self.client.patch(url, data={}, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_create_or_update_unknown_pin_is_rejected(self) -> None:
        url = reverse("review-create-or-update", args=[999999])
        response = self.client.patch(url, data={"rating": 3}, content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_cannot_create_review_as_another_profile(self) -> None:
        other_profile = baker.make(User).profile
        response = self.client.patch(
            reverse("review-create-or-update", args=[self.pin.pk]),
            data={"rating": 4, "profile": other_profile.pk},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Review.objects.get(pin=self.pin).profile, self.profile)

    def test_create_or_update_rejects_another_users_pin_like_unknown_pin(self) -> None:
        """Foreign pins get the same 400 as nonexistent ones, so pin ids cannot be enumerated."""
        other_pin = baker.make(Pin, profile=baker.make(User).profile)
        foreign = self.client.patch(
            reverse("review-create-or-update", args=[other_pin.pk]),
            data={"rating": 3},
            content_type="application/json",
        )
        unknown = self.client.patch(
            reverse("review-create-or-update", args=[999999]),
            data={"rating": 3},
            content_type="application/json",
        )
        self.assertEqual(foreign.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(foreign.json(), unknown.json())
        self.assertFalse(Review.objects.filter(pin=other_pin).exists())
