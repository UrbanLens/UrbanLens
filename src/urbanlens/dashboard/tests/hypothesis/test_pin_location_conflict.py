"""Tests for the location-conflict dialog's backend: slug/uuid resolution and the merge flow.

Regression coverage for a reported bug bundle: (1) a candidate Location whose
`slug` is null gets a uuid-fallback wiki/link URL that 404s because the
lookup only ever filtered by `slug=`; (2) since Pin has a unique-per-profile
constraint on `location` (root pins only), "switching" the just-created pin
to a Location the profile already has a root pin at can never succeed as a
plain reassignment - it must merge into the existing pin instead.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile

from .test_places_campus import square as _square


def _profile() -> Profile:
    return baker.make(User).profile


class SlugOrUuidQuerySetTests(TestCase):
    """PublicDashboardQuerySet.slug_or_uuid, exercised via Location."""

    def test_finds_row_by_real_slug(self) -> None:
        location = baker.make(Location, latitude="40.0", longitude="-74.0", official_name="Front Street")
        found = Location.objects.slug_or_uuid(location.slug).first()
        self.assertEqual(found, location)

    def test_finds_row_by_uuid_when_slug_is_null(self) -> None:
        location = baker.make(Location, latitude="40.1", longitude="-74.1", official_name="Elm Street")
        Location.objects.filter(pk=location.pk).update(slug=None)
        found = Location.objects.slug_or_uuid(str(location.uuid)).first()
        self.assertEqual(found, location)

    def test_non_uuid_value_does_not_raise_when_no_slug_matches(self) -> None:
        """A plain slug-shaped miss must return no results, not a DB error from the uuid branch."""
        self.assertIsNone(Location.objects.slug_or_uuid("does-not-exist").first())


class PinRelinkViewTests(TestCase):
    """PinRelinkView's merge-vs-relink behaviour.

    Every ``target`` here sits within ~50 m of ``self.origin`` on purpose. Relinking
    is what confers wiki access (``location_visible_to`` grants on an exact Location
    match), so the view only accepts a target the profile can already reach or one
    covering the pin's own coordinate - the 50 m proximity fallback for place-less
    coordinates is what puts these inside the pin's own access domain. These
    coordinates used to be kilometres apart, which quietly asserted that relinking to
    an arbitrary Location was allowed; that is the hole, not the contract.
    """

    def setUp(self) -> None:
        baker.make(User)  # first user is auto-promoted to bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.origin = baker.make(Location, latitude="40.70", longitude="-74.00")
        self.pin = baker.make(Pin, profile=self.profile, location=self.origin)
        self.client.force_login(self.user)

    def _post(self, location_slug: str, *, xhr: bool = False):
        headers = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"} if xhr else {}
        return self.client.post(reverse("pin.link.to", args=[self.pin.slug, location_slug]), **headers)

    def test_relinks_to_a_location_with_no_existing_pin(self) -> None:
        target = baker.make(Location, latitude="40.7001", longitude="-74.00")
        response = self._post(target.slug)
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, target.pk)
        self.assertIsNone(self.pin.parent_pin_id)

    def test_relinks_to_a_target_location_with_null_slug_via_uuid_fallback(self) -> None:
        """Regression: the client sends `loc.slug or str(loc.uuid)` - the server must accept either."""
        target = baker.make(Location, latitude="40.7002", longitude="-74.00")
        Location.objects.filter(pk=target.pk).update(slug=None)
        response = self._post(str(target.uuid))
        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, target.pk)

    def test_merges_into_existing_pin_instead_of_colliding_on_unique_constraint(self) -> None:
        target = baker.make(Location, latitude="40.73", longitude="-74.03")
        existing_pin = baker.make(Pin, profile=self.profile, location=target)

        response = self._post(target.slug)

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.parent_pin_id, existing_pin.pk)
        # The new pin's own location is untouched - it's a child of the existing pin now.
        self.assertEqual(self.pin.location_id, self.origin.pk)

    def test_merge_via_xhr_returns_json_with_existing_pin_url(self) -> None:
        target = baker.make(Location, latitude="40.74", longitude="-74.04")
        existing_pin = baker.make(Pin, profile=self.profile, location=target, name="The Mill")

        response = self._post(target.slug, xhr=True)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["merged"])
        self.assertIn(existing_pin.slug, payload["existing_pin_url"])

    def test_plain_relink_via_xhr_reports_not_merged(self) -> None:
        target = baker.make(Location, latitude="40.7003", longitude="-74.00")
        response = self._post(target.slug, xhr=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["merged"])

    def test_another_profiles_pin_at_the_target_location_does_not_trigger_a_merge(self) -> None:
        """The unique constraint is scoped per-profile - someone else's pin there isn't a conflict."""
        target = baker.make(Location, latitude="40.7004", longitude="-74.00")
        baker.make(Pin, profile=_profile(), location=target)

        response = self._post(target.slug)

        self.assertEqual(response.status_code, 200)
        self.pin.refresh_from_db()
        self.assertEqual(self.pin.location_id, target.pk)
        self.assertIsNone(self.pin.parent_pin_id)


class ConflictingLocationsPayloadTests(TestCase):
    """MapController.post_add_pin's conflicting_locations payload.

    Two *unrelated* parcels whose recorded outlines overlap - the only case
    that still produces a choice now that everything inside one property
    resolves to one place. The smaller wins resolution; the larger is offered
    as the alternative the user may have meant.
    """

    def setUp(self) -> None:
        baker.make(User)  # bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def _overlapping_places(self, latitude: float, longitude: float):
        """A tight parcel and a looser one covering the same coordinate."""
        from urbanlens.dashboard.models.place.model import PlaceKind

        from .place_helpers import make_place

        tight = make_place(PlaceKind.PARCEL, _square(longitude, latitude, 0.002))
        loose = make_place(PlaceKind.PARCEL, _square(longitude, latitude, 0.01))
        return tight, loose

    def test_candidate_with_existing_pin_gets_existing_pin_url(self) -> None:
        _tight, loose = self._overlapping_places(41.0, -73.0)
        other = baker.make(Location, latitude="41.008", longitude="-73.008")
        self.assertEqual(other.place, loose)
        existing_pin = baker.make(Pin, profile=self.profile, location=other, name="Old Mill")

        response = self.client.post(
            reverse("pin.add"),
            {"name": "New Pin", "latitude": "41.00", "longitude": "-73.00"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        entries = {entry["uuid"]: entry for entry in payload["conflicting_locations"]}
        self.assertIn(existing_pin.slug, entries[str(other.uuid)]["existing_pin_url"])
        current_uuid = next(uuid for uuid, entry in entries.items() if entry["is_current"])
        self.assertNotIn("existing_pin_url", entries[current_uuid])

    def test_candidate_with_no_existing_pin_omits_the_field(self) -> None:
        _tight, loose = self._overlapping_places(41.1, -73.1)
        other = baker.make(Location, latitude="41.108", longitude="-73.108")
        self.assertEqual(other.place, loose)

        response = self.client.post(
            reverse("pin.add"),
            {"name": "New Pin", "latitude": "41.10", "longitude": "-73.10"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        entries = {entry["uuid"]: entry for entry in payload["conflicting_locations"]}
        self.assertNotIn("existing_pin_url", entries[str(other.uuid)])


class BlankNamePinCreationTests(TestCase):
    """A pin added without a typed name must stay blank (not get a placeholder
    string like 'Unnamed Location' written to it) so `Pin.effective_name`'s
    fallback to the location's own display name keeps working, and so the
    pin isn't incorrectly locked out of future name upgrades via
    `name_is_user_provided`."""

    def setUp(self) -> None:
        baker.make(User)  # bootstrap site admin
        self.user = baker.make(User)
        self.profile = Profile.objects.get(user=self.user)
        self.client.force_login(self.user)

    def test_blank_name_leaves_pin_name_empty_and_not_user_provided(self) -> None:
        response = self.client.post(
            reverse("pin.add"),
            {"name": "", "latitude": "42.00", "longitude": "-73.50"},
        )

        self.assertEqual(response.status_code, 200)
        pin = Pin.objects.get(location__latitude="42.00", location__longitude="-73.50")
        self.assertEqual(pin.name, "")
        self.assertFalse(pin.name_is_user_provided)

    def test_blank_name_falls_back_to_location_display_name(self) -> None:
        response = self.client.post(
            reverse("pin.add"),
            {"name": "", "latitude": "42.10", "longitude": "-73.60"},
        )

        self.assertEqual(response.status_code, 200)
        pin = Pin.objects.get(location__latitude="42.10", location__longitude="-73.60")
        pin.location.official_name = "Old Grain Mill"
        pin.location.save(update_fields=["official_name"])
        self.assertEqual(pin.effective_name, "Old Grain Mill")

    def test_typed_name_is_saved_and_marked_user_provided(self) -> None:
        response = self.client.post(
            reverse("pin.add"),
            {"name": "My Spot", "latitude": "42.20", "longitude": "-73.70"},
        )

        self.assertEqual(response.status_code, 200)
        pin = Pin.objects.get(location__latitude="42.20", location__longitude="-73.70")
        self.assertEqual(pin.name, "My Spot")
        self.assertTrue(pin.name_is_user_provided)
