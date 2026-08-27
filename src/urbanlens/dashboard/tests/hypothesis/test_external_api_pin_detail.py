"""Tests for GET/PATCH/DELETE ``/pins/{slug}/`` - the external API's pin-detail surface.

Covers the full detail payload's extra fields (dates, security, notes,
aliases, links, custom fields, boundary, cover photo, wiki slug), the PATCH
fields mirroring internal ``PinViewSet`` semantics (name/icon/last_visited/
coordinate move) plus the new ``parent_id`` detach/reparent capability, and
DELETE's child-pin decision handshake - the same behavior
``test_pin_delete_view.py`` already covers for the internal endpoint, since
both now share ``services.pins.pin_edit``.
"""

from __future__ import annotations

from urllib.parse import urlencode
from uuid import uuid4

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.account.model import ApiKey, ApiKeyScope
from urbanlens.dashboard.models.aliases.model import PinAlias
from urbanlens.dashboard.models.links.model import PinLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.pin.note import PinNote
from urbanlens.dashboard.models.pin_tombstone import PinTombstone
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.services.auth.api_keys import generate_api_key
from urbanlens.dashboard.services.pins.pin_creation import create_pin_for_profile


def _bearer(raw_key: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw_key}"}


def _url(pin: Pin) -> str:
    return f"/dashboard/api/external/v1/pins/{pin.slug or pin.uuid}/"


class PinDetailGetTests(TestCase):
    """GET returns the sync payload plus every detail-only field."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Detail client")
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5, description="Rusty catwalks").pin

    def _get(self, pin: Pin):
        return self.client.get(_url(pin), **_bearer(self.raw_key))

    def test_requires_pins_read_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_WRITE.value])
        response = self._get(self.pin)
        self.assertEqual(response.status_code, 403)

    def test_no_credentials_is_rejected(self) -> None:
        response = self.client.get(_url(self.pin))
        self.assertEqual(response.status_code, 401)

    def test_other_users_pin_is_a_404_not_a_403(self) -> None:
        other = baker.make(User)
        other_pin = create_pin_for_profile(Profile.objects.get(user=other), name="Not yours", latitude=1.0, longitude=1.0).pin
        response = self._get(other_pin)
        self.assertEqual(response.status_code, 404)

    def test_unknown_slug_is_404(self) -> None:
        response = self.client.get(f"/dashboard/api/external/v1/pins/{uuid4()}/", **_bearer(self.raw_key))
        self.assertEqual(response.status_code, 404)

    def test_base_sync_fields_are_present(self) -> None:
        body = self._get(self.pin).json()
        self.assertEqual(body["uuid"], str(self.pin.uuid))
        self.assertEqual(body["description"], "Rusty catwalks")
        self.assertIsNone(body["parent_uuid"])

    def test_dates_default_to_null(self) -> None:
        body = self._get(self.pin).json()
        self.assertIsNone(body["date_built"])
        self.assertIsNone(body["date_abandoned"])
        self.assertIsNone(body["date_last_active"])

    def test_dates_are_serialized_when_set(self) -> None:
        from datetime import date

        Pin.objects.filter(pk=self.pin.pk).update(date_built=date(1950, 1, 1), date_abandoned=date(1999, 6, 15))
        body = self._get(self.pin).json()
        self.assertEqual(body["date_built"], "1950-01-01")
        self.assertEqual(body["date_abandoned"], "1999-06-15")

    def test_security_fields_default_to_unknown(self) -> None:
        body = self._get(self.pin).json()
        self.assertEqual(set(body["security"]), {"fences", "alarms", "cameras", "security", "signs", "vps", "plywood", "locked"})
        self.assertTrue(all(value == "unknown" for value in body["security"].values()))

    def test_security_field_reflects_a_set_value(self) -> None:
        Pin.objects.filter(pk=self.pin.pk).update(fences="everywhere")
        body = self._get(self.pin).json()
        self.assertEqual(body["security"]["fences"], "everywhere")

    def test_notes_are_included_newest_first(self) -> None:
        PinNote.objects.create(pin=self.pin, text="First")
        PinNote.objects.create(pin=self.pin, text="Second")
        body = self._get(self.pin).json()
        self.assertEqual([n["text"] for n in body["notes"]], ["Second", "First"])
        self.assertEqual(body["note_count"], 2)

    def test_aliases_are_included(self) -> None:
        PinAlias.objects.create(pin=self.pin, name="Old Name")
        body = self._get(self.pin).json()
        # Pin.save() auto-ensures an alias for the pin's own current name (see
        # models.pin.model.Pin.save), so "Old Mill" - this pin's name - is
        # already present alongside the explicitly-added "Old Name".
        self.assertEqual([a["name"] for a in body["aliases"]], ["Old Mill", "Old Name"])
        self.assertEqual(body["alias_count"], 2)

    def test_links_are_included(self) -> None:
        PinLink.objects.create(pin=self.pin, name="Article", url="https://example.com/article")
        body = self._get(self.pin).json()
        self.assertEqual(body["links"][0]["url"], "https://example.com/article")
        self.assertEqual(body["link_count"], 1)

    def test_no_drawn_boundary_falls_back_to_a_circle_around_the_pin(self) -> None:
        """Matches the website's own map/pin display: no polygon still yields a circle, never null."""
        body = self._get(self.pin).json()
        self.assertIsNotNone(body["boundary"])
        self.assertIn(body["boundary"]["type"], {"Polygon", "MultiPolygon"})

    def test_no_wiki_is_null_slug(self) -> None:
        body = self._get(self.pin).json()
        self.assertIsNone(body["wiki_slug"])

    def test_no_cover_photo_is_null_url(self) -> None:
        body = self._get(self.pin).json()
        self.assertIsNone(body["cover_photo_url"])

    def test_no_custom_fields_is_an_empty_list(self) -> None:
        body = self._get(self.pin).json()
        self.assertEqual(body["custom_fields"], [])

    def test_address_components_default_to_null(self) -> None:
        body = self._get(self.pin).json()
        self.assertIsNone(body["city"])
        self.assertIsNone(body["state"])
        self.assertIsNone(body["county"])
        self.assertIsNone(body["zipcode"])

    def test_address_components_are_read_from_the_pins_location(self) -> None:
        # city/state/county are Python properties aliasing locality/
        # administrative_area_level_1/administrative_area_level_2 - a bulk
        # .update() has to target the real field names.
        Location.objects.filter(pk=self.pin.location_id).update(
            locality="Troy",
            administrative_area_level_1="NY",
            administrative_area_level_2="Rensselaer",
            country="US",
            zipcode="12180",
        )
        body = self._get(self.pin).json()
        self.assertEqual(body["city"], "Troy")
        self.assertEqual(body["state"], "NY")
        self.assertEqual(body["county"], "Rensselaer")
        self.assertEqual(body["country"], "US")
        self.assertEqual(body["zipcode"], "12180")

    def test_child_pin_reports_its_parent_uuid(self) -> None:
        # A child pin is exempt from db_pin_unique_location_per_profile once
        # parent_pin is set at creation - build it directly rather than via
        # create_pin_for_profile, whose fuzzy Location dedup would otherwise
        # resolve this nearby point onto the parent's own Location and reject
        # the create as a duplicate top-level pin.
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin.location, parent_pin=self.pin, name="Entrance")
        body = self._get(child).json()
        self.assertEqual(body["parent_uuid"], str(self.pin.uuid))


class PinDetailPatchTests(TestCase):
    """PATCH mirrors PinViewSet semantics: name/icon/last_visited/coordinate move."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Edit client")
        self.pin = create_pin_for_profile(self.profile, name="Old Mill", latitude=42.5, longitude=-73.5).pin

    def _patch(self, pin: Pin, payload: dict):
        return self.client.patch(_url(pin), data=payload, content_type="application/json", **_bearer(self.raw_key))

    def test_requires_pins_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self._patch(self.pin, {"name": "New Name"})
        self.assertEqual(response.status_code, 403)

    def test_renames_the_pin(self) -> None:
        response = self._patch(self.pin, {"name": "New Name"})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "New Name")
        self.assertTrue(self.pin.name_is_user_provided)

    def test_updates_icon(self) -> None:
        response = self._patch(self.pin, {"icon": "star"})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.icon, "star")

    def test_updates_last_visited(self) -> None:
        response = self._patch(self.pin, {"last_visited": "2024-05-01T12:00:00Z"})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertIsNotNone(self.pin.last_visited)

    def test_moving_coordinates_relinks_the_location(self) -> None:
        old_location_id = self.pin.location_id
        response = self._patch(self.pin, {"latitude": 10.0, "longitude": 20.0})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertNotEqual(self.pin.location_id, old_location_id)
        self.assertAlmostEqual(float(self.pin.location.latitude), 10.0, places=3)

    def test_latitude_without_longitude_is_rejected(self) -> None:
        response = self._patch(self.pin, {"latitude": 10.0})
        self.assertEqual(response.status_code, 400)

    def test_nan_coordinates_are_rejected(self) -> None:
        response = self.client.patch(
            _url(self.pin),
            data='{"latitude": NaN, "longitude": 20.0}',
            content_type="application/json",
            **_bearer(self.raw_key),
        )
        self.assertEqual(response.status_code, 400)

    def test_other_users_pin_is_a_404(self) -> None:
        other = baker.make(User)
        other_pin = create_pin_for_profile(Profile.objects.get(user=other), name="Not yours", latitude=1.0, longitude=1.0).pin
        response = self._patch(other_pin, {"name": "Hijacked"})
        self.assertEqual(response.status_code, 404)
        other_pin.refresh_from_db()
        self.assertEqual(other_pin.name, "Not yours")

    def test_detaches_a_child_pin_via_null_parent_id(self) -> None:
        parent = create_pin_for_profile(self.profile, name="Campus", latitude=50.0, longitude=50.0).pin
        Pin.objects.filter(pk=self.pin.pk).update(parent_pin=parent)
        response = self._patch(self.pin, {"parent_id": None})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.parent_pin_id)

    def test_detach_conflict_when_a_root_pin_already_occupies_the_location(self) -> None:
        """self.pin shares its parent's own Location - detaching would collide with the parent itself."""
        parent = create_pin_for_profile(self.profile, name="Campus", latitude=50.0, longitude=50.0).pin
        Pin.objects.filter(pk=self.pin.pk).update(parent_pin=parent, location_id=parent.location_id)
        response = self._patch(self.pin, {"parent_id": None})
        self.assertEqual(response.status_code, 400)
        self.assertIn("already have a top-level pin", response.json()["error"])

    def test_reparents_under_another_of_the_callers_own_pins(self) -> None:
        new_parent = create_pin_for_profile(self.profile, name="Campus", latitude=50.0, longitude=50.0).pin
        response = self._patch(self.pin, {"parent_id": str(new_parent.uuid)})
        self.assertEqual(response.status_code, 200, response.content)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.parent_pin_id, new_parent.pk)

    def test_reparenting_under_a_descendant_is_rejected_as_a_cycle(self) -> None:
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin.location, parent_pin=self.pin, name="Entrance")
        response = self._patch(self.pin, {"parent_id": str(child.uuid)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("circular", response.json()["error"])

    def test_reparenting_under_another_profiles_pin_is_rejected_without_leaking_it(self) -> None:
        other = baker.make(User)
        other_pin = create_pin_for_profile(Profile.objects.get(user=other), name="Theirs", latitude=1.0, longitude=1.0).pin
        response = self._patch(self.pin, {"parent_id": str(other_pin.uuid)})
        self.assertEqual(response.status_code, 400)
        self.pin.refresh_from_db()
        self.assertIsNone(self.pin.parent_pin_id)

    def test_a_rejected_reparent_rolls_back_other_fields_in_the_same_patch(self) -> None:
        """A single PATCH is all-or-nothing: an invalid parent_id must not leave a partial rename applied."""
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin.location, parent_pin=self.pin, name="Entrance")
        response = self._patch(self.pin, {"name": "Should Not Stick", "parent_id": str(child.uuid)})
        self.assertEqual(response.status_code, 400)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.name, "Old Mill")

    def test_response_body_is_the_full_updated_detail(self) -> None:
        response = self._patch(self.pin, {"name": "New Name"})
        body = response.json()
        self.assertEqual(body["name"], "New Name")
        self.assertIn("security", body)
        self.assertIn("notes", body)


class PinDetailDeleteTests(TestCase):
    """DELETE mirrors PinViewSet.destroy: child-pin decision handshake, undo, tombstones."""

    def setUp(self) -> None:
        baker.make(User)  # first user auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        _api_key, self.raw_key = generate_api_key(self.user, "Delete client")
        self.pin = create_pin_for_profile(self.profile, name="Doomed", latitude=42.5, longitude=-73.5).pin

    def _delete(self, pin: Pin, **params):
        # PinDetailView.delete() reads ``children`` from request.query_params -
        # the test client's `data=` on delete() serializes to the request body,
        # not the query string, so it must be appended to the URL directly.
        url = f"{_url(pin)}?{urlencode(params)}" if params else _url(pin)
        return self.client.delete(url, **_bearer(self.raw_key))

    def test_requires_pins_write_scope(self) -> None:
        ApiKey.objects.filter(user=self.user).update(scopes=[ApiKeyScope.PINS_READ.value])
        response = self._delete(self.pin)
        self.assertEqual(response.status_code, 403)

    def test_deletes_a_leaf_pin(self) -> None:
        pin_uuid = self.pin.uuid
        response = self._delete(self.pin)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Pin.objects.filter(uuid=pin_uuid).exists())

    def test_delete_writes_a_tombstone(self) -> None:
        pin_uuid = self.pin.uuid
        self._delete(self.pin)
        self.assertTrue(PinTombstone.objects.filter(pin_uuid=pin_uuid).exists())

    def test_other_users_pin_is_a_404(self) -> None:
        other = baker.make(User)
        other_pin = create_pin_for_profile(Profile.objects.get(user=other), name="Not yours", latitude=1.0, longitude=1.0).pin
        response = self._delete(other_pin)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Pin.objects.filter(pk=other_pin.pk).exists())

    def test_pin_with_children_requires_a_decision(self) -> None:
        baker.make("dashboard.Pin", profile=self.profile, location=self.pin.location, parent_pin=self.pin, name="Entrance")
        response = self._delete(self.pin)
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertTrue(body["requires_children_decision"])
        self.assertEqual(body["children"], 1)
        self.assertTrue(Pin.objects.filter(pk=self.pin.pk).exists())

    def test_children_delete_removes_the_whole_subtree(self) -> None:
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin.location, parent_pin=self.pin, name="Entrance")
        response = self._delete(self.pin, children="delete")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Pin.objects.filter(pk__in=[self.pin.pk, child.pk]).exists())

    def test_children_keep_promotes_them_to_top_level(self) -> None:
        child = baker.make("dashboard.Pin", profile=self.profile, location=self.pin.location, parent_pin=self.pin, name="Entrance")
        response = self._delete(self.pin, children="keep")
        self.assertEqual(response.status_code, 204)
        child.refresh_from_db()
        self.assertIsNone(child.parent_pin_id)
