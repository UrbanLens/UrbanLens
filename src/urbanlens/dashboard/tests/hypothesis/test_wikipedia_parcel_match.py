"""An article whose coordinates are on the pin's parcel is that parcel's article.

Reproduces the HRSH courtyard pin on k3s-staging: the lookup ran with the name hint "Courtyard Drive" and the
locality "Fairview" (OSM's census-designated place), the article's lead names neither, and the match was cached
as a miss, though Wikipedia places Hudson River State Hospital at 41.73306, -73.92833 - on the campus parcel.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.services.apis.assets.wikipedia import WikipediaGateway
from urbanlens.dashboard.services.locations.boundaries import ResolvedBoundaries, generate_location_boundaries

_LAT, _LON = 41.73266, -73.92736
_CAMPUS = MultiPolygon(Polygon.from_bbox((-73.934, 41.730, -73.923, 41.737)), srid=4326)
_HRSH_SUMMARY = {
    "title": "Hudson River State Hospital",
    "extract": "The Hudson River State Hospital was a New York state psychiatric hospital. It is located on US 9 on the "
    "Poughkeepsie-Hyde Park town line.",
    "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Hudson_River_State_Hospital"}},
}
_COMPONENTS = {"locality": "Fairview", "route": "", "street_number": "", "administrative_area_level_1": ""}


class ParcelContainmentMatchTests(SimpleTestCase):
    def _lookup(self, candidate: dict, within: MultiPolygon | None) -> dict | None:
        with (
            mock.patch.object(WikipediaGateway, "_geo_search", return_value=[candidate]),
            mock.patch.object(WikipediaGateway, "_fetch_summary", return_value=_HRSH_SUMMARY),
            mock.patch.object(WikipediaGateway, "_fill_full_extract"),
            mock.patch.object(WikipediaGateway, "_fetch_infobox", return_value=[]),
        ):
            return WikipediaGateway().get_article_for_location(
                _LAT, _LON, _COMPONENTS, name="Courtyard Drive", within=within
            )

    def test_an_article_placed_on_the_parcel_matches(self) -> None:
        article = self._lookup(
            {"title": "Hudson River State Hospital", "lat": 41.73305556, "lon": -73.92833333}, _CAMPUS
        )
        assert article is not None
        self.assertEqual(article["title"], "Hudson River State Hospital")

    def test_without_the_parcel_the_same_article_is_a_miss(self) -> None:
        self.assertIsNone(
            self._lookup({"title": "Hudson River State Hospital", "lat": 41.73305556, "lon": -73.92833333}, None)
        )

    def test_an_article_placed_off_the_parcel_is_not_matched_by_it(self) -> None:
        self.assertIsNone(self._lookup({"title": "Hudson River State Hospital", "lat": 41.745, "lon": -73.93}, _CAMPUS))


class _Fixture(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.parcel = Place.objects.create(kind=PlaceKind.PARCEL, geometry=_CAMPUS)
        Place.objects.filter(pk=self.parcel.pk).update(domain_root=self.parcel.pk)
        self.location = baker.make(Location, latitude=_LAT, longitude=_LON)


class ParcelPassedToTheLookupTests(_Fixture):
    def test_enrichment_passes_the_parcel_outline(self) -> None:
        from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaEnrichmentSource

        Location.objects.filter(pk=self.location.pk).update(place=self.parcel)
        self.location.refresh_from_db()
        with (
            mock.patch(
                "urbanlens.dashboard.plugins.builtin.wikipedia.match_address_components", return_value=_COMPONENTS
            ),
            mock.patch.object(WikipediaGateway, "get_article_for_location", return_value=None) as lookup,
        ):
            WikipediaEnrichmentSource().fetch(self.location)
        within = lookup.call_args.kwargs["within"]
        assert within is not None
        self.assertTrue(within.equals(self.parcel.geometry))

    def test_no_parcel_passes_nothing(self) -> None:
        from urbanlens.dashboard.plugins.builtin.wikipedia import WikipediaEnrichmentSource

        with (
            mock.patch(
                "urbanlens.dashboard.plugins.builtin.wikipedia.match_address_components", return_value=_COMPONENTS
            ),
            mock.patch.object(WikipediaGateway, "get_article_for_location", return_value=None) as lookup,
        ):
            WikipediaEnrichmentSource().fetch(baker.make(Location, latitude=41.80, longitude=-73.80))
        self.assertIsNone(lookup.call_args.kwargs["within"])


class ParcelArrivalRetriesAMissTests(_Fixture):
    def _resolve(self) -> mock.MagicMock:
        resolved = ResolvedBoundaries(property_polygon=_CAMPUS)
        with (
            mock.patch(
                "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries",
                return_value=resolved,
            ),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
            self.captureOnCommitCallbacks(execute=True),
        ):
            generate_location_boundaries(self.location)
        return enqueue

    @staticmethod
    def _prefetches(enqueue: mock.MagicMock) -> list:
        return [
            c
            for c in enqueue.call_args_list
            if getattr(c.args[0], "name", "").endswith("prefetch_location_external_data")
        ]

    def test_a_cached_miss_is_dropped_and_asked_again(self) -> None:
        Place.objects.filter(pk=self.parcel.pk).update(geometry_generated_at=None)
        LocationCache.set(self.location, "wikipedia", {}, query_key="Courtyard Drive (Fairview)")
        enqueue = self._resolve()
        self.assertFalse(LocationCache.objects.filter(location=self.location, source="wikipedia").exists())
        self.assertEqual(len(self._prefetches(enqueue)), 1)

    def test_a_cached_match_is_kept(self) -> None:
        LocationCache.set(self.location, "wikipedia", {"title": "Hudson River State Hospital"}, query_key="x")
        enqueue = self._resolve()
        self.assertTrue(LocationCache.objects.filter(location=self.location, source="wikipedia").exists())
        self.assertEqual(self._prefetches(enqueue), [])
