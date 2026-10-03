"""A campus page's CRIS documents come from every record on the site, read from REData's real answer for HRSH (P234).

The fixture is REData's recorded ``/cultural-resources/lookup/`` answer for the Hudson River State Hospital campus,
trimmed to the fields the plugin reads, with the neighbouring Quiet Cove Riverfront Park district the same lookup
returned on 2026-09-30.
"""

from __future__ import annotations

import copy
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from urbanlens.core.tests.testcase import SimpleTestCase
from urbanlens.dashboard.models.location.model import Location
from urbanlens.dashboard.models.pin.model import Pin
from urbanlens.dashboard.plugins.builtin.cris_buildings import CrisBuildingPanelSource, is_pdf_document, site_resource
from urbanlens.dashboard.services.apis.property_records.redata_gateway import RedataGateway

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "hrsh_cris_lookup.json").read_text())
_LATITUDE = 41.73328
_LONGITUDE = -73.92812
#: CRIS's own survey of the campus; REData answers the site's buildings from its roster.
_SITE_SURVEY = "12SD00541"
_NR_LISTING = "Hudson River State Hospital, Main Building"
_REDEVELOPMENT_REVIEWS = (
    "Hudson Heritage Project/Hudson River State Hospital Site Redevelopment",
    "Hudson Heritage Development/Former Hudson River State Hospital/Property Subdivision",
)
_NEIGHBOUR = "Quiet Cove Riverfront Park"
_OFF_CAMPUS = ("Dutchess County Jail", "Marist University Allied Health", "Schatz Ball Bearing Co.")


def _lookup(*, with_neighbour: bool = False) -> list[dict[str, Any]]:
    rows = copy.deepcopy(_FIXTURE["results"])
    return [copy.deepcopy(_FIXTURE["neighbour_district"]), *rows] if with_neighbour else rows


def _named(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(row for row in rows if row["name"] == name)


def _document_ids(row: dict[str, Any]) -> set[str]:
    return {f"{row['uuid']}.{attachment['id']}" for attachment in row["attachments"] if is_pdf_document(attachment)}


def _roster(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row["resource_type"] == "building"
        and any(link.get("external_id") == _SITE_SURVEY for link in row["linked_from"])
    ]


class _HrshFetchMixin:
    def fetch(self, *, site_scope: bool = True, with_neighbour: bool = False) -> dict[str, Any]:
        """Run the panel's fetch against the recorded lookup and return the payload it caches."""
        rows = _lookup(with_neighbour=with_neighbour)
        by_uuid = {row["uuid"]: row for row in rows}
        location = Location(latitude=Decimal("41.733280"), longitude=Decimal("-73.928120"))
        pin = Pin(location=location)
        pin._site_scope_cache = site_scope
        self.detail_calls: list[str] = []

        def detail(resource_uuid: str) -> dict[str, Any]:
            self.detail_calls.append(resource_uuid)
            return copy.deepcopy(by_uuid[resource_uuid])

        with (
            patch.object(RedataGateway, "__post_init__", lambda _self: None),
            patch.object(RedataGateway, "lookup_cultural_resources", side_effect=lambda *_a, **_k: copy.deepcopy(rows)),
            patch.object(RedataGateway, "fetch_cultural_resource_detail", side_effect=detail),
            patch.object(RedataGateway, "queue_cultural_resource_details", return_value={}),
            patch.object(CrisBuildingPanelSource, "_seed_from_site", return_value=0),
            patch("urbanlens.dashboard.services.core.celery.safely_enqueue_task") as self.enqueue,
            patch("urbanlens.dashboard.models.cache.location_cache.LocationCache.set") as cache_set,
        ):
            CrisBuildingPanelSource()._fetch_now(pin)
        return cache_set.call_args.args[2]

    @staticmethod
    def listed(data: dict[str, Any], *, site_scope: bool = True) -> set[str]:
        return {
            document.document_id for document in CrisBuildingPanelSource().source_documents(data, site_scope=site_scope)
        }


class CampusDocumentsTests(_HrshFetchMixin, SimpleTestCase):
    """Article > Sources on the campus pin lists every CRIS document for the campus."""

    def test_every_document_of_every_building_on_the_campus_survey_is_listed(self) -> None:
        roster = _roster(_lookup())
        expected = set().union(*(_document_ids(row) for row in roster))
        listed = self.listed(self.fetch())
        self.assertEqual(len(roster), 124, "the fixture's survey roster is CRIS's whole campus")
        self.assertEqual(
            expected - listed, set(), f"{len(expected - listed)} of {len(expected)} campus documents missing"
        )

    def test_the_national_register_nomination_is_listed(self) -> None:
        listing = _named(_lookup(), _NR_LISTING)
        self.assertLessEqual(_document_ids(listing), self.listed(self.fetch()))

    def test_the_sites_own_review_records_are_listed(self) -> None:
        rows = _lookup()
        listed = self.listed(self.fetch())
        for name in _REDEVELOPMENT_REVIEWS:
            with self.subTest(name):
                self.assertLessEqual(_document_ids(_named(rows, name)), listed)

    def test_a_neighbouring_district_is_not_taken_for_the_site(self) -> None:
        data = self.fetch(with_neighbour=True)
        self.assertNotEqual(data["district"].get("USNName"), _NEIGHBOUR)
        roster = _roster(_lookup())
        expected = set().union(*(_document_ids(row) for row in roster))
        self.assertEqual(expected - self.listed(data), set())

    def test_buildings_off_the_campus_are_left_out(self) -> None:
        rows = _lookup()
        listed_resources = {document_id.rpartition(".")[0] for document_id in self.listed(self.fetch())}
        for name in _OFF_CAMPUS:
            with self.subTest(name):
                self.assertNotIn(_named(rows, name)["uuid"], listed_resources)

    def test_review_records_cost_no_extraction_and_stay_out_of_the_gallery(self) -> None:
        rows = _lookup()
        data = self.fetch()
        reviews = {_named(rows, name)["uuid"] for name in _REDEVELOPMENT_REVIEWS}
        queued = {call.args[2] for call in self.enqueue.call_args_list}
        self.assertFalse(queued & reviews)
        urls = [item.url for item in CrisBuildingPanelSource().media_items(data)]
        self.assertFalse([url for url in urls if any(f"/{uuid}/" in url for uuid in reviews)])

    def test_a_building_page_lists_its_own_records_and_the_listing_not_the_campus(self) -> None:
        rows = _lookup()
        data = self.fetch(site_scope=False)
        main = next(row for row in rows if row["name"].startswith("BLDG 51/MAIN/ADMIN"))
        self.assertEqual(
            self.listed(data, site_scope=False), _document_ids(main) | _document_ids(_named(rows, _NR_LISTING))
        )


class BuildingChildFromSiteTests(_HrshFetchMixin, SimpleTestCase):
    """A building child answered from the campus payload keeps each record's own identity."""

    def test_a_child_gets_its_building_and_the_listing_never_the_reviews(self) -> None:
        rows = _lookup()
        site_data = self.fetch()
        chapel = next(row for row in rows if row["name"].startswith("BLDG 35/PROTESTANT CHAPEL"))
        location = Location(
            latitude=Decimal(str(chapel["source_latitude"])), longitude=Decimal(str(chapel["source_longitude"]))
        )
        answer = CrisBuildingPanelSource()._answer_from_site(site_data, location)
        self.assertIsNotNone(answer)
        self.assertEqual(
            self.listed(answer, site_scope=False),
            _document_ids(chapel) | _document_ids(_named(rows, _NR_LISTING)),
        )


class SiteRecordChoiceTests(SimpleTestCase):
    """A site record whose own boundary excludes the place is a neighbour, never the place's site."""

    def test_a_neighbour_listed_first_is_passed_over_for_the_listing_containing_the_point(self) -> None:
        chosen = site_resource(_lookup(with_neighbour=True), _LATITUDE, _LONGITUDE)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["name"], _NR_LISTING)

    def test_only_a_neighbour_means_no_site(self) -> None:
        self.assertIsNone(site_resource([copy.deepcopy(_FIXTURE["neighbour_district"])], _LATITUDE, _LONGITUDE))
