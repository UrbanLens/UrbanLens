"""A campus's site-scope CRIS fetch answers its building children, so a campus costs one site fetch, not a REData round trip per building.

On dev, HRSH's building children each fetched their own ``cris_building_usn`` row: 4,548 REData calls in three hours,
1,595 rate-limited and 2,972 failed, and BLDG 166, 152 and 67 never got a row at all - while the parent's own
site-scope fetch had already retrieved every one of those buildings' records.

REData is modelled at its HTTP boundary (the uncoalesced ``_now`` methods, the lookup and the bulk queue), so the
gateway's own request sharing still applies and a count here is a count of real requests.
"""

from __future__ import annotations

from collections import Counter
import copy
from datetime import timedelta
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.gis.geos import GEOSGeometry, MultiPolygon
from django.core.cache import cache
from model_bakery import baker

from urbanlens.core.tests.testcase import TestCase
from urbanlens.dashboard.models.cache.location_cache import LocationCache
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin, PinType
from urbanlens.dashboard.models.place.model import Place, PlaceKind
from urbanlens.dashboard.plugins.builtin import cris_buildings as cris_module
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingEnrichmentSource, CrisBuildingPanelSource
from urbanlens.dashboard.services.apis.property_records.redata_gateway import (
    PropertyRecordsUnavailableError,
    RedataGateway,
)
from urbanlens.dashboard.services.geo.geo_boundary import GeoBoundary
from urbanlens.dashboard.services.locations.site_scope import PARCEL_BUILDINGS_CACHE_SOURCE
from urbanlens.dashboard.services.pins.external_data import run_panel_fetch
from urbanlens.dashboard.tests.hypothesis.building_fixtures import CAMPUS_LAT, CAMPUS_LNG, offset, parcel_square, rect

_SOURCE = "cris_building"
_CACHE = "cris_building_usn"
_NY_ISH = GeoBoundary.from_bboxes([(40.0, 45.0, -80.0, -73.0)])

#: ``(uuid, USN, name, north_m, east_m)`` for each CRIS building on the campus.
_BUILDINGS = [
    ("b-51", "02714.000051", "BLDG 51/MAIN/ADMIN", 20, 10),
    ("b-28", "02714.000028", "BLDG 28/CATHOLIC CHAPEL", 60, -80),
    ("b-45", "02714.000045", "BLDG 45/MORTUARY & LAB", -60, 90),
    ("b-166", "02714.000166", "BLDG 166/GARAGE", 100, 100),
    ("b-152", "02714.000152", "BLDG 152/STOREHOUSE", -120, -120),
    ("b-67", "02714.000067", "BLDG 67/COTTAGE", 150, 40),
]


def _form(attachment_id: int) -> dict:
    return {
        "id": attachment_id,
        "kind": "document",
        "name": "Building Inventory Form",
        "content_type": "application/pdf",
    }


class FakeRedata:
    """REData's CRIS endpoints for one campus, counting every request."""

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.district = {
            "uuid": "dist-hrsh",
            "provider": "ny_cris",
            "resource_type": "building_district",
            "name": "Hudson River State Hospital",
            "attributes": {"HistoricName": "Hudson River State Hospital", "NRNum": "89001168"},
            "geometry": parcel_square(300.0),
        }
        self.buildings: dict[str, dict] = {}
        self.details: dict[str, dict] = {
            "dist-hrsh": {**self.district, "attachments": [_form(900)], "linked_resources": []}
        }
        for index, (uuid, usn, name, north, east) in enumerate(_BUILDINGS):
            latitude, longitude = offset(north, east)
            row = {
                "uuid": uuid,
                "provider": "ny_cris",
                "resource_type": "building",
                "external_id": usn,
                "name": name,
                "source_latitude": latitude,
                "source_longitude": longitude,
                "attributes": {"USNName": name, "USNNum": usn, "EligibilityDesc": "Eligible"},
            }
            self.buildings[uuid] = row
            self.details[uuid] = {
                **row,
                "attachments": [_form(100 + index), {"id": 200 + index, "kind": "photo", "name": "Elevation"}],
            }
        self.transient_details: set[str] = set()

    @property
    def total(self) -> int:
        return sum(self.calls.values())

    def lookup(self, _gateway, _lat, _lng, *, radius_meters, provider=None) -> list[dict]:
        self.calls["lookup"] += 1
        return copy.deepcopy([*self.buildings.values(), self.district])

    def detail(self, _gateway, resource_uuid: str) -> dict:
        self.calls["detail"] += 1
        if resource_uuid in self.transient_details:
            raise PropertyRecordsUnavailableError("rate_limited", "budget spent")
        if resource_uuid not in self.details:
            raise PropertyRecordsUnavailableError("source_error", "no detail")
        return copy.deepcopy(self.details[resource_uuid])

    def queue(self, _gateway, _lat, _lng, *, radius_meters) -> dict:
        self.calls["queue"] += 1
        return {"queued": 0}

    def extract(self, _gateway, _resource_uuid, _attachment_id, *, timeout=30) -> dict:
        self.calls["extract"] += 1
        return {"extracted_images": []}


class CampusSeedingTestCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        baker.make(User)
        self.profile = baker.make(User).profile
        self.site_location = baker.make(Location, latitude=CAMPUS_LAT, longitude=CAMPUS_LNG, google_place=None)
        self.site = baker.make(
            Pin,
            profile=self.profile,
            location=self.site_location,
            parent_pin=None,
            pin_type=PinType.PARCEL,
            pin_type_is_user_provided=True,
        )
        self.redata = FakeRedata()
        self.children: dict[str, Pin] = {
            uuid: self.child_at(north, east) for uuid, _usn, _name, north, east in _BUILDINGS
        }
        self.enqueued: list[tuple] = []

    def child_at(self, north_m: float, east_m: float, **location_fields) -> Pin:
        latitude, longitude = (round(value, 6) for value in offset(north_m, east_m))
        location = Location.objects.filter(latitude=latitude, longitude=longitude).first() or baker.make(
            Location, latitude=latitude, longitude=longitude, google_place=None
        )
        if location_fields:
            Location.objects.filter(pk=location.pk).update(**location_fields)
            location.refresh_from_db()
        return baker.make(
            Pin,
            profile=self.profile,
            location=location,
            parent_pin=self.site,
            pin_type=PinType.BUILDING,
            pin_type_is_user_provided=False,
        )

    def run_fetch(self, pin: Pin) -> None:
        with (
            patch.object(CrisBuildingPanelSource, "geo_boundary", _NY_ISH),
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", autospec=True, side_effect=self.redata.lookup),
            patch.object(
                RedataGateway, "_fetch_cultural_resource_detail_now", autospec=True, side_effect=self.redata.detail
            ),
            patch.object(
                RedataGateway, "queue_cultural_resource_details", autospec=True, side_effect=self.redata.queue
            ),
            patch.object(
                RedataGateway,
                "_extract_cultural_resource_attachment_now",
                autospec=True,
                side_effect=self.redata.extract,
            ),
            patch(
                "urbanlens.dashboard.services.core.celery.safely_enqueue_task",
                side_effect=lambda *args, **kwargs: self.enqueued.append(args),
            ),
        ):
            run_panel_fetch(_SOURCE, Pin.objects.select_related("location", "profile").get(pk=pin.pk), None)

    def row(self, pin: Pin) -> LocationCache | None:
        return LocationCache.objects.filter(location_id=pin.location_id, source=_CACHE).first()

    def landed(self, pin: Pin) -> LocationCache:
        row = self.row(pin)
        assert row is not None, f"pin {pin.pk} has no {_CACHE} row ({self.redata.calls})"
        return row

    def extractions_for(self, pin: Pin) -> list[tuple]:
        return [args for args in self.enqueued if len(args) > 1 and args[1] == pin.location_id]


class SiteFetchSeedsChildrenTests(CampusSeedingTestCase):
    def test_the_site_fetch_answers_every_covered_child(self) -> None:
        self.run_fetch(self.site)

        panel = CrisBuildingPanelSource()
        for uuid, _usn, name, _north, _east in _BUILDINGS:
            child = self.children[uuid]
            row = self.landed(child)
            self.assertEqual(row.data.get("USNName"), name)
            self.assertEqual(row.data.get("resource_uuid"), uuid)
            self.assertTrue(panel.is_ready(child), f"{name}'s card would still schedule its own fetch")
            self.assertEqual(
                row.query_key,
                f"{float(child.location.latitude)},{float(child.location.longitude)}",
                "keyed as the child's own fetch keys it",
            )

    def test_a_covered_childs_full_record_costs_no_redata_request(self) -> None:
        self.run_fetch(self.site)
        site_cost = dict(self.redata.calls)

        for child in self.children.values():
            self.run_fetch(child)

        self.assertEqual(
            dict(self.redata.calls),
            site_cost,
            f"per-child requests after the site fetch: {self.redata.calls - Counter(site_cost)}",
        )
        panel = CrisBuildingPanelSource()
        for uuid, child in self.children.items():
            data = self.landed(child).data
            self.assertTrue(panel.media_is_ready(data), f"{uuid}'s media half is still unfilled")
            self.assertEqual(
                {a["resource_uuid"] for a in data["attachments"]},
                {uuid, "dist-hrsh"},
                "its own building and its site, nobody else's",
            )
            self.assertFalse(
                [a for a in data["attachments"] if a.get("site_building")], "these are the child's own records now"
            )
            self.assertEqual(data["district"]["resource_uuid"], "dist-hrsh")
            self.assertIs(data["district"]["contains_point"], True)
            self.assertIs(data["site_scope"], False)
            self.assertTrue(panel.render_context(child, data), "the building card renders")

    def test_a_covered_childs_documents_are_extracted_when_it_is_opened_not_before(self) -> None:
        self.run_fetch(self.site)
        mortuary = self.children["b-45"]
        self.assertEqual(
            self.extractions_for(mortuary), [], "the site fetch spends no extraction on buildings nobody opened"
        )

        self.run_fetch(mortuary)

        self.assertEqual(
            [(args[2], args[3]) for args in self.extractions_for(mortuary)], [("b-45", [102]), ("dist-hrsh", [900])]
        )

    def test_the_cost_of_the_whole_campus_is_the_site_fetch(self) -> None:
        self.run_fetch(self.site)
        for child in self.children.values():
            self.run_fetch(child)

        self.assertLessEqual(self.redata.calls["lookup"], 2, self.redata.calls)
        self.assertLessEqual(
            self.redata.calls["detail"], len(_BUILDINGS) + 1, "one detail per resource, however many pins ask"
        )

    def test_an_uncovered_child_still_fetches_its_own(self) -> None:
        """A footprint with no CRIS point of its own is not any campus record's building."""
        stranger = self.child_at(-150, 150)
        self.run_fetch(self.site)
        self.assertIsNone(self.row(stranger))
        lookups = self.redata.calls["lookup"]

        self.run_fetch(stranger)

        self.assertEqual(self.redata.calls["lookup"], lookups + 1)
        self.assertIsNotNone(self.row(stranger))

    def test_a_footprint_holding_a_cris_point_is_that_building(self) -> None:
        """A child standing inside its footprint, away from the CRIS point, is still the building the point is in."""
        latitude, longitude = offset(-120, -120)
        footprint = baker.make(
            Place,
            kind=PlaceKind.BUILDING,
            geometry=MultiPolygon(GEOSGeometry(json.dumps(rect(-110, -120, 40, 40)), srid=4326)),
        )
        self.children["b-152"].delete()
        storehouse = self.child_at(-95, -120, place=footprint)

        self.run_fetch(self.site)

        self.assertEqual(self.landed(storehouse).data.get("resource_uuid"), "b-152", (latitude, longitude))

    def test_a_child_that_fetched_its_own_record_keeps_it(self) -> None:
        chapel = self.children["b-28"]
        LocationCache.set(
            chapel.location,
            _CACHE,
            {"USNName": "Own answer", "attachments": [], "attachments_fetched": True},
            query_key="own",
        )

        self.run_fetch(self.site)

        self.assertEqual(self.landed(chapel).data["USNName"], "Own answer")

    def test_a_child_row_written_by_enrichment_is_replaced(self) -> None:
        """An info-only row has no attachments behind it; the site's answer is at least as good."""
        chapel = self.children["b-28"]
        LocationCache.set(chapel.location, _CACHE, {"USNName": "Stale name"}, query_key="enrichment")

        self.run_fetch(self.site)

        self.assertEqual(self.landed(chapel).data["USNName"], "BLDG 28/CATHOLIC CHAPEL")

    def test_seeded_rows_are_as_old_as_the_site_answer(self) -> None:
        self.run_fetch(self.site)
        site_row = self.landed(self.site)

        for child in self.children.values():
            self.assertEqual(
                self.landed(child).updated, site_row.updated, "a seeded row must not outlive the data it came from"
            )


class ChildBeforeOrWithoutSiteTests(CampusSeedingTestCase):
    def test_a_child_created_after_the_site_fetch_is_answered_from_it(self) -> None:
        self.children["b-166"].delete()
        self.run_fetch(self.site)
        site_cost = dict(self.redata.calls)
        garage = self.child_at(100, 100)

        self.run_fetch(garage)

        self.assertEqual(dict(self.redata.calls), site_cost)
        self.assertEqual(self.landed(garage).data["USNName"], "BLDG 166/GARAGE")
        self.assertTrue(self.landed(garage).data["attachments_fetched"])

    def test_a_child_opened_first_fetches_its_site_once_for_everyone(self) -> None:
        self.run_fetch(self.children["b-67"])
        after_first = dict(self.redata.calls)

        for child in self.children.values():
            self.run_fetch(child)

        self.assertIs(
            self.landed(self.site).data.get("site_scope"), True, "the site's answer was fetched at site scope"
        )
        self.assertEqual(dict(self.redata.calls), after_first, "the first child's site fetch answered every sibling")
        self.assertLessEqual(self.redata.calls["lookup"], 2, self.redata.calls)

    def test_a_stale_site_answer_is_refreshed_once_not_per_child(self) -> None:
        self.run_fetch(self.site)
        long_ago = self.landed(self.site).updated - timedelta(days=3650)
        LocationCache.objects.filter(source=_CACHE).update(updated=long_ago)
        cache.clear()
        lookups = self.redata.calls["lookup"]

        for child in self.children.values():
            self.run_fetch(child)

        self.assertLessEqual(self.redata.calls["lookup"] - lookups, 2, "one site refresh, not one lookup per building")
        self.assertFalse(self.landed(self.site).is_stale)
        for child in self.children.values():
            self.assertFalse(self.landed(child).is_stale)

    def test_a_building_the_site_pass_did_not_detail_costs_its_detail_only(self) -> None:
        with patch.object(cris_module, "_MAX_SITE_DETAIL_FETCHES", 0):
            self.run_fetch(self.site)
        mortuary = self.children["b-45"]
        self.assertEqual(
            self.landed(mortuary).data["USNName"], "BLDG 45/MORTUARY & LAB", "the lookup row alone fills the card"
        )
        before = Counter(self.redata.calls)

        self.run_fetch(mortuary)

        self.assertEqual(self.redata.calls - before, Counter({"detail": 1}), "no lookup, no site-record detail")
        self.assertIn(102, [a["id"] for a in self.landed(mortuary).data["attachments"]])

    def test_a_throttled_detail_is_retried_later_not_cached_as_nothing(self) -> None:
        with patch.object(cris_module, "_MAX_SITE_DETAIL_FETCHES", 0):
            self.run_fetch(self.site)
        mortuary = self.children["b-45"]
        self.redata.transient_details.add("b-45")

        self.run_fetch(mortuary)

        self.assertFalse(
            CrisBuildingPanelSource().media_is_ready(self.landed(mortuary).data),
            "an outage is not 'CRIS has no records'",
        )

    def test_a_child_waits_for_the_site_fetch_already_in_flight(self) -> None:
        """The parent's page and its building cards open together; the site must be fetched once, not once per card."""
        self.run_fetch(self.site)
        site_data, site_key = self.landed(self.site).data, self.landed(self.site).query_key
        LocationCache.objects.filter(source=_CACHE).delete()
        before = Counter(self.redata.calls)
        key = CrisBuildingPanelSource().site_fetch_key(self.site_location.pk)
        cache.delete(key)
        cache.set(f"{key}:flight", "another-worker", 60)

        def other_worker_finishes(_seconds: float) -> None:
            LocationCache.set(self.site_location, _CACHE, site_data, query_key=site_key)
            cache.set(key, (None,), 60)
            cache.delete(f"{key}:flight")

        with patch("urbanlens.dashboard.services.core.coalesce.time.sleep", side_effect=other_worker_finishes):
            self.run_fetch(self.children["b-28"])

        self.assertEqual(self.redata.calls - before, Counter(), "the child waited for the site fetch in flight")
        self.assertEqual(self.landed(self.children["b-28"]).data["USNName"], "BLDG 28/CATHOLIC CHAPEL")


class EnrichmentTakesTheSiteAnswerTests(CampusSeedingTestCase):
    def test_background_enrichment_reads_a_covered_childs_record_from_its_site(self) -> None:
        self.run_fetch(self.site)
        chapel = self.children["b-28"]
        LocationCache.objects.filter(location=chapel.location, source=_CACHE).delete()
        before = self.redata.total

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", autospec=True, side_effect=self.redata.lookup),
        ):
            self.assertTrue(CrisBuildingEnrichmentSource().enrich(chapel.location))

        self.assertEqual(self.redata.total, before)
        data = self.landed(chapel).data
        self.assertEqual(data["USNName"], "BLDG 28/CATHOLIC CHAPEL")
        self.assertNotIn("attachments_fetched", data, "enrichment fills the card, never the media half")
        self.assertEqual(self.landed(chapel).updated, self.landed(self.site).updated)

    def test_background_enrichment_of_an_uncovered_child_still_looks_it_up(self) -> None:
        self.run_fetch(self.site)
        stranger = self.child_at(-150, 150)
        before = self.redata.calls["lookup"]

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", autospec=True, side_effect=self.redata.lookup),
        ):
            CrisBuildingEnrichmentSource().enrich(stranger.location)

        self.assertEqual(self.redata.calls["lookup"], before + 1)


class SweepSeedsTheBuildingsItCreatesTests(CampusSeedingTestCase):
    """A sweep that turns the parcel's CRIS points into pins after the site fetch ran gives each its record."""

    def setUp(self) -> None:
        super().setUp()
        for child in self.children.values():
            child.delete()
        parcel = baker.make(
            Place,
            kind=PlaceKind.PARCEL,
            geometry=MultiPolygon(GEOSGeometry(json.dumps(parcel_square(300.0)), srid=4326)),
        )
        Location.objects.filter(pk=self.site_location.pk).update(place=parcel)
        records = [
            {
                "ref": f"cris:{usn}",
                "name": name,
                "latitude": offset(north, east)[0],
                "longitude": offset(north, east)[1],
                "source": "cris",
                "building_number": "",
                "is_on_property": True,
            }
            for _uuid, usn, name, north, east in _BUILDINGS
        ]
        LocationCache.set(
            self.site_location,
            PARCEL_BUILDINGS_CACHE_SOURCE,
            {"buildings": records, "provider": "cris"},
            query_key="test",
        )

    def test_the_sweeps_new_building_pins_are_answered_without_a_fetch(self) -> None:
        from urbanlens.dashboard.services.pins.auto_nest import auto_nest_pin

        self.run_fetch(self.site)
        with patch("urbanlens.dashboard.services.pins.external_data.schedule_panel_fetch"):
            created = auto_nest_pin(Pin.objects.get(pk=self.site.pk))

        self.assertEqual(created, len(_BUILDINGS))
        rows = {pin.name: self.row(pin) for pin in self.site.descendants()}
        self.assertEqual(
            {name for name, row in rows.items() if row is not None and row.data.get("USNName") == name},
            {name for _u, _n, name, _a, _b in _BUILDINGS},
        )
