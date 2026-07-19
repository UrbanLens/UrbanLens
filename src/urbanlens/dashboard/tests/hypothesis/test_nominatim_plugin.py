"""Tests for the Nominatim plugin: extra OSM fields, old_name alias splitting, OSM link auto-add."""

from __future__ import annotations

from unittest.mock import patch

from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.links.model import PinLink, WikiLink
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.profile.model import Profile
from urbanlens.dashboard.plugins.builtin.nominatim import NominatimPanelSource
from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

_RAW_NOMINATIM_RESPONSE = {
    "osm_type": "way",
    "osm_id": "141919400",
    "name": "Summit Behavioral Center",
    "namedetails": {"name": "Summit Behavioral Center"},
    "display_name": "Summit Behavioral Center, Cincinnati, OH",
    "extratags": {
        "old_name": "Pauline Warfield Lewis Center;Cincinnati State Hospital",
        "operator": "Summit Behavioral Healthcare",
        "operator:type": "public",
        "gnis:feature_id": "2733172",
        "phone": "+1-513-948 3600",
    },
    "address": {},
}


class NominatimNormaliseExtraFieldsTests(SimpleTestCase):
    """_normalise() surfaces old_name/operator:type/gnis:feature_id, previously dropped."""

    def test_old_name_is_captured_verbatim(self) -> None:
        data = NominatimGateway._normalise(_RAW_NOMINATIM_RESPONSE)
        self.assertEqual(data["old_name"], "Pauline Warfield Lewis Center;Cincinnati State Hospital")

    def test_operator_type_and_gnis_id_appear_in_extra_details(self) -> None:
        data = NominatimGateway._normalise(_RAW_NOMINATIM_RESPONSE)
        by_key = {d["key"]: d["value"] for d in data["extra_details"]}
        self.assertEqual(by_key["operator:type"], "Public")
        self.assertEqual(by_key["gnis:feature_id"], "2733172")


class NominatimFetchAddsOsmLinkTests(TestCase):
    """NominatimPanelSource.fetch() adds the OSM element URL to pin (and wiki) links."""

    def setUp(self) -> None:
        baker.make("auth.User")  # bootstrap site admin
        self.user = baker.make("auth.User")
        self.profile = Profile.objects.get(user=self.user)
        self.location = baker.make(Location, latitude="39.19749", longitude="-84.46964")
        self.pin = baker.make(Pin, profile=self.profile, location=self.location, name="Summit Behavioral Center")

    def _fetch(self) -> None:
        with patch.object(NominatimGateway, "reverse_geocode", return_value=NominatimGateway._normalise(_RAW_NOMINATIM_RESPONSE)):
            NominatimPanelSource().fetch(self.pin)

    def test_adds_pin_link(self) -> None:
        self._fetch()
        self.assertTrue(PinLink.objects.filter(pin=self.pin, url="https://www.openstreetmap.org/way/141919400").exists())

    def test_does_not_duplicate_link_on_repeated_fetch(self) -> None:
        self._fetch()
        self._fetch()
        self.assertEqual(PinLink.objects.filter(pin=self.pin, url="https://www.openstreetmap.org/way/141919400").count(), 1)

    def test_adds_wiki_link_when_wiki_exists(self) -> None:
        wiki = baker.make("dashboard.Wiki", location=self.location)
        self._fetch()
        self.assertTrue(WikiLink.objects.filter(wiki=wiki, url="https://www.openstreetmap.org/way/141919400").exists())

    def test_no_wiki_link_created_when_no_wiki_exists(self) -> None:
        self._fetch()
        self.assertFalse(WikiLink.objects.exists())
