"""Tests for services.ai.page_context (plan §9, batch 3)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.ai.page_context import (
    PageObject,
    page_object_from_dict,
    page_object_to_dict,
    resolve_page_context,
    verify_page_object,
)


class ResolvePageContextTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user: User = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)

    def test_unresolvable_path_returns_none(self) -> None:
        self.assertIsNone(resolve_page_context("/this/route/does/not/exist/", self.profile))

    def test_a_resolvable_but_unregistered_page_returns_none(self) -> None:
        # settings.view is a real URL this module has no resolver for.
        self.assertIsNone(resolve_page_context(reverse("settings.view"), self.profile))

    def test_map_resolves_with_no_object(self) -> None:
        context = resolve_page_context(reverse("map.view"), self.profile)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.url_name, "map.view")
        self.assertEqual(context.page_help_key, "map")
        self.assertIsNone(context.object)

    def test_query_string_is_stripped_before_resolution(self) -> None:
        context = resolve_page_context(reverse("map.view") + "?foo=bar&baz=1", self.profile)
        self.assertIsNotNone(context)

    def test_own_pin_resolves_to_its_object(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile)
        context = resolve_page_context(reverse("pin.details", args=[pin.slug]), self.profile)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.page_help_key, "pin_detail")
        self.assertEqual(context.object, PageObject(kind="pin", id=pin.pk))

    def test_another_profiles_pin_resolves_with_no_object(self) -> None:
        """The page itself is real (pin_detail help still applies) but the object is not this profile's to see."""
        other_pin = baker.make("dashboard.Pin", profile=self.other_profile)
        context = resolve_page_context(reverse("pin.details", args=[other_pin.slug]), self.profile)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.page_help_key, "pin_detail")
        self.assertIsNone(context.object)

    def test_a_spoofed_pin_slug_resolves_with_no_object(self) -> None:
        context = resolve_page_context(reverse("pin.details", args=["not-a-real-slug"]), self.profile)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertIsNone(context.object)

    def test_own_trip_resolves_to_its_object(self) -> None:
        trip = baker.make("dashboard.Trip", creator=self.profile)
        context = resolve_page_context(reverse("trips.detail", args=[trip.slug]), self.profile)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.page_help_key, "trip_detail")
        self.assertEqual(context.object, PageObject(kind="trip", id=trip.pk))

    def test_another_profiles_private_trip_resolves_with_no_object(self) -> None:
        other_trip = baker.make("dashboard.Trip", creator=self.other_profile)
        context = resolve_page_context(reverse("trips.detail", args=[other_trip.slug]), self.profile)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertIsNone(context.object)


class VerifyPageObjectTests(TestCase):
    def setUp(self) -> None:
        self.user: User = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.other_user: User = baker.make(User)
        self.other_profile = Profile.objects.get(user=self.other_user)

    def test_own_pin_is_verified(self) -> None:
        pin = baker.make("dashboard.Pin", profile=self.profile)
        self.assertTrue(verify_page_object(self.profile, PageObject(kind="pin", id=pin.pk)))

    def test_another_profiles_pin_is_not_verified(self) -> None:
        other_pin = baker.make("dashboard.Pin", profile=self.other_profile)
        self.assertFalse(verify_page_object(self.profile, PageObject(kind="pin", id=other_pin.pk)))

    def test_own_trip_is_verified(self) -> None:
        trip = baker.make("dashboard.Trip", creator=self.profile)
        self.assertTrue(verify_page_object(self.profile, PageObject(kind="trip", id=trip.pk)))

    def test_another_profiles_trip_is_not_verified(self) -> None:
        other_trip = baker.make("dashboard.Trip", creator=self.other_profile)
        self.assertFalse(verify_page_object(self.profile, PageObject(kind="trip", id=other_trip.pk)))

    def test_unknown_kind_is_not_verified(self) -> None:
        self.assertFalse(verify_page_object(self.profile, PageObject(kind="wiki", id=1)))


class PageObjectSerializationTests(TestCase):
    def test_round_trips(self) -> None:
        obj = PageObject(kind="pin", id=42)
        self.assertEqual(page_object_from_dict(page_object_to_dict(obj)), obj)

    def test_none_round_trips_to_none(self) -> None:
        self.assertIsNone(page_object_to_dict(None))
        self.assertIsNone(page_object_from_dict(None))

    def test_malformed_data_is_none_not_a_raise(self) -> None:
        self.assertIsNone(page_object_from_dict({}))
        self.assertIsNone(page_object_from_dict({"kind": "pin"}))
        self.assertIsNone(page_object_from_dict({"kind": "pin", "id": "not-an-int"}))
        self.assertIsNone(page_object_from_dict("not-a-dict"))  # type: ignore[arg-type]
