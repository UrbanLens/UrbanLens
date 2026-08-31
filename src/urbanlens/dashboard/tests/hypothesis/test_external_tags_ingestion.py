"""Wire-up tests: the Nominatim/Overture panel+enrichment sources actually sync PlaceExternalTag.

Mocks the gateway HTTP calls (no real network) and asserts on the database
side effect - both the pre-existing LocationCache write (unchanged behavior)
and the new PlaceExternalTag sync.
"""

from __future__ import annotations

from unittest import mock

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.place.external_tag import ExternalTagSource, PlaceExternalTag
from urbanlens.dashboard.models.place.model import Place
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.plugins.builtin.nominatim import NominatimEnrichmentSource, NominatimPanelSource
from urbanlens.dashboard.plugins.builtin.overture_building_attributes import OvertureBuildingAttributesPanelSource
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

_NOMINATIM_RESULT = {
    "name": "Riverside Diner",
    "category": "amenity",
    "type": "restaurant",
    "building": "",
    "amenity": "restaurant",
    "tourism": "",
    "historic": "",
    "kind_label": "Restaurant",
    "extra_details": [],
}


def _make_pin(*, with_place: bool) -> Pin:
    baker.make("auth.User")  # first user is auto-promoted to bootstrap site admin
    user = baker.make("auth.User")
    place = baker.make(Place) if with_place else None
    location = baker.make(Location, latitude=40.0, longitude=-74.0, place=place)
    return baker.make(Pin, location=location, profile=Profile.objects.get(user=user))


class NominatimPanelSourceIngestionTests(TestCase):
    def test_fetch_syncs_tags_onto_the_locations_place(self):
        pin = _make_pin(with_place=True)

        with mock.patch.object(NominatimGateway, "reverse_geocode", return_value=dict(_NOMINATIM_RESULT)):
            NominatimPanelSource().fetch(pin)

        self.assertTrue(LocationCache.objects.filter(location=pin.location, source="nominatim").exists())
        tags = set(pin.location.place.external_tags.values_list("source", "key", "value"))
        self.assertIn((ExternalTagSource.OSM, "amenity", "restaurant"), tags)

    def test_fetch_skips_tag_sync_cleanly_when_location_has_no_place(self):
        pin = _make_pin(with_place=False)

        with mock.patch.object(NominatimGateway, "reverse_geocode", return_value=dict(_NOMINATIM_RESULT)):
            NominatimPanelSource().fetch(pin)  # must not raise

        self.assertTrue(LocationCache.objects.filter(location=pin.location, source="nominatim").exists())
        self.assertFalse(PlaceExternalTag.objects.exists())

    def test_a_second_fetch_for_a_different_pin_on_the_same_place_does_not_resync_while_fresh(self):
        place = baker.make(Place)
        baker.make("auth.User")
        user_a = baker.make("auth.User")
        user_b = baker.make("auth.User")
        location_a = baker.make(Location, latitude=40.0, longitude=-74.0, place=place)
        location_b = baker.make(Location, latitude=40.0001, longitude=-74.0001, place=place)
        pin_a = baker.make(Pin, location=location_a, profile=Profile.objects.get(user=user_a))
        pin_b = baker.make(Pin, location=location_b, profile=Profile.objects.get(user=user_b))

        # wraps=, not a bare Mock: is_fresh_for's freshness check reads the
        # rows sync_for_source itself writes, so replacing it outright would
        # make every fetch look "not fresh" (nothing was ever really synced)
        # and defeat the very behavior this test is checking.
        with mock.patch.object(NominatimGateway, "reverse_geocode", return_value=dict(_NOMINATIM_RESULT)), mock.patch.object(PlaceExternalTag, "sync_for_source", wraps=PlaceExternalTag.sync_for_source) as sync:
            NominatimPanelSource().fetch(pin_a)
            NominatimPanelSource().fetch(pin_b)

        self.assertEqual(sync.call_count, 1)


class NominatimEnrichmentSourceIngestionTests(TestCase):
    def test_enrich_syncs_tags_onto_the_locations_place(self):
        place = baker.make(Place)
        location = baker.make(Location, latitude=40.0, longitude=-74.0, place=place)

        with mock.patch.object(NominatimGateway, "reverse_geocode", return_value=dict(_NOMINATIM_RESULT)):
            NominatimEnrichmentSource().enrich(location)

        tags = set(place.external_tags.values_list("source", "key", "value"))
        self.assertIn((ExternalTagSource.OSM, "amenity", "restaurant"), tags)

    def test_enrich_skips_tag_sync_cleanly_when_location_has_no_place(self):
        location = baker.make(Location, latitude=40.0, longitude=-74.0, place=None)

        with mock.patch.object(NominatimGateway, "reverse_geocode", return_value=dict(_NOMINATIM_RESULT)):
            result = NominatimEnrichmentSource().enrich(location)  # must not raise

        self.assertTrue(result)
        self.assertFalse(PlaceExternalTag.objects.exists())

    def test_enrich_handles_a_nothing_found_geocode(self):
        location = baker.make(Location, latitude=40.0, longitude=-74.0, place=baker.make(Place))

        with mock.patch.object(NominatimGateway, "reverse_geocode", return_value=None):
            NominatimEnrichmentSource().enrich(location)  # must not raise

        self.assertFalse(PlaceExternalTag.objects.exists())


class OvertureBuildingAttributesPanelSourceIngestionTests(TestCase):
    def test_fetch_syncs_tags_onto_the_locations_place(self):
        pin = _make_pin(with_place=True)

        with mock.patch.object(OvertureMapsGateway, "get_building_attributes", return_value={"subtype": "single_family_residential", "class_": "residential"}), mock.patch.object(OvertureMapsGateway, "get_nearby_places", return_value=[]):
            OvertureBuildingAttributesPanelSource().fetch(pin)

        tags = set(pin.location.place.external_tags.values_list("source", "key", "value"))
        self.assertIn((ExternalTagSource.OVERTURE, "building_subtype", "single_family_residential"), tags)
        self.assertIn((ExternalTagSource.OVERTURE, "building_class", "residential"), tags)

    def test_nearby_places_categories_never_become_this_places_tags(self):
        pin = _make_pin(with_place=True)

        with mock.patch.object(OvertureMapsGateway, "get_building_attributes", return_value={"subtype": "single_family_residential"}), mock.patch.object(OvertureMapsGateway, "get_nearby_places", return_value=[{"name": "Corner Bakery", "category": "bakery", "distance_m": 8.0}]):
            OvertureBuildingAttributesPanelSource().fetch(pin)

        values = set(pin.location.place.external_tags.values_list("value", flat=True))
        self.assertNotIn("bakery", values)

    def test_fetch_skips_tag_sync_cleanly_when_location_has_no_place(self):
        pin = _make_pin(with_place=False)

        with mock.patch.object(OvertureMapsGateway, "get_building_attributes", return_value={"subtype": "single_family_residential"}), mock.patch.object(OvertureMapsGateway, "get_nearby_places", return_value=[]):
            OvertureBuildingAttributesPanelSource().fetch(pin)  # must not raise

        self.assertFalse(PlaceExternalTag.objects.exists())

    def test_fetch_with_no_attributes_writes_no_tags(self):
        pin = _make_pin(with_place=True)

        with mock.patch.object(OvertureMapsGateway, "get_building_attributes", return_value=None), mock.patch.object(OvertureMapsGateway, "get_nearby_places", return_value=[]):
            OvertureBuildingAttributesPanelSource().fetch(pin)  # must not raise

        self.assertFalse(PlaceExternalTag.objects.exists())
