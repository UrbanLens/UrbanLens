"""P110: inside the US, Overture's buildings and places come from REData's mirror and the public release is never read.

REData's own HTTP is answered at the transport, below every gateway and its rate limiter. The fixtures are REData
``release/0.3.0``'s wire shapes: ``BuildingRecord.to_dict()`` for an ``OvertureBuilding`` row
(``overture_buildings.lookup.overture_building_to_record``, whose ``attributes`` carry the roof columns since
``7ac19bf6``), and ``PointOfInterestSerializer`` over an ``OverturePlace`` proxy
(``overture_places.lookup.overture_place_to_poi``, with ``operating_status`` and ``taxonomy`` in ``attributes``), each
in ``api.coordinates.provider_results_response``'s envelope.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
import json
from typing import TYPE_CHECKING, Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.contrib.gis.geos import Point
from django.utils import timezone
from model_bakery import baker
import pytest
import requests

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.api_call_log import ApiCallLog
from urbanlens.dashboard.models.cache.location_cache import (
    PARTIAL_ANSWER_STALE_AFTER,
    UNANSWERED_SOURCES_KEY,
    LocationCache,
)
from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
from urbanlens.dashboard.services.apis.locations.boundaries.overture_maps import OvertureMapsGateway
from urbanlens.dashboard.services.apis.locations.redata_context_gateway import LocationContextUnavailableError
from urbanlens.dashboard.services.locations.boundaries import BoundaryProviderChain
from urbanlens.dashboard.tests.hypothesis.redata_helpers import REDATA_TEST_URL, RedataConfiguredMixin
from urbanlens.UrbanLens.settings.app import settings as app_settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from urbanlens.dashboard.models.pin.model import Pin
    from urbanlens.dashboard.services.apis.locations.base import BoundaryProvider

_MAPS = "urbanlens.dashboard.services.apis.locations.boundaries.overture_maps"
_BUILDINGS = "/api/v1/buildings/"
_PLACES = "/api/v1/points-of-interest/lookup/"
_SOURCE = "overture_building_attributes"

#: The US Capitol, the coordinate REData's own ``/buildings/`` view test uses.
_INSIDE = (38.8895, -77.0075)
#: Paris, outside every US box.
_ABROAD = (48.8584, 2.2945)

_CAPITOL_RING = [[-77.01, 38.888], [-77.01, 38.891], [-77.005, 38.891], [-77.005, 38.888], [-77.01, 38.888]]
#: A larger footprint also containing the point; the smaller one is the building.
_CAMPUS_RING = [[-77.02, 38.88], [-77.02, 38.90], [-76.99, 38.90], [-76.99, 38.88], [-77.02, 38.88]]
_PARIS_RING = [[2.2940, 48.8580], [2.2940, 48.8588], [2.2950, 48.8588], [2.2950, 48.8580], [2.2940, 48.8580]]


def _building_record(
    ring: list[list[float]],
    *,
    name: str,
    subtype: str,
    building_class: str,
    height: float | None,
    num_floors: int | None,
    roof_shape: str | None = None,
    roof_material: str | None = None,
) -> dict[str, Any]:
    return {
        "source": "overture",
        "name": name,
        "address": "",
        "building_number": "",
        "year_built": None,
        "latitude": (ring[0][1] + ring[2][1]) / 2,
        "longitude": (ring[0][0] + ring[2][0]) / 2,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "attributes": {
            "subtype": subtype,
            "class": building_class,
            "height": height,
            "num_floors": num_floors,
            "sources": [
                {
                    "property": "",
                    "dataset": "OpenStreetMap",
                    "record_id": "w66418809@32",
                    "update_time": "2024-11-04T19:11:52.000Z",
                    "confidence": None,
                    "between": None,
                }
            ],
            "num_floors_underground": None,
            "min_height": None,
            "min_floor": None,
            "is_underground": False,
            "has_parts": True,
            "level": None,
            "roof_shape": roof_shape,
            "roof_material": roof_material,
            "roof_color": None,
            "roof_direction": None,
            "roof_orientation": None,
            "roof_height": None,
            "facade_color": None,
            "facade_material": None,
            "version": 2,
            "names": {"primary": name or None, "common": None, "rules": None},
        },
        "match_scope": "point_radius",
        "distance_meters": 0.0,
        "on_parcel": None,
        "is_on_property": True,
        "on_survey_roster": False,
    }


_CAPITOL = _building_record(
    _CAPITOL_RING,
    name="United States Capitol",
    subtype="civic",
    building_class="government",
    height=87.6,
    num_floors=3,
    roof_shape="dome",
    roof_material="metal",
)
_CAMPUS = _building_record(_CAMPUS_RING, name="", subtype="", building_class="", height=None, num_floors=None)


def _place_row(
    name: str,
    *,
    category: str,
    latitude: float,
    longitude: float,
    confidence: float,
    operating_status: str | None = "open",
) -> dict[str, Any]:
    return {
        "uuid": "4b0c2b0e-8f53-4c39-9d77-1c8f1f3c6a10",
        "provider": "overture",
        "provenance": "",
        "external_id": "08f2aa8460c2a6ac03e9b6b0f3c6b6a1",
        "name": name,
        "category": category,
        "description": "",
        "url": "",
        "latitude": latitude,
        "longitude": longitude,
        "attributes": {
            "category_alternate": [],
            "confidence": confidence,
            "brand_name": "",
            "brand_wikidata": "",
            "addresses": [],
            "websites": [],
            "socials": [],
            "emails": [],
            "phones": [],
            "basic_category": category,
            "taxonomy": {"primary": category, "hierarchy": [category], "alternates": []},
            "operating_status": operating_status,
            "sources": [
                {
                    "property": "",
                    "dataset": "meta",
                    "record_id": "107436122627153",
                    "update_time": "2025-03-03T08:00:00.000Z",
                    "confidence": confidence,
                    "between": None,
                }
            ],
            "version": 1,
        },
        "record_retrieved_at": "2026-10-05T03:12:40.512345Z",
        "created": None,
        "updated": None,
    }


#: About 30 m north of the Capitol point.
_CAFE = _place_row("Capitol Cafe", category="coffee_shop", latitude=38.88977, longitude=-77.0075, confidence=0.93)
#: About 90 m away, so the nearer one comes first.
_MUSEUM = _place_row(
    "Capitol Visitor Center",
    category="museum",
    latitude=38.89031,
    longitude=-77.0075,
    confidence=0.71,
    operating_status="closed",
)


def _envelope(results: list[dict[str, Any]], *, radius_meters: float) -> dict[str, Any]:
    return {
        "count": len(results),
        "complete": True,
        "results": results,
        "providers": [
            {
                "provider": "overture",
                "status": "ok",
                "count": len(results),
                "message": None,
                "radius_meters": radius_meters,
                "limit": None,
            }
        ],
    }


_OUTAGE = {
    "count": 0,
    "complete": False,
    "results": [],
    "providers": [
        {
            "provider": "overture",
            "status": "unavailable",
            "count": 0,
            "message": "Overture database is unreachable",
            "radius_meters": 10.0,
            "limit": None,
        }
    ],
    "error": "all_providers_unavailable",
    "message": "overture: Overture database is unreachable",
}

#: The places lookup's own way of saying its one provider did not answer: a 200 with nothing in it.
_PLACES_UNANSWERED = {**_OUTAGE, "error": None, "message": None}


@contextmanager
def _redata_answers(answers: dict[str, tuple[int, object]]) -> Iterator[list[requests.PreparedRequest]]:
    """Answer REData's paths at the transport; any other request fails the test."""
    asked: list[requests.PreparedRequest] = []

    def send(adapter: Any, request: requests.PreparedRequest, *args: Any, **kwargs: Any) -> requests.Response:
        asked.append(request)
        url = urlsplit(request.url or "")
        if f"{url.scheme}://{url.netloc}" != REDATA_TEST_URL or url.path not in answers:
            raise AssertionError(f"unexpected request to {request.url}")
        status, body = answers[url.path]
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(body).encode()  # noqa: SLF001
        response.url = request.url or ""
        response.request = request
        return response

    with mock.patch.object(requests.adapters.HTTPAdapter, "send", send):
        yield asked


class _RefusedModule:
    def __init__(self, touched: list[str]) -> None:
        self._touched = touched

    def __getattr__(self, attribute: str) -> Any:
        self._touched.append(f"overturemaps.core.{attribute}")
        raise AssertionError(f"the public Overture release was read via overturemaps.core.{attribute}")


@contextmanager
def _public_release_refused() -> Iterator[list[str]]:
    """Every way into the public release, refusing and recording any use, including from a worker thread."""
    touched: list[str] = []

    def refuse(name: str) -> Any:
        def refused(*args: Any, **kwargs: Any) -> Any:
            touched.append(name)
            raise AssertionError(f"the public Overture release was read via {name}")

        return refused

    with (
        mock.patch.object(OvertureMapsGateway, "__post_init__", refuse("OvertureMapsGateway()")),
        mock.patch(f"{_MAPS}._overture_geodataframe", refuse("overturemaps.geodataframe")),
        mock.patch(f"{_MAPS}._overture_core", _RefusedModule(touched)),
        mock.patch(f"{_MAPS}.urlopen", refuse("urlopen")),
    ):
        yield touched


def _overture_step() -> BoundaryProvider:
    """The default chain's Overture provider."""
    return next(
        provider for provider in BoundaryProviderChain().providers if "overture" in (provider.service_key or "")
    )


def _query(request: requests.PreparedRequest) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.url or "").query)


def _reset_public_reader() -> None:
    from urbanlens.dashboard.services.apis.locations.boundaries import overture_maps

    overture_maps._stac_unavailable_until = 0.0  # noqa: SLF001
    overture_maps._latest_release_cache = None  # noqa: SLF001
    overture_maps._stac_index_cache.clear()  # noqa: SLF001


class InsideTheUsTests(RedataConfiguredMixin, TestCase):
    """REData answers, and nothing reaches the public release."""

    def _provider(self) -> Any:
        from urbanlens.dashboard.services.apis.locations.boundaries.overture import OvertureProvider

        return OvertureProvider()

    def test_building_attributes_come_from_redatas_mirror(self) -> None:
        with (
            _public_release_refused() as touched,
            _redata_answers({_BUILDINGS: (200, _envelope([_CAMPUS, _CAPITOL], radius_meters=10.0))}) as asked,
        ):
            attributes = self._provider().get_building_attributes(*_INSIDE)

        self.assertEqual(touched, [])
        self.assertEqual(
            attributes,
            {
                "class_": "government",
                "subtype": "civic",
                "height_m": 87.6,
                "num_floors": 3,
                "roof_shape": "dome",
                "roof_material": "metal",
                "primary_name": "United States Capitol",
            },
        )
        self.assertEqual(len(asked), 1)
        self.assertEqual(_query(asked[0])["provider"], ["overture"])

    def test_nearby_places_come_from_redatas_overture_provider(self) -> None:
        with (
            _public_release_refused() as touched,
            _redata_answers({_PLACES: (200, _envelope([_MUSEUM, _CAFE], radius_meters=150.0))}) as asked,
        ):
            places = self._provider().get_nearby_places(*_INSIDE, radius_m=150, limit=5)

        self.assertEqual(touched, [])
        self.assertEqual([place["name"] for place in places], ["Capitol Cafe", "Capitol Visitor Center"])
        self.assertEqual(places[0]["category"], "coffee_shop")
        self.assertEqual(places[0]["confidence"], 0.93)
        self.assertEqual([place["operating_status"] for place in places], ["open", "closed"])
        self.assertAlmostEqual(places[0]["distance_m"], 30.0, delta=1.0)
        query = _query(asked[0])
        self.assertEqual(query["provider"], ["overture"])
        self.assertEqual(float(query["radius_meters"][0]), 150.0)

    def test_a_buildings_name_is_read_from_the_published_name_field(self) -> None:
        """``name`` is in REData's schema; ``attributes.names`` is an untyped copy it may stop sending."""
        record = _building_record(
            _CAPITOL_RING, name="Capitol", subtype="civic", building_class="", height=None, num_floors=None
        )
        del record["attributes"]["names"]
        with _redata_answers({_BUILDINGS: (200, _envelope([record], radius_meters=10.0))}):
            attributes = self._provider().get_building_attributes(*_INSIDE)

        assert attributes is not None
        self.assertEqual(attributes["primary_name"], "Capitol")

    def test_the_chains_overture_step_takes_redatas_footprint(self) -> None:
        with (
            _public_release_refused() as touched,
            _redata_answers({_BUILDINGS: (200, _envelope([_CAMPUS, _CAPITOL], radius_meters=10.0))}),
        ):
            typed = _overture_step().get_typed_boundaries(*_INSIDE)

        self.assertEqual(touched, [])
        building = typed["building"]
        assert building is not None
        self.assertTrue(building.contains(Point(_INSIDE[1], _INSIDE[0], srid=4326)))
        self.assertAlmostEqual(building.area, 0.005 * 0.003, places=9)

    def test_an_empty_answer_is_no_data_not_a_reason_to_read_the_public_release(self) -> None:
        """REData answers a point no synced shard covers "ok" with no rows, so empty must stay empty."""
        empty = {
            _BUILDINGS: (200, _envelope([], radius_meters=10.0)),
            _PLACES: (200, _envelope([], radius_meters=150.0)),
        }
        with _public_release_refused() as touched, _redata_answers(empty):
            provider = self._provider()
            attributes = provider.get_building_attributes(*_INSIDE)
            places = provider.get_nearby_places(*_INSIDE)
            typed = _overture_step().get_typed_boundaries(*_INSIDE)

        self.assertEqual(touched, [])
        self.assertIsNone(attributes)
        self.assertEqual(places, [])
        self.assertIsNone(typed["building"])

    def test_without_redata_there_is_no_data_and_still_no_public_read(self) -> None:
        with (
            mock.patch.object(app_settings, "redata_api_url", None),
            _public_release_refused() as touched,
            _redata_answers({}) as asked,
        ):
            provider = self._provider()
            attributes = provider.get_building_attributes(*_INSIDE)
            places = provider.get_nearby_places(*_INSIDE)
            typed = _overture_step().get_typed_boundaries(*_INSIDE)

        self.assertEqual((touched, asked), ([], []))
        self.assertIsNone(attributes)
        self.assertEqual(places, [])
        self.assertIsNone(typed["building"])

    def test_an_outage_raises_rather_than_answering_nothing(self) -> None:
        outage = {_BUILDINGS: (503, _OUTAGE), _PLACES: (200, _PLACES_UNANSWERED)}
        with _public_release_refused() as touched, _redata_answers(outage):
            provider = self._provider()
            with pytest.raises(LocationContextUnavailableError):
                provider.get_building_attributes(*_INSIDE)
            with pytest.raises(LocationContextUnavailableError):
                provider.get_nearby_places(*_INSIDE)

        self.assertEqual(touched, [])

    def test_an_outage_defers_the_chains_overture_step(self) -> None:
        """Deferred is retried later; a None would let the chain settle on a coarser footprint for good."""
        with (
            _public_release_refused() as touched,
            _redata_answers({_BUILDINGS: (503, _OUTAGE)}),
            pytest.raises(BoundaryProviderDeferredError),
        ):
            _overture_step().get_typed_boundaries(*_INSIDE)

        self.assertEqual(touched, [])

    def test_a_throttled_redata_defers_the_step_for_as_long_as_it_asked(self) -> None:
        throttled = {_BUILDINGS: (429, {"detail": "Request was throttled. Expected available in 42 seconds."})}
        with _redata_answers(throttled), pytest.raises(BoundaryProviderDeferredError) as deferred:
            _overture_step().get_typed_boundaries(*_INSIDE)

        self.assertEqual(deferred.value.retry_after, 42)

    def test_the_whole_chain_lists_the_overture_step_as_deferred_and_settles_no_building(self) -> None:
        chain = BoundaryProviderChain(providers=(_overture_step(),))
        with _public_release_refused() as touched, _redata_answers({_BUILDINGS: (503, _OUTAGE)}):
            resolved = chain.get_boundaries(*_INSIDE)

        self.assertEqual(touched, [])
        self.assertIsNone(resolved.building_polygon)
        self.assertEqual(resolved.deferred, ["overture"])


class PanelInsideTheUsTests(RedataConfiguredMixin, TestCase):
    """The Building Characteristics panel, through its own fetch."""

    def setUp(self) -> None:
        super().setUp()
        self.pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=baker.make(User).profile,
            location=baker.make("dashboard.Location", latitude=_INSIDE[0], longitude=_INSIDE[1]),
        )

    def _source(self) -> Any:
        from urbanlens.dashboard.plugins.builtin.overture_building_attributes import (
            OvertureBuildingAttributesPanelSource,
        )

        return OvertureBuildingAttributesPanelSource()

    def _row_exists(self) -> bool:
        return LocationCache.objects.filter(location=self.pin.location, source=_SOURCE).exists()

    def test_the_panel_caches_redatas_answer_and_never_reads_the_public_release(self) -> None:
        answers = {
            _BUILDINGS: (200, _envelope([_CAPITOL], radius_meters=10.0)),
            _PLACES: (200, _envelope([_CAFE, _MUSEUM], radius_meters=150.0)),
        }
        with _public_release_refused() as touched, _redata_answers(answers):
            self._source().fetch(self.pin)

        self.assertEqual(touched, [])
        cached = LocationCache.get_fresh(self.pin.location, _SOURCE)
        assert cached is not None
        self.assertEqual(cached.data["subtype"], "civic")
        self.assertEqual(cached.data["roof_shape"], "dome")
        self.assertNotIn(UNANSWERED_SOURCES_KEY, cached.data)
        context = self._source().render_context(self.pin, cached.data)
        assert context is not None
        self.assertIn({"label": "Roof Shape", "value": "Dome"}, context["meta"])
        self.assertIn("Capitol Visitor Center (closed)", " ".join(fact["text"] for fact in context["facts"]))

    def test_a_building_outage_caches_nothing(self) -> None:
        """P187: a row marks the source fetched for the whole cache term."""
        with (
            _public_release_refused() as touched,
            _redata_answers({_BUILDINGS: (503, _OUTAGE)}),
            pytest.raises(LocationContextUnavailableError),
        ):
            self._source().fetch(self.pin)

        self.assertEqual(touched, [])
        self.assertFalse(self._row_exists())

    def test_a_places_outage_keeps_the_building_marked_partial_for_the_hour(self) -> None:
        """The building is an answer; the missing places are not "none nearby", so the row lapses within the hour."""
        answers = {_BUILDINGS: (200, _envelope([_CAPITOL], radius_meters=10.0)), _PLACES: (200, _PLACES_UNANSWERED)}
        with _public_release_refused() as touched, _redata_answers(answers):
            self._source().fetch(self.pin)

        self.assertEqual(touched, [])
        row = LocationCache.objects.get(location=self.pin.location, source=_SOURCE)
        self.assertEqual(row.data["subtype"], "civic")
        self.assertEqual(row.data["nearby_places"], [])
        self.assertTrue(row.data[UNANSWERED_SOURCES_KEY])
        self.assertIsNotNone(LocationCache.get_fresh(self.pin.location, _SOURCE))
        later = timezone.now() + PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.assertIsNone(LocationCache.get_fresh(self.pin.location, _SOURCE))

    def test_a_places_outage_with_no_building_caches_nothing(self) -> None:
        """Nothing known and something unasked is not an answer."""
        answers = {_BUILDINGS: (200, _envelope([], radius_meters=10.0)), _PLACES: (200, _PLACES_UNANSWERED)}
        with _redata_answers(answers), pytest.raises(LocationContextUnavailableError):
            self._source().fetch(self.pin)

        self.assertFalse(self._row_exists())

    def test_the_panel_is_not_scheduled_inside_the_us_without_redata(self) -> None:
        with mock.patch.object(app_settings, "redata_api_url", None):
            self.assertFalse(self._source().gate(self.pin))
        self.assertTrue(self._source().gate(self.pin))

    def test_the_panel_is_still_scheduled_abroad_without_redata(self) -> None:
        pin: Pin = baker.make_recipe(
            "dashboard.pin",
            profile=self.pin.profile,
            location=baker.make("dashboard.Location", latitude=_ABROAD[0], longitude=_ABROAD[1]),
        )
        with mock.patch.object(app_settings, "redata_api_url", None):
            self.assertTrue(self._source().gate(pin))


def _paris_building() -> dict[str, Any]:
    """A building as the public release's GeoDataFrame yields it."""
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [_PARIS_RING]},
        "properties": {
            "class": "commercial",
            "subtype": "commercial",
            "height": 12.0,
            "num_floors": 4,
            "names": {"primary": "Tour"},
            "roof_shape": "flat",
            "roof_material": None,
        },
    }


def _paris_place() -> dict[str, Any]:
    """A place in the public release's schema since 2026-09-23, which has ``taxonomy`` and no ``categories``."""
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [2.2946, 48.8585]},
        "properties": {
            "names": {"primary": "Le Cafe"},
            "taxonomy": {"primary": "cafe", "hierarchy": ["food_and_drink", "cafe"], "alternates": []},
            "basic_category": "cafe",
            "confidence": 0.8,
            "operating_status": "open",
        },
    }


class OutsideTheUsTests(RedataConfiguredMixin, TestCase):
    """Abroad, the public release is still read, under its own budget, and REData is not asked."""

    def setUp(self) -> None:
        super().setUp()
        _reset_public_reader()
        self.addCleanup(_reset_public_reader)
        latest = mock.patch("overturemaps.core.get_latest_release", return_value="2026-09-23.1")
        latest.start()
        self.addCleanup(latest.stop)

    def test_building_attributes_abroad_read_the_public_release_under_its_budget(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overture import OvertureProvider

        with (
            _redata_answers({}) as asked,
            mock.patch(f"{_MAPS}._intersecting_files", return_value=["bucket/one.parquet"]),
            mock.patch(f"{_MAPS}._read_files", return_value=[_paris_building()]) as read,
        ):
            attributes = OvertureProvider().get_building_attributes(*_ABROAD)

        self.assertEqual(asked, [])
        read.assert_called_once()
        assert attributes is not None
        self.assertEqual(attributes["class_"], "commercial")
        self.assertEqual(attributes["roof_shape"], "flat")
        entry = ApiCallLog.objects.for_service("overture_maps").latest("created")
        self.assertTrue(entry.success)

    def test_a_place_abroad_takes_its_category_from_the_taxonomy(self) -> None:
        from urbanlens.dashboard.services.apis.locations.boundaries.overture import OvertureProvider

        with (
            _redata_answers({}) as asked,
            mock.patch(f"{_MAPS}._intersecting_files", return_value=["bucket/one.parquet"]),
            mock.patch(f"{_MAPS}._read_files", return_value=[_paris_place()]),
        ):
            places = OvertureProvider().get_nearby_places(*_ABROAD)

        self.assertEqual(asked, [])
        self.assertEqual([(place["name"], place["category"]) for place in places], [("Le Cafe", "cafe")])

    def test_the_chains_overture_step_abroad_reads_the_public_release(self) -> None:
        with (
            _redata_answers({}) as asked,
            mock.patch(f"{_MAPS}._intersecting_files", return_value=["bucket/one.parquet"]),
            mock.patch(f"{_MAPS}._read_files", return_value=[_paris_building()]),
        ):
            typed = _overture_step().get_typed_boundaries(*_ABROAD)

        self.assertEqual(asked, [])
        self.assertIsNotNone(typed["building"])


class ThePublicReaderRefusesTheUsTests(SimpleTestCase):
    """The public reader itself refuses a US bbox, so no caller can reach it there by going around the router."""

    def test_a_us_bbox_is_refused_before_the_budget_or_the_index(self) -> None:
        bbox = (_INSIDE[1] - 0.001, _INSIDE[0] - 0.001, _INSIDE[1] + 0.001, _INSIDE[0] + 0.001)
        with (
            mock.patch.object(OvertureMapsGateway, "_reserve_call_budget") as reserve,
            mock.patch(f"{_MAPS}._intersecting_files") as index,
            pytest.raises(ValueError, match="REData"),
        ):
            OvertureMapsGateway().get_buildings(bbox)

        reserve.assert_not_called()
        index.assert_not_called()

    def test_the_registry_names_the_redata_buildings_budget(self) -> None:
        from urbanlens.dashboard.controllers.site_admin import _API_LIMIT_CATEGORIES
        from urbanlens.dashboard.services.apis.locations.redata_buildings_gateway import RedataBuildingsGateway
        from urbanlens.dashboard.services.core.rate_limiter import all_service_defaults

        self.assertEqual(RedataBuildingsGateway.service_key, "redata_buildings")
        self.assertIn("redata_buildings", all_service_defaults())
        self.assertEqual(_API_LIMIT_CATEGORIES["redata_buildings"], "Boundaries & GIS")
