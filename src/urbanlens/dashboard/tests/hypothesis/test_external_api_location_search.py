"""Tests for the external API's ``locations/search/`` and ``locations/resolve/``.

The external places provider is always mocked here: these tests assert the
*gating* around it - that a profile which turned external lookups off never
reaches the provider at all, and that a client is told why (``places_disabled``)
rather than being handed a silently shorter result list.
"""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.map_pins.autocomplete import AutocompleteResult
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile

_SEARCH_URL = "/dashboard/api/external/v1/locations/search/"
_RESOLVE_URL = "/dashboard/api/external/v1/locations/resolve/"

#: Patch targets are the names bound in ``external_api.views``, not the ones in
#: the autocomplete service - the view imported them at module load.
_SEARCH_PLACES = "urbanlens.dashboard.external_api.views.search_google_places"
_RESOLVE_PLACE = "urbanlens.dashboard.external_api.views.resolve_google_place"


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _place_result() -> AutocompleteResult:
    return AutocompleteResult(
        type="place",
        title="Old Mill",
        subtitle="Troy, NY",
        lat=None,
        lng=None,
        zoom=15,
        icon="place",
        place_id="place-123",
    )


class LocationSearchTestCase(TestCase):
    """Shared fixture: a user with a places-capable key and one local pin."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Search client")
        create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5)

    def _search(self, **params):
        return self.client.get(_SEARCH_URL, data=params, **_bearer(self.raw_key))


class LocationSearchTests(LocationSearchTestCase):
    """The merged local + places autocomplete."""

    def test_requires_pins_read_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PROFILE_READ.value])
        self.assertEqual(self._search(q="Mill").status_code, 403)

    def test_no_credentials_is_rejected(self) -> None:
        self.assertEqual(self.client.get(_SEARCH_URL, data={"q": "Mill"}).status_code, 401)

    def test_short_query_returns_an_empty_result_without_calling_the_provider(self) -> None:
        with patch(_SEARCH_PLACES) as search_places:
            body = self._search(q="M").json()
        self.assertEqual(body["results"], [])
        self.assertFalse(body["places_disabled"])
        search_places.assert_not_called()

    def test_local_source_finds_the_users_own_pin(self) -> None:
        body = self._search(q="Mill", sources="local").json()
        titles = [row["title"] for row in body["results"]]
        self.assertIn("Old Mill", titles)

    def test_local_only_never_calls_the_places_provider(self) -> None:
        with patch(_SEARCH_PLACES) as search_places:
            self._search(q="Mill", sources="local")
        search_places.assert_not_called()

    def test_external_apis_disabled_flags_places_and_skips_the_provider(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(external_apis_enabled=False)
        with patch(_SEARCH_PLACES) as search_places:
            body = self._search(q="Mill", sources="local,places").json()

        self.assertTrue(body["places_disabled"])
        search_places.assert_not_called()
        # The local half still works - the toggle scopes external lookups only.
        self.assertIn("Old Mill", [row["title"] for row in body["results"]])

    def test_results_use_the_shared_autocomplete_wire_shape(self) -> None:
        body = self._search(q="Mill", sources="local").json()
        self.assertEqual(set(body["results"][0]), set(_place_result().to_dict()))

    def test_limit_caps_the_result_count(self) -> None:
        # Offset well clear of the setUp pin's coordinates - a pin resolving to
        # an already-pinned Location is refused by create_pin_for_profile.
        for index in range(4):
            create_pin_for_profile(
                self.profile, name=f"Mill House {index}", latitude=40.0 + index, longitude=-70.0 - index
            )
        body = self._search(q="Mill", sources="local", limit=2).json()
        self.assertEqual(len(body["results"]), 2)

    def test_missing_query_is_a_400(self) -> None:
        self.assertEqual(self._search().status_code, 400)


class PlaceResolveTests(LocationSearchTestCase):
    """Resolving a selected suggestion to coordinates."""

    def _resolve(self, **params):
        return self.client.get(_RESOLVE_URL, data=params, **_bearer(self.raw_key))

    def test_missing_place_id_is_a_400(self) -> None:
        self.assertEqual(self._resolve().status_code, 400)

    def test_external_apis_disabled_is_forbidden_and_skips_the_provider(self) -> None:
        # The internal MapController.resolve_place omits this gate; this
        # surface must not reproduce that (see docs/PROBLEMS.md).
        Profile.objects.filter(pk=self.profile.pk).update(external_apis_enabled=False)
        with patch(_RESOLVE_PLACE) as resolve_place:
            response = self._resolve(place_id="place-123")

        self.assertEqual(response.status_code, 403)
        resolve_place.assert_not_called()

    def test_resolved_place_returns_coordinates_and_name(self) -> None:
        with (
            patch("urbanlens.dashboard.external_api.views.settings.google_unrestricted_api_key", "test-key"),
            patch(_RESOLVE_PLACE, return_value=(42.5, -73.5, "Old Mill")) as resolve_place,
        ):
            body = self._resolve(place_id="place-123").json()

        self.assertEqual(body, {"lat": 42.5, "lng": -73.5, "name": "Old Mill"})
        resolve_place.assert_called_once()

    def test_unresolvable_place_is_a_404(self) -> None:
        with (
            patch("urbanlens.dashboard.external_api.views.settings.google_unrestricted_api_key", "test-key"),
            patch(_RESOLVE_PLACE, return_value=(None, None, None)),
        ):
            self.assertEqual(self._resolve(place_id="nope").status_code, 404)
