"""Tests for PinEditView category update logic."""

from __future__ import annotations

from datetime import date
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import RequestFactory
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.controllers.pin_edit import PinEditView, PinOverviewView
from urbanlens.dashboard.models.labels.model import Label
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.rate_limiter import RateLimitExceededError

if TYPE_CHECKING:
    from django.http import HttpResponseBase


class PinEditCategoryUpdateTests(TestCase):
    """Regression tests for partial updates and category scoping."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile)
        self.existing_cat = baker.make(
            Label, name="existing", kind="category", profile=self.profile,
        )
        self.pin.labels.add(self.existing_cat)

    def _post(self, body: dict) -> HttpResponseBase:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        # The rendered overview partial checks pin.has_place_name, which
        # resolves an uncached Location's place name from Google - mock it
        # so the response render doesn't make an outbound API call.
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def _categories(self):
        return self.pin.labels.filter(kind="category")

    def test_partial_priority_update_preserves_existing_categories(self) -> None:
        """Submitting only priority must not clear the pin's categories."""
        response = self._post({"priority": 3})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        category_ids = list(self._categories().values_list("id", flat=True))
        self.assertIn(
            self.existing_cat.id,
            category_ids,
            "Partial edit (priority only) must not clear categories",
        )

    def test_explicit_category_update_uses_owner_categories(self) -> None:
        """Submitting categories should resolve/create against the pin owner's profile."""
        new_cat_name = "wilderness"
        response = self._post({"categories": new_cat_name})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        cats = list(self._categories())
        self.assertEqual(len(cats), 1)
        self.assertEqual(cats[0].name, new_cat_name)
        # Must be owned by the pin's profile, not global
        self.assertEqual(cats[0].profile_id, self.profile.pk)

    def test_explicit_empty_categories_clears_all(self) -> None:
        """Submitting an empty categories string explicitly clears all categories."""
        response = self._post({"categories": ""})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self._categories().count(), 0)

    def test_duplicate_category_names_are_deduplicated(self) -> None:
        """Comma-separated list with duplicates should not create two labels."""
        response = self._post({"categories": "nature,nature,Nature"})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self._categories().count(), 1)


class PinEditNameAliasTests(TestCase):
    """Renames preserve every name as an alias, including the current one."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile, name="Old Factory", name_is_user_provided=True)

    def _post(self, body: dict) -> HttpResponseBase:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        # The rendered overview partial checks pin.has_place_name, which
        # resolves an uncached Location's place name from Google - mock it
        # so the response render doesn't make an outbound API call.
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def test_renaming_pin_keeps_old_and_new_names_as_aliases(self) -> None:
        response = self._post({"name": "New Factory"})

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "New Factory")
        self.assertCountEqual(list(self.pin.aliases.values_list("name", flat=True)), ["Old Factory", "New Factory"])

    def test_resubmitting_same_name_does_not_add_an_alias(self) -> None:
        response = self._post({"name": "Old Factory"})

        self.assertEqual(response.status_code, 200)
        # Only the creation-time alias for the current name exists.
        self.assertEqual(list(self.pin.aliases.values_list("name", flat=True)), ["Old Factory"])

    def test_partial_update_without_name_does_not_add_an_alias(self) -> None:
        response = self._post({"priority": 3})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(self.pin.aliases.values_list("name", flat=True)), ["Old Factory"])


class PinEditDateFieldsTests(TestCase):
    """date_built (and its siblings) round-trip through the edit endpoint."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile, name="Old Factory", name_is_user_provided=True)

    def _post(self, body: dict) -> HttpResponseBase:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def test_date_built_saves_and_clears(self) -> None:
        response = self._post({"date_built": "1912-05-01"})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.date_built, date(1912, 5, 1))

        response = self._post({"date_built": ""})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.date_built)

    def test_partial_update_without_date_built_preserves_it(self) -> None:
        self.pin.date_built = date(1900, 1, 1)
        self.pin.save(update_fields=["date_built"])
        response = self._post({"priority": 3})
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.date_built, date(1900, 1, 1))


class PinOverviewAddressBackfillDispatchTests(TestCase):
    """A route-less location's address backfill is dispatched to Celery, never geocoded inline.

    Successor to the old inline-geocoding failure regression test: the view
    used to call ensure_location_address (a live Google Geocoding call)
    synchronously on every /overview/ visit for a route-less location - first
    500ing on rate-limit errors, then (after that was fixed) still blocking
    the render on the API round-trip. It now enqueues
    tasks.backfill_location_address instead, mirroring the place-name lazy
    dispatch - so a rate-limited/slow/down geocoding API can no longer affect
    this page's render at all.
    """

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.profile.external_apis_enabled = True
        self.profile.save(update_fields=["external_apis_enabled"])
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile)
        self.pin.location.route = ""
        self.pin.location.save(update_fields=["route"])

    def _get(self, mock_enqueue_target: str = "urbanlens.dashboard.services.celery.safely_enqueue_task"):
        req = self.factory.get(f"/map/pin/{self.pin.slug}/overview/")
        req.user = self.user
        with (
            patch(mock_enqueue_target) as mock_enqueue,
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
        ):
            response = PinOverviewView.as_view()(req, pin_slug=self.pin.slug)
        return response, mock_enqueue

    def _address_dispatches(self, mock_enqueue) -> list[int]:
        from urbanlens.dashboard.tasks import backfill_location_address

        return [call.args[1] for call in mock_enqueue.call_args_list if call.args and call.args[0] is backfill_location_address]

    def test_route_less_location_dispatches_the_backfill_task(self) -> None:
        response, mock_enqueue = self._get()
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.pin.location_id, self._address_dispatches(mock_enqueue))

    def test_location_with_a_route_does_not_dispatch(self) -> None:
        self.pin.location.route = "Somewhere St"
        self.pin.location.save(update_fields=["route"])
        _response, mock_enqueue = self._get()
        self.assertEqual(self._address_dispatches(mock_enqueue), [])

    def test_render_never_geocodes_inline(self) -> None:
        """The actual guarantee: no live geocoding call can run (and therefore
        neither block nor 500 this render), regardless of API state."""
        req = self.factory.get(f"/map/pin/{self.pin.slug}/overview/")
        req.user = self.user
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.geocoding.GoogleGeocodingGateway.geocode_coordinates") as mock_geocode,
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
            patch("urbanlens.dashboard.services.celery.safely_enqueue_task"),
        ):
            response = PinOverviewView.as_view()(req, pin_slug=self.pin.slug)
        self.assertEqual(response.status_code, 200)
        mock_geocode.assert_not_called()


class PinOverviewEditableTitleTests(TestCase):
    """The pin title renders as a click-to-edit-in-place element, wired to pin.quick_edit."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile, name="Old Factory")

    def _get(self):
        req = self.factory.get(f"/map/pin/{self.pin.slug}/overview/")
        req.user = self.user
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
            # pin.location.cached_place_name is falsy for a fresh baker Location (no
            # GooglePlace stub relation), so PinOverviewView.get() tries to enqueue a
            # real Celery task here - mock it so the test never touches a broker.
            patch("urbanlens.dashboard.services.celery.safely_enqueue_task"),
        ):
            return PinOverviewView.as_view()(req, pin_slug=self.pin.slug)

    def test_title_is_marked_editable(self) -> None:
        content = self._get().content.decode()
        self.assertIn("pin-title--editable", content)

    def test_title_carries_the_raw_name_for_the_edit_input(self) -> None:
        content = self._get().content.decode()
        self.assertIn('data-raw-name="Old Factory"', content)

    def test_title_carries_the_location_fallback_name(self) -> None:
        content = self._get().content.decode()
        self.assertIn(f'data-location-name="{self.pin.location.display_name}"', content)

    def test_title_wiring_posts_to_quick_edit(self) -> None:
        content = self._get().content.decode()
        self.assertIn(f"/dashboard/map/quick-edit/{self.pin.slug}/", content)


class PinOverviewEditableDescriptionTests(TestCase):
    """The pin description renders as a click-to-edit-in-place element, wired to pin.edit."""

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile, description="A crumbling old mill.")

    def _get(self, pin=None):
        pin = pin or self.pin
        req = self.factory.get(f"/map/pin/{pin.slug}/overview/")
        req.user = self.user
        with (
            patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None),
            patch("urbanlens.dashboard.services.celery.safely_enqueue_task"),
        ):
            return PinOverviewView.as_view()(req, pin_slug=pin.slug)

    def test_description_is_marked_editable(self) -> None:
        content = self._get().content.decode()
        self.assertIn("pin-description--editable", content)

    def test_description_carries_the_raw_value_for_the_edit_textarea(self) -> None:
        content = self._get().content.decode()
        self.assertIn('data-raw-description="A crumbling old mill."', content)

    def test_description_wiring_posts_to_pin_edit(self) -> None:
        content = self._get().content.decode()
        self.assertIn(f"/map/pin/{self.pin.slug}/edit/", content)

    def test_empty_description_still_renders_with_a_placeholder(self) -> None:
        empty_pin = baker.make(Pin, profile=self.profile, description=None)
        content = self._get(empty_pin).content.decode()
        self.assertIn("pin-description--empty", content)
        self.assertIn("Add a description...", content)
        self.assertIn('data-raw-description=""', content)

    def test_populated_description_does_not_carry_the_empty_modifier(self) -> None:
        content = self._get().content.decode()
        self.assertNotIn("pin-description--empty", content)


class PinEditRatingClearTests(TestCase):
    """Rating lives on Review, not Pin - regression coverage for the "clear
    rating" (x) button, which submits rating=0.

    A prior reassignment (rating=0 -> rating=None) meant the downstream
    `elif rating == 0` delete branch could never actually match, so clicking
    "clear" silently left the underlying Review row (and thus the displayed
    rating) untouched.
    """

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.profile = baker.make(User).profile
        self.user = self.profile.user
        self.pin = baker.make(Pin, profile=self.profile)

    def _post(self, body: dict) -> HttpResponseBase:
        req = self.factory.post(
            f"/map/pin/{self.pin.slug}/edit/",
            data=json.dumps(body),
            content_type="application/json",
        )
        req.user = self.user
        with patch("urbanlens.dashboard.services.apis.locations.google.place_info.GooglePlaceService._resolve_name", return_value=None):
            return PinEditView.as_view()(req, pin_slug=self.pin.slug)

    def test_setting_a_rating_creates_a_review(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        self._post({"rating": 4})

        self.assertEqual(Review.objects.for_pair(self.profile, self.pin).first().rating, 4)

    def test_clearing_an_existing_rating_deletes_the_review(self) -> None:
        from urbanlens.dashboard.models.reviews.model import Review

        Review.objects.update_or_create(profile=self.profile, pin=self.pin, defaults={"rating": 3})

        self._post({"rating": 0})

        self.assertFalse(Review.objects.for_pair(self.profile, self.pin).exists())
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.rating, 0)

    def test_editing_an_unrelated_field_does_not_touch_a_nonexistent_review(self) -> None:
        """rating defaults to 0 (Pin.rating property) when no Review exists -
        an unrelated field edit must not misinterpret that default as an
        explicit clear request and issue a pointless delete every time."""
        from urbanlens.dashboard.models.reviews.queryset import QuerySet as ReviewQuerySet

        with patch.object(ReviewQuerySet, "delete") as mock_delete:
            self._post({"priority": 2})

        mock_delete.assert_not_called()
