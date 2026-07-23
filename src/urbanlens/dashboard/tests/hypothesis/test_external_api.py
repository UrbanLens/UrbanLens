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

from datetime import timedelta
from unittest import mock
from uuid import uuid4

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope, ApiKeyUsageLog
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin_tombstone import PinTombstone
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.api_keys import generate_api_key
from urbanlens.dashboard.services.pin_creation import create_pin_for_profile


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
        self.url = reverse("external_api:pins")
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

    def test_zero_latitude_and_longitude_are_valid_coordinates(self) -> None:
        """0/0.0 (equator, prime meridian) must not be treated as a missing coordinate."""
        response = self._post({"name": "Null Island", "latitude": 0, "longitude": 0})
        self.assertEqual(response.status_code, 201, response.content)
        pin = Pin.objects.get(uuid=response.json()["uuid"])
        self.assertAlmostEqual(float(pin.location.latitude), 0.0, places=3)
        self.assertAlmostEqual(float(pin.location.longitude), 0.0, places=3)

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
            reverse("external_api:pins"),
            data={"name": "Old Mill", "latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        entry = ApiKeyUsageLog.objects.for_api_key(self.api_key).get()
        self.assertEqual(entry.endpoint, reverse("external_api:pins"))

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
            reverse("external_api:pins"),
            data={"name": "Old Mill", "latitude": 42.5, "longitude": -73.5},
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(ApiKeyUsageLog.objects.for_api_key(self.api_key).exists())


class PinSyncViewTests(TestCase):
    """GET pins/ delta-syncs the key owner's pins: window, cursor, watermark, scope."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:pins")
        _api_key, self.raw_key = generate_api_key(self.user, "Sync client")

    def _get(self, **params):
        return self.client.get(self.url, data=params, **_bearer(self.raw_key))

    def _make_pin(self, name: str, latitude: float, longitude: float) -> Pin:
        return create_pin_for_profile(self.profile, name=name, latitude=latitude, longitude=longitude).pin

    def test_requires_pins_read_scope(self) -> None:
        """GET and POST on the same path demand different scopes - a write-only key cannot read."""
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_WRITE.value])
        response = self._get()
        self.assertEqual(response.status_code, 403)

    def test_write_scope_alone_cannot_be_used_to_read_via_post_scope(self) -> None:
        """The inverse asymmetry: a read-only key cannot create."""
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self.client.post(self.url, data={"latitude": 1, "longitude": 1}, content_type="application/json", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 403)

    def test_empty_sync_returns_no_pins_and_a_watermark(self) -> None:
        response = self._get()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["pins"], [])
        self.assertIsNone(body["next_cursor"])
        self.assertTrue(body["sync_watermark"])

    def test_full_sync_includes_sync_fields(self) -> None:
        pin = self._make_pin("Old Mill", 42.5, -73.5)
        body = self._get().json()
        self.assertEqual(len(body["pins"]), 1)
        payload = body["pins"][0]
        self.assertEqual(payload["uuid"], str(pin.uuid))
        self.assertIsNone(payload["parent_uuid"])
        self.assertEqual(payload["pin_type"], pin.pin_type)
        self.assertTrue(payload["updated"])
        self.assertTrue(payload["created"])
        self.assertAlmostEqual(float(payload["latitude"]), 42.5, places=3)

    def test_other_users_pins_are_never_served(self) -> None:
        other = baker.make(User)
        create_pin_for_profile(Profile.objects.get(user=other), name="Not yours", latitude=1.0, longitude=1.0)
        body = self._get().json()
        self.assertEqual(body["pins"], [])

    def test_cursor_pages_through_every_pin_without_duplicates(self) -> None:
        expected = {str(self._make_pin(f"Pin {i}", 40.0 + i, -73.0).uuid) for i in range(3)}
        seen: list[str] = []
        cursor = None
        for _ in range(5):
            params = {"limit": 1}
            if cursor:
                params["cursor"] = cursor
            body = self._get(**params).json()
            seen.extend(p["uuid"] for p in body["pins"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), expected)

    def test_modified_since_excludes_unchanged_pins(self) -> None:
        old_pin = self._make_pin("Untouched", 42.5, -73.5)
        Pin.objects.filter(pk=old_pin.pk).update(updated=timezone.now() - timedelta(days=2))
        fresh_pin = self._make_pin("Fresh", 43.5, -74.5)
        body = self._get(modified_since=(timezone.now() - timedelta(days=1)).isoformat()).json()
        uuids = [p["uuid"] for p in body["pins"]]
        self.assertEqual(uuids, [str(fresh_pin.uuid)])

    def test_include_total_counts_the_whole_window(self) -> None:
        for i in range(2):
            self._make_pin(f"Pin {i}", 40.0 + i, -73.0)
        body = self._get(limit=1, include_total="1").json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["pins"]), 1)

    def test_garbage_cursor_is_a_400_not_a_500(self) -> None:
        response = self._get(cursor="not-a-cursor")
        self.assertEqual(response.status_code, 400)

    def test_child_pins_are_served_with_their_parent_uuid(self) -> None:
        parent = self._make_pin("Campus", 42.5, -73.5)
        child = create_pin_for_profile(self.profile, name="Entrance", latitude=42.5001, longitude=-73.5001).pin
        Pin.objects.filter(pk=child.pk).update(parent_pin=parent)
        by_uuid = {p["uuid"]: p for p in self._get().json()["pins"]}
        self.assertEqual(by_uuid[str(child.uuid)]["parent_uuid"], str(parent.uuid))
        self.assertIsNone(by_uuid[str(parent.uuid)]["parent_uuid"])


class PinTombstoneTests(TestCase):
    """Deleting a pin durably records a tombstone; the deleted feed serves it."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:pins.deleted")
        _api_key, self.raw_key = generate_api_key(self.user, "Sync client")

    def _get(self, **params):
        return self.client.get(self.url, data=params, **_bearer(self.raw_key))

    def _make_pin(self, name: str, latitude: float, longitude: float) -> Pin:
        return create_pin_for_profile(self.profile, name=name, latitude=latitude, longitude=longitude).pin

    def test_single_delete_writes_a_tombstone(self) -> None:
        pin = self._make_pin("Doomed", 42.5, -73.5)
        pin_uuid = pin.uuid
        pin.delete()
        tombstone = PinTombstone.objects.get(pin_uuid=pin_uuid)
        self.assertEqual(tombstone.profile_id, self.profile.pk)

    def test_queryset_delete_writes_tombstones_for_every_pin(self) -> None:
        uuids = {self._make_pin(f"Pin {i}", 40.0 + i, -73.0).uuid for i in range(2)}
        Pin.objects.filter(profile=self.profile).delete()
        self.assertEqual(set(PinTombstone.objects.values_list("pin_uuid", flat=True)), uuids)

    def test_deleting_a_parent_tombstones_its_cascaded_children_too(self) -> None:
        parent = self._make_pin("Campus", 42.5, -73.5)
        child = create_pin_for_profile(self.profile, name="Entrance", latitude=42.6, longitude=-73.6).pin
        Pin.objects.filter(pk=child.pk).update(parent_pin=parent)
        expected = {parent.uuid, child.uuid}
        parent.delete()
        self.assertEqual(set(PinTombstone.objects.values_list("pin_uuid", flat=True)), expected)

    def test_account_deletion_writes_no_tombstones_and_does_not_crash(self) -> None:
        """A profile/user cascade must not insert rows FK'ing the mid-delete profile."""
        self._make_pin("Goes with the account", 42.5, -73.5)
        self.user.delete()
        self.assertFalse(PinTombstone.objects.exists())

    def test_deleted_feed_serves_the_tombstone(self) -> None:
        pin = self._make_pin("Doomed", 42.5, -73.5)
        pin_uuid = str(pin.uuid)
        pin.delete()
        body = self._get().json()
        self.assertEqual([t["pin_uuid"] for t in body["tombstones"]], [pin_uuid])
        self.assertTrue(body["tombstones"][0]["deleted_at"])

    def test_deleted_since_excludes_older_tombstones(self) -> None:
        old_pin = self._make_pin("Long gone", 42.5, -73.5)
        old_uuid = old_pin.uuid
        old_pin.delete()
        PinTombstone.objects.filter(pin_uuid=old_uuid).update(created=timezone.now() - timedelta(days=2))
        fresh_pin = self._make_pin("Just deleted", 43.5, -74.5)
        fresh_uuid = str(fresh_pin.uuid)
        fresh_pin.delete()
        body = self._get(deleted_since=(timezone.now() - timedelta(days=1)).isoformat()).json()
        self.assertEqual([t["pin_uuid"] for t in body["tombstones"]], [fresh_uuid])

    def test_requires_pins_read_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_WRITE.value])
        response = self._get()
        self.assertEqual(response.status_code, 403)

    def test_other_users_deletions_are_never_served(self) -> None:
        other = baker.make(User)
        other_pin = create_pin_for_profile(Profile.objects.get(user=other), name="Not yours", latitude=1.0, longitude=1.0).pin
        other_pin.delete()
        body = self._get().json()
        self.assertEqual(body["tombstones"], [])


class PinCreateIdempotencyTests(TestCase):
    """A caller-generated uuid makes POST pins/ safe to retry from an offline outbox."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:pins")
        _api_key, self.raw_key = generate_api_key(self.user, "Outbox client")

    def _post(self, payload: dict):
        return self.client.post(self.url, data=payload, content_type="application/json", **_bearer(self.raw_key))

    def test_client_uuid_is_stamped_onto_the_created_pin(self) -> None:
        client_uuid = str(uuid4())
        response = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5, "uuid": client_uuid})
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["uuid"], client_uuid)
        self.assertTrue(response.json()["created"])
        self.assertTrue(Pin.objects.filter(uuid=client_uuid, profile=self.profile).exists())

    def test_replaying_the_same_uuid_returns_the_existing_pin_without_a_duplicate(self) -> None:
        client_uuid = str(uuid4())
        payload = {"name": "Old Mill", "latitude": 42.5, "longitude": -73.5, "uuid": client_uuid}
        first = self._post(payload)
        self.assertEqual(first.status_code, 201, first.content)
        replay = self._post(payload)
        self.assertEqual(replay.status_code, 200, replay.content)
        self.assertEqual(replay.json()["uuid"], client_uuid)
        self.assertFalse(replay.json()["created"])
        self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 1)

    def test_a_uuid_owned_by_another_profile_is_rejected_without_leaking_it(self) -> None:
        other = baker.make(User)
        other_pin = create_pin_for_profile(Profile.objects.get(user=other), name="Theirs", latitude=1.0, longitude=1.0).pin
        response = self._post({"name": "Mine", "latitude": 42.5, "longitude": -73.5, "uuid": str(other_pin.uuid)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Pin.objects.filter(profile=self.profile).count(), 0)

    def test_duplicate_location_without_a_uuid_is_a_clean_400(self) -> None:
        """The one-root-pin-per-location constraint surfaces as a friendly error, not a 500."""
        first = self._post({"name": "Old Mill", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(first.status_code, 201, first.content)
        duplicate = self._post({"name": "Old Mill again", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("already have a pin", duplicate.json()["error"])


class PinCreateFieldTests(TestCase):
    """The field-capture payload: description and pin_type on POST pins/."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.url = reverse("external_api:pins")
        _api_key, self.raw_key = generate_api_key(self.user, "Capture client")

    def _post(self, payload: dict):
        return self.client.post(self.url, data=payload, content_type="application/json", **_bearer(self.raw_key))

    def test_description_and_pin_type_are_stored(self) -> None:
        response = self._post({"name": "Boiler house", "latitude": 42.5, "longitude": -73.5, "description": "Rusted catwalks, watch the floor.", "pin_type": "building"})
        self.assertEqual(response.status_code, 201, response.content)
        pin = Pin.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(pin.description, "Rusted catwalks, watch the floor.")
        self.assertEqual(pin.pin_type, "building")
        self.assertTrue(pin.pin_type_is_user_provided)

    def test_omitted_pin_type_keeps_the_classifiable_default(self) -> None:
        """No explicit type must leave the pin eligible for automatic classification."""
        response = self._post({"name": "Somewhere", "latitude": 42.5, "longitude": -73.5})
        self.assertEqual(response.status_code, 201, response.content)
        pin = Pin.objects.get(uuid=response.json()["uuid"])
        self.assertEqual(pin.pin_type, "location")
        self.assertFalse(pin.pin_type_is_user_provided)

    def test_unknown_pin_type_is_rejected(self) -> None:
        response = self._post({"name": "Bad type", "latitude": 42.5, "longitude": -73.5, "pin_type": "spaceship"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Pin.objects.filter(name="Bad type").exists())
