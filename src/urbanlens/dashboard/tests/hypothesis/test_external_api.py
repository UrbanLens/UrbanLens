"""Tests for the external API: auth, scope enforcement, and the pin-create endpoint.

This surface is reachable by anyone holding a user's API key - unlike the
internal /rest/ API (test_rest_api_security.py), it is *designed* to be used
by something other than the site's own frontend, so these tests focus on:
a session alone must never work here, a revoked/malformed key must never
work, each endpoint must only honor the scope it declares, and pin creation
must validate its input before anything reaches the shared
services.pin_creation.create_pin_for_profile call.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope, ApiKeyUsageLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.api_keys import generate_api_key


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


class WhoAmIAuthTests(TestCase):
    """WhoAmIView requires a valid, active, profile:read-scoped API key."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:whoami")

    def test_no_credentials_is_rejected(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_a_logged_in_session_alone_does_not_authenticate(self) -> None:
        """The external API is API-key-only - session auth must never work here."""
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 401)

    def test_malformed_bearer_token_is_rejected(self) -> None:
        response = self.client.get(self.url, **_bearer("garbage"))
        self.assertEqual(response.status_code, 401)

    def test_revoked_key_is_rejected(self) -> None:
        api_key, raw_key = generate_api_key(self.user, "Zapier")
        ApiKey.objects.filter(pk=api_key.pk).update(revoked_at=api_key.created)
        response = self.client.get(self.url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 401)

    def test_valid_key_returns_only_the_profile_uuid(self) -> None:
        _api_key, raw_key = generate_api_key(self.user, "Zapier")
        response = self.client.get(self.url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"uuid": str(self.profile.uuid)})

    def test_key_missing_the_required_scope_is_forbidden(self) -> None:
        api_key, raw_key = generate_api_key(self.user, "Zapier")
        ApiKey.objects.filter(pk=api_key.pk).update(scopes=[ApiKeyScope.PINS_WRITE.value])
        response = self.client.get(self.url, **_bearer(raw_key))
        self.assertEqual(response.status_code, 403)


class PinCreateViewTests(TestCase):
    """PinCreateView creates pins via the shared service and validates untrusted input."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:pins.create")
        _api_key, self.raw_key = generate_api_key(self.user, "Zapier")

    def _post(self, payload: dict):
        return self.client.post(self.url, data=payload, content_type="application/json", **_bearer(self.raw_key))

    def test_creates_a_pin_owned_by_the_key_holder(self) -> None:
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 201, response.content)
        pin = Pin.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(pin.profile_id, self.profile.pk)
        self.assertEqual(pin.name, "Old Mill")
        self.assertTrue(pin.name_is_user_provided)

    def test_missing_coordinates_and_address_is_rejected(self) -> None:
        response = self._post({"name": "Nowhere"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Pin.objects.filter(name="Nowhere").exists())

    def test_out_of_range_latitude_is_rejected(self) -> None:
        response = self._post({"latitude": 200, "longitude": -73.5})
        self.assertEqual(response.status_code, 400)

    def test_address_only_geocodes_through_the_shared_service(self) -> None:
        with mock.patch("urbanlens.dashboard.services.pin_creation.get_pin_by_address", return_value=(42.6, -73.6)):
            response = self._post({"name": "Geocoded Spot", "address": "1 Main St"})
        self.assertEqual(response.status_code, 201, response.content)
        pin = Pin.objects.get(uuid=response.json()["uuid"])
        self.assertAlmostEqual(float(pin.location.latitude), 42.6, places=3)

    def test_geocoding_disabled_for_profile_rejects_address_only_submission(self) -> None:
        """A profile-setting block is 403 (typed PinCreationForbiddenError), not a 400 input error."""
        Profile.objects.filter(pk=self.profile.pk).update(external_apis_enabled=False)
        response = self._post({"name": "Geocoded Spot", "address": "1 Main St"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Pin.objects.filter(name="Geocoded Spot").exists())

    def test_key_missing_the_required_scope_is_forbidden(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PROFILE_READ.value])
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Pin.objects.filter(name="Old Mill").exists())

    def test_revoked_key_cannot_create_pins(self) -> None:
        ApiKey.objects.filter(user=self.user).update(revoked_at=self.profile.created)
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 401)

    def test_cannot_create_a_pin_for_another_user_via_session_alone(self) -> None:
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            data={"name": "Old Mill", "latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_label_and_google_place_ids_are_not_accepted_fields(self) -> None:
        """The external surface is deliberately conservative - internal-only ids are silently ignored, not honored."""
        response = self._post(
            {
                "name": "Old Mill",
                "latitude": 42.5,
                "longitude": -73.5,
                "label_ids": ["1", "2"],
                "google_place_id": "abc123",
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        pin = Pin.objects.get(uuid=response.json()["uuid"])
        self.assertFalse(pin.labels.exists())


class ApiKeyUsageLoggingTests(TestCase):
    """A successfully authenticated request logs activity; a rejected one never does."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.api_key, self.raw_key = generate_api_key(self.user, "Zapier")

    def test_successful_whoami_call_logs_the_endpoint(self) -> None:
        self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        entry = ApiKeyUsageLog.objects.for_api_key(self.api_key).get()
        self.assertEqual(entry.endpoint, reverse("external_api:whoami"))

    def test_successful_pin_create_call_logs_the_endpoint(self) -> None:
        self.client.post(
            reverse("external_api:pins.create"),
            data={"name": "Old Mill", "latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        entry = ApiKeyUsageLog.objects.for_api_key(self.api_key).get()
        self.assertEqual(entry.endpoint, reverse("external_api:pins.create"))

    def test_invalid_key_is_never_logged(self) -> None:
        self.client.get(reverse("external_api:whoami"), **_bearer("ulk_not_a_real_key"))
        self.assertFalse(ApiKeyUsageLog.objects.exists())

    def test_revoked_key_is_never_logged(self) -> None:
        ApiKey.objects.filter(pk=self.api_key.pk).update(revoked_at=self.api_key.created)
        self.client.get(reverse("external_api:whoami"), **_bearer(self.raw_key))
        self.assertFalse(ApiKeyUsageLog.objects.exists())

    def test_request_rejected_for_missing_scope_is_still_logged(self) -> None:
        """Scope enforcement happens after authentication - the attempt itself is real activity."""
        ApiKey.objects.filter(pk=self.api_key.pk).update(scopes=[ApiKeyScope.PROFILE_READ.value])
        response = self.client.post(
            reverse("external_api:pins.create"),
            data={"name": "Old Mill", "latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ApiKeyUsageLog.objects.for_api_key(self.api_key).exists())
