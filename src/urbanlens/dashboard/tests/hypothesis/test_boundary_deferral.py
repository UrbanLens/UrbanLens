"""A provider that refused for now is not a provider that found nothing.

Reproduces the HRSH courtyard pin on k3s-staging (41.73266, -73.92736): REData's Dutchess budget was
spent, the parcel lookup was refused, and the miss was stamped as "no parcel here" for the whole
boundary cache window. The same point sits inside the campus tax parcel.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.gis.geos import MultiPolygon, Point, Polygon
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
from urbanlens.dashboard.services.apis.locations.boundaries.overpass import OverpassGateway
from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    REASON_SOURCE_RATE_LIMITED,
    PropertyRecordsUnavailableError,
)
from urbanlens.dashboard.services.locations.boundaries import (
    BoundaryProviderChain,
    ResolvedBoundaries,
    boundary_generation_ran,
    generate_location_boundaries,
)

_LAT, _LON = 41.73266, -73.92736


class _Refusing:
    service_key = "redata_boundary"
    boundary_kind = "property"

    def get_typed_boundaries(self, latitude, longitude, *, name=None):
        raise BoundaryProviderDeferredError(self.service_key, retry_after=120)


class _Empty:
    service_key = "overpass"
    boundary_kind = "property"

    def get_typed_boundaries(self, latitude, longitude, *, name=None):
        return {"property": None, "building": None}


class RedataRefusalIsADeferralTests(SimpleTestCase):
    _GATEWAY = "urbanlens.dashboard.services.apis.locations.boundaries.redata.RedataGateway"

    def test_a_transient_refusal_defers_rather_than_answering_none(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.boundaries.redata.settings") as cfg,
            mock.patch(self._GATEWAY) as gateway,
        ):
            cfg.redata_api_url, cfg.redata_api_key = "https://redata.example", "key"
            gateway.return_value.lookup_parcel.side_effect = PropertyRecordsUnavailableError(
                REASON_SOURCE_RATE_LIMITED, "Dutchess budget exhausted"
            )
            with self.assertRaises(BoundaryProviderDeferredError):
                RedataBoundaryProvider().get_typed_boundaries(_LAT, _LON)

    def test_a_settled_answer_is_still_none(self) -> None:
        with (
            mock.patch("urbanlens.dashboard.services.apis.locations.boundaries.redata.settings") as cfg,
            mock.patch(self._GATEWAY) as gateway,
        ):
            cfg.redata_api_url, cfg.redata_api_key = "https://redata.example", "key"
            gateway.return_value.lookup_parcel.side_effect = PropertyRecordsUnavailableError("no_data_found", "none")
            result = RedataBoundaryProvider().get_typed_boundaries(_LAT, _LON)
        self.assertEqual(result, {"property": None, "building": None})


class ChainRecordsDeferralTests(SimpleTestCase):
    def test_a_deferring_provider_is_recorded_and_the_chain_continues(self) -> None:
        later = _Empty()
        with mock.patch.object(later, "get_typed_boundaries", wraps=later.get_typed_boundaries) as asked:
            resolved = BoundaryProviderChain(providers=(_Refusing(), later)).get_boundaries(_LAT, _LON)
        self.assertEqual(resolved.deferred, ["redata_boundary"])
        self.assertEqual(resolved.retry_after, 120)
        asked.assert_called_once()


class DeferredMissIsNotStampedTests(TestCase):
    def test_a_deferred_miss_is_left_unresolved_and_retried(self) -> None:
        location = baker.make(Location, latitude=_LAT, longitude=_LON)
        resolved = ResolvedBoundaries(deferred=["redata_boundary"], retry_after=300)
        with (
            mock.patch(
                "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries",
                return_value=resolved,
            ),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            place = generate_location_boundaries(location)

        self.assertIsNone(place)
        location.refresh_from_db()
        self.assertFalse(boundary_generation_ran(location))
        retries = [
            call
            for call in enqueue.call_args_list
            if getattr(call.args[0], "name", "").endswith("generate_boundaries_for_location")
        ]
        self.assertEqual(len(retries), 1)
        self.assertGreaterEqual(retries[0].kwargs["countdown"], 300)

    def test_an_undeferred_miss_is_still_stamped(self) -> None:
        location = baker.make(Location, latitude=_LAT, longitude=_LON)
        with mock.patch(
            "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries",
            return_value=ResolvedBoundaries(),
        ):
            generate_location_boundaries(location)
        location.refresh_from_db()
        self.assertTrue(boundary_generation_ran(location))

    def test_retries_stop_after_the_last_attempt_but_the_miss_stays_unstamped(self) -> None:
        from urbanlens.dashboard.services.locations.boundaries import MAX_DEFERRED_RETRIES

        location = baker.make(Location, latitude=_LAT, longitude=_LON)
        with (
            mock.patch(
                "urbanlens.dashboard.services.locations.boundaries.BoundaryProviderChain.get_boundaries",
                return_value=ResolvedBoundaries(deferred=["redata_boundary"]),
            ),
            mock.patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as enqueue,
        ):
            generate_location_boundaries(location, attempt=MAX_DEFERRED_RETRIES)
        location.refresh_from_db()
        self.assertFalse(boundary_generation_ran(location))
        self.assertFalse(
            [
                c
                for c in enqueue.call_args_list
                if getattr(c.args[0], "name", "").endswith("generate_boundaries_for_location")
            ]
        )


def _ring(west: float, south: float, east: float, north: float) -> list[dict]:
    corners = [(west, south), (east, south), (east, north), (west, north), (west, south)]
    return [{"lat": lat, "lon": lon} for lon, lat in corners]


class OverpassContainingAreaTests(SimpleTestCase):
    """A campus polygon whose edges are all farther than the search radius still contains the point."""

    def test_the_query_asks_which_areas_contain_the_point(self) -> None:
        with mock.patch.object(OverpassGateway, "elements_for_query", return_value=[]) as run:
            OverpassGateway().get_typed_boundaries(_LAT, _LON)
        query = run.call_args.args[0]
        self.assertIn(f"is_in({_LAT:.7f},{_LON:.7f})", query)
        self.assertIn("pivot", query)

    def test_a_named_site_area_containing_the_point_is_the_property(self) -> None:
        campus = {
            "type": "way",
            "id": 889025160,
            "tags": {"landuse": "construction", "name": "Hudson Heritage"},
            "geometry": _ring(-73.9337, 41.7300, -73.9229, 41.7373),
        }
        with mock.patch.object(OverpassGateway, "elements_for_query", return_value=[campus]):
            typed = OverpassGateway().get_typed_boundaries(_LAT, _LON)
        assert isinstance(typed["property"], Polygon)
        self.assertTrue(typed["property"].contains(Point(_LON, _LAT, srid=4326)))

    def test_an_unnamed_zoning_area_is_not_a_property(self) -> None:
        """A residential landuse polygon covers a neighbourhood; adopting it would merge every house's access domain."""
        zoning = {
            "type": "way",
            "id": 1,
            "tags": {"landuse": "residential"},
            "geometry": _ring(-73.94, 41.72, -73.91, 41.74),
        }
        with mock.patch.object(OverpassGateway, "elements_for_query", return_value=[zoning]):
            typed = OverpassGateway().get_typed_boundaries(_LAT, _LON)
        self.assertIsNone(typed["property"])

    def test_a_named_zoning_area_is_not_a_property_either(self) -> None:
        zoning = {
            "type": "way",
            "id": 2,
            "tags": {"landuse": "residential", "name": "Fairview Estates"},
            "geometry": _ring(-73.94, 41.72, -73.91, 41.74),
        }
        with mock.patch.object(OverpassGateway, "elements_for_query", return_value=[zoning]):
            typed = OverpassGateway().get_typed_boundaries(_LAT, _LON)
        self.assertIsNone(typed["property"])


class OverpassSiteSizeCapTests(SimpleTestCase):
    def test_a_named_site_larger_than_the_cap_is_ignored(self) -> None:
        huge = MultiPolygon(Polygon.from_bbox((-74.2, 41.5, -73.7, 41.9)), srid=4326)
        element = {
            "type": "relation",
            "id": 3,
            "tags": {"leisure": "nature_reserve", "name": "A Very Large Reserve"},
            "members": [{"role": "outer", "geometry": [{"lat": y, "lon": x} for x, y in huge[0].exterior_ring.coords]}],
        }
        with mock.patch.object(OverpassGateway, "elements_for_query", return_value=[element]):
            typed = OverpassGateway().get_typed_boundaries(_LAT, _LON)
        self.assertIsNone(typed["property"])
