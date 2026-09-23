"""The shared Wikipedia lookup is driven by public data only, and runs once the place has an address."""

from __future__ import annotations

from unittest import mock

from django.contrib.auth.models import User
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.abstract.versioning import WriteSource, writing_as
from urbanlens.dashboard.models.article.model import Article
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.models.wiki.model import Wiki
from urbanlens.dashboard.plugins.builtin.wikipedia import (
    WikipediaPanelSource,
    match_address_components,
    public_name_hint,
)
from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway
from urbanlens.dashboard.services.apis.locations.nominatim import NominatimGateway

_PRIVATE = "Secret Tunnel Entrance"
_HRSH = "Hudson River State Hospital"
_SUMMARY = {
    "title": _HRSH,
    "extract": "The Hudson River State Hospital is a former psychiatric hospital in Poughkeepsie, New York.",
    "extract_html": "<p>The <b>Hudson River State Hospital</b> is a former psychiatric hospital in Poughkeepsie, New York.</p>",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital"}},
    "pageid": 1,
}


def _no_address_lookup():
    return mock.patch("urbanlens.dashboard.services.locations.addresses.ensure_location_address", return_value=False)


class PrivatePinNameNeverDrivesTheSharedLookupTests(TestCase):
    """Exploit: a pin's private name steering a Location-wide lookup leaks it to everyone at the place.

    A nearby article titled like the private name would be matched on that name alone, cached for the
    whole Location, and surface as the community wiki's name; the name itself would sit in the shared
    row's query key.
    """

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = baker.make(
            Location, latitude=41.73328, longitude=-73.92812, official_name="", locality="", route=""
        )
        self.pin = baker.make(
            Pin, profile=self.profile, location=self.location, name=_PRIVATE, name_is_user_provided=True
        )

    def test_the_panel_lookup_never_sees_the_private_name(self) -> None:
        article = {"title": _PRIVATE, "extract": "<p>x</p>", "url": "https://en.wikipedia.org/wiki/Secret"}
        with (
            _no_address_lookup(),
            mock.patch.object(NominatimGateway, "reverse_geocode_admin", return_value=None),
            mock.patch.object(WikipediaGateway, "get_article_for_location", return_value=article) as lookup,
        ):
            WikipediaPanelSource().fetch(self.pin)

        self.assertNotIn(_PRIVATE.lower(), lookup.call_args.kwargs.get("name", "").lower())
        row = LocationCache.objects.get(location=self.location, source="wikipedia")
        self.assertNotIn(_PRIVATE.lower(), row.query_key.lower())

    def test_a_title_matching_only_the_private_name_is_not_adopted(self) -> None:
        summary = {
            **_SUMMARY,
            "title": _PRIVATE,
            "extract": "An unrelated tunnel.",
            "extract_html": "<p>An unrelated tunnel.</p>",
        }
        with (
            _no_address_lookup(),
            mock.patch.object(NominatimGateway, "reverse_geocode_admin", return_value=None),
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=[{"title": _PRIVATE}]),
            mock.patch.object(WikipediaGateway, "_fetch_summary", return_value=summary),
        ):
            WikipediaPanelSource().fetch(self.pin)

        self.assertEqual(LocationCache.objects.get(location=self.location, source="wikipedia").data, {})

    def test_the_hint_is_the_public_name(self) -> None:
        self.assertEqual(public_name_hint(self.location), "")
        with writing_as(WriteSource.AUTOMATIC):
            baker.make(Wiki, location=self.location, name=_HRSH)
        self.assertEqual(public_name_hint(self.location), _HRSH)


class MatchAddressComponentsTests(TestCase):
    def test_an_addressed_location_is_used_as_is(self) -> None:
        location = baker.make(Location, latitude=41.7, longitude=-73.9, locality="Poughkeepsie", route="Main St")
        with mock.patch.object(NominatimGateway, "reverse_geocode_admin") as nominatim:
            components = match_address_components(location)

        self.assertEqual(components["locality"], "Poughkeepsie")
        nominatim.assert_not_called()

    def test_the_street_address_is_looked_up_first(self) -> None:
        location = baker.make(Location, latitude=41.7, longitude=-73.9, locality="", route="")

        def _geocode(target: Location) -> bool:
            target.locality = "Poughkeepsie"
            target.save(update_fields=["locality"])
            return True

        with (
            mock.patch(
                "urbanlens.dashboard.services.locations.addresses.ensure_location_address", side_effect=_geocode
            ),
            mock.patch(
                "urbanlens.dashboard.services.locations.enrichment.AddressEnrichmentSource.gate", return_value=True
            ),
        ):
            components = match_address_components(location)

        self.assertEqual(components["locality"], "Poughkeepsie")

    def test_openstreetmap_supplies_the_municipality_when_there_is_no_street_address(self) -> None:
        location = baker.make(Location, latitude=41.7, longitude=-73.9, locality="", route="")
        with (
            _no_address_lookup(),
            mock.patch.object(
                NominatimGateway,
                "reverse_geocode_admin",
                return_value={"city": "Town of Poughkeepsie", "state": "New York", "country": "US"},
            ),
        ):
            components = match_address_components(location)

        self.assertEqual(components["locality"], "Poughkeepsie")

    def test_a_failed_openstreetmap_lookup_leaves_the_locality_empty(self) -> None:
        location = baker.make(Location, latitude=41.7, longitude=-73.9, locality="", route="")
        with (
            _no_address_lookup(),
            mock.patch.object(NominatimGateway, "reverse_geocode_admin", side_effect=OSError("down")),
        ):
            components = match_address_components(location)

        self.assertEqual(components["locality"], "")


class PrefetchFindsTheArticleForACoordinateOnlyPinTests(TestCase):
    """A pin dropped on bare coordinates has no address when the prefetch runs; the lookup must not miss for that."""

    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.location = baker.make(
            Location, latitude=41.73328, longitude=-73.92812, official_name="", locality="", route=""
        )
        self.pin = baker.make(
            Pin,
            profile=self.profile,
            location=self.location,
            name="e2e private campus notes",
            name_is_user_provided=True,
        )
        with writing_as(WriteSource.AUTOMATIC):
            self.wiki = baker.make(Wiki, location=self.location, name="Unnamed Location")

    def _prefetch(self) -> None:
        from urbanlens.dashboard import tasks

        with (
            _no_address_lookup(),
            mock.patch.object(NominatimGateway, "reverse_geocode_admin", return_value={"city": "Town of Poughkeepsie"}),
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=[{"title": _HRSH}]),
            mock.patch.object(WikipediaGateway, "_fetch_summary", return_value=_SUMMARY),
            mock.patch.object(WikipediaGateway, "_fill_full_extract"),
            mock.patch.object(WikipediaGateway, "_fetch_infobox", return_value=[]),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                return_value=False,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            tasks.prefetch_location_external_data(self.location.pk, profile_id=self.profile.pk)

    def test_the_article_is_matched_and_names_the_wiki_and_location(self) -> None:
        self._prefetch()

        self.assertEqual(LocationCache.objects.get(location=self.location, source="wikipedia").data["title"], _HRSH)
        self.wiki.refresh_from_db()
        self.location.refresh_from_db()
        self.pin.refresh_from_db()
        self.assertEqual(self.wiki.name, _HRSH)
        self.assertEqual(self.location.official_name, _HRSH)
        self.assertEqual(self.pin.name, "e2e private campus notes")

    def test_the_wiki_and_the_pin_get_the_attributed_article(self) -> None:
        self._prefetch()

        for article in (Article.objects.get(wiki=self.wiki), Article.objects.get(pin=self.pin)):
            self.assertIn("Poughkeepsie", article.content)
            self.assertIn("Wikipedia", article.content)

    def test_a_later_miss_does_not_overwrite_the_match(self) -> None:
        self._prefetch()
        LocationCache.objects.filter(location=self.location, source="wikipedia").update(updated="2000-01-01T00:00:00Z")

        from urbanlens.dashboard import tasks

        with (
            _no_address_lookup(),
            mock.patch.object(NominatimGateway, "reverse_geocode_admin", return_value=None),
            mock.patch.object(WikipediaGateway, "get_article_for_location", return_value=None),
            mock.patch(
                "urbanlens.dashboard.services.apis.locations.redata_context_gateway.redata_configured",
                return_value=False,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            tasks.prefetch_location_external_data(self.location.pk)

        self.assertEqual(LocationCache.objects.get(location=self.location, source="wikipedia").data["title"], _HRSH)
