"""services.map_pins.autocomplete.search_local - external-tag matching for the map's
"Jump To" pin search bar. The wiki-side (domain-access) coverage lives in
test_search_wiki_domain_access.py::ExternalTagSearchDomainAccessTests; this file covers the
pin block, which is scoped by plain profile ownership rather than domain access.
"""

from __future__ import annotations

from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.place.external_tag import ExternalTagSource, ExtractedTag, PlaceExternalTag
from urbanlens.dashboard.models.place.model import Place
from urbanlens.dashboard.services.map_pins.autocomplete import search_local


class PinExternalTagAutocompleteTests(TestCase):
    def setUp(self):
        self.user = baker.make("auth.User")
        self.profile = self.user.profile
        self.other_user = baker.make("auth.User")
        self.other_profile = self.other_user.profile

        self.place = baker.make(Place)
        self.location = baker.make("dashboard.Location", latitude="39.10", longitude="-84.51", place=self.place)
        self.pin = baker.make("dashboard.Pin", profile=self.profile, location=self.location, name="The Grand Eatery")
        PlaceExternalTag.sync_for_source(self.place, ExternalTagSource.OVERTURE, [ExtractedTag(key="building_subtype", value="restaurant", is_primary=True)])

    def test_finds_the_pin_by_its_tag(self):
        results = search_local("restaurant", self.profile)
        self.assertTrue(any(r.title == "The Grand Eatery" for r in results))

    def test_plural_term_still_finds_the_pin(self):
        results = search_local("restaurants", self.profile)
        self.assertTrue(any(r.title == "The Grand Eatery" for r in results))

    def test_does_not_return_another_users_pin_via_tag_match(self):
        other_place = baker.make(Place)
        other_location = baker.make("dashboard.Location", latitude="10.0", longitude="10.0", place=other_place)
        baker.make("dashboard.Pin", profile=self.other_profile, location=other_location, name="Someone Else's Diner")
        PlaceExternalTag.sync_for_source(other_place, ExternalTagSource.OVERTURE, [ExtractedTag(key="building_subtype", value="restaurant", is_primary=True)])

        results = search_local("restaurant", self.profile)

        self.assertFalse(any(r.title == "Someone Else's Diner" for r in results))

    def test_a_place_with_no_matching_tag_is_not_found(self):
        results = search_local("church", self.profile)
        self.assertFalse(any(r.title == "The Grand Eatery" for r in results))
