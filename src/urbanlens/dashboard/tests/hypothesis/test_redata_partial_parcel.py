"""A parcel REData answers as partial is not settled: the boundary waits, the body is shared briefly, the cache keeps it an hour.

REData answers a parcel one of its tiers could not reach with a top-level ``complete: false`` and the tier under
``sources`` (``{tier, status, host, http_status, message}``), and holds it only briefly before asking that tier again.
Tier 1 is the county GIS layer that draws the parcel's boundary. A pin looked up while it was down got the scored
boundary or the buildings' hull instead, and kept that for the whole ``boundary_cache_days`` window.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest import mock

from django.contrib.gis.geos import Polygon
from django.core.cache import cache
from django.utils import timezone
from model_bakery import baker

from urbanlens.core.tests.testcase import SimpleTestCase, TestCase
from urbanlens.dashboard.models.cache.location_cache import (
    PARTIAL_ANSWER_STALE_AFTER,
    UNANSWERED_SOURCES_KEY,
    LocationCache,
)
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.services.apis.locations.base import BoundaryProviderDeferredError
from urbanlens.dashboard.services.apis.locations.boundaries.redata import RedataBoundaryProvider
from urbanlens.dashboard.services.apis.property_records.redata_gateway import ParcelBuildings, RedataGateway
from urbanlens.dashboard.services.core import coalesce
from urbanlens.dashboard.tests.hypothesis.redata_helpers import RedataConfiguredMixin

_GATEWAY_CLASS_PATH = "urbanlens.dashboard.services.apis.locations.boundaries.redata.RedataGateway"
_LAT, _LON = 42.005, -73.005
_SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]]
_GEOJSON_SQUARE = {"type": "Polygon", "coordinates": [_SQUARE]}
_SCORED = [{"geometry": _GEOJSON_SQUARE, "is_suggested": True}]
_TRIANGLE_BUILDINGS = [
    {"latitude": 42.0, "longitude": -73.0},
    {"latitude": 42.0, "longitude": -73.01},
    {"latitude": 42.01, "longitude": -73.005},
]


def _failed(tier: int, status: str = "error") -> dict[str, Any]:
    """One entry of REData's ``sources``, shaped as a failed source in its 503 body."""
    return {
        "tier": tier,
        "status": status,
        "host": "gis.county.example",
        "http_status": 503,
        "message": f"Tier {tier} source gis.county.example could not be reached.",
    }


def _incomplete_body(*failed: dict[str, Any], record_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """REData's 200 for a parcel some tier could not answer, with only the top-level marker."""
    return {
        "uuid": "parcel-1",
        "parcel_geometry": None,
        "complete": False,
        "sources": list(failed),
        "record_payload": {"owner_name": ["Jane Smith"], **(record_payload or {})},
    }


class _BoundaryCase(RedataConfiguredMixin, SimpleTestCase):
    def boundaries(self, payload: dict[str, Any]) -> tuple[dict[str, Any], mock.MagicMock]:
        """What the provider answers for a parcel lookup of ``payload``, and the gateway it asked, with a scored boundary on offer."""
        with mock.patch(_GATEWAY_CLASS_PATH) as gateway_class:
            gateway = gateway_class.return_value
            gateway.lookup_parcel.return_value = payload
            gateway.lookup_boundaries.return_value = _SCORED
            gateway.lookup_parcel_buildings.return_value = ParcelBuildings(_TRIANGLE_BUILDINGS)
            return dict(RedataBoundaryProvider().get_typed_boundaries(_LAT, _LON)), gateway


class BoundaryDeferralTests(_BoundaryCase):
    def test_no_geometry_while_tier_1_is_unanswered_defers_instead_of_falling_back(self) -> None:
        for status in ("error", "rate_limited"):
            with self.subTest(status=status):
                payload = {"uuid": "parcel-1", "parcel_geometry": None, UNANSWERED_SOURCES_KEY: [_failed(1, status)]}

                with mock.patch(_GATEWAY_CLASS_PATH) as gateway_class:
                    gateway_class.return_value.lookup_parcel.return_value = payload
                    gateway_class.return_value.lookup_boundaries.return_value = _SCORED
                    with self.assertRaises(BoundaryProviderDeferredError) as raised:
                        RedataBoundaryProvider().get_typed_boundaries(_LAT, _LON)

                self.assertEqual(raised.exception.service_key, "redata_boundary")
                gateway_class.return_value.lookup_boundaries.assert_not_called()
                gateway_class.return_value.lookup_parcel_buildings.assert_not_called()

    def test_tier_1_among_other_unanswered_tiers_still_defers(self) -> None:
        payload = {"uuid": "parcel-1", UNANSWERED_SOURCES_KEY: [_failed(2), _failed(1, "rate_limited")]}

        with self.assertRaises(BoundaryProviderDeferredError):
            self.boundaries(payload)

    def test_a_complete_answer_without_geometry_keeps_the_scored_boundary(self) -> None:
        result, gateway = self.boundaries({"uuid": "parcel-1", "parcel_geometry": None})

        self.assertIsInstance(result["property"], Polygon)
        gateway.lookup_boundaries.assert_called_once_with("parcel-1")

    def test_a_complete_answer_with_an_empty_marker_keeps_the_fallback(self) -> None:
        result, _gateway = self.boundaries({"uuid": "parcel-1", "parcel_geometry": None, UNANSWERED_SOURCES_KEY: []})

        self.assertIsInstance(result["property"], Polygon)

    def test_a_complete_answer_without_geometry_or_a_scored_boundary_keeps_the_hull(self) -> None:
        with mock.patch(_GATEWAY_CLASS_PATH) as gateway_class:
            gateway = gateway_class.return_value
            gateway.lookup_parcel.return_value = {"uuid": "parcel-1", "parcel_geometry": None}
            gateway.lookup_boundaries.return_value = []
            gateway.lookup_parcel_buildings.return_value = ParcelBuildings(_TRIANGLE_BUILDINGS)

            result = RedataBoundaryProvider().get_typed_boundaries(_LAT, _LON)

        self.assertIsInstance(result["property"], Polygon)

    def test_a_tier_that_does_not_draw_the_boundary_does_not_hold_it_back(self) -> None:
        result, _gateway = self.boundaries({"uuid": "parcel-1", UNANSWERED_SOURCES_KEY: [_failed(2), _failed(3)]})

        self.assertIsInstance(result["property"], Polygon)

    def test_an_incomplete_answer_naming_no_tier_does_not_hold_it_back(self) -> None:
        result, _gateway = self.boundaries({"uuid": "parcel-1", UNANSWERED_SOURCES_KEY: ["unknown"]})

        self.assertIsInstance(result["property"], Polygon)

    def test_the_parcels_own_geometry_is_used_even_when_tier_1_is_unanswered(self) -> None:
        payload = {"uuid": "parcel-1", "parcel_geometry": _GEOJSON_SQUARE, UNANSWERED_SOURCES_KEY: [_failed(1)]}

        result, gateway = self.boundaries(payload)

        self.assertIsInstance(result["property"], Polygon)
        gateway.lookup_boundaries.assert_not_called()


class _GatewayCase(SimpleTestCase):
    def gateway(self, body: dict[str, Any]) -> tuple[RedataGateway, mock.Mock]:
        session = mock.Mock()
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = body
        session.get.return_value = response
        return RedataGateway(base_url="https://redata.example.test", api_key="k", session=session), session

    def shared_for(self, body: dict[str, Any]) -> int:
        """Seconds the parcel lookup's answer is shared for, as the shared cache is told."""
        gateway, _session = self.gateway(body)
        with mock.patch.object(coalesce, "cache", wraps=cache) as shared:
            gateway.lookup_parcel(41.7321, -73.9262)
        return int(shared.set.call_args.args[2])


class ShareTests(_GatewayCase):
    def test_an_incomplete_body_is_shared_for_about_five_minutes(self) -> None:
        self.assertEqual(self.shared_for(_incomplete_body(_failed(1))), 300)

    def test_a_complete_body_is_shared_for_an_hour(self) -> None:
        body = {"uuid": "parcel-1", "complete": True, "sources": [], "record_payload": {"owner_name": ["Jane Smith"]}}

        self.assertEqual(self.shared_for(body), 3600)

    def test_a_body_from_before_the_fields_existed_is_shared_for_an_hour(self) -> None:
        self.assertEqual(self.shared_for({"uuid": "parcel-1", "record_payload": {"owner_name": ["Jane Smith"]}}), 3600)

    def test_a_body_naming_its_unanswered_tiers_only_in_the_record_payload_is_shared_briefly(self) -> None:
        body = {"uuid": "parcel-1", "record_payload": {UNANSWERED_SOURCES_KEY: [_failed(1)]}}

        self.assertEqual(self.shared_for(body), 300)

    def test_an_incomplete_body_is_one_call_within_the_share(self) -> None:
        gateway, session = self.gateway(_incomplete_body(_failed(1)))

        gateway.lookup_parcel(41.7321, -73.9262)
        gateway.lookup_parcel_uuid(41.7321, -73.9262)

        session.get.assert_called_once()

    def test_an_incomplete_body_is_asked_again_once_its_share_has_lapsed(self) -> None:
        gateway, session = self.gateway(_incomplete_body(_failed(1)))
        real_time = coalesce.time.time

        gateway.lookup_parcel(41.7321, -73.9262)
        with mock.patch("time.time", side_effect=lambda: real_time() + 301):
            gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(session.get.call_count, 2)

    def test_a_complete_body_is_still_shared_after_that_long(self) -> None:
        gateway, session = self.gateway({"uuid": "parcel-1", "complete": True, "sources": [], "record_payload": {}})
        real_time = coalesce.time.time

        gateway.lookup_parcel(41.7321, -73.9262)
        with mock.patch("time.time", side_effect=lambda: real_time() + 301):
            gateway.lookup_parcel(41.7321, -73.9262)

        session.get.assert_called_once()


class CoalescedTtlTests(SimpleTestCase):
    def test_the_share_may_depend_on_the_answer(self) -> None:
        with mock.patch.object(coalesce, "cache", wraps=cache) as shared:
            coalesce.coalesced("t:short", lambda: {"partial": True}, ttl=lambda answer: 30 if answer["partial"] else 60)
            coalesce.coalesced("t:long", lambda: {"partial": False}, ttl=lambda answer: 30 if answer["partial"] else 60)

        self.assertEqual([call.args[2] for call in shared.set.call_args_list], [30, 60])


class UnansweredMarkerTests(_GatewayCase):
    def test_the_top_level_fields_mark_the_payload(self) -> None:
        gateway, _session = self.gateway(_incomplete_body(_failed(1)))

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload[UNANSWERED_SOURCES_KEY], [_failed(1)])

    def test_the_top_level_sources_win_over_the_record_payloads_own_list(self) -> None:
        body = _incomplete_body(_failed(1), record_payload={UNANSWERED_SOURCES_KEY: [_failed(3)]})
        gateway, _session = self.gateway(body)

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload[UNANSWERED_SOURCES_KEY], [_failed(1)])

    def test_an_incomplete_body_that_names_no_source_falls_back_to_the_record_payloads_list(self) -> None:
        body = {"uuid": "parcel-1", "complete": False, "record_payload": {UNANSWERED_SOURCES_KEY: [_failed(2)]}}
        gateway, _session = self.gateway(body)

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload[UNANSWERED_SOURCES_KEY], [_failed(2)])

    def test_an_incomplete_body_that_names_nothing_anywhere_is_still_marked(self) -> None:
        gateway, _session = self.gateway({"uuid": "parcel-1", "complete": False, "record_payload": {}})

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload[UNANSWERED_SOURCES_KEY], ["unknown"])

    def test_without_the_top_level_fields_the_record_payloads_list_marks_it(self) -> None:
        body = {
            "uuid": "parcel-1",
            "record_payload": {"owner_name": ["Jane Smith"], UNANSWERED_SOURCES_KEY: [_failed(1)]},
        }
        gateway, _session = self.gateway(body)

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload[UNANSWERED_SOURCES_KEY], [_failed(1)])

    def test_a_complete_body_is_not_marked(self) -> None:
        body = {"uuid": "parcel-1", "complete": True, "sources": [], "record_payload": {"owner_name": ["Jane Smith"]}}
        gateway, _session = self.gateway(body)

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertNotIn(UNANSWERED_SOURCES_KEY, payload)

    def test_a_complete_body_clears_a_stale_list_in_the_record_payload(self) -> None:
        body = {
            "uuid": "parcel-1",
            "complete": True,
            "sources": [],
            "record_payload": {UNANSWERED_SOURCES_KEY: [_failed(1)]},
        }
        gateway, _session = self.gateway(body)

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertFalse(payload[UNANSWERED_SOURCES_KEY])

    def test_a_body_from_before_the_fields_existed_is_exactly_what_it_was(self) -> None:
        record = {"owner_name": ["Jane Smith"], "apn": "1-2-3"}
        gateway, _session = self.gateway({"record_payload": record})

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload, record)

    def test_an_old_body_with_an_empty_record_payload_list_is_exactly_what_it_was(self) -> None:
        record = {"owner_name": ["Jane Smith"], UNANSWERED_SOURCES_KEY: []}
        gateway, _session = self.gateway({"uuid": "parcel-1", "record_payload": record})

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload, {**record, "uuid": "parcel-1"})

    def test_a_malformed_marker_is_ignored(self) -> None:
        gateway, _session = self.gateway({"record_payload": {"apn": "1-2-3"}, "complete": "no", "sources": "gis"})

        payload = gateway.lookup_parcel(41.7321, -73.9262)

        self.assertEqual(payload, {"apn": "1-2-3"})


class PartialParcelIsKeptBrieflyTests(RedataConfiguredMixin, TestCase):
    """The marker, taken from the top-level fields, is what the cache reads to let the row lapse in an hour."""

    def setUp(self) -> None:
        super().setUp()
        self.location = baker.make(Location, latitude="41.733000", longitude="-73.930000", google_place=None)
        self.pin = baker.make(Pin, profile=baker.make("auth.User").profile, location=self.location)

    def fetch_panel(self, body: dict[str, Any]) -> LocationCache:
        from urbanlens.dashboard.plugins.builtin.property_records import PropertyRecordsPanelSource

        with (
            mock.patch.object(RedataGateway, "__post_init__", return_value=None),
            mock.patch.object(RedataGateway, "_get_json", return_value=body),
        ):
            PropertyRecordsPanelSource().fetch(self.pin)
        return LocationCache.objects.get(location=self.location, source=PropertyRecordsPanelSource.cache_source)

    def is_fresh_after(self, delay: timedelta) -> bool:
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + delay):
            return LocationCache.get_fresh(self.location, "property_records") is not None

    def test_an_incomplete_body_is_cached_for_an_hour(self) -> None:
        row = self.fetch_panel(
            {"complete": False, "sources": [_failed(1)], "record_payload": {"owner_name": ["Jane Smith"]}}
        )

        self.assertTrue(row.data[UNANSWERED_SOURCES_KEY])
        self.assertTrue(self.is_fresh_after(PARTIAL_ANSWER_STALE_AFTER - timedelta(minutes=1)))
        self.assertFalse(self.is_fresh_after(PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)))

    def test_a_complete_body_keeps_the_window(self) -> None:
        row = self.fetch_panel({"complete": True, "sources": [], "record_payload": {"owner_name": ["Jane Smith"]}})

        self.assertFalse(row.data.get(UNANSWERED_SOURCES_KEY))
        self.assertTrue(self.is_fresh_after(PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)))

    def test_a_body_from_before_the_fields_existed_keeps_the_window(self) -> None:
        row = self.fetch_panel({"record_payload": {"owner_name": ["Jane Smith"]}})

        self.assertNotIn(UNANSWERED_SOURCES_KEY, row.data)
        self.assertTrue(self.is_fresh_after(PARTIAL_ANSWER_STALE_AFTER + timedelta(minutes=1)))
