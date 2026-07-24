"""Tests for the external API's pin-suggestion endpoint: POST pin-suggestions/.

Unlike POST pins/ (test_external_api.py's PinCreateFieldTests etc.), nothing
here creates a real Pin - the submission is staged as a pending PinSuggestion
the key's owner must explicitly accept before anything appears on their map.
Covers: it never creates a Pin outright, the same scope/validation rules
PinsView.post already enforces (missing coords/address, geocoding gate,
unknown pin_type), the new fields (description/pin_type/aliases/links/photos)
land on the suggestion, matching an existing pin is reported, and the
same visit-logging-off gate ingest_location_hits already enforces.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.images.model import Image, ImageSource
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_suggestions.model import MAX_SUGGESTION_ALIASES, MAX_SUGGESTION_LINKS, MAX_SUGGESTION_PHOTOS, PinSuggestion, PinSuggestionOrigin, PinSuggestionStatus
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.api_keys import generate_api_key


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _ok_photo_response(content: bytes = b"fake-jpeg-bytes") -> mock.Mock:
    response = mock.Mock()
    response.raise_for_status = mock.Mock()
    response.raw.read.return_value = content
    response.is_redirect = False
    return response


class PinSuggestionsViewTests(TestCase):
    """POST pin-suggestions/ stages a pending PinSuggestion, never a real Pin."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:pin_suggestions")
        _api_key, self.raw_key = generate_api_key(self.user, "Discovery client")

    def _post(self, payload: dict):
        return self.client.post(self.url, data=payload, content_type="application/json", **_bearer(self.raw_key))

    def test_valid_submission_creates_a_pending_suggestion_not_a_pin(self) -> None:
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 201, response.content)
        suggestion = PinSuggestion.objects.get(pk=response.json()["suggestion_id"])
        self.assertEqual(suggestion.profile_id, self.profile.pk)
        self.assertEqual(suggestion.status, PinSuggestionStatus.PENDING)
        self.assertEqual(suggestion.origin, PinSuggestionOrigin.EXTERNAL_API)
        self.assertFalse(Pin.objects.filter(profile=self.profile).exists())
        self.assertFalse(response.json()["matched_existing_pin"])

    def test_description_pin_type_aliases_and_links_land_on_the_suggestion(self) -> None:
        response = self._post(
            {
                "name": "Old Mill",
                "latitude": 42.5,
                "longitude": -73.5,
                "description": "Rusted catwalks, watch the floor.",
                "pin_type": "building",
                "aliases": ["The Sawmill", "Old Mill Ruins"],
                "links": [{"name": "Historical society", "url": "https://example.test/mill"}],
            }
        )
        self.assertEqual(response.status_code, 201, response.content)
        suggestion = PinSuggestion.objects.get(pk=response.json()["suggestion_id"])
        self.assertEqual(suggestion.suggested_name, "Old Mill")
        self.assertEqual(suggestion.suggested_description, "Rusted catwalks, watch the floor.")
        self.assertEqual(suggestion.suggested_pin_type, "building")
        self.assertEqual(suggestion.suggested_aliases, ["The Sawmill", "Old Mill Ruins"])
        self.assertEqual(suggestion.suggested_links, [{"name": "Historical society", "url": "https://example.test/mill"}])

    def test_accepting_never_fabricates_a_visit(self) -> None:
        """A discovery submission isn't evidence anyone visited - visit_dates
        must come out empty regardless of when the API call happened."""
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        suggestion = PinSuggestion.objects.get(pk=response.json()["suggestion_id"])
        self.assertEqual(suggestion.visit_dates, [])

    def test_photos_are_downloaded_and_staged_as_candidate_images(self) -> None:
        with mock.patch("urbanlens.dashboard.services.pin_suggestions.requests.get", return_value=_ok_photo_response()), mock.patch(
            "socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]
        ):
            response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5, "photos": ["https://example.test/photo.jpg"]})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["photos_attached"], 1)
        suggestion = PinSuggestion.objects.get(pk=response.json()["suggestion_id"])
        image = Image.objects.get(pin_suggestion=suggestion)
        self.assertEqual(image.source, ImageSource.EXTERNAL_API)

    def test_submission_matching_an_existing_pin_is_reported(self) -> None:
        location = baker.make_recipe("dashboard.location", latitude=42.5, longitude=-73.5)
        baker.make_recipe("dashboard.pin", profile=self.profile, location=location)
        response = self._post({"latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(response.json()["matched_existing_pin"])

    def test_missing_coordinates_and_address_is_rejected(self) -> None:
        response = self._post({"name": "Nowhere"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_unknown_pin_type_is_rejected(self) -> None:
        response = self._post({"latitude": 42.5, "longitude": -73.5, "pin_type": "spaceship"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_too_many_aliases_is_rejected(self) -> None:
        response = self._post({"latitude": 42.5, "longitude": -73.5, "aliases": [f"Alias {i}" for i in range(MAX_SUGGESTION_ALIASES + 1)]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_too_many_links_is_rejected(self) -> None:
        links = [{"name": f"L{i}", "url": f"https://example.test/{i}"} for i in range(MAX_SUGGESTION_LINKS + 1)]
        response = self._post({"latitude": 42.5, "longitude": -73.5, "links": links})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_too_many_photos_is_rejected(self) -> None:
        photos = [f"https://example.test/{i}.jpg" for i in range(MAX_SUGGESTION_PHOTOS + 1)]
        response = self._post({"latitude": 42.5, "longitude": -73.5, "photos": photos})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_non_http_link_scheme_is_rejected(self) -> None:
        response = self._post({"latitude": 42.5, "longitude": -73.5, "links": [{"name": "Bad", "url": "javascript:alert(1)"}]})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_address_only_geocodes_through_the_shared_helper(self) -> None:
        with mock.patch("urbanlens.dashboard.external_api.views.get_pin_by_address", return_value=(42.6, -73.6)):
            response = self._post({"name": "Geocoded Spot", "address": "1 Main St"})
        self.assertEqual(response.status_code, 201, response.content)
        suggestion = PinSuggestion.objects.get(pk=response.json()["suggestion_id"])
        self.assertAlmostEqual(float(suggestion.latitude), 42.6, places=3)

    def test_geocoding_disabled_for_profile_rejects_address_only_submission(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(external_apis_enabled=False)
        response = self._post({"name": "Geocoded Spot", "address": "1 Main St"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_visit_logging_disabled_is_403(self) -> None:
        Profile.objects.filter(pk=self.profile.pk).update(track_pin_visits=False)
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_key_missing_the_required_scope_is_forbidden(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PROFILE_READ.value])
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(PinSuggestion.objects.exists())

    def test_read_only_scope_cannot_submit_suggestions(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 403)

    def test_a_logged_in_session_alone_does_not_authenticate(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(self.url, data={"latitude": 42.5, "longitude": -73.5}, content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_cannot_submit_a_suggestion_for_another_user_via_session_alone(self) -> None:
        other = baker.make(User)
        self.client.force_login(other)
        response = self.client.post(self.url, data={"latitude": 42.5, "longitude": -73.5}, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertFalse(PinSuggestion.objects.filter(profile=Profile.objects.get(user=other)).exists())
